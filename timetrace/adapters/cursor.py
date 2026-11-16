"""Cursor over the Agent Client Protocol: `cursor-agent acp`.

The run itself works like Gemini's (one generic ACP client), but Cursor is never dispatched: its plans can bill
on-demand usage beyond the included amount, and the CLI exposes no signal this runner could check before a spawn.
The billing verdict is therefore fixed at unverified, and adapter_dispatch_problem keeps answering
`billing_unverified`. There is deliberately no configuration that changes this.
"""
from timetrace.adapters.acp import AcpAdapter, safe_capabilities  # noqa: F401 (safe_capabilities: compatibility)
from timetrace.billing import StaticBilling

CURSOR_BILLING_REASON = "cursor_billing_unverifiable"


class CursorAdapter(AcpAdapter):
    name = "cursor"
    adapter_version = "cursor-agent/acp-1"
    default_bin = "cursor-agent"
    launch_args = ("acp",)

    def __init__(self, cfg=None, which=None):
        # No `billing` parameter: not even code can hand this adapter a verified verdict.
        super().__init__(cfg, billing=None, which=which)

    def default_billing(self):
        return StaticBilling(False, CURSOR_BILLING_REASON, provider="cursor")
