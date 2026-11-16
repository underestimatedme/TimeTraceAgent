"""Codex adapter: `codex exec --json` for runs, `codex app-server` for quota.

Facts this relies on (verified 2026-09-02 on Codex CLI 0.151.0):
- `codex app-server` speaks JSON-RPC over stdio; `account/rateLimits/read` returns
  `rateLimitsByLimitId` = {limit_id: {primary, secondary, ...}} with usedPercent,
  resetsAt (epoch), windowDurationMins. No quota is consumed.
- `codex exec --json` prints JSONL: thread.started(thread_id), turn.started,
  item.completed / turn.completed on success, `error` + `turn.failed` when the
  usage limit is hit (exit code 1). The error text has only a local clock time,
  so the reset epoch is taken from app-server instead.
- `codex exec resume <thread_id> --json <prompt>` continues a thread.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from timetrace import __version__
from timetrace.adapters.base import WORK_SESSION_RULES, PLANNING_RULES, CHAT_RULES, FOLDER_RULES, IMPORT_RULES, REVIEW_RULES, SAFETY_RULES, ToolAdapter, run_streaming
from timetrace import tiers, usage, worktree
from timetrace.mcp_access import ENV_NAME, McpAccess
from timetrace.redact import mask_file, masking
from timetrace.models import CODEX, RunResult, Sample
from timetrace.billing import billing_env_keys, codex_verifier, sanitized_env
from timetrace.quota import merge_capabilities

LIMIT_TEXT_MARKERS = ("usage limit", "rate limit", "try again at")
PRIMARY_BUCKET = "codex:codex:primary"


# ---- quota ------------------------------------------------------------------
def parse_rate_limits(response: Dict[str, Any], tool: str = CODEX) -> List[Sample]:
    by_id = response.get("rateLimitsByLimitId")
    if not by_id:
        single = response.get("rateLimits") or {}
        by_id = {single.get("limitId") or "codex": single}
    samples: List[Sample] = []
    permission = response.get("ordinaryUsageAllowed")
    if type(permission) is not bool or tool != CODEX:
        permission = None
    for limit_id, snap in by_id.items():
        if not isinstance(snap, dict):
            continue
        for win_name in ("primary", "secondary"):
            w = snap.get(win_name)
            if not w or w.get("usedPercent") is None:
                continue
            samples.append(Sample(
                bucket_key="%s:%s:%s" % (tool, limit_id, win_name), tool=tool,
                used_pct=float(w["usedPercent"]), reset_at=_int_or_none(w.get("resetsAt")),
                window_mins=_int_or_none(w.get("windowDurationMins")),
                is_representative=(limit_id == "codex" and win_name == "primary"),
                source="live",
                codex_ordinary_usage_allowed=permission if (limit_id == "codex" or
                    (limit_id == "base_model_inference" and snap.get("limitName") == "gpt-reserve")) else None,
            ))
    return samples


# Methods timetrace never sends. Using a reset credit is the user's own
# decision in the official client (spec §10b); the runner only reads them.
FORBIDDEN_METHODS = ("account/rateLimitResetCredit/consume",)
RESET_CREDITS_TIMEOUT = 20.0
RESET_CREDIT_STATUSES = ("available", "redeeming", "redeemed", "unknown")
MAX_RESET_CREDITS = 50


def _forbidden(method: str) -> bool:
    return method in FORBIDDEN_METHODS or method.startswith("account/rateLimitResetCredit/")


def app_server_request(bin_: str, method: str, params: Optional[dict] = None,
                       timeout: float = 15.0) -> Dict[str, Any]:
    """Minimal JSON-RPC client: initialize, initialized, one request, then kill."""
    if _forbidden(method):
        raise ValueError("timetrace never calls %s" % method)
    env = sanitized_env(CODEX, os.environ)
    env.pop("RUST_LOG", None)
    proc = subprocess.Popen(
        [bin_, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    answer: Dict[str, Any] = {}
    done = threading.Event()

    def reader():
        for line in iter(proc.stdout.readline, ""):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                answer.update(msg)
                done.set()
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "timetrace", "title": "timetrace", "version": __version__}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
        ):
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        if not done.wait(timeout):
            raise TimeoutError("codex app-server did not answer %s in %ss" % (method, timeout))
    finally:
        proc.kill()
        proc.wait()
        t.join(timeout=2)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
    if "error" in answer:
        raise RuntimeError("app-server error: %s" % json.dumps(answer["error"]))
    return answer.get("result") or {}


def _iso(epoch: Any) -> Optional[str]:
    try:
        value = int(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_reset_credits(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`rateLimitResetCredits` of an account/rateLimits/read response as
    {available_count, credits[]}; None when absent or malformed."""
    summary = response.get("rateLimitResetCredits") if isinstance(response, dict) else None
    if not isinstance(summary, dict):
        return None
    count = summary.get("availableCount")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
        return None
    credits = []
    for raw in summary.get("credits") or []:
        if not isinstance(raw, dict):
            continue
        credit_id = str(raw.get("id") or "").strip()
        if not credit_id or len(credit_id) > 128:
            continue
        status = raw.get("status") if raw.get("status") in RESET_CREDIT_STATUSES else "unknown"
        credit = {"id": credit_id, "reset_type": str(raw.get("resetType") or "unknown")[:64], "status": status}
        granted, expires = _iso(raw.get("grantedAt")), _iso(raw.get("expiresAt"))
        if granted:
            credit["granted_at"] = granted
        if expires:
            credit["expires_at"] = expires
        description = raw.get("description") or raw.get("title")
        if isinstance(description, str) and description.strip():
            credit["description"] = description.strip()[:200]
        credits.append(credit)
        if len(credits) >= MAX_RESET_CREDITS:
            break
    return {"available_count": count, "credits": credits}


