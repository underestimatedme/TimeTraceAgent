"""One iteration of the daemon loop (spec §6). Pure over (db, adapters, cfg, now)."""
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from timetrace import hooks, limits, worktree
from timetrace.db import Database
from timetrace.dispatch import (DispatchDenied, LockBusy, UnclearedOwner, adapter_dispatch_problem,
                           coding_slot_lock, enforce_spawn_authority, spawn_authority)
from timetrace.models import (BLOCKED, CLAUDE, DONE, EV_CIRCUIT_OPEN, EV_SAMPLE_FAILURE,
                         EV_TASK_BLOCKED, EV_TASK_DONE, EV_TASK_FAILED, EV_TASK_RESUMED,
                         EV_TOOL_SWITCHED, EV_WINDOW_RESET, FAILED, PENDING, RUNNABLE,
                         RUNNING, TOOLS, RunResult, Sample)

RESUME_PROMPT = (
    "You were interrupted by a usage limit while working on the task below and are now"
    " being resumed in the same session. Continue from where you left off; do not start"
    " over.\n\nOriginal task:\n%s"
)


def run_once(
    db: Database, adapters: Dict[str, Any], cfg: Dict[str, Any], home: Path,
    now: Optional[int] = None, log: Callable[[str], None] = print,
    ensure_worktree: Callable = worktree.ensure, hook_runner: Callable = hooks.run_hook,
    rng: Callable[[], float] = random.random, clock: Optional[Callable[[], int]] = None,
) -> str:
    clock = clock or (lambda: int(time.time()))
    now = now or clock()
    # 1. inbox
    n = hooks.ingest(db, home, cfg, now)
    if n:
        log("ingested %d task(s) from inbox" % n)
    # 2. dependencies
    _advance_dependencies(db, now)
    # 3. wake blocked
    for t in db.tasks_in_state(BLOCKED):
        if t["blocked_until"] is not None and t["blocked_until"] <= now:
            db.update_task(t["id"], state=RUNNABLE, now=now)
            db.add_event(EV_WINDOW_RESET, tool=t["tool"],
                         payload={"task_id": t["id"], "blocked_until": t["blocked_until"]}, at=now)
            log("task %d woke up" % t["id"])
    # 4. circuit breaker
    if _circuit_open(db, cfg, now, log):
        return "circuit open"
    # 5/6. pick task and tool
    rows = limits.snapshot(db)
    for task in db.tasks_in_state(RUNNABLE):
        tool = pick_tool(task, rows, adapters, db, now)
        if tool is None:
            continue
        return _dispatch(db, adapters[tool], tool, task, cfg, home, now, log,
                         ensure_worktree, hook_runner, rng, clock)
    return "idle"


def _advance_dependencies(db: Database, now: int) -> None:
    for t in db.tasks_in_state(PENDING):
        dep = db.get_task(t["depends_on"]) if t["depends_on"] is not None else None
        if dep is None:
            db.update_task(t["id"], state=FAILED, last_error="dependency missing", now=now)
            db.add_event(EV_TASK_FAILED, payload={"task_id": t["id"], "reason": "dependency missing"},
                         at=now)
        elif dep["state"] == DONE:
            db.update_task(t["id"], state=RUNNABLE, now=now)
        elif dep["state"] == FAILED:
            db.update_task(t["id"], state=FAILED,
                           last_error="dependency %d failed" % dep["id"], now=now)
            db.add_event(EV_TASK_FAILED, payload={"task_id": t["id"],
                                                  "reason": "dependency %d failed" % dep["id"]}, at=now)


