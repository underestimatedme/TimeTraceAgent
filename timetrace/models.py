"""Shared constants and small data classes. No I/O here."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Task states (spec §4)
PENDING = "pending"
RUNNABLE = "runnable"
RUNNING = "running"
BLOCKED = "blocked"
DONE = "done"
FAILED = "failed"
STATES = (PENDING, RUNNABLE, RUNNING, BLOCKED, DONE, FAILED)

CLAUDE = "claude"
CODEX = "codex"
TOOLS = (CLAUDE, CODEX)
# Agents driven over the Agent Client Protocol (timetrace.adapters.acp); registered only when configured, never
# picked automatically by the local queue.
ACP_TOOLS = ("gemini", "cursor")

# Event types (spec §4)
EV_RATE_LIMIT = "rate_limit"
EV_WINDOW_RESET = "window_reset"
EV_SAMPLE_FAILURE = "sample_failure"
EV_TASK_BLOCKED = "task_blocked"
EV_TASK_RESUMED = "task_resumed"
EV_TOOL_SWITCHED = "tool_switched"
EV_TASK_DONE = "task_done"
EV_TASK_FAILED = "task_failed"
EV_TASK_WAITING = "task_waiting"
EV_TASK_INTERRUPTED = "task_interrupted"
EV_CIRCUIT_OPEN = "circuit_open"
EV_HOOK_GENERATED = "hook_generated"
EV_HOOK_REJECTED = "hook_rejected"


@dataclass
class Sample:
    """One reading of one rate-limit bucket."""

    bucket_key: str
    tool: str
    used_pct: float
    reset_at: Optional[int] = None
    window_mins: Optional[int] = None
    is_representative: bool = False
    source: str = "run"
    # Vendor permission, not inferred from percentages; legacy adapters omit it.
    codex_ordinary_usage_allowed: Optional[bool] = None


@dataclass
class RunResult:
    """Outcome of one headless run (start or resume) of a tool."""

    exit_code: int = 0
    session_id: Optional[str] = None
    ok: bool = False
    blocked: bool = False
    reset_at: Optional[int] = None
    samples: List[Sample] = field(default_factory=list)
    error: Optional[str] = None
    output: str = ""
    # Tokens this run used (timetrace.usage): {"model", "input_tokens" (cache reads not included), "output_tokens",
    # "cache_read_tokens", "cache_write_tokens"}; None when the tool reported none.
    usage: Optional[Dict[str, Any]] = None
    # The commands this run made as its tool's stream showed them (timetrace.run_facts): {"commands": {ran, failed},
    # "tests": {ran, failed, last?, tools?}}; None when this kind of run is not watched or its stream was not read whole.
    commands: Optional[Dict[str, Any]] = None
