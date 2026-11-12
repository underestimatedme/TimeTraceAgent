"""One gate for every start/resume, plus the file locks that fence concurrent
dispatch on a single runner.

Both the cloud agent and the local scheduler must funnel through here so a Plan /
workspace is never driven by two processes at once, and nothing runs without a
verifiable zero-additional-spend guarantee.
"""
import errno
import fcntl
import hashlib
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional


@dataclass(frozen=True)
class DispatchGate:
    cancelled: bool
    lease_valid: bool
    runner_online: bool
    dependencies_ready: bool
    zero_spend_verified: bool


def deny_reason(gate: DispatchGate) -> Optional[str]:
    """First blocking reason, or None when dispatch may proceed. Cancellation
    dominates; a missing billing guarantee always blocks."""
    checks = [
        (gate.cancelled, "cancelled"),
        (not gate.lease_valid, "lease_expired"),
        (not gate.runner_online, "runner_offline"),
        (not gate.dependencies_ready, "dependencies_pending"),
        (not gate.zero_spend_verified, "billing_unverified"),
    ]
    return next((reason for blocked, reason in checks if blocked), None)


def adapter_capabilities(adapter: Any) -> Dict[str, Any]:
    """Read an adapter capability declaration without trusting its surface."""
    try:
        capabilities = getattr(adapter, "capabilities", None)
        if not callable(capabilities):
            return {}
        caps = capabilities()
    except Exception:
        return {}
    return caps if isinstance(caps, dict) else {}


def adapter_zero_spend_verified(adapter: Any) -> bool:
    """Return true only when an adapter explicitly verifies zero-spend mode."""
    return adapter_capabilities(adapter).get("can_enforce_zero_spend") is True


def adapter_dispatch_problem(adapter: Any, resuming: bool = False) -> str:
    caps = adapter_capabilities(adapter)
    if caps.get("can_enforce_zero_spend") is not True:
        return "billing_unverified"
    if caps.get("can_dispatch") is not True:
        return "dispatch_unavailable"
    if resuming and caps.get("can_resume") is not True:
        return "resume_unavailable"
    return ""


class DispatchDenied(RuntimeError):
    """Authority changed before the actual OS process creation."""


_spawn_check = ContextVar("timetrace_spawn_check", default=None)


@contextmanager
def spawn_authority(check):
    """Carry the caller's live authority through adapters to run_streaming.
    Install inside the worker thread; contexts are not inherited by executors.
    """
    token = _spawn_check.set(check)
    try:
        yield
    finally:
        _spawn_check.reset(token)


