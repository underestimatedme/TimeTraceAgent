"""Durable checkpoints and the natural-recovery decision.

A checkpoint captures just enough to resume the *same* provider session in the
*same* canonical workspace after a quota block — never secrets or the CLI
environment. Resume is only ever the original tool/session; a different tool or a
missing native session is not a resume.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

CHECKPOINT_SCHEMA_VERSION = 2


@dataclass
class Checkpoint:
    plan_id: str
    job_id: str
    attempt_id: str
    tool_profile_id: str
    provider_session_id: str
    canonical_workspace: str
    git_head: str
    dirty_paths_digest: str
    last_output_offset: int
    completed_criteria: List[str]
    side_effect_summary: str
    reason: str
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    provider: str = ""
    execution_path: str = ""
    output_path: str = ""
    # The sub-task branch the run was prepared with ("" = the default
    # `timetrace/<id>`, and folder runs); None in checkpoints written before
    # branches were made unique per job.
    branch: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Checkpoint":
        allowed = {f: row[f] for f in cls.__dataclass_fields__ if f in row}
        allowed.setdefault("schema_version", 1)
        return cls(**allowed)


def checkpoint_problem(cp: Checkpoint, provider: str, profile: str, workspace: str,
                       native_resume: bool) -> str:
    """Fail closed for incomplete/changed recovery evidence; never start fresh."""
    if cp.schema_version != CHECKPOINT_SCHEMA_VERSION:
        return "checkpoint schema requires manual recovery"
    if (any(not isinstance(getattr(cp, key), str) for key in (
            "plan_id", "job_id", "attempt_id",
            "provider_session_id", "provider", "tool_profile_id", "canonical_workspace",
            "execution_path", "output_path", "git_head", "dirty_paths_digest"))
            or not isinstance(cp.branch, (str, type(None)))
            or type(cp.last_output_offset) is not int):
        return "checkpoint fields malformed"
    if any(not getattr(cp, key).strip() for key in ("plan_id", "job_id", "attempt_id")):
        return "checkpoint identity missing"
    if not cp.provider_session_id or not cp.provider_session_id.strip():
        return "checkpoint has no native session"
    if cp.provider != provider or not resume_allowed(cp.tool_profile_id, profile, native_resume):
        return "checkpoint provider/profile/resume capability changed"
    if cp.canonical_workspace != workspace:
        return "checkpoint registered workspace changed"
    if not cp.execution_path or not Path(cp.execution_path).is_dir():
        return "checkpoint execution directory missing"
    if str(Path(cp.execution_path).resolve()) != cp.execution_path:
        return "checkpoint execution directory changed"
    if not cp.git_head or not cp.dirty_paths_digest:
        return "checkpoint git evidence missing"
    if not cp.output_path or not Path(cp.output_path).is_file():
        return "checkpoint output missing"
    if cp.last_output_offset < 0 or Path(cp.output_path).stat().st_size < cp.last_output_offset:
        return "checkpoint output truncated"
    return ""


def resume_allowed(original_profile: str, requested_profile: str, native_resume: bool) -> bool:
    """A resume must keep the same tool profile and rely on a real native resume;
    the session context does not travel across tools, and there is no honest
    'fake resume' when the tool cannot continue a session."""
    return bool(original_profile) and original_profile == requested_profile and native_resume


@dataclass(frozen=True)
class ResumeInputs:
    cancelled: bool
    original_profile: str
    requested_profile: str
    native_resume: bool
    auto_resume_enabled: bool
    zero_spend_verified: bool
    availability: str  # available / blocked / unknown
    reliable_reset_passed: bool
    probe_used: bool
    has_native_session: bool


def resume_decision(inputs: ResumeInputs) -> str:
    """The natural-recovery state machine, as a pure decision.

    Returns one of:
      no_resume     - cancelled: the wake generation is invalid, never restart.
      waiting_input - no native session, or a cross-tool / non-native request.
      refresh_only  - auto-resume off: quota may be refreshed but nothing starts.
      resume        - fresh sample says available (+ zero-spend verified).
      probe         - unknown but a reliable reset passed; one controlled probe.
      wait          - blocked, unknown without a reliable reset, a spent probe,
                      or no verified zero-spend guarantee.
    """
    if inputs.cancelled:
        return "no_resume"
    if not inputs.has_native_session:
        return "waiting_input"
    if not resume_allowed(inputs.original_profile, inputs.requested_profile, inputs.native_resume):
        return "waiting_input"
    if not inputs.auto_resume_enabled:
        return "refresh_only"
    if not inputs.zero_spend_verified:
        return "wait"
    if inputs.availability == "available":
        return "resume"
    if inputs.availability == "unknown" and inputs.reliable_reset_passed and not inputs.probe_used:
        return "probe"
    return "wait"
