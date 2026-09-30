"""Claude Code adapter: `claude -p --output-format stream-json`.

Facts this relies on (verified 2026-09-02 on Claude Code 2.1.258):
- every -p run emits one `rate_limit_event` whose `rate_limit_info.unifiedWindows`
  holds per-window utilization (0..1) and resetsAt; `rateLimitType` names the
  binding window; `status` is allowed / allowed_warning / rejected.
- the final `result` message carries session_id, is_error, subtype, api_error_status.
- there is NO on-demand quota query, so read_limits() returns None.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from timetrace.adapters.base import CHAT_RULES, FOLDER_RULES, IMPORT_RULES, REVIEW_RULES, SAFETY_RULES, ToolAdapter, run_streaming
from timetrace.billing import billing_env_keys, claude_verifier
from timetrace import tiers
from timetrace.adapters import claude_usage
from timetrace.claude_stream import run_stream
from timetrace.models import CLAUDE, RunResult, Sample
from timetrace.quota import merge_capabilities

LIMIT_TEXT_MARKERS = ("hit your limit", "usage limit", "rate limit")


def build_cmd(
    cfg: Dict[str, Any], prompt: str, session_id: Optional[str] = None,
    resume: Optional[str] = None,
) -> List[str]:
    cmd = [cfg.get("bin", "claude"), "-p", "--output-format", "stream-json", "--verbose"]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--permission-mode", cfg.get("permission_mode", "acceptEdits")]
    cmd += ["--disallowedTools", "Bash(git push*)"]
    allowed = list(cfg.get("allowed_tools") or [])
    if allowed:
        cmd += ["--allowedTools"] + allowed
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    # Only the user's own settings: project/local settings come from worktree
    # content, and -p mode skips the trust dialog that would guard them.
    if cfg.get("setting_sources", "user"):
        cmd += ["--setting-sources", str(cfg.get("setting_sources", "user"))]
    cmd += ["--append-system-prompt", SAFETY_RULES]
    cmd += list(cfg.get("extra_args") or [])
    cmd += [safe_positional(prompt)]
    return cmd


def build_chat_cmd(
    cfg: Dict[str, Any], prompt: str, session_id: Optional[str] = None,
    resume: Optional[str] = None, rules: str = CHAT_RULES,
) -> List[str]:
    """A read-only conversation turn in the user's main checkout: plan mode
    (no edits, no command execution), and none of the task-run widenings
    (`allowed_tools`, `permission_mode`, `extra_args`) from the config."""
    cmd = [cfg.get("bin", "claude"), "-p", "--output-format", "stream-json", "--verbose"]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--permission-mode", "plan"]
    cmd += ["--disallowedTools", "Bash(git push*)"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    # H-5: the checkout's own .claude/settings*.json are repository content.
    if cfg.get("setting_sources", "user"):
        cmd += ["--setting-sources", str(cfg.get("setting_sources", "user"))]
    # No MCP server (user or project configured) is reachable from a chat
    # turn: its tools are outside plan mode's read-only guarantee.
    cmd += ["--strict-mcp-config"]
    cmd += ["--append-system-prompt", rules]
    cmd += [safe_positional(prompt)]
    return cmd


# No shell in a folder run: the whole Bash tool is denied. Deny rules take
# precedence over allow rules, so a `permissions.allow` entry for Bash in the
# user's settings (still loaded: --setting-sources user) cannot bring it back.
FOLDER_DENIED_TOOLS = ("Bash",)


def build_folder_cmd(
    cfg: Dict[str, Any], prompt: str, workspace: str, session_id: Optional[str] = None,
    resume: Optional[str] = None,
) -> List[str]:
    """A run in a folder workspace: cwd is the task's output directory, the
    workspace is added for reading the source material. acceptEdits in
    restricted mode with no MCP servers and the Bash tool denied entirely; none of the task-run widenings
    (`allowed_tools`, `permission_mode`, `extra_args`) apply. The runner's
    before/after snapshot is what enforces "nothing outside the output
    directory changed"."""
    cmd = [cfg.get("bin", "claude"), "-p", "--output-format", "stream-json", "--verbose"]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--permission-mode", "acceptEdits"]
    cmd += ["--add-dir", workspace]
    # --restricted (Claude Code >= 2.1.283): no command/code-running tools or
    # WebFetch, user/project/local settings files ignored (so no user
    # `permissions.allow` entry widens the run), file tools confined to cwd
    # plus --add-dir. --strict-mcp-config without --mcp-config: no MCP
    # servers at all. --setting-sources is left out: restricted mode already
    # ignores every settings file and must not be re-widened.
    cmd += ["--restricted", "--strict-mcp-config"]
    cmd += ["--disallowedTools"] + list(FOLDER_DENIED_TOOLS)
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    cmd += ["--append-system-prompt", FOLDER_RULES]
    cmd += [safe_positional(prompt)]
    return cmd