def _circuit_open(db: Database, cfg: Dict[str, Any], now: int, log: Callable[[str], None]) -> bool:
    threshold = int(cfg.get("circuit_breaker_failures", 3))
    window = int(cfg.get("circuit_window_mins", 300)) * 60
    failures = db.failed_runs_since(now - window)
    if failures < threshold:
        return False
    last = db.list_events(limit=1, type_=EV_CIRCUIT_OPEN)
    if not last or last[0]["at"] < now - window:
        db.add_event(EV_CIRCUIT_OPEN, payload={"failures": failures, "window_sec": window}, at=now)
    log("circuit open: %d failures in the last %d min, waiting for a human" % (failures, window // 60))
    return True


def pick_tool(task: Dict[str, Any], rows: List[Dict[str, Any]], adapters: Dict[str, Any],
              db: Database, now: int) -> Optional[str]:
    """Spec §6 step 6. Returns the tool to use now, or None to skip this round."""
    available = [t for t in TOOLS if t in adapters]
    if task.get("session_id"):
        # Half-done work cannot change tool: the session context does not travel.
        tool = task["tool"]
        return tool if tool in available and not limits.tool_exhausted(rows, tool, now) else None
    preferred = task.get("tool")
    if preferred is None:
        preferred = _most_remaining(rows, available, now) or CLAUDE
        if preferred not in available:
            return None
        return preferred
    if preferred not in available:
        return None
    if not limits.tool_exhausted(rows, preferred, now):
        return preferred
    if not task.get("any_tool"):
        return None
    others = [t for t in available if t != preferred and not limits.tool_exhausted(rows, t, now)]
    if not others:
        return None
    chosen = _most_remaining(rows, others, now) or others[0]
    db.add_event(EV_TOOL_SWITCHED, tool=chosen,
                 payload={"task_id": task["id"], "from": preferred, "to": chosen}, at=now)
    return chosen


def _most_remaining(rows: List[Dict[str, Any]], tools: List[str], now: int) -> Optional[str]:
    best, best_val = None, -2.0
    for t in tools:
        if limits.tool_exhausted(rows, t, now):
            continue
        rem = limits.tool_min_remaining(rows, t)
        # Unknown quota is uncertain, not full: it must not outrank a tool with a
        # known, non-exhausted remaining reading. It is only a last resort when no
        # tool has a known reading.
        val = -1.0 if rem is None else rem
        if val > best_val:
            best, best_val = t, val
    return best


def _dispatch(db: Database, adapter: Any, tool: str, task: Dict[str, Any], cfg: Dict[str, Any],
              home: Path, now: int, log: Callable[[str], None], ensure_worktree: Callable,
              hook_runner: Callable, rng: Callable[[], float], clock: Callable[[], int]) -> str:
    """Fence local execution through the same runner-wide coding slot as the
    cloud agent so the two can never drive a process at the same time."""
    try:
        slot = coding_slot_lock(home).acquire()
    except LockBusy as exc:
        return "task %d → deferred (runner busy)" % task["id"] + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
    try:
        return _dispatch_locked(db, adapter, tool, task, cfg, home, now, log,
                                ensure_worktree, hook_runner, rng, clock)
    finally:
        slot.release()


def _dispatch_locked(db: Database, adapter: Any, tool: str, task: Dict[str, Any], cfg: Dict[str, Any],
                     home: Path, now: int, log: Callable[[str], None], ensure_worktree: Callable,
                     hook_runner: Callable, rng: Callable[[], float], clock: Callable[[], int]) -> str:
    task_id = task["id"]
    resuming = bool(task.get("session_id"))
    reason = adapter_dispatch_problem(adapter, resuming)
    if reason:
        db.update_task(task_id, last_error=reason, now=now)
        return "task %d → blocked (%s)" % (task_id, reason)
    session_id = task.get("session_id") or str(uuid.uuid4())
    try:
        wt, branch = task.get("worktree"), task.get("branch")
        if not wt:
            base = "HEAD"
            if task.get("depends_on") is not None:
                dep = db.get_task(task["depends_on"])
                if dep and dep.get("branch") and dep.get("repo") == task["repo"]:
                    base = dep["branch"]  # build on the previous step's work
            wt, branch = ensure_worktree(task["repo"], task_id, home, base)
    except Exception as exc:
        db.update_task(task_id, state=FAILED, last_error="worktree: %s" % exc, now=now)
        db.add_event(EV_TASK_FAILED, tool=tool, payload={"task_id": task_id, "reason": str(exc)}, at=now)
        return "task %d → failed (worktree)" % task_id
    # Preparation can take long enough for the adapter's authority to change.
    reason = adapter_dispatch_problem(adapter, resuming)
    if reason:
        db.update_task(task_id, last_error=reason, now=now)
        return "task %d → blocked (%s)" % (task_id, reason)
    db.update_task(task_id, state=RUNNING, tool=tool, session_id=session_id, worktree=wt,
                   branch=branch, now=now)
    logs_dir = Path(home) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = db.add_run(task_id, tool, session_id, "", now=now)
    log_path = str(logs_dir / ("task%d-run%d.log" % (task_id, run_id)))
    db.conn.execute("UPDATE run SET log_path=? WHERE id=?", (log_path, run_id))
    if resuming:
        db.add_event(EV_TASK_RESUMED, tool=tool, payload={"task_id": task_id, "run_id": run_id,
                                                          "session_id": session_id}, at=now)
    log("task %d: %s with %s (session %s)" % (task_id, "resume" if resuming else "start", tool,
                                              session_id))
    try:
        with spawn_authority(lambda: adapter_dispatch_problem(adapter, resuming)):
            enforce_spawn_authority()
            if resuming:
                res = adapter.resume(RESUME_PROMPT % task["prompt"], wt, session_id, log_path)
            else:
                res = adapter.start(task["prompt"], wt, session_id, log_path)
    except DispatchDenied as exc:
        reason = str(exc)
        db.finish_run(run_id, -1, blocked=False, summary="dispatch blocked: " + reason, now=clock())
        db.update_task(task_id, state=RUNNABLE, session_id=task.get("session_id"), last_error=reason, now=clock())
        return "task %d → blocked (%s)" % (task_id, reason)
    except Exception as exc:
        res = RunResult(exit_code=-1, session_id=session_id, error="adapter crashed: %s" % exc)
    ended = max(now, clock())
    session_id = res.session_id or session_id
    _record_samples(db, res.samples, tool, adapter, ended, log)
    if res.blocked:
        base = res.reset_at or (ended + int(cfg.get("default_block_sleep_sec", 3600)))
        until = int(base + rng() * int(cfg.get("jitter_sec", 300)))
        db.finish_run(run_id, res.exit_code, blocked=True, summary=(res.error or "rate limited")[:500],
                      session_id=session_id, now=ended)
        db.update_task(task_id, state=BLOCKED, blocked_until=until, session_id=session_id,
                       last_error=res.error, now=ended)
        db.add_event(EV_TASK_BLOCKED, tool=tool, payload={"task_id": task_id, "run_id": run_id,
                                                          "reset_at": res.reset_at,
                                                          "blocked_until": until}, at=ended)
        return "task %d → blocked until %d" % (task_id, until)
    if res.ok:
        db.finish_run(run_id, res.exit_code, blocked=False, summary=(res.output or "")[:500],
                      session_id=session_id, now=ended)
        db.update_task(task_id, state=DONE, session_id=session_id, last_error=None, now=ended)
        db.add_event(EV_TASK_DONE, tool=tool, payload={"task_id": task_id, "run_id": run_id}, at=ended)
        if task.get("on_success"):
            fresh = db.get_task(task_id)
            hook_log = str(logs_dir / ("task%d-hook.log" % task_id))
            # Follow-up generation also resumes the provider session. Keep the
            # same live guard at its Popen boundary; hooks record any denial.
            with spawn_authority(lambda: adapter_dispatch_problem(adapter, True)):
                hook_runner(db, adapter, fresh, home, cfg, ended, hook_log)
        return "task %d → done" % task_id
    err = res.error or "exit %d" % res.exit_code
    db.finish_run(run_id, res.exit_code if res.exit_code else 1, blocked=False, summary=err[:500],
                  session_id=session_id, now=ended)
    db.update_task(task_id, state=FAILED, session_id=session_id, last_error=err, now=ended)
    db.add_event(EV_TASK_FAILED, tool=tool, payload={"task_id": task_id, "run_id": run_id,
                                                     "reason": err[:500]}, at=ended)
    return "task %d → failed: %s" % (task_id, err[:120])


def _record_samples(db: Database, samples: List[Sample], tool: str, adapter: Any, at: int,
                    log: Callable[[str], None]) -> None:
    if samples:
        limits.record_samples(db, samples, at)
    try:
        live = adapter.read_limits()
    except Exception as exc:
        db.add_event(EV_SAMPLE_FAILURE, tool=tool, payload={"error": str(exc)}, at=at)
        log("live quota read failed for %s: %s" % (tool, exc))
        return
    if live:
        limits.record_samples(db, live, at)