def enforce_spawn_authority(cancel_event=None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DispatchDenied("cancelled")
    check = _spawn_check.get()
    if check is not None:
        try:
            reason = check()
        except Exception as exc:
            raise DispatchDenied("authority_unavailable") from exc
        if reason:
            raise DispatchDenied(reason)


class LockBusy(RuntimeError):
    """Raised when a non-blocking file lock is already held elsewhere."""


class UnclearedOwner(LockBusy):
    """The OS lock is free but an unclean execution left its durable fence."""


def manual_clearance(path: str) -> str:
    return ("manual recovery required for %s: stop all runners and writer descendants, "
            "verify no process is writing, then clear this marker under an exclusive lock; "
            "do not unlink an active lock file" % path)


def lock_diagnostics(home):
    """Read-only diagnosis; never clear markers or infer safety from a PID."""
    messages = []
    for path in sorted(_locks_dir(home).glob("*.lock")):
        fd = os.open(str(path), os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                messages.append("active execution lock: %s" % path)
            else:
                if os.read(fd, 1):
                    messages.append(manual_clearance(str(path)))
        finally:
            os.close(fd)
    return messages


class FileLock:
    """Non-blocking exclusive advisory lock over a lock file. flock treats each
    open file description independently, so a second acquirer — even in the same
    process — is denied while the first holds it.

    A durable ownership marker also fences an unclean exit: flock alone is
    released when the parent dies, even if a provider child is still writing.
    Never reclaim based on parent PID liveness. An uncleared marker requires
    manual verification that ALL descendants have stopped before clearing it.
    """

    def __init__(self, path: str):
        self.path = str(path)
        self._fd: Optional[int] = None

    def acquire(self) -> "FileLock":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(self.path) from exc
            raise
        try:
            if os.read(fd, 1):
                raise UnclearedOwner(manual_clearance(self.path))
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                os.fsync(self._fd)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _locks_dir(home) -> Path:
    return Path(home) / "locks"


def coding_slot_lock(home, index: int = 0) -> FileLock:
    """One of the runner-wide coding-process slots. Slot 0 keeps the original
    file name (the local scheduler uses only it); the cloud agent may use
    slots 0..max_parallel-1, so at most max_parallel tool processes run."""
    name = "coding-slot.lock" if not index else "coding-slot-%d.lock" % int(index)
    return FileLock(str(_locks_dir(home) / name))


def tool_slot_lock(home, tool: str, index: int) -> FileLock:
    """One of a tool's own slots (config `max_parallel_per_tool`): an AI job
    holds one of these for its provider and one runner-wide coding slot, so
    Claude and Codex jobs run side by side up to their own limits."""
    name = re.sub(r"[^a-z0-9_-]", "_", str(tool).lower())[:32] or "_"
    return FileLock(str(_locks_dir(home) / ("coding-slot-%s-%d.lock" % (name, int(index)))))


class SlotLimits(NamedTuple):
    """The limits the cloud agent's jobs obey: `overall` coding slots (`max_parallel`) and, when per-tool
    limits are configured, `per_tool[tool]` slots of the tool's own."""
    overall: int
    per_tool: Optional[Dict[str, int]] = None


class ToolSlotBusy(LockBusy):
    """Every slot of the job's tool is taken (or fenced)."""

    def __init__(self, tool: str, cause: Exception):
        super().__init__(str(cause))
        self.tool, self.cause = tool, cause


def first_free(locks):
    """The first lock of `locks` that can be taken. A lock with a crash fence is skipped (it stays fenced
    for manual recovery); only when none is free does a fence surface, as UnclearedOwner."""
    busy = fenced = None
    for lock in locks:
        try:
            return lock.acquire()
        except UnclearedOwner as exc:
            fenced = fenced or exc
        except LockBusy as exc:
            busy = exc
    raise fenced or busy


def acquire_job_slots(home, tool: Optional[str], limits: Optional[SlotLimits] = None) -> List[FileLock]:
    """The slots one local job holds, in the order the agent takes its own: the tool's slot, then the first
    free overall slot, so a queue task beside an online agent stays inside the same limits. Without `limits`
    only overall slot 0 is taken (the original local-only fence). A failure never leaves a slot taken;
    raises ToolSlotBusy / LockBusy / UnclearedOwner."""
    if limits is None:
        return [coding_slot_lock(home).acquire()]
    held: List[FileLock] = []
    if tool is not None and limits.per_tool is not None:
        count = max(1, int(limits.per_tool.get(tool, 1)))
        try:
            held.append(first_free([tool_slot_lock(home, tool, i) for i in range(count)]))
        except LockBusy as exc:
            raise ToolSlotBusy(tool, exc) from exc
    try:
        held.append(first_free([coding_slot_lock(home, i) for i in range(max(1, int(limits.overall)))]))
    except BaseException:
        for lock in reversed(held):
            lock.release()
        raise
    return held


def plan_lock(home, plan_key: str) -> FileLock:
    """Per-Plan lock: a Plan (and its checkpoint / execution directory) is
    never driven by two jobs at once, whatever workspace it runs in."""
    digest = hashlib.sha256(str(plan_key).encode("utf-8")).hexdigest()[:16]
    return FileLock(str(_locks_dir(home) / ("plan-%s.lock" % digest)))


def workspace_lock(home, canonical_path: str) -> FileLock:
    """Per canonical-workspace lock. A git workspace holds it only while a
    job prepares its worktree (shared .git writes); a folder workspace holds
    it for the whole run, because its change check needs a quiet folder."""
    digest = hashlib.sha256(os.path.realpath(canonical_path).encode("utf-8")).hexdigest()[:16]
    return FileLock(str(_locks_dir(home) / ("workspace-%s.lock" % digest)))
