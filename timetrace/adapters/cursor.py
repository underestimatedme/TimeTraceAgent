"""Cursor adapter — management tier only.

Until Cursor's real headless command surface and billing behaviour are verified
(with saved, de-identified fixtures and a verification date), this adapter only
records/represents the tool. It never fakes start/resume, so dispatch and resume
stay disabled and no unattended execution is possible.
"""
from typing import Any, Dict, Optional

from timetrace.adapters.base import ToolAdapter
from timetrace.quota import merge_capabilities


def safe_capabilities() -> Dict[str, bool]:
    """Conservative management-mode defaults: record only. Dispatch, resume and
    zero-spend enforcement stay off until proven per tool."""
    return {"can_record": True, "can_read_quota": False, "can_dispatch": False,
            "can_resume": False, "can_enforce_zero_spend": False}


class CursorAdapter(ToolAdapter):
    name = "cursor"
    adapter_version = "cursor/unverified"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or {}

    def capabilities(self) -> Dict[str, bool]:
        return merge_capabilities(safe_capabilities())

    # No start/resume override: the base raises NotImplementedError, so an
    # unverified tool can never be dispatched or resumed.