# Remote task runs (protocol 2) are bidirectional: the prompt and appended
# instructions go in as stream-json user messages on stdin (never argv), and
# permission prompts come back to the runner over the same channel.
STREAM_IO = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
# Who answers permission prompts. `--permission-prompts host` alone is not
# enough: Claude Code 2.1.285 routes prompts to the stdio host only with
# `--permission-prompt-tool stdio` (what the Agent SDK passes).
HOST_PROMPTS = ["--permission-prompts", "host", "--permission-prompt-tool", "stdio"]
# Push is denied by the CLI itself as well as by the local rules; the
# documented prefix form and the older glob form.
PUSH_DENIED = ["Bash(git push:*)", "Bash(git push*)"]
# Configured permission modes that only mean "the default for task runs";
# a stream run replaces them with `default` so every edit and command that
# Claude would auto-accept reaches the local rule table.
DEFAULT_MODES = ("", "acceptEdits", "default", "manual")


def stream_permission_mode(cfg: Dict[str, Any]) -> str:
    mode = str(cfg.get("permission_mode") or "")
    return "default" if mode in DEFAULT_MODES else mode


def build_stream_cmd(cfg: Dict[str, Any], session_id: Optional[str] = None,
                     resume: Optional[str] = None) -> List[str]:
    """A remote task run in its worktree: stream-json both ways, permission
    prompts answered by the runner (local rules, then the phone)."""
    cmd = [cfg.get("bin", "claude"), "-p"] + STREAM_IO
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--permission-mode", stream_permission_mode(cfg)]
    cmd += HOST_PROMPTS
    cmd += ["--disallowedTools"] + PUSH_DENIED
    allowed = list(cfg.get("allowed_tools") or [])
    if allowed:
        cmd += ["--allowedTools"] + allowed
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    # H-5: project settings are worktree content.
    if cfg.get("setting_sources", "user"):
        cmd += ["--setting-sources", str(cfg.get("setting_sources", "user"))]
    cmd += ["--append-system-prompt", SAFETY_RULES]
    cmd += list(cfg.get("extra_args") or [])
    return cmd


def build_stream_folder_cmd(cfg: Dict[str, Any], workspace: str, session_id: Optional[str] = None,
                            resume: Optional[str] = None) -> List[str]:
    """A folder-workspace run with stream-json input (for appended
    instructions). Same containment as build_folder_cmd; nothing is ever
    asked: `--permission-prompts none` denies every prompt locally."""
    cmd = [cfg.get("bin", "claude"), "-p"] + STREAM_IO
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    cmd += ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    cmd += ["--add-dir", workspace]
    cmd += ["--restricted", "--strict-mcp-config"]
    cmd += ["--disallowedTools"] + list(FOLDER_DENIED_TOOLS)
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    cmd += ["--append-system-prompt", FOLDER_RULES]
    return cmd


def safe_positional(prompt: str) -> str:
    """The prompt is untrusted (it comes from the phone through Valley). A
    leading "-" would make Claude's option parser read it as a flag such as
    --settings or --permission-mode; a leading space keeps it positional."""
    return " " + prompt if prompt.startswith("-") else prompt


