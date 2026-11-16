"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60 (it runs twenty agents over ACP); it was
not exercised against a real Gemini CLI on this machine. Dispatch is allowed only with a verified zero-spend setup
(billing.verify_gemini: personal Google login, paid AI credits explicitly "never", no key / Vertex / Cloud project
anywhere the CLI could read one, no settings-path override). Each run repeats the whole check right before its
spawn against the run's one environment snapshot, with what its own directory adds (every .env on the way up, the
workspace's .gemini/settings.json), and the child is started with exactly that snapshot minus the billing keys.
"""
from typing import Dict, Optional, Tuple

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

    def prepare(self, cwd: str) -> Tuple[str, Optional[Dict[str, str]]]:
        # The run's one environment snapshot (taken by the base class) is what the whole check reads (HOME, every
        # variable, every path derived from them) and exactly what the child is started with, minus the billing
        # keys. Nothing is read from os.environ again between the check and the spawn.
        problem, snapshot = super().prepare(cwd)
        problem = problem or gemini_workspace_problem(cwd, snapshot, extra_args=self.cfg.get("extra_args") or ())
        return problem, snapshot
