"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60 (it runs twenty agents over ACP); it was
not exercised against a real Gemini CLI on this machine. Dispatch is allowed only with a verified personal Google
login (billing.verify_gemini), and each run repeats the whole check right before its spawn with what its own
directory adds: the .env file Gemini CLI loads from there and the workspace's .gemini/settings.json
(billing.gemini_workspace_problem). The child never inherits the Gemini / Google Cloud billing variables, and its
GEMINI_CLI_HOME is pinned to the config home that was checked.
"""
import os
from typing import Dict, Tuple

from timetrace.adapters.acp import AcpAdapter
from timetrace.billing import gemini_config_home, gemini_verifier, gemini_workspace_problem


class GeminiAdapter(AcpAdapter):
    name = "gemini"
    adapter_version = "gemini-cli/acp-1"
    default_bin = "gemini"
    launch_args = ("--acp", "--skip-trust")

    def default_billing(self):
        # Reads files only (no command, no model call); cached, and forced fresh at the gate before every spawn.
        return gemini_verifier(self.cfg)

    def prepare(self, cwd: str) -> Tuple[str, Dict[str, str]]:
        # One snapshot of the environment decides both the check and the child's config home: GEMINI_CLI_HOME is
        # pinned to the directory whose settings and .env were just verified (the user's override when set, else the
        # home), so an override can neither slip past the check nor change between the check and the spawn.
        env = dict(os.environ)
        home, reason = gemini_config_home(env)
        if home is None:
            return reason, {}
        problem = gemini_workspace_problem(cwd, env=env)
        return problem, ({} if problem else {"GEMINI_CLI_HOME": str(home)})
