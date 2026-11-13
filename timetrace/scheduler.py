"""One iteration of the daemon loop (spec §6). Pure over (db, adapters, cfg, now)."""
import inspect
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from timetrace import hooks, limits, prerequisites, worktree
from timetrace.db import Database
from timetrace.dispatch import (DispatchDenied, LockBusy, SlotLimits, ToolSlotBusy, acquire_job_slots, UnclearedOwner, adapter_dispatch_problem,
                           enforce_spawn_authority, spawn_authority)
from timetrace.models import (BLOCKED, CLAUDE, DONE, EV_CIRCUIT_OPEN, EV_HOOK_REJECTED, EV_SAMPLE_FAILURE,
                         EV_TASK_BLOCKED, EV_TASK_DONE, EV_TASK_FAILED, EV_TASK_INTERRUPTED, EV_TASK_RESUMED,
                         EV_TASK_WAITING, EV_TOOL_SWITCHED, EV_WINDOW_RESET, FAILED, PENDING, RUNNABLE,
                         RUNNING, TOOLS, RunResult, Sample)
from timetrace.process import ToolExitUnconfirmed

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
    slots: Optional[SlotLimits] = None, stop: Optional[threading.Event] = None,
) -> str:
    """`stop` is the queue's stop request (a `process.StopRequest`, set by SIGINT / SIGTERM). Once it is set nothing
    new is started, and the tool already running is stopped by the process runner, which reports back on it."""
    clock = clock or (lambda: int(time.time()))
    now = now or clock()
    if stop is not None and stop.is_set():
        return "stopping"
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
    withheld: List[str] = []
    for task in db.tasks_in_state(RUNNABLE):
        if stop is not None and stop.is_set():
            return "stopping"
        # A runnable row may have been written by something that never looked at the parent (an older
        # `timetrace retry`, an older scheduler). Settle that before a tool is chosen, so a task that cannot
        # start now costs no tool-switch event and no slot, and so it never stands in front of the queue.
        held_back = _withhold_until_prerequisite(db, task, now)
        if held_back is not None:
            withheld.append(held_back)
            continue
        tool = pick_tool(task, rows, adapters, db, now)
        if tool is None:
            continue
        out = _dispatch(db, adapters[tool], tool, task, cfg, home, now, log,
                        ensure_worktree, hook_runner, rng, clock, slots, stop)
        if isinstance(out, _Withheld):  # the start-time check said no: nothing ran, go on with the queue
            withheld.append(str(out))
            continue
        return out
    return "; ".join(withheld) if withheld else "idle"


class _Withheld(str):
    """A task was NOT started because the work before it has not passed (or can never pass). The text says why."""


def _advance_dependencies(db: Database, now: int) -> None:
    """Move tasks that wait for a parent along. A task whose parent has not finished simply keeps waiting: a parent
    that failed can still be retried by a person (with its session and worktree), and everything that waits for it
    must still be there when it passes. Only a parent that no longer exists is final.

    Every move is made only if the row is still in the state it was read in (`move_task`): a pass over a list read
    a moment ago can never undo what someone else did to a row meanwhile."""
    # An older scheduler failed such tasks the moment their parent failed ("dependency N failed") and a retry of
    # the parent never brought them back. They never ran: put them back where they belong.
    for t in db.tasks_failed_only_by_dependency():
        if db.get_task(t["depends_on"]) is not None and db.move_task(t["id"], FAILED, now=now, state=PENDING,
                                                                      last_error=None):
            db.add_event(EV_TASK_WAITING, payload={
                "task_id": t["id"], "depends_on": t["depends_on"],
                "reason": "an older version marked it failed only because #%d failed; it never ran" % t["depends_on"]},
                at=now)
    for t in db.tasks_in_state(PENDING):
        try:
            _settle_pending(db, t, now)
        except Exception as exc:  # one unreadable row must not stop every other dependency from advancing
            db.add_event("loop_error", payload={"task_id": t["id"], "error": "dependency check: %s" % exc}, at=now)


