"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60 (it runs twenty agents over ACP); it was
not exercised against a real Gemini CLI on this machine. Dispatch is allowed only with a verified personal Google
login (billing.verify_gemini), and each run first checks what its own directory would add: the .env file Gemini
CLI loads from there and the workspace's .gemini/settings.json (billing.gemini_workspace_problem). The child never
inherits the Gemini / Google Cloud billing variables.
"""
from timetrace.adapters.acp import AcpAdapter
from timetrace.billing import gemini_verifier, gemini_workspace_problem


class GeminiAdapter(AcpAdapter):
    name = "gemini"
    adapter_version = "gemini-cli/acp-1"
    default_bin = "gemini"
    launch_args = ("--acp", "--skip-trust")

    def default_billing(self):
        # Reads files only (no command, no model call); cached, and forced fresh at the gate before every spawn.
        return gemini_verifier(self.cfg)

    def preflight(self, cwd: str) -> str:
        return gemini_workspace_problem(cwd)
