"""Tool adapters: one interface, one implementation per tool."""
from typing import Any, Dict

from timetrace.adapters.base import ToolAdapter
from timetrace.adapters.claude import ClaudeAdapter
from timetrace.adapters.codex import CodexAdapter
from timetrace.adapters.cursor import CursorAdapter
from timetrace.adapters.gemini import GeminiAdapter
from timetrace.models import CLAUDE, CODEX


def build_adapters(cfg: Dict[str, Any]) -> Dict[str, ToolAdapter]:
    adapters: Dict[str, ToolAdapter] = {CLAUDE: ClaudeAdapter(cfg[CLAUDE]), CODEX: CodexAdapter(cfg[CODEX])}
    # The ACP agents register only when the user has configured a profile (e.g. {"gemini": {"bin": "gemini"}}).
    # They run through the generic ACP client but are never dispatched: both carry a fixed unverified billing verdict
    # (zero-spend and credential isolation could not be verified; see README).
    if cfg.get("cursor"):
        adapters["cursor"] = CursorAdapter(cfg["cursor"])
    if cfg.get("gemini"):
        adapters["gemini"] = GeminiAdapter(cfg["gemini"])
    return adapters
