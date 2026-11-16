"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60 (it runs twenty agents over ACP); it was
not exercised against a real Gemini CLI on this machine. The zero-spend gate is hermetic and allow-only
(billing.gemini_billing_problem): the child gets an allowlisted environment from the run's one snapshot, and a run is
verified only when no .env or system settings file exists anywhere the CLI could read one, the user's settings say
exactly "personal Google login, AI credits never", no workspace settings touch security / billing / advanced, and the
binary reports the audited version 0.60.x. Each run repeats the whole check right before its spawn.
"""
from typing import Dict, Optional, Tuple

from timetrace.adapters.acp import AcpAdapter
from timetrace.billing import BillingVerifier, gemini_child_env, gemini_workspace_problem, verify_gemini


class GeminiAdapter(AcpAdapter):
    name = "gemini"
    adapter_version = "gemini-cli/acp-1"
    default_bin = "gemini"
    launch_args = ("--acp", "--skip-trust")

    def default_billing(self):
        # Reads files and runs `<bin> --version` (no model call); cached, and forced fresh at the gate before every
        # spawn.
        return BillingVerifier(lambda now: verify_gemini({"bin": self.binary()}, now=now))

    def prepare(self, cwd: str) -> Tuple[str, Optional[Dict[str, str]]]:
        # The run's one environment snapshot (taken by the base class) is what the whole check reads, and its
        # allowlisted part is the child's entire environment: nothing is read from os.environ again before the spawn.
        problem, snapshot = super().prepare(cwd)
        problem = problem or gemini_workspace_problem(cwd, snapshot, binary=self.binary())
        return problem, gemini_child_env(snapshot)