def parse_stream(lines: List[str]) -> RunResult:
    """Turn stream-json lines into a RunResult (exit_code left for the caller)."""
    res = RunResult()
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mtype = msg.get("type")
        if mtype == "rate_limit_event":
            _apply_rate_limit(msg.get("rate_limit_info") or {}, res)
        elif mtype == "result":
            _apply_result(msg, res)
        if not res.session_id and msg.get("session_id"):
            res.session_id = msg["session_id"]
    return res


def _apply_rate_limit(info: Dict[str, Any], res: RunResult) -> None:
    rep = info.get("rateLimitType")
    windows = info.get("unifiedWindows") or {}
    for key, w in windows.items():
        util = w.get("utilization")
        if util is None:
            continue
        res.samples.append(Sample(
            bucket_key="claude:%s" % key, tool=CLAUDE, used_pct=round(float(util) * 100, 2),
            reset_at=_int_or_none(w.get("resetsAt")), window_mins=_window_mins(key),
            is_representative=(key == rep), source="run",
        ))
    if info.get("status") == "rejected":
        res.blocked = True
        res.reset_at = _int_or_none(info.get("resetsAt")) or res.reset_at
    elif info.get("resetsAt") and rep:
        # remember the binding window's reset even when allowed; used if we get blocked later
        res.reset_at = res.reset_at or _int_or_none(info.get("resetsAt"))


def _apply_result(msg: Dict[str, Any], res: RunResult) -> None:
    res.output = msg.get("result") or ""
    res.session_id = msg.get("session_id") or res.session_id
    is_error = bool(msg.get("is_error"))
    status = msg.get("api_error_status")
    text = (res.output or "").lower()
    if status == 429 or any(m in text for m in LIMIT_TEXT_MARKERS) and is_error:
        res.blocked = True
    if is_error and not res.blocked:
        res.error = "%s: %s" % (msg.get("subtype", "error"), res.output[:500])
    res.ok = (not is_error) and (not res.blocked) and msg.get("subtype") == "success"


