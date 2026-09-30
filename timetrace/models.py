"""Shared constants and small data classes. No I/O here."""
from dataclasses import dataclass, field
from typing import List, Optional

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

# Event types (spec §4)
EV_RATE_LIMIT = "rate_limit"
EV_WINDOW_RESET = "window_reset"
EV_SAMPLE_FAILURE = "sample_failure"
EV_TASK_BLOCKED = "task_blocked"
EV_TASK_RESUMED = "task_resumed"
EV_TOOL_SWITCHED = "tool_switched"
EV_TASK_DONE = "task_done"
EV_TASK_FAILED = "task_failed"
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