def _settle_pending(db: Database, t: Dict[str, Any], now: int) -> None:
    if t["depends_on"] is None:
        _fail_unstartable(db, t, "dependency missing", now, PENDING)
        return
    pre = prerequisites.check(db, t)
    if pre.status == prerequisites.READY:
        db.move_task(t["id"], PENDING, now=now, state=RUNNABLE)
    elif pre.status != prerequisites.WAITING:
        _fail_unstartable(db, t, _unsatisfiable_reason(pre), now, PENDING)


def _unsatisfiable_reason(pre: "prerequisites.Prerequisite") -> str:
    if pre.status == prerequisites.MISSING:
        return "dependency missing" if pre.missing == pre.parent else "dependency %d missing" % pre.missing
    return "dependency cycle"


def _fail_unstartable(db: Database, t: Dict[str, Any], reason: str, now: int, expected: str,
                      kept: Optional[Tuple[str, str]] = None) -> bool:
    if not db.move_task(t["id"], expected, now=now, state=FAILED, last_error=reason):
        return False
    db.add_event(EV_TASK_FAILED, payload=dict({"task_id": t["id"], "reason": reason}, **_kept_payload(kept)), at=now)
    return True


def _kept_payload(kept: Optional[Tuple[str, str]]) -> Dict[str, Any]:
    """A worktree prepared for a start that did not happen stays on disk for a person to look at. It is named in
    the event, not written to the task row: a worktree on the row would say the task ran."""
    return {"worktree": kept[0], "branch": kept[1], "worktree_kept": True} if kept and kept[0] else {}


def _kept_note(kept: Optional[Tuple[str, str]]) -> str:
    return "; worktree %s kept" % kept[0] if kept and kept[0] else ""


def _withhold_until_prerequisite(db: Database, task: Dict[str, Any], now: int,
                                 kept: Optional[Tuple[str, str]] = None) -> Optional[str]:
    """None when the RUNNABLE `task` may start now. Otherwise it was NOT started: it has been put where it belongs
    (waiting, or failed for a reason that is its own to explain) and the returned text says what happened.

    `task` is whatever row the caller holds; the parent is read here, from the database as it is now. Every start
    goes through this, and the start itself repeats it under the execution slot (see `_dispatch`), once more when
    the worktree is ready (see `_claim_start`) and a last time at the spawn gate (see `_start_authority`).
    `kept` is the (worktree, branch) prepared for this start: it stays on disk and is named in the event and in
    the text, never written to the row."""
    pre = prerequisites.check(db, task)
    if pre.ready:
        return None
    task_id = task["id"]
    changed = "task %d → skipped (changed meanwhile)" % task_id
    if pre.status != prerequisites.WAITING:
        reason = _unsatisfiable_reason(pre)
        if not _fail_unstartable(db, task, reason, now, RUNNABLE, kept):
            return changed
        return "task %d → failed (%s)%s" % (task_id, reason, _kept_note(kept))
    if prerequisites.has_executed(db, task):
        # It really ran before the work in front of it was done. Starting it again is not ours to decide: keep the
        # evidence (session, worktree, runs), leave it failed, and say what a person can do.
        reason = "前置工作 #%d 尚未完成；本任务此前已执行过，不自动重跑（#%d 完成后可 timetrace retry %d）" % (
            pre.blocker, pre.blocker, task_id)
        if not _fail_unstartable(db, task, reason, now, RUNNABLE, kept):
            return changed
        return "task %d → failed (%s)%s" % (task_id, reason, _kept_note(kept))
    if not db.move_task(task_id, RUNNABLE, now=now, state=PENDING, blocked_until=None):
        return changed
    db.add_event(EV_TASK_WAITING, tool=task.get("tool"), payload=dict({
        "task_id": task_id, "depends_on": pre.parent, "blocker": pre.blocker, "blocker_state": pre.blocker_state,
        "reason": "runnable before its parent finished; put back to waiting"}, **_kept_payload(kept)), at=now)
    return "task %d → %s%s" % (task_id, prerequisites.describe(pre), _kept_note(kept))


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
              hook_runner: Callable, rng: Callable[[], float], clock: Callable[[], int],
              slots: Optional[SlotLimits] = None, stop: Optional[threading.Event] = None) -> str:
    """Fence local execution through the same runner-wide coding slots as the cloud agent, so the two never
    drive more processes than the agent's own limits allow. `slots` None: overall slot 0 only (the queue
    runs alone). Given: the tool's slot and the first free overall slot, as the agent takes them (the queue
    runs beside an online agent)."""
    try:
        held = acquire_job_slots(home, tool, slots)
    except ToolSlotBusy as exc:
        return ("task %d → deferred (tool busy: %s)" % (task["id"], exc.tool)
                + ("; " + str(exc.cause) if isinstance(exc.cause, UnclearedOwner) else ""))
    except LockBusy as exc:
        return "task %d → deferred (runner busy)" % task["id"] + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
    try:
        # `task` was read before the slot was ours. Under the slot, only a task that is still runnable runs...
        current = db.get_task(task["id"])
        if current is None or current["state"] != RUNNABLE:
            return "task %d → skipped (no longer runnable: %s)" % (task["id"], current["state"] if current else "deleted")
        task = current
        # ...and only one whose explicit parent is DONE right now. This is the check that counts: it reads the rows
        # under the slot, so no candidate list, retry or runnable row written earlier can get past it.
        held_back = _withhold_until_prerequisite(db, task, now)
        if held_back is not None:
            return _Withheld(held_back)
        return _dispatch_locked(db, adapter, tool, task, cfg, home, now, log,
                                ensure_worktree, hook_runner, rng, clock, stop)
    except ToolExitUnconfirmed:
        # A tool that may still be running keeps its slot fenced: the locks are closed but their ownership
        # markers stay, exactly as after a crash, until a person has verified nothing is writing and cleared them.
        fenced, held = held, []
        for lock in fenced:
            lock.abandon()
        raise
    finally:
        for lock in reversed(held):
            lock.release()