# ---- exec -----------------------------------------------------------------------
TASK_PROFILE = "timetrace_task"
# A host, or `*.` and a host: at least two labels and a name for the last one, so `*`, `*.com`, an address or a URL
# cannot be written as an allowance.
_DOMAIN = re.compile(r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z](?:[a-z0-9-]*[a-z0-9])?$")


def network_domains(cfg: Dict[str, Any]) -> List[str]:
    """The domains a task run's commands may reach (`codex.network_domains`), lowercased, each once. Empty - the network
    stays off - unless the run is workspace-write and every entry is a narrow domain: a list that cannot be read as
    written must not half apply."""
    value = cfg.get("network_domains")
    if str(cfg.get("sandbox", "workspace-write")) != "workspace-write" or not isinstance(value, list):
        return []
    domains: List[str] = []
    for entry in value:
        domain = entry.lower() if isinstance(entry, str) else ""
        if not _DOMAIN.match(domain):
            return []
        if domain not in domains:
            domains.append(domain)
    return domains


def _toml_table(entries: List[Tuple[str, str]]) -> str:
    return "{%s}" % ",".join("%s=%s" % (json.dumps(key), value) for key, value in entries)


def _network_profile(domains: List[str], add_dirs: Optional[List[str]]) -> List[str]:
    """A permission profile in place of the workspace-write settings (Codex takes one or the other): the same writes -
    the worktree, temp, and the narrow git paths; Codex keeps `.git` read-only in a project root - and the network only
    through Codex's proxy, which refuses every other domain, so `git push` to a host not listed still fails."""
    writes = [(":root", '"read"'), (":project_roots", '{"."="write"}'), (":tmpdir", '"write"'), (":slash_tmp", '"write"')]
    writes += [(path, '"write"') for path in add_dirs or []]
    allow = [(domain, '"allow"') for domain in domains]
    overrides = ["features.network_proxy=true",
                 "default_permissions=%s" % json.dumps(TASK_PROFILE),
                 "permissions.%s.filesystem=%s" % (TASK_PROFILE, _toml_table(writes)),
                 "permissions.%s.network.enabled=true" % TASK_PROFILE,
                 "permissions.%s.network.domains=%s" % (TASK_PROFILE, _toml_table(allow))]
    return [part for override in overrides for part in ("-c", override)]


