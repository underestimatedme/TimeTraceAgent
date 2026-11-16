"""Coding agents driven over the Agent Client Protocol: the adapter surface Gemini CLI and Cursor share.

One generic client (timetrace.acp_client) runs every turn; a subclass only names its launch command. Neither agent is
ever dispatched: both adapters have a fixed unverified billing verdict (zero-spend and credential isolation could
not be verified, see README), so adapter_dispatch_problem always answers `billing_unverified`. The run path is kept
working and hardened for whoever audits an agent on a real install:

- one environment snapshot per run; the child gets only its allowlisted variables (ACP_ENV_ALLOWLIST), with PATH
  reduced to its absolute entries;
- the binary is resolved once against that PATH and started by its absolute real path, so what was resolved is what
  runs (a relative PATH entry cannot swap in a project's own `gemini`, or `node` for a `#!/usr/bin/env node` script);
- per-stream transcript buffers, redaction before any cut, bounded stdin writes and the MCP token only in the
  session/new header (timetrace.acp_client).

What the gates see: can_dispatch / can_resume follow the binary; can_enforce_zero_spend is the (fixed, false) billing
verdict; can_read_quota / can_interact are false. A run receives the job's RunControl (`control=`) as its permission
host and the job's TimeTrace MCP access (`mcp=`) as an http MCP server when the agent takes one.
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

# The only variables an ACP child receives, from the run's one environment snapshot:
#   PATH          finding `node` (both CLIs are node scripts) and the commands the model runs; absolute entries only
#   HOME          where the CLI keeps its own login and settings
#   USER, LOGNAME the account name tools such as git fall back to
#   SHELL         the shell the CLI's command tool starts
#   LANG, LC_*    text encoding of the CLI's and the tools' output
#   TERM          output formatting of the tools the model runs
#   TMPDIR        the per-user temporary directory macOS assigns (node's os.tmpdir())
#   TZ            timestamps
# Everything else stays out by construction: API keys, endpoint overrides (CODE_ASSIST_ENDPOINT, *_BASE_URL),
# config-location overrides (GEMINI_CLI_HOME, ...), GOOGLE_APPLICATION_CREDENTIALS, proxies, extra CA certificates.
ACP_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")
ACP_ENV_ALLOWED_PREFIXES = ("LC_",)


def absolute_path_entries(value: str) -> str:
    """PATH with only its absolute entries: an empty or relative entry would resolve against the run's directory,
    which holds repository content."""
    return os.pathsep.join(entry for entry in (value or "").split(os.pathsep) if entry and os.path.isabs(entry))


def acp_child_env(snapshot: Dict[str, str]) -> Dict[str, str]:
    """An ACP child's whole environment: the allowlisted variables of the run's snapshot, nothing else."""
    env = {key: value for key, value in snapshot.items()
           if key in ACP_ENV_ALLOWLIST or key.startswith(ACP_ENV_ALLOWED_PREFIXES)}
    if "PATH" in env:
        env["PATH"] = absolute_path_entries(env["PATH"])
    return env


def resolve_binary(binary: str, path: str) -> Optional[str]:
    """The absolute real path of `binary` (an absolute `bin`, or a name looked up on `path`), or None."""
    if not binary:
        return None
    found = binary if os.path.isabs(binary) else shutil.which(binary, path=absolute_path_entries(path))
    if not found or not is_executable(found):
        return None
    real = os.path.realpath(found)
    return real if is_executable(real) else None


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
        self._which = which   # tests only; otherwise the same resolution a run uses

    def default_billing(self):
        return StaticBilling(False, "management_only", provider=self.name)

    def binary(self) -> str:
        value = self.cfg.get("bin")
        return value.strip() if isinstance(value, str) and value.strip() else self.default_bin

    def resolved_binary(self) -> Optional[str]:
        if self._which is not None and not os.path.isabs(self.binary()):
            return self._which(self.binary())
        return resolve_binary(self.binary(), os.environ.get("PATH", ""))

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

    def prepare(self, cwd: str) -> Tuple[str, Optional[Dict[str, str]], List[str]]:
        """(why this run cannot start, "" to go ahead; the child's environment; the command). One snapshot of the
        environment is taken here: the binary is resolved against its PATH (absolute entries only) and started by its
        absolute real path, and its allowlisted variables are the child's whole environment. os.environ is not read
        again before the spawn."""
        env = acp_child_env(dict(os.environ))
        cmd = self.command()
        real = resolve_binary(cmd[0], env.get("PATH", ""))
        if real is None:
            return "could not start %s: not found on an absolute PATH entry" % os.path.basename(cmd[0]), None, cmd
        return "", env, [real] + cmd[1:]

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
        problem, env, cmd = self.prepare(cwd)
        if problem:
            with open(log_file, "a", encoding="utf-8") as log:
                log.write("--- not started: %s ---\n" % problem)
            return RunResult(exit_code=127, session_id=session_id, error=problem)
        # The rules go first in the one prompt ACP has (it has no system-prompt channel), as for Codex.
        session = AcpSession(cmd, cwd, log_file, SAFETY_RULES + "\n" + prompt, session_id=session_id,
                             host=control, mcp=mcp, timeout=float(self.cfg.get("timeout_seconds", 3600)),
                             cancel_event=cancel_event, drop_env=billing_env_keys(self.name), base_env=env)
        with masking([mcp.token] if mcp is not None else []):
            return run_result(session.run(), session_id)
