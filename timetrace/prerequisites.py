"""What a queue task added with `--after N` is waiting for. Pure over a Database: it reads, it never writes.

The rule is one sentence: a task with an explicit parent starts only when that parent is DONE, read at the moment
it starts. Everything else here is naming the cases around it so the scheduler, `timetrace retry` and
`timetrace ls` all say the same thing:

  READY        no parent, or the parent is DONE.
  WAITING      the parent has not finished (queued, running, paused by a limit, waiting itself, or failed and
               still retryable by a person). The task has not run and nothing is wrong with it: it keeps its
               place and starts when the work before it has passed. `blocker` is the task whose work has to
               happen first: the parent, or the nearest ancestor up the chain that cannot move on by itself.
  MISSING      the parent, or an ancestor the chain depends on, no longer exists (`timetrace rm`). That can
               never be satisfied.
  UNAVAILABLE  the chain loops back on itself (only a hand-edited database has that).

What the task did itself is a different question, answered by `has_executed`: a task that really ran (a run row,
a session or a worktree) may have changed things, so nothing here ever starts it again by itself.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from timetrace.models import BLOCKED, FAILED, DONE, PENDING, RUNNABLE, RUNNING
from timetrace.render import local_time

READY = "ready"
WAITING = "waiting"
MISSING = "missing"
UNAVAILABLE = "unavailable"

WAITING_TEXT = "等待前置工作"

# The states from which the chain is followed further up: a pending task moves when its own parent is done, and
# a failed one needs a person's retry, which itself waits for ITS parent. The other states (queued, running,
# paused by a limit) move by themselves, so the wait is for them.
_STALLED = (PENDING, FAILED)


@dataclass(frozen=True)
class Prerequisite:
    status: str
    parent: Optional[int] = None
    blocker: Optional[int] = None
    blocker_state: Optional[str] = None
    blocked_until: Optional[int] = None
    missing: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self.status == READY

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "parent": self.parent, "blocker": self.blocker,
                "blocker_state": self.blocker_state, "note": describe(self)}


def check(db: Any, task: Dict[str, Any]) -> Prerequisite:
    """The task's prerequisite, from the rows as they are now."""
    parent = task.get("depends_on")
    if parent is None:
        return Prerequisite(READY)
    seen = {task["id"]}
    chain = []
    cur = parent
    while True:
        if cur in seen:
            return Prerequisite(UNAVAILABLE, parent=parent)
        seen.add(cur)
        row = db.get_task(cur)
        if row is None:
            return Prerequisite(MISSING, parent=parent, missing=cur)
        if row["state"] == DONE:
            if not chain:
                return Prerequisite(READY, parent=parent)
            break
        chain.append(row)
        cur = row.get("depends_on")
        if cur is None or row["state"] not in _STALLED:
            break
    top = chain[-1]
    return Prerequisite(WAITING, parent=parent, blocker=top["id"], blocker_state=top["state"],
                        blocked_until=top.get("blocked_until"))


def has_executed(db: Any, task: Dict[str, Any]) -> bool:
    """Whether a tool was ever started for the task. A queue task that only waited, or that failed before its
    tool started (the worktree could not be made), has not."""
    return bool(task.get("session_id") or task.get("worktree") or db.task_has_run(task["id"]))


def describe(pre: Prerequisite) -> str:
    """One line for a person: what the task waits for and, when someone has to act, what to do."""
    if pre.status == READY:
        return "前置工作 #%d 已完成，即将启动" % pre.parent if pre.parent is not None else "没有前置工作"
    if pre.status == MISSING:
        return "前置工作 #%d 已不存在" % pre.missing
    if pre.status == UNAVAILABLE:
        return "前置工作互相依赖，永远无法完成"
    state = pre.blocker_state
    if state == FAILED:
        what = "已失败：先运行 timetrace retry %d" % pre.blocker
    elif state == BLOCKED:
        what = "因额度暂停" + ("，约 %s 恢复" % local_time(pre.blocked_until) if pre.blocked_until else "")
    elif state == RUNNING:
        what = "运行中"
    elif state == RUNNABLE:
        what = "排队中"
    else:
        what = "即将启动"
    via = "" if pre.blocker == pre.parent else "上游 #%d " % pre.blocker
    return "%s #%d（%s%s）" % (WAITING_TEXT, pre.parent, via, what)