def build_cmd(cfg: Dict[str, Any], prompt: str, cwd: str, resume: Optional[str] = None,
              last_msg_file: Optional[str] = None, add_dirs: Optional[List[str]] = None,
              mcp: Optional[McpAccess] = None) -> List[str]:
    sandbox = str(cfg.get("sandbox", "workspace-write"))
    domains = network_domains(cfg)
    cmd = [cfg.get("bin", "codex"), "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += ["--json", "--skip-git-repo-check"]
    if domains:
        if not resume:
            cmd += ["-C", cwd]
        cmd += _network_profile(domains, add_dirs)
    else:
        if not resume:
            cmd += ["-s", sandbox, "-C", cwd]
        # `codex exec resume` has no -s: pin the sandbox through config so a
        # resumed run cannot fall back to a laxer mode from ~/.codex/config.toml.
        cmd += ["-c", "sandbox_mode=%s" % json.dumps(sandbox)]
        # Without `network_domains` model-run commands have no network (the model API is called
        # by codex itself, outside the sandbox); this also makes `git push` fail.
        cmd += ["-c", "sandbox_workspace_write.network_access=false"]
        # Directories the workspace-write sandbox may also write: the parts of the
        # main repo's .git that `git commit` in a linked worktree needs. Passed as
        # config because `codex exec resume` rejects --add-dir.
        if add_dirs:
            cmd += ["-c", "sandbox_workspace_write.writable_roots=%s" % json.dumps(list(add_dirs))]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if last_msg_file:
        cmd += ["-o", last_msg_file]
    if mcp is not None:
        # TimeTrace's MCP server for this run. The address is in the command (it is not a secret); the token is not: Codex
        # reads it from the environment variable named here, which only this process is given.
        cmd += ["-c", "mcp_servers.timetrace.url=%s" % json.dumps(mcp.url),
                "-c", "mcp_servers.timetrace.bearer_token_env_var=%s" % json.dumps(ENV_NAME),
                # `codex exec` has approval policy "never", which fails every MCP tool call that is not pre-approved. This
                # server only: the tools a dispatched run may call are limited by Valley (the run's fixed policy), not here.
                "-c", 'mcp_servers.timetrace.default_tools_approval_mode="approve"',
                # Codex writes the environment of its shell (this token included) to CODEX_HOME/shell_snapshots/*.sh for the
                # model's commands, and the file stays after the run: a run that holds the token does not write one.
                "-c", "features.shell_snapshot=false"]
    cmd += list(cfg.get("extra_args") or [])
    cmd += [SAFETY_RULES + "\n" + prompt]
    return cmd


def build_chat_cmd(cfg: Dict[str, Any], prompt: str, cwd: str, resume: Optional[str] = None,
                   last_msg_file: Optional[str] = None, rules: str = CHAT_RULES) -> List[str]:
    """A read-only conversation turn in the user's main checkout. The sandbox
    is always read-only (never the configured task sandbox), pinned through
    config as well so `exec resume` cannot fall back to ~/.codex/config.toml
    (H-3). No writable roots and no configured `extra_args`."""
    cmd = [cfg.get("bin", "codex"), "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += ["--json", "--skip-git-repo-check"]
    if not resume:
        cmd += ["-s", "read-only", "-C", cwd]
    cmd += ["-c", "sandbox_mode=%s" % json.dumps("read-only")]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if last_msg_file:
        cmd += ["-o", last_msg_file]
    # The rules first also keep a phone prompt starting with "-" positional.
    cmd += [rules + "\n" + prompt]
    return cmd


def build_folder_cmd(cfg: Dict[str, Any], prompt: str, cwd: str, resume: Optional[str] = None,
                     last_msg_file: Optional[str] = None) -> List[str]:
    """A run in a folder workspace. `cwd` is the task's output directory and
    the only writable root; the rest of the disk (the source material
    included) stays readable, as in every Codex sandbox. The sandbox is always
    workspace-write, pinned through config for `exec resume` (H-3); the
    configured `sandbox` and `extra_args` do not apply."""
    cmd = [cfg.get("bin", "codex"), "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += ["--json", "--skip-git-repo-check"]
    if not resume:
        cmd += ["-s", "workspace-write", "-C", cwd]
    cmd += ["-c", "sandbox_mode=%s" % json.dumps("workspace-write")]
    cmd += ["-c", "sandbox_workspace_write.network_access=false"]
    cmd += ["-c", "sandbox_workspace_write.writable_roots=%s" % json.dumps([cwd])]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if last_msg_file:
        cmd += ["-o", last_msg_file]
    cmd += [FOLDER_RULES + "\n" + prompt]
    return cmd


def parse_exec(lines: List[str], model: str = "", start: Optional[Dict[str, Any]] = None,
               resumed: bool = False) -> RunResult:
    """`codex exec --json` lines → RunResult. `turn.completed.usage` is the thread's running total (Codex sets it from
    the last token-usage update, whose `total` includes what a resumed thread had saved), so the run used the last one
    minus `start`, the thread's totals before the run; a resumed thread whose totals were not known reports no usage.
    The model is one an event names, else `model` (the configured one)."""
    res = RunResult()
    saw_turn_completed = False
    saw_failure = False
    totals = None
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        t = msg.get("type", "")
        if isinstance(msg.get("model"), str) and msg["model"].strip():
            model = msg["model"]
        if t == "thread.started":
            res.session_id = msg.get("thread_id") or res.session_id
        elif t == "turn.completed":
            saw_turn_completed = True
            if isinstance(msg.get("usage"), dict):
                totals = msg["usage"]
        elif t in ("error", "turn.failed"):
            saw_failure = True
            err = msg.get("error") if isinstance(msg.get("error"), dict) else msg
            text = str((err or {}).get("message") or "")
            if any(m in text.lower() for m in LIMIT_TEXT_MARKERS):
                res.blocked = True
            res.error = res.error or text or t
        elif t == "item.completed":
            item = msg.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                res.output = item["text"]
    res.ok = saw_turn_completed and not saw_failure and not res.blocked
    if res.blocked:
        res.ok = False
    if totals is not None and (start is not None or not resumed):
        res.usage = usage.codex_delta(totals, start, model)
    return res


class ThreadWatch:
    """The thread id of the first `thread.started` event, kept as the stream is read. run_streaming returns only the
    last MAX_CAPTURED_LINES lines, so a long run (or a long failure) would otherwise lose the one line that says which
    session it was: a new thread has no other source for its id, and without it the Runner cannot tell its own run
    from a session the person ran (timetrace.local_sessions)."""

    def __init__(self):
        self.thread_id = None

    def __call__(self, line: str) -> None:
        if self.thread_id is not None or '"thread.started"' not in line:
            return
        try:
            msg = json.loads(line.strip())
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "thread.started":
            thread_id = msg.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.thread_id = thread_id.strip()


_THREAD_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
ROLLOUT_TAIL_BYTES = 4 * 1024 * 1024


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def thread_totals(thread_id: Any, home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The running token totals a Codex thread had at its last recorded model call - what a resumed run continues
    from - read from the end of its rollout file (`sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`). {} when the thread
    recorded none; None when the file is missing, unreadable or its last record is not within the bytes read."""
    if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
        return None
    try:
        found = sorted((home or codex_home()).joinpath("sessions").glob("*/*/*/rollout-*-%s.jsonl" % thread_id))
        if len(found) != 1 or found[0].is_symlink() or not found[0].is_file():
            return None
        with open(found[0], "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - ROLLOUT_TAIL_BYTES))
            data = fh.read(ROLLOUT_TAIL_BYTES)
    except (OSError, ValueError):
        return None
    lines = data.split(b"\n")
    if size > ROLLOUT_TAIL_BYTES:
        lines = lines[1:]  # the first one was cut
    for raw in reversed(lines):
        if b"token_count" not in raw and b"token_usage_record" not in raw:
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
        if line.get("type") == "token_usage_record" and isinstance(payload.get("thread_token_usage"), dict):
            return payload["thread_token_usage"]
        info = payload.get("info")
        if (line.get("type") == "event_msg" and payload.get("type") == "token_count" and isinstance(info, dict)
                and isinstance(info.get("total_token_usage"), dict)):
            return info["total_token_usage"]
    return {} if size <= ROLLOUT_TAIL_BYTES else None


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class CodexAdapter(ToolAdapter):
    name = CODEX
    adapter_version = "codex-cli/work-sessions-1"
    # Execution runs in a worktree take the job's TimeTrace MCP access (`mcp=`).
    supports_mcp = True

    def __init__(self, cfg: Dict[str, Any], billing=None):
        self.cfg = cfg
        # Real check: `codex login status` must report a ChatGPT login and the
        # auth file must hold no API key; no key may exist in the environment.
        self.billing = billing or codex_verifier(cfg)

    def capabilities(self) -> Dict[str, bool]:
        # Implemented surface: records runs, reads quota on demand via
        # app-server (no quota consumed), dispatches headless, resumes a thread.
        # Zero-spend comes only from the billing verdict.
        return merge_capabilities({
            "can_record": True,
            "can_read_quota": True,
            "can_dispatch": True,
            "can_resume": True,
            "can_interact": self.cfg.get("sandbox", "workspace-write") in ("workspace-write", "read-only"),
            "can_enforce_zero_spend": self.billing.verdict().verified,
        })

    def read_limits(self) -> Optional[List[Sample]]:
        resp = app_server_request(self.cfg.get("bin", "codex"), "account/rateLimits/read")
        return parse_rate_limits(resp)

    def reset_credits_entry(self, timeout: float = RESET_CREDITS_TIMEOUT) -> Optional[Dict[str, Any]]:
        """This login's reset credits for inventory `reset_credits` (read
        only, a short-lived app-server). Any failure is status "unknown",
        never a count of 0. None when there is no Codex login to name."""
        key = self.account_key()
        if not key:
            return None
        entry = {"pool_id": "pool-codex-" + key, "tool_profile_id": "codex-default"}
        try:
            parsed = parse_reset_credits(app_server_request(self.cfg.get("bin", "codex"), "account/rateLimits/read",
                                                            timeout=timeout))
        except Exception:
            parsed = None
        if parsed is None:
            entry["status"] = "unknown"
            return entry
        entry.update({"status": "ok", "read_at": _iso(time.time())}, **parsed)
        return entry

    def _auth_path(self) -> Path:
        return Path(self.cfg.get("auth_path") or Path.home() / ".codex" / "auth.json")

    def plan_tier(self) -> Optional[str]:
        return tiers.codex_plan_tier(self._auth_path())

    def routing_account_key(self) -> Optional[str]:
        return tiers.codex_account_key(self._auth_path(), digest_length=64)

    def account_key(self) -> Optional[str]:
        return tiers.codex_account_key(self._auth_path())

    @staticmethod
    def _writable_extras(cwd: str) -> List[str]:
        try:
            return worktree.sandbox_write_roots(cwd)
        except Exception:
            return []

    def _masked_run(self, cmd: List[str], cwd: str, log_file: str, cancel_event, mcp: Optional[McpAccess],
                    resume: Optional[str] = None) -> RunResult:
        """One execution run. Codex writes its final answer to the `-o` file itself, outside the streams that are masked
        as they are read, so that file is masked once the run is over however it ended (finished, failed, cancelled, an
        exception): a token the model repeated is never left in it."""
        with masking([mcp.token] if mcp is not None else []):
            try:
                return self._run(cmd, cwd, log_file, cancel_event, env=mcp.env() if mcp is not None else None,
                                 resume=resume)
            finally:
                mask_file(log_file + ".last.md")

    def work_session(self, prompt, cwd, session_id, log_file, cancel_event, *,
                     control, resuming, read_only, mcp=None) -> RunResult:
        from timetrace.adapters.codex_interactive import CodexTurn

        cmd = [self.cfg.get("bin", "codex"), "app-server"]
        sandbox = "read-only" if read_only else self.cfg.get("sandbox", "workspace-write")
        if sandbox not in ("workspace-write", "read-only"):
            return RunResult(exit_code=1, error="unsupported interactive sandbox")
        domains = network_domains(self.cfg) if sandbox == "workspace-write" else []
        config = {}
        if read_only:
            cmd += ["--disable", "apps", "--disable", "plugins", "--disable", "hooks",
                    "-c", "mcp_servers={}", "-c", 'web_search="disabled"']
            config["mcp_servers"] = {}
        params = {"cwd": cwd, "modelProvider": "openai",
                  "approvalPolicy": "never" if read_only else "on-request", "approvalsReviewer": "user",
                  "developerInstructions": CHAT_RULES if read_only else WORK_SESSION_RULES,
                  "config": config}
        if self.cfg.get("model"):
            params["model"] = self.cfg["model"]
        roots = self._writable_extras(cwd) if sandbox == "workspace-write" else []
        if domains:
            cmd += _network_profile(domains, roots)
        else:
            params["sandbox"] = sandbox
            config["sandbox_workspace_write.network_access"] = False
            config["sandbox_workspace_write.writable_roots"] = roots
        if mcp is not None and not read_only:
            config.update({
                "mcp_servers.timetrace.url": mcp.url,
                "mcp_servers.timetrace.bearer_token_env_var": ENV_NAME,
                "mcp_servers.timetrace.default_tools_approval_mode": "approve",
                "features.shell_snapshot": False,
            })
        env = mcp.env() if mcp is not None and not read_only else None
        turn = CodexTurn(cmd, cwd, control, cancel_event, float(self.cfg.get("timeout_seconds", 3600)), read_only, env)
        if resuming:
            try:
                turn.saved_totals = thread_totals(session_id)
            except Exception:
                turn.saved_totals = None
        with masking([mcp.token] if env else []):
            return turn.run(prompt, session_id if resuming else None, params, log_file)

    def start(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
              mcp: Optional[McpAccess] = None) -> RunResult:
        cmd = build_cmd(self.cfg, prompt, cwd, last_msg_file=log_file + ".last.md",
                        add_dirs=self._writable_extras(cwd), mcp=mcp)
        return self._masked_run(cmd, cwd, log_file, cancel_event, mcp)

    def resume(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
               mcp: Optional[McpAccess] = None) -> RunResult:
        cmd = build_cmd(self.cfg, prompt, cwd, resume=session_id,
                        last_msg_file=log_file + ".last.md", add_dirs=self._writable_extras(cwd), mcp=mcp)
        res = self._masked_run(cmd, cwd, log_file, cancel_event, mcp, resume=session_id)
        res.session_id = res.session_id or session_id
        return res

    def start_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                     cancel_event=None) -> RunResult:
        cmd = build_folder_cmd(self.cfg, prompt, cwd, last_msg_file=log_file + ".last.md")
        return self._run(cmd, cwd, log_file, cancel_event)

    def resume_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                      cancel_event=None) -> RunResult:
        cmd = build_folder_cmd(self.cfg, prompt, cwd, resume=session_id, last_msg_file=log_file + ".last.md")
        res = self._run(cmd, cwd, log_file, cancel_event, resume=session_id)
        res.session_id = res.session_id or session_id
        return res

    def review(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only acceptance review: a chat turn's argv (read-only
        sandbox pinned through config, no writable roots) with the review
        rules, always a new thread."""
        return self.chat(prompt, cwd, "", log_file, cancel_event, rules=REVIEW_RULES)

    def plan(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only project planning request: a chat turn's argv (read-only
        sandbox pinned through config, no writable roots, no git repository
        required) with the planning rules, always a new thread."""
        return self.chat(prompt, cwd, "", log_file, cancel_event, rules=PLANNING_RULES)

    def parse_import(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only conversation import: a chat turn's argv (read-only
        sandbox pinned through config, no writable roots, no git repository
        required) with the import rules, always a new thread."""
        return self.chat(prompt, cwd, "", log_file, cancel_event, rules=IMPORT_RULES)

    def chat(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
             rules: str = CHAT_RULES) -> RunResult:
        """One read-only conversation turn; an empty session_id starts a new
        thread. The reply is the `-o` last-message file (the full final
        message), falling back to the last streamed agent message."""
        last = Path(log_file + ".last.md")
        try:
            last.unlink()  # never report an earlier turn's answer
        except FileNotFoundError:
            pass
        cmd = build_chat_cmd(self.cfg, prompt, cwd, resume=session_id or None, last_msg_file=str(last), rules=rules)
        res = self._run(cmd, cwd, log_file, cancel_event, resume=session_id or None)
        try:
            if last.is_file() and not last.is_symlink():
                text = last.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    res.output = text
        except OSError:
            pass
        res.session_id = res.session_id or session_id or None
        return res

    def _run(self, cmd: List[str], cwd: str, log_file: str, cancel_event=None,
             env: Optional[Dict[str, str]] = None, resume: Optional[str] = None) -> RunResult:
        """`resume`: the thread a resumed run continues; its saved token totals are read first, so the run reports
        only what it used itself."""
        try:
            start = thread_totals(resume) if resume else None
        except Exception:
            start = None  # never a reason for a run to fail: it only reports no usage
        watch = ThreadWatch()
        code, lines = run_streaming(cmd, cwd, log_file, timeout=float(self.cfg.get("timeout_seconds", 3600)),
                                    cancel_event=cancel_event, drop_env=billing_env_keys(CODEX), env=env,
                                    on_line=watch)
        res = parse_exec(lines, model=str(self.cfg.get("model") or ""), start=start, resumed=bool(resume))
        # Every Codex exec run (task, resume, folder, chat, review, plan, import) passes here.
        res.session_id = watch.thread_id or res.session_id
        res.exit_code = code
        if code != 0 and not res.blocked:
            res.ok = False
            res.error = res.error or "codex exited with %d" % code
        if res.blocked:
            # The error text only says "try again at 7:01 PM"; ask app-server for the epoch.
            try:
                samples = self.read_limits() or []
                res.samples.extend(samples)
                for s in samples:
                    if s.bucket_key == PRIMARY_BUCKET and s.reset_at:
                        res.reset_at = s.reset_at
                if res.reset_at is None:
                    resets = [s.reset_at for s in samples if s.used_pct >= 100 and s.reset_at]
                    res.reset_at = min(resets) if resets else None
            except Exception as exc:  # pragma: no cover - network/tool failure path
                res.error = (res.error or "") + " | rateLimits read failed: %s" % exc
        return res
