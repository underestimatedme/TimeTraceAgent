"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60 (it runs twenty agents over ACP); it was
not exercised against a real Gemini CLI on this machine. The zero-spend gate is hermetic and allow-only and runs
nothing (billing.gemini_billing_problem): the binary's version is read from its package.json (0.60.x only), the
user's settings must be exactly "personal Google login, AI credits never", the cached login a personal one, and no
.env, workspace Gemini or agent configuration, user extension / command / skill / policy / agent directory or
system configuration may exist. Each run repeats the whole check right before its spawn, starts exactly the binary
that was checked, and gives the child only the allowlisted part of the run's one environment snapshot.
"""
from typing import Dict, List, Optional, Tuple

from timetrace.adapters.acp import AcpAdapter
from timetrace.billing import BillingVerifier, gemini_billing_problem, gemini_child_env, verify_gemini


class GeminiAdapter(AcpAdapter):
    name = "gemini"
    adapter_version = "gemini-cli/acp-1"
    default_bin = "gemini"
    launch_args = ("--acp", "--skip-trust")

    def default_billing(self):
        # Reads files only (nothing is run); cached, and forced fresh at the gate before every spawn.
        return BillingVerifier(lambda now: verify_gemini(
            {"bin": self.binary(), "extra_args": self.cfg.get("extra_args") or []}, now=now))

    def prepare(self, cwd: str) -> Tuple[str, Optional[Dict[str, str]], List[str]]:
        # The run's one environment snapshot (taken by the base class) is what the whole check reads; its allowlisted
        # part is the child's entire environment, and the child is the binary whose package.json was just read.
        problem, snapshot, cmd = super().prepare(cwd)
        if problem:
            return problem, None, cmd
        problem, resolved = gemini_billing_problem(snapshot, cwd, self.binary(), self.cfg.get("extra_args") or ())
        if problem or resolved is None:
            return problem or "gemini_version_unknown", None, cmd
        return "", gemini_child_env(snapshot), [resolved] + cmd[1:]