def _window_mins(key: str) -> Optional[int]:
    if key.startswith("five_hour"):
        return 300
    if key.startswith("seven_day"):
        return 7 * 24 * 60
    return None


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class ClaudeAdapter(ToolAdapter):
    name = CLAUDE
    adapter_version = "claude-code/0.5"
    # Runs accept a RunControl (`control=`): stream-json input, appended
    # instructions while running, permission prompts answered by the host.
    interactive_runs = True

    def __init__(self, cfg: Dict[str, Any], billing=None, credentials=None):
        self.cfg = cfg
        # The local OAuth login (file, else Keychain). Tests inject a stub so
        # they never touch the developer's real login.
        self._credentials = credentials or (lambda: tiers.claude_oauth(self.credentials_path()))
        # Real check: `claude auth status` must report a claude.ai subscription
        # login with the first-party provider, and no API-key path may exist in
        # the environment or settings. Tests inject a StaticBilling.
        self.billing = billing or claude_verifier(cfg)

    def credentials_path(self) -> Path:
        return Path(self.cfg.get("credentials_path") or Path.home() / ".claude" / ".credentials.json")

    def plan_tier(self) -> Optional[str]:
        return tiers.claude_plan_tier(self.credentials_path())

    def account_key(self) -> Optional[str]:
        return tiers.claude_account_key(Path(self.cfg.get("claude_json_path") or Path.home() / ".claude.json"))

    def read_limits(self) -> Optional[List[Sample]]:
        """On-demand read through the OAuth usage endpoint; None when the local
        login is missing, expired, or the endpoint refuses."""
        return claude_usage.usage_samples(self._credentials(), now=time.time())

    def capabilities(self) -> Dict[str, bool]:
        # Implemented surface: records runs, reads quota on demand through the
        # OAuth usage endpoint when a local login exists, dispatches headless,
        # resumes the original session via `--resume`. Zero-spend comes only
        # from the billing verdict; unverified keeps the gate closed.
        return merge_capabilities({
            "can_record": True,
            "can_read_quota": bool((self._credentials() or {}).get("accessToken")),
            "can_dispatch": True,
            "can_resume": True,
            "can_enforce_zero_spend": self.billing.verdict().verified,
        })


    def start(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
              control=None) -> RunResult:
        if control is not None:
            return self._run_stream(build_stream_cmd(self.cfg, session_id=session_id), cwd, log_file, prompt,
                                    session_id, cancel_event, control)
        return self._run(build_cmd(self.cfg, prompt, session_id=session_id), cwd, log_file,
                         session_id, cancel_event)

    def resume(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
               control=None) -> RunResult:
        if control is not None:
            return self._run_stream(build_stream_cmd(self.cfg, resume=session_id), cwd, log_file, prompt,
                                    session_id, cancel_event, control)
        return self._run(build_cmd(self.cfg, prompt, resume=session_id), cwd, log_file,
                         session_id, cancel_event)

    def start_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                     cancel_event=None, control=None) -> RunResult:
        if control is not None:
            return self._run_stream(build_stream_folder_cmd(self.cfg, workspace, session_id=session_id), cwd,
                                    log_file, prompt, session_id, cancel_event, control)
        return self._run(build_folder_cmd(self.cfg, prompt, workspace, session_id=session_id), cwd, log_file,
                         session_id, cancel_event)

    def resume_folder(self, prompt: str, cwd: str, workspace: str, session_id: str, log_file: str,
                      cancel_event=None, control=None) -> RunResult:
        if control is not None:
            return self._run_stream(build_stream_folder_cmd(self.cfg, workspace, resume=session_id), cwd,
                                    log_file, prompt, session_id, cancel_event, control)
        return self._run(build_folder_cmd(self.cfg, prompt, workspace, resume=session_id), cwd, log_file,
                         session_id, cancel_event)

    def chat(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only conversation turn. An empty session_id starts a new
        conversation under a fresh uuid4; otherwise that session is resumed."""
        if session_id:
            cmd = build_chat_cmd(self.cfg, prompt, resume=session_id)
        else:
            session_id = str(uuid.uuid4())
            cmd = build_chat_cmd(self.cfg, prompt, session_id=session_id)
        return self._run(cmd, cwd, log_file, session_id, cancel_event)

    def review(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only acceptance review: a chat turn's argv (plan mode,
        user settings only, no MCP server) with the review rules, always in
        a new session."""
        session_id = str(uuid.uuid4())
        cmd = build_chat_cmd(self.cfg, prompt, session_id=session_id, rules=REVIEW_RULES)
        return self._run(cmd, cwd, log_file, session_id, cancel_event)

    def parse_import(self, prompt: str, cwd: str, log_file: str, cancel_event=None) -> RunResult:
        """One read-only conversation import: a chat turn's argv (plan mode,
        user settings only, no MCP server) with the import rules, always in
        a new session."""
        session_id = str(uuid.uuid4())
        cmd = build_chat_cmd(self.cfg, prompt, session_id=session_id, rules=IMPORT_RULES)
        return self._run(cmd, cwd, log_file, session_id, cancel_event)

    def _run_stream(self, cmd: List[str], cwd: str, log_file: str, prompt: str, session_id: str,
                    cancel_event, control) -> RunResult:
        # Time spent waiting for a permission decision does not count
        # toward timeout_seconds (the session pauses its clock).
        code, lines = run_stream(cmd, cwd, log_file, prompt, host=control,
                                 timeout=float(self.cfg.get("timeout_seconds", 3600)),
                                 cancel_event=cancel_event, drop_env=billing_env_keys(CLAUDE))
        return self._result(code, lines, session_id)

    def _run(self, cmd: List[str], cwd: str, log_file: str, session_id: str, cancel_event=None) -> RunResult:
        code, lines = run_streaming(cmd, cwd, log_file, timeout=float(self.cfg.get("timeout_seconds", 3600)),
                                    cancel_event=cancel_event, drop_env=billing_env_keys(CLAUDE))
        return self._result(code, lines, session_id)

    @staticmethod
    def _result(code: int, lines: List[str], session_id: str) -> RunResult:
        res = parse_stream(lines)
        res.exit_code = code
        res.session_id = res.session_id or session_id
        if code != 0 and not res.blocked and res.ok:
            res.ok = False
        if code != 0 and not res.blocked and not res.error:
            res.error = "claude exited with %d" % code
        return res
