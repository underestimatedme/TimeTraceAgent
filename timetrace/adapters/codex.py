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
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from timetrace import __version__
from timetrace.adapters.base import CHAT_RULES, FOLDER_RULES, IMPORT_RULES, REVIEW_RULES, SAFETY_RULES, ToolAdapter, run_streaming
from timetrace import tiers, worktree
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
def build_cmd(cfg: Dict[str, Any], prompt: str, cwd: str, resume: Optional[str] = None,
              last_msg_file: Optional[str] = None, add_dirs: Optional[List[str]] = None) -> List[str]:
    sandbox = str(cfg.get("sandbox", "workspace-write"))
    cmd = [cfg.get("bin", "codex"), "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += ["--json", "--skip-git-repo-check"]
    if not resume:
        cmd += ["-s", sandbox, "-C", cwd]
    # `codex exec resume` has no -s: pin the sandbox through config so a
    # resumed run cannot fall back to a laxer mode from ~/.codex/config.toml.
    cmd += ["-c", "sandbox_mode=%s" % json.dumps(sandbox)]
    # Model-run commands never need the network (the model API is called by
    # codex itself, outside the sandbox); this also makes `git push` fail.
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


def parse_exec(lines: List[str]) -> RunResult:
    res = RunResult()
    saw_turn_completed = False
    saw_failure = False
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        t = msg.get("type", "")
        if t == "thread.started":
            res.session_id = msg.get("thread_id") or res.session_id
        elif t == "turn.completed":
            saw_turn_completed = True
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
    return res


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class CodexAdapter(ToolAdapter):
    name = CODEX
    adapter_version = "codex-cli/0.151"

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

    def account_key(self) -> Optional[str]:
        return tiers.codex_account_key(self._auth_path())

    @staticmethod
    def _writable_extras(cwd: str) -> List[str]:
        try:
            return worktree.sandbox_write_roots(cwd)
        except Exception:
            return []

    def start(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None) -> RunResult:
        cmd = build_cmd(self.cfg, prompt, cwd, last_msg_file=log_file + ".last.md",
                        add_dirs=self._writable_extras(cwd))
        return self._run(cmd, cwd, log_file, cancel_event)

    def resume(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None) -> RunResult:
        cmd = build_cmd(self.cfg, prompt, cwd, resume=session_id,
                        last_msg_file=log_file + ".last.md", add_dirs=self._writable_extras(cwd))
        res = self._run(cmd, cwd, log_file, cancel_event)
        res.session_id = res.session_id or session_id
        return res

    def start_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                     cancel_event=None) -> RunResult:
        cmd = build_folder_cmd(self.cfg, prompt, cwd, last_msg_file=log_file + ".last.md")
        return self._run(cmd, cwd, log_file, cancel_event)

    def resume_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                      cancel_event=None) -> RunResult:
        cmd = build_folder_cmd(self.cfg, prompt, cwd, resume=session_id, last_msg_file=log_file + ".last.md")
        res = self._run(cmd, cwd, log_file, cancel_event)
        res.session_id = res.session_id or session_id
        return res

    def review(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only acceptance review: a chat turn's argv (read-only
        sandbox pinned through config, no writable roots) with the review
        rules, always a new thread."""
        return self.chat(prompt, cwd, "", log_file, cancel_event, rules=REVIEW_RULES)

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
        res = self._run(cmd, cwd, log_file, cancel_event)
        try:
            if last.is_file() and not last.is_symlink():
                text = last.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    res.output = text
        except OSError:
            pass
        res.session_id = res.session_id or session_id or None
        return res

    def _run(self, cmd: List[str], cwd: str, log_file: str, cancel_event=None) -> RunResult:
        code, lines = run_streaming(cmd, cwd, log_file, timeout=float(self.cfg.get("timeout_seconds", 3600)),
                                    cancel_event=cancel_event, drop_env=billing_env_keys(CODEX))
        res = parse_exec(lines)
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