def _dispatch_locked(db: Database, adapter: Any, tool: str, task: Dict[str, Any], cfg: Dict[str, Any],
                     home: Path, now: int, log: Callable[[str], None], ensure_worktree: Callable,
                     hook_runner: Callable, rng: Callable[[], float], clock: Callable[[], int],
                     stop: Optional[threading.Event] = None) -> str:
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
        if stop is not None and stop.is_set():  # a terminal's Ctrl-C reaches git too: nothing ran, nothing failed
            return _Withheld("task %d → not started (stop requested)" % task_id)
        db.update_task(task_id, state=FAILED, last_error="worktree: %s" % exc, now=now)
        db.add_event(EV_TASK_FAILED, tool=tool, payload={"task_id": task_id, "reason": str(exc)}, at=now)
        return "task %d → failed (worktree)" % task_id
    # Preparation can take long enough for the adapter's authority to change.
    reason = adapter_dispatch_problem(adapter, resuming)
    if reason:
        db.update_task(task_id, last_error=reason, now=now)
        return "task %d → blocked (%s)" % (task_id, reason)
    if stop is not None and stop.is_set():
        return _Withheld("task %d → not started (stop requested)" % task_id)
    logs_dir = Path(home) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    # The worktree took as long as it took. The parent is read again, and the task is claimed, in ONE short
    # transaction: a `timetrace rm` of the parent lands entirely before it (nothing starts) or entirely after it
    # (the task started while its parent was DONE). No tool runs inside it.
    claimed = _claim_start(db, task, tool, session_id, wt, branch, now, logs_dir)
    if isinstance(claimed, _Withheld):
        return claimed
    run_id, log_path = claimed
    log("task %d: %s with %s (session %s)" % (task_id, "resume" if resuming else "start", tool,
                                              session_id))
    try:
        with spawn_authority(_start_authority(db, adapter, resuming, task_id)):
            enforce_spawn_authority()
            # The stop request travels as the adapter's cancel_event: the process runner stops the tool's whole
            # process group on it. Without one (the cloud agent has its own) the call is as it was.
            extra = _stop_kwargs(stop, adapter.resume if resuming else adapter.start)
            if resuming:
                db.add_event(EV_TASK_RESUMED, tool=tool, payload={"task_id": task_id, "run_id": run_id,
                                                                  "session_id": session_id}, at=now)
                res = adapter.resume(RESUME_PROMPT % task["prompt"], wt, session_id, log_path, **extra)
            else:
                res = adapter.start(task["prompt"], wt, session_id, log_path, **extra)
    except DispatchDenied as exc:
        reason = str(exc)
        if reason == "cancelled" and stop is not None and stop.is_set():
            # The stop landed between the claim and the spawn: no tool process was created. Take the claim back.
            return _unstart(db, task, run_id, clock(), (wt, branch), stopped=True)
        if reason in _PREREQUISITE_DENIALS:
            # The explicit parent went away (or stopped being DONE) after the claim, before any tool process
            # existed. Nothing ran: take the claim back as if it had not been made.
            return _unstart(db, task, run_id, clock(), (wt, branch))
        db.finish_run(run_id, -1, blocked=False, summary="dispatch blocked: " + reason, now=clock())
        db.update_task(task_id, state=RUNNABLE, session_id=task.get("session_id"), last_error=reason, now=clock())
        return "task %d → blocked (%s)" % (task_id, reason)
    except ToolExitUnconfirmed as exc:
        try:
            _record_unconfirmed_stop(db, task_id, run_id, tool, session_id, stop, exc, max(now, clock()))
        except Exception as write_error:  # whatever happens to the record, the caller must still see THIS error
            log("could not record the unconfirmed stop of task %d: %s" % (task_id, write_error))
        raise
    except Exception as exc:
        res = RunResult(exit_code=-1, session_id=session_id, error="adapter crashed: %s" % exc)
    ended = max(now, clock())
    session_id = res.session_id or session_id
    _record_samples(db, res.samples, tool, adapter, ended, log)
    if stop is not None and getattr(stop, "tool_signalled", False) and not res.ok:
        # The tool was signalled to stop and did not complete its work. (One that completed anyway is `ok` and
        # falls through to done: a finished task is never turned back into an interrupted one.)
        return _record_interrupted_stop(db, task_id, run_id, tool, session_id, res, stop, ended)
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
        if task.get("on_success") and stop is not None and stop.is_set():
            db.add_event(EV_HOOK_REJECTED, tool=tool, payload={
                "task_id": task_id, "reason": "stop requested before the follow-up tasks were generated"}, at=ended)
        elif task.get("on_success"):
            fresh = db.get_task(task_id)
            hook_log = str(logs_dir / ("task%d-hook.log" % task_id))
            extra = _stop_kwargs(stop, hook_runner)
            # Follow-up generation also resumes the provider session. Keep the
            # same live guard at its Popen boundary; hooks record any denial.
            try:
                with spawn_authority(lambda: adapter_dispatch_problem(adapter, True)):
                    hook_runner(db, adapter, fresh, home, cfg, ended, hook_log, **extra)
            except ToolExitUnconfirmed as exc:  # the task itself is done; the hook's tool may still be running
                try:
                    db.add_event(EV_TASK_INTERRUPTED, tool=tool, payload={
                        "task_id": task_id, "run_id": run_id, "phase": "on_success hook", "exit_confirmed": False,
                        "error": str(exc)}, at=max(ended, clock()))
                except Exception as write_error:
                    log("could not record the unconfirmed stop of task %d's follow-up: %s" % (task_id, write_error))
                raise
        return "task %d → done" % task_id
    err = res.error or "exit %d" % res.exit_code
    db.finish_run(run_id, res.exit_code if res.exit_code else 1, blocked=False, summary=err[:500],
                  session_id=session_id, now=ended)
    db.update_task(task_id, state=FAILED, session_id=session_id, last_error=err, now=ended)
    db.add_event(EV_TASK_FAILED, tool=tool, payload={"task_id": task_id, "run_id": run_id,
                                                     "reason": err[:500]}, at=ended)
    return "task %d → failed: %s" % (task_id, err[:120])


