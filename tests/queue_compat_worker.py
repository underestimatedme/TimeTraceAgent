"""A real queue scheduler process for the dependency compatibility tests (tests/test_prerequisite_compat.py).

    queue_compat_worker.py HOME ROLE [--no-guard] [--hold LABEL]

ROLE `new`     is this tree's scheduler.
     `legacy`  is the scheduler as it was before this change (commit c1b577b, the version being upgraded from):
               its `_advance_dependencies`, the check at the start of its `_dispatch` and its `_dispatch_locked`
               (the start itself: worktree, claim, spawn gate), copied here unchanged. `_dispatch_locked` is
               copied because this tree's version now re-reads the parent after the worktree is ready and at the
               spawn gate, which an older process does not do. Everything else it did is code this change leaves
               as it was, so it is called from the tree.

Both take the same process locks the real `timetrace run` takes (`cli._SchedulerGuard`: scheduler.lock, then
agent.lock), loop `run_once` until the queue is idle and print one line per iteration. The "tool" is a scripted
executor that touches nothing but HOME/exec.log: one JSON line per call, appended in a single write. Its answers
come from HOME/script.json ({label: [exit code, ...]}, one entry per call of that label; no entry means success).
`--hold LABEL` makes that task wait inside the executor until HOME/release exists, to keep a slot held.
`--no-guard` skips the process locks (to race several workers on the slot and single-run checks alone).
`--ticks N` bounds the iterations (default 400).
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timetrace import cli, limits, scheduler  # noqa: E402
from timetrace.db import Database  # noqa: E402
from timetrace.dispatch import (DispatchDenied, LockBusy, SlotLimits, ToolSlotBusy, acquire_job_slots,  # noqa: E402
                                adapter_dispatch_problem, enforce_spawn_authority, spawn_authority)
from timetrace.models import (BLOCKED, DONE, EV_TASK_BLOCKED, EV_TASK_DONE, EV_TASK_FAILED, EV_TASK_RESUMED,  # noqa: E402
                              FAILED, PENDING, RUNNABLE, RUNNING, RunResult)

CFG = {"interval_sec": 1, "jitter_sec": 0, "default_block_sleep_sec": 600, "circuit_breaker_failures": 99,
       "circuit_window_mins": 300, "hook_max_tasks": 5}


# ---- the scheduler of c1b577b, unchanged -----------------------------------------------------------------
def legacy_advance_dependencies(db, now):
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


def legacy_dispatch(db, adapter, tool, task, cfg, home, now, log, ensure_worktree, hook_runner, rng, clock,
                    slots=None):
    try:
        held = acquire_job_slots(home, tool, slots)
    except ToolSlotBusy as exc:
        return "task %d → deferred (tool busy: %s)" % (task["id"], exc.tool)
    except LockBusy:
        return "task %d → deferred (runner busy)" % task["id"]
    try:
        # `task` was read before the slot was ours. Under the slot, only a task that is still runnable runs.
        current = db.get_task(task["id"])
        if current is None or current["state"] != RUNNABLE:
            return "task %d → skipped (no longer runnable: %s)" % (task["id"], current["state"] if current else "deleted")
        task = current
        return legacy_dispatch_locked(db, adapter, tool, task, cfg, home, now, log,
                                      ensure_worktree, hook_runner, rng, clock)
    finally:
        for lock in reversed(held):
            lock.release()


def legacy_dispatch_locked(db, adapter, tool, task, cfg, home, now, log, ensure_worktree, hook_runner, rng, clock):
    """`_dispatch_locked` of c1b577b (identical in 646c8df), unchanged: it never looks at the parent."""
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
                res = adapter.resume(scheduler.RESUME_PROMPT % task["prompt"], wt, session_id, log_path)
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
    scheduler._record_samples(db, res.samples, tool, adapter, ended, log)
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


def legacy_run_once(db, adapters, cfg, home, now, ensure_worktree, slots):
    legacy_advance_dependencies(db, now)
    rows = limits.snapshot(db)
    for task in db.tasks_in_state(RUNNABLE):
        tool = scheduler.pick_tool(task, rows, adapters, db, now)
        if tool is None:
            continue
        return legacy_dispatch(db, adapters[tool], tool, task, cfg, home, now, lambda s: None, ensure_worktree,
                               lambda *a: None, lambda: 0.0, lambda: int(time.time()), slots)
    return "idle"


# ---- the "tool" -------------------------------------------------------------------------------------------
class LoggedExecutor:
    name = "claude"

    def __init__(self, home, hold):
        self.home, self.hold = Path(home), hold

    def read_limits(self):
        return None

    def capabilities(self):
        return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

    def start(self, prompt, cwd, session_id, log_file):
        return self._run("start", prompt, cwd, session_id)

    def resume(self, prompt, cwd, session_id, log_file):
        return self._run("resume", prompt, cwd, session_id)

    def _log(self, **line):
        line.update(pid=os.getpid(), at=time.time())
        fd = os.open(str(self.home / "exec.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(line, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)

    def _calls_of(self, label):
        path = self.home / "exec.log"
        if not path.exists():
            return 0
        return sum(1 for ln in path.read_text().splitlines() if json.loads(ln).get("event") == "start"
                   and json.loads(ln).get("label") == label)

    def _run(self, kind, prompt, cwd, session_id):
        label = prompt.rsplit("\n", 1)[-1]
        script = json.loads((self.home / "script.json").read_text()) if (self.home / "script.json").exists() else {}
        nth = self._calls_of(label)
        self._log(event="start", label=label, kind=kind, session=session_id, cwd=cwd)
        if label == self.hold:
            while not (self.home / "release").exists():
                time.sleep(0.01)
        answers = script.get(label, [])
        code = answers[nth] if nth < len(answers) else 0
        self._log(event="end", label=label, exit_code=code)
        return RunResult(ok=code == 0, exit_code=code, session_id=session_id, error=None if code == 0 else "scripted failure")


def fake_worktree(repo, task_id, home, base="HEAD"):
    path = Path(home) / "worktrees" / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return str(path), "timetrace/%d" % task_id


def main(argv):
    home, role = Path(argv[1]), argv[2]
    guarded = "--no-guard" not in argv
    hold = argv[argv.index("--hold") + 1] if "--hold" in argv else None
    ticks = int(argv[argv.index("--ticks") + 1]) if "--ticks" in argv else 400
    guard = None
    slots = None
    if guarded:
        guard = cli._SchedulerGuard(home)
        err = guard.acquire()
        if err:
            print(err, file=sys.stderr, flush=True)
            return 1
        slots = SlotLimits(overall=1, per_tool={}) if guard.mode == "beside-agent" else None
    print("ready %s" % role, flush=True)
    db = Database(home / "timetrace.db")
    adapters = {"claude": LoggedExecutor(home, hold)}
    try:
        idle = 0
        while idle < 2 and ticks > 0:
            ticks -= 1
            now = int(time.time())
            if role == "legacy":
                out = legacy_run_once(db, adapters, CFG, home, now, fake_worktree, slots)
            else:
                out = scheduler.run_once(db, adapters, CFG, home, now=now, log=lambda s: None,
                                         ensure_worktree=fake_worktree, hook_runner=lambda *a: None,
                                         rng=lambda: 0.0, clock=lambda: int(time.time()), slots=slots)
            print(out, flush=True)
            idle = idle + 1 if out == "idle" else 0
            if out.endswith("(runner busy)") or "tool busy" in out:
                time.sleep(0.02)
        return 0
    finally:
        db.close()
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
