"""Gemini CLI over the Agent Client Protocol: `gemini --acp --skip-trust`.

The launch command is the one the Codeg project pins for Gemini CLI >= 0.60; it was not exercised against a real
Gemini CLI on this machine. A Gemini run works technically (the generic ACP client), but Gemini is never dispatched:
its zero-spend and credential isolation could not be verified without auditing a real install. Four review rounds
kept finding configuration the CLI loads and executes on its own (workspace and user settings, MCP servers,
extensions, commands, policies, agents), credential types beyond a personal login, a cached MCP OAuth token that may
be refreshed against a cached third-party URL, and PATH / `node` resolution that can swap in other code. The billing
verdict is therefore fixed at unverified until such an audit (see README), and there is deliberately no
configuration, and no constructor argument, that changes this.
"""
from timetrace.adapters.acp import AcpAdapter
from timetrace.billing import StaticBilling

GEMINI_BILLING_REASON = "gemini_billing_unverifiable"


class GeminiAdapter(AcpAdapter):
    name = "gemini"
    adapter_version = "gemini-cli/acp-1"
    default_bin = "gemini"
    launch_args = ("--acp", "--skip-trust")

    def __init__(self, cfg=None, which=None):
        # No `billing` parameter: not even code can hand this adapter a verified verdict.
        super().__init__(cfg, billing=None, which=which)

    def default_billing(self):
        # Dispatch stays off until zero-spend and credential isolation are audited on a real install.
        return StaticBilling(False, GEMINI_BILLING_REASON, provider="gemini")