def _stop_kwargs(stop: Optional[threading.Event], fn: Callable) -> Dict[str, Any]:
    """`cancel_event=stop` for a start/resume/hook that takes one (every adapter of this tree does: it is part of
    the ToolAdapter interface), nothing for one that does not or when there is no stop request: that call is
    exactly what it always was, and a tool started through it cannot be stopped by the request."""
    if stop is None:
        return {}
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return {}
    if any(p.name == "cancel_event" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return {"cancel_event": stop}
    return {}


# Why the spawn gate says no when it is the explicit parent (or the task itself) that has changed. Only these
# mean "no tool process was created and the claim can be taken back"; any other denial keeps its old handling.
_PREREQUISITE_DENIALS = ("dependency_missing", "dependencies_pending", "dependency_cycle", "task_removed")


def _claim_start(db: Database, task: Dict[str, Any], tool: str, session_id: str, wt: str, branch: str,
                 now: int, logs_dir: Path) -> Any:
    """Turn a RUNNABLE `task` into a RUNNING one with its run row — or, when its explicit parent is not DONE any
    more (removed, or not finished), leave it where `_withhold_until_prerequisite` puts it.

    This is the linearization point of a start. It is one `BEGIN IMMEDIATE` transaction: the parent and the task
    are read and the claim is written without another process's write in between, so `timetrace rm` of the parent
    either commits first (this sees it gone: nothing starts, no run row, no session on the row) or after (the task
    started when its parent was DONE, like any parent removed while a child runs). It holds the write lock for a
    few statements only, never while a tool runs, and never while the worktree is prepared (that is before it).

    Returns (run_id, log_path), or a `_Withheld` text for a task that did not start."""
    task_id = task["id"]
    with db.transaction():
        current = db.get_task(task_id)
        if current is None or current["state"] != RUNNABLE:
            return _Withheld("task %d → skipped (no longer runnable: %s)" % (
                task_id, current["state"] if current else "deleted"))
        held_back = _withhold_until_prerequisite(db, current, now, kept=(wt, branch))
        if held_back is not None:
            return _Withheld(held_back)
        db.update_task(task_id, state=RUNNING, tool=tool, session_id=session_id, worktree=wt,
                       branch=branch, now=now)
        run_id = db.add_run(task_id, tool, session_id, "", now=now)
        log_path = str(logs_dir / ("task%d-run%d.log" % (task_id, run_id)))
        db.conn.execute("UPDATE run SET log_path=? WHERE id=?", (log_path, run_id))
    return run_id, log_path


def _start_authority(db: Database, adapter: Any, resuming: bool, task_id: int) -> Callable[[], str]:
    """The live check carried to the OS boundary (`spawn_authority`): the adapter's own authority first, then the
    task's explicit parent, read from the database at that moment. It runs where the tool is about to be started,
    after the worktree, the claim and whatever the adapter prepares, so nothing decided earlier can stand in for
    it. Same thread as the adapter, so it uses the scheduler's connection. Empty text: allowed."""
    def check() -> str:
        return adapter_dispatch_problem(adapter, resuming) or _prerequisite_problem(db, task_id)
    return check


def _prerequisite_problem(db: Database, task_id: int) -> str:
    row = db.get_task(task_id)
    if row is None:
        return "task_removed"
    pre = prerequisites.check(db, row)
    if pre.ready:
        return ""
    if pre.status == prerequisites.MISSING:
        return "dependency_missing"
    return "dependencies_pending" if pre.status == prerequisites.WAITING else "dependency_cycle"


def _unstart(db: Database, task: Dict[str, Any], run_id: int, now: int, kept: Tuple[str, str],
             stopped: bool = False) -> "_Withheld":
    """The spawn gate refused for the explicit parent: no tool process was created. Take the claim back so the
    task is not recorded as one that ran — the run row made for it is deleted, and the row goes back to what it
    was before the claim (`task` is that row: no session, no worktree if it had none). The prepared worktree stays
    on disk and is named in the event. What happens to the task then is decided by the same rule as before the
    claim: failed if the parent is gone, waiting if it is not finished. No tool call is recorded. `stopped`: the
    refusal was the queue's stop request, not the parent: the task simply stays runnable, not started."""
    task_id = task["id"]
    with db.transaction():
        db.discard_unstarted_run(run_id)
        if not db.move_task(task_id, RUNNING, now=now, state=RUNNABLE, tool=task.get("tool"),
                            session_id=task.get("session_id"), worktree=task.get("worktree"),
                            branch=task.get("branch")):
            return _Withheld("task %d → skipped (changed meanwhile)" % task_id)
        if stopped:
            return _Withheld("task %d → not started (stop requested)" % task_id)
        held_back = _withhold_until_prerequisite(db, task, now, kept=kept)
    if held_back is None:  # the parent is DONE again by now: the task stays runnable and starts on the next pass
        return _Withheld("task %d → deferred (prerequisite changed while starting)" % task_id)
    return _Withheld(held_back)


def _record_interrupted_stop(db: Database, task_id: int, run_id: int, tool: str, session_id: str, res: RunResult,
                             stop: Any, at: int) -> str:
    """The tool was stopped on request, its process group is gone (the process runner confirmed it) and its work is
    not complete. The run gets its end, the task an explainable state and the event is written, in ONE short
    transaction. The task is `failed` with its session kept, not `runnable`: nothing here restarts it, and
    `timetrace retry` (a person's decision) resumes the original session. A stop is not a tool failure, so the run
    is marked `interrupted` and the circuit breaker does not count it."""
    why = stop.reason or "stop"
    summary = ("interrupted by %s; the tool's process group was stopped and its exit confirmed (exit %s); not "
               "restarted automatically: `timetrace retry %d` resumes session %s" % (why, res.exit_code, task_id,
                                                                                    session_id))
    with db.transaction():
        db.finish_run(run_id, res.exit_code, blocked=False, summary=summary[:500], session_id=session_id, now=at,
                      interrupted=True)
        db.move_task(task_id, RUNNING, now=at, state=FAILED, session_id=session_id,
                     last_error="interrupted (%s): tool stopped" % why)
        db.add_event(EV_TASK_INTERRUPTED, tool=tool, payload={
            "task_id": task_id, "run_id": run_id, "signal": why, "exit_code": res.exit_code,
            "exit_confirmed": True, "session_id": session_id}, at=at)
    return "task %d → interrupted (%s)" % (task_id, why)


def _record_unconfirmed_stop(db: Database, task_id: int, run_id: int, tool: str, session_id: str, stop: Any,
                             exc: Exception, at: int) -> None:
    """A stop was requested but the tool's exit could not be confirmed: it may still be running. Nothing here says it
    stopped. The run is closed with no exit code, the task is `failed` for a person to check (`待核对`), and the
    caller keeps the slot fenced (see `_dispatch`)."""
    why = getattr(stop, "reason", None) or "stop"
    summary = ("stop requested (%s) but the tool's exit is NOT confirmed (待核对): it may still be running. Its slot "
               "stays fenced: verify nothing is writing in the worktree, clear the slot's lock marker, then "
               "`timetrace retry %d` (%s)" % (why, task_id, exc))
    with db.transaction():
        db.finish_run(run_id, None, blocked=False, summary=summary[:500], session_id=session_id, now=at,
                      interrupted=True)
        db.move_task(task_id, RUNNING, now=at, state=FAILED, session_id=session_id,
                     last_error="stop requested (%s); tool exit NOT confirmed (待核对)" % why)
        db.add_event(EV_TASK_INTERRUPTED, tool=tool, payload={
            "task_id": task_id, "run_id": run_id, "signal": why, "exit_confirmed": False, "error": str(exc),
            "session_id": session_id}, at=at)


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
