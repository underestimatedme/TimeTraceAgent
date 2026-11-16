"""Coding agents driven over the Agent Client Protocol: the adapter surface Gemini CLI and Cursor share.

One generic client (timetrace.acp_client) runs every turn; a subclass only names its launch command and its billing
verdict. What the gates see is the same as for Claude Code and Codex:

- can_dispatch / can_resume: the binary resolves (an absolute `bin` that is executable, or a name on PATH, which is
  what Popen will look up). Whether the agent can resume a session is only known from its `initialize` answer; one
  that cannot fails the resumed run with `resume_unsupported` instead of silently starting a new session.
- can_enforce_zero_spend: from the billing verdict only. adapter_dispatch_problem therefore blocks every tool whose
  billing is not verified, whatever its surface claims.
- can_read_quota / can_interact: false. ACP has no quota read, and the work-session protocol (confirmed delivery of
  each message) is Claude's / Codex's own; appended instructions run as resumed turns (`can_resume`).

The run receives the job's RunControl (`control=`) as its permission host: the worktree's local rules, then the
phone; without one every permission prompt is refused. The job's TimeTrace MCP access (`mcp=`) is offered to the
agent as an http MCP server when the agent says it takes one.
"""
import os
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple

from timetrace.acp_client import AcpResult, AcpSession, safe_text
from timetrace.adapters.base import SAFETY_RULES, ToolAdapter
from timetrace.billing import StaticBilling, billing_env_keys
from timetrace.mcp_access import McpAccess
from timetrace.models import RunResult
from timetrace.quota import merge_capabilities
from timetrace.redact import masking
from timetrace.toolpath import is_executable

# A prompt that failed with one of these is a usage limit (the session stays resumable), not a broken run.
LIMIT_TEXT_MARKERS = ("rate limit", "usage limit", "quota", "resource_exhausted", "resource exhausted", "429")


def safe_capabilities() -> Dict[str, bool]:
    """Management-only: record the tool and nothing else. What an ACP adapter declares while its binary cannot be
    found; dispatch, resume and zero-spend enforcement are never claimed without proof."""
    return {"can_record": True, "can_read_quota": False, "can_dispatch": False,
            "can_resume": False, "can_enforce_zero_spend": False}


def run_result(acp: AcpResult, session_id: Optional[str]) -> RunResult:
    """An ACP turn as the scheduler / agent read a run. Only a turn that ended with `end_turn` succeeded; how the
    idle agent was stopped afterwards (its exit code is usually a signal) says nothing about the work."""
    res = RunResult(session_id=acp.session_id or session_id,
                    output=(acp.last_message.strip() or acp.text.strip()))
    if acp.error is None and acp.stop_reason == "end_turn":
        res.ok, res.exit_code = True, 0
        return res
    res.exit_code = acp.exit_code if acp.exit_code not in (None, 0) else 1
    if acp.error:
        res.error = safe_text(acp.error, 500)   # agent-supplied text: masked whole, then cut
        # A limit can only be told apart once a session exists to resume; before that it is an ordinary failure.
        if acp.failed_method == "session/prompt" and any(m in acp.error.lower() for m in LIMIT_TEXT_MARKERS):
            res.blocked = True
    else:
        res.error = "the agent ended the turn: %s" % (safe_text(acp.stop_reason) or "no stop reason")
    return res


class AcpAdapter(ToolAdapter):
    name = "acp"
    adapter_version = "acp/1"
    default_bin = ""
    launch_args: tuple = ()
    # Runs accept the job's RunControl (`control=`) as permission host, and its TimeTrace MCP access (`mcp=`).
    interactive_runs = True
    supports_mcp = True

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, billing=None,
                 which: Optional[Callable[[str], Optional[str]]] = None):
        self.cfg = cfg or {}
        self.billing = billing if billing is not None else self.default_billing()
        self._which = which or shutil.which

    def default_billing(self):
        return StaticBilling(False, "management_only", provider=self.name)

    def binary(self) -> str:
        value = self.cfg.get("bin")
        return value.strip() if isinstance(value, str) and value.strip() else self.default_bin

    def resolved_binary(self) -> Optional[str]:
        binary = self.binary()
        if not binary:
            return None
        if os.path.isabs(binary):
            return binary if is_executable(binary) else None
        return self._which(binary)

    def command(self) -> List[str]:
        """The launch command: the pinned ACP entry point, then the user's own `extra_args`. Nothing secret is ever
        in it (the MCP token travels inside the protocol)."""
        return [self.binary()] + list(self.launch_args) + [str(arg) for arg in self.cfg.get("extra_args") or []]

    def capabilities(self) -> Dict[str, bool]:
        caps = dict(safe_capabilities())
        if self.resolved_binary() is not None:
            caps.update(can_dispatch=True, can_resume=True)
        caps["can_enforce_zero_spend"] = self.billing.verdict().verified
        return merge_capabilities(caps)

    def prepare(self, cwd: str) -> Tuple[str, Optional[Dict[str, str]]]:
        """(why this run must not start from `cwd`, "" to go ahead; the run's one environment snapshot). The snapshot
        is what a subclass checks and exactly what the child is started with (minus the billing keys and secret-named
        variables): os.environ is not read again between the check and the spawn."""
        return "", dict(os.environ)

    def start(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
              control=None, mcp: Optional[McpAccess] = None) -> RunResult:
        # An ACP agent names its own sessions: the id TimeTrace drew for a fresh run is not sent anywhere, the
        # result carries the agent's (what a checkpoint keeps and a resume sends back).
        return self._run(prompt, cwd, None, log_file, cancel_event, control, mcp)

    def resume(self, prompt: str, cwd: str, session_id: str, log_file: str, cancel_event=None,
               control=None, mcp: Optional[McpAccess] = None) -> RunResult:
        if not session_id:
            return RunResult(exit_code=1, error="no session to resume")
        return self._run(prompt, cwd, session_id, log_file, cancel_event, control, mcp)

    def _run(self, prompt, cwd, session_id, log_file, cancel_event, control, mcp) -> RunResult:
        problem, snapshot = self.prepare(cwd)
        if problem:
            with open(log_file, "a", encoding="utf-8") as log:
                log.write("--- not started: billing_unverified (%s) ---\n" % problem)
            return RunResult(exit_code=1, session_id=session_id, error="billing_unverified: " + problem)
        # The rules go first in the one prompt ACP has (it has no system-prompt channel), as for Codex.
        session = AcpSession(self.command(), cwd, log_file, SAFETY_RULES + "\n" + prompt, session_id=session_id,
                             host=control, mcp=mcp, timeout=float(self.cfg.get("timeout_seconds", 3600)),
                             cancel_event=cancel_event, drop_env=billing_env_keys(self.name), base_env=snapshot)
        with masking([mcp.token] if mcp is not None else []):
            return run_result(session.run(), session_id)
