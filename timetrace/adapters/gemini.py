"""Gemini adapter — management tier only (same posture as Cursor).

Delivers record/represent capability now; dispatch/resume/zero-spend stay off
until the real command surface and billing safety are verified.
"""
from typing import Any, Dict, Optional

from timetrace.adapters.base import ToolAdapter
from timetrace.adapters.cursor import safe_capabilities
from timetrace.quota import merge_capabilities


class GeminiAdapter(ToolAdapter):
    name = "gemini"
    adapter_version = "gemini/unverified"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or {}

    def capabilities(self) -> Dict[str, bool]:
        return merge_capabilities(safe_capabilities())
