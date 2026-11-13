"""The local queue's `--after` dependency: a task that has not run yet WAITS for its explicit parent's work.

Before this fix a parent that failed (after its worktree and session were kept) made the scheduler mark every
unstarted child FAILED ("dependency N failed"). `timetrace retry <parent>` then left the children failed, and
`timetrace retry <child>` made the child RUNNABLE with the parent still unfinished, so it ran first. The
children had to be rebuilt by hand.

These tests run the real CLI against a real SQLite database with a scripted executor that does nothing. They
pin: a child never starts unless its explicit parent is DONE at the moment it starts (under the slot, not from a
candidate list read earlier); an unstarted child whose parent can still be retried waits and keeps its place;
what a child did itself (it failed, it really ran) or a missing parent is kept apart from plain waiting and
never re-runs by itself; and a withheld child never locks the queue behind it.
"""
import unittest
from unittest.mock import patch

from timetrace import scheduler
from timetrace.models import BLOCKED, DONE, FAILED, PENDING, RUNNABLE, RUNNING, RunResult

from tests.queue_fixture import CFG, FixedExecutor, QueueCase

WAITING = "等待前置工作"


class ReferenceScenarioTest(QueueCase):
    """The scenario measured on the installed CLI: parent, a child --after it, an independent task."""

    def test_a_failed_parent_keeps_its_unstarted_child_waiting_and_retry_resumes_the_same_session(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        independent = self.add("independent")
        self.assertEqual(self.states(parent, child, independent), [RUNNABLE, PENDING, RUNNABLE])
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom"), RunResult(ok=True)]})

        self.assertIn("failed", self.tick(ex))                       # the parent runs and fails
        failed = self.db.get_task(parent)
        self.assertEqual(self.states(parent, child, independent), [FAILED, PENDING, RUNNABLE])
        self.assertTrue(failed["session_id"] and failed["worktree"])  # the session and worktree are kept
        self.assertIn("done", self.tick(ex))                         # independent work still runs
        self.assertEqual(self.states(parent, child, independent), [FAILED, PENDING, DONE])
        self.assertEqual(self.tick(ex), "idle")                      # the child is not started, not failed
        self.assertEqual(self.states(child), [PENDING])

        code, out, err = self.cli("retry", str(parent))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(parent, child, independent), [RUNNABLE, PENDING, DONE])  # child not lost

        self.assertIn("done", self.tick(ex))                         # the parent resumes...
        resumed = ex.calls[-1]
        self.assertEqual((resumed.kind, resumed.label), ("resume", "parent"))
        self.assertEqual(resumed.session_id, failed["session_id"])   # ...in the same session
        self.assertEqual(resumed.cwd, failed["worktree"])            # ...and the same worktree
        self.assertEqual(self.states(parent, child), [DONE, PENDING])
        self.assertIn("done", self.tick(ex))                         # only now does the child start
        self.assertEqual(self.states(parent, child, independent), [DONE, DONE, DONE])
        self.assertEqual(ex.labels(), ["parent", "independent", "parent", "child"])  # each exactly when due
        self.assertIn((child, "timetrace/%d" % parent), self.worktrees.bases)         # on top of the parent's work
        self.assertEqual(len(self.db.list_tasks(include_done=True)), 3)               # nothing duplicated

    def test_a_parent_paused_by_a_usage_limit_keeps_its_child_waiting_until_it_has_passed(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        reset = self.clock + 100
        ex = FixedExecutor({"parent": [RunResult(blocked=True, error="limit", reset_at=reset), RunResult(ok=True)]})
        self.assertIn("blocked", self.tick(ex))
        self.assertEqual(self.states(parent, child), [BLOCKED, PENDING])
        code, out, err = self.cli("ls")
        self.assertIn(WAITING, out)
        self.assertIn("因额度暂停", out)
        self.assertEqual(self.tick(ex), "idle")                      # still paused: nothing runs, nothing fails
        self.assertEqual(self.states(parent, child), [BLOCKED, PENDING])
        self.clock = reset + 5
        self.assertIn("done", self.tick(ex))                         # the limit is over: the parent resumes first
        self.assertEqual(self.states(parent, child), [DONE, PENDING])
        self.assertIn("done", self.tick(ex))
        self.assertEqual(ex.labels(), ["parent", "parent", "child"])
        self.assertEqual([c.kind for c in ex.calls], ["start", "resume", "start"])

    def test_retrying_the_child_does_not_jump_ahead_of_its_failed_parent(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom"), RunResult(ok=True)]})
        self.tick(ex)
        self.assertEqual(self.states(parent, child), [FAILED, PENDING])

        code, out, err = self.cli("retry", str(child))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(child), [PENDING])              # not runnable: the parent is not done
        self.assertIn(WAITING, out)
        self.assertIn("#%d" % parent, out)
        self.assertIn("timetrace retry %d" % parent, out)            # tells what to do about it
        self.assertEqual(self.tick(ex), "idle")
        self.assertEqual(ex.labels(), ["parent"])                    # the child did not run

        self.cli("retry", str(parent))
        self.tick(ex)
        self.tick(ex)
        self.assertEqual(ex.labels(), ["parent", "parent", "child"])
        self.assertEqual(self.states(parent, child), [DONE, DONE])

    def test_retry_on_a_child_that_already_waits_is_accepted_changes_nothing_and_says_what_it_waits_for(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        for _ in range(2):                                           # asking again is just as harmless
            code, out, err = self.cli("retry", str(child))
            self.assertEqual(code, 0, err)
            self.assertEqual(self.states(parent, child), [RUNNABLE, PENDING])
            self.assertIn(WAITING, out)
            self.assertIn("#%d" % parent, out)
        self.assertEqual(self.events("task_waiting"), [])            # no event for a task that was already waiting

    def test_retry_on_a_waiting_child_whose_parent_is_done_makes_it_runnable(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        self.db.update_task(parent, state=DONE)
        code, out, err = self.cli("retry", str(child))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(child), [RUNNABLE])


class StartCriticalSectionTest(QueueCase):
    """The parent is confirmed DONE when the task STARTS (under the slot), from a fresh read."""

    def setUp(self):
        super().setUp()
        self.parent = self.db.add_task("parent", "/repo", tool="claude")
        self.child = self.db.add_task("child", "/repo", tool="claude", depends_on=self.parent)

    def dispatch(self, ex, snapshot):
        return scheduler._dispatch(self.db, ex, "claude", snapshot, CFG, self.home, 100, lambda s: None,
                                   self.worktrees, lambda *a: None, lambda: 0.0, lambda: 100)

    def legacy_runnable(self):
        """What an older `timetrace retry <child>` (or an older scheduler) writes: RUNNABLE, nothing checked."""
        self.db.update_task(self.child, state=RUNNABLE, blocked_until=None)
        return self.db.get_task(self.child)

    def test_a_runnable_row_read_before_the_slot_does_not_start_while_the_parent_is_failed(self):
        self.db.update_task(self.parent, state=FAILED, session_id="s1", last_error="boom")
        stale = self.legacy_runnable()
        ex = FixedExecutor()
        out = self.dispatch(ex, stale)
        self.assertEqual(ex.calls, [])
        self.assertIn(WAITING, out)
        self.assertEqual(self.states(self.child), [PENDING])         # back to waiting, not failed, not lost
        self.assertEqual(len(self.events("task_waiting")), 1)

    def test_every_parent_state_that_is_not_done_withholds_the_child(self):
        for state in (RUNNABLE, RUNNING, BLOCKED, FAILED, PENDING):
            with self.subTest(parent=state):
                self.db.update_task(self.parent, state=state)
                ex = FixedExecutor()
                self.dispatch(ex, self.legacy_runnable())
                self.assertEqual(ex.calls, [])
                self.assertEqual(self.states(self.child), [PENDING])

    def test_the_parent_is_read_again_under_the_slot_not_taken_from_the_candidate_check(self):
        # The candidate check saw the parent DONE; before the start the parent is no longer done (the row was
        # changed in between). The start decides from the row it reads now.
        self.db.update_task(self.parent, state=DONE)
        real = scheduler.acquire_job_slots

        def acquire_then_parent_changes(home, tool, slots):
            held = real(home, tool, slots)
            self.db.update_task(self.parent, state=FAILED)
            return held

        ex = FixedExecutor()
        with patch.object(scheduler, "acquire_job_slots", acquire_then_parent_changes):
            out = self.dispatch(ex, self.legacy_runnable())
        self.assertEqual(ex.calls, [])
        self.assertIn(WAITING, out)
        self.assertEqual(self.states(self.child), [PENDING])

    def test_a_done_parent_lets_the_child_start(self):
        self.db.update_task(self.parent, state=DONE)
        ex = FixedExecutor()
        self.assertIn("done", self.dispatch(ex, self.legacy_runnable()))
        self.assertEqual(ex.labels(), ["child"])

    def test_a_missing_parent_fails_the_child_for_that_reason_only(self):
        self.db.delete_task(self.parent)
        ex = FixedExecutor()
        self.dispatch(ex, self.legacy_runnable())
        child = self.db.get_task(self.child)
        self.assertEqual(ex.calls, [])
        self.assertEqual(child["state"], FAILED)
        self.assertIn("dependency missing", child["last_error"])

    def test_a_child_that_already_ran_is_never_put_back_to_wait_and_rerun_by_itself(self):
        # It really ran once (a run row, a session, a worktree): whoever wrote RUNNABLE did it before the
        # parent finished. It is not run now, and it does not come back by itself either: a person decides.
        self.db.update_task(self.parent, state=FAILED)
        self.db.update_task(self.child, session_id="s2", worktree="/wt/child", branch="timetrace/%d" % self.child)
        self.db.finish_run(self.db.add_run(self.child, "claude", "s2", "/log", now=50), 1, False, "x", now=60)
        ex = FixedExecutor()
        out = self.dispatch(ex, self.legacy_runnable())
        child = self.db.get_task(self.child)
        self.assertEqual(ex.calls, [])
        self.assertEqual(child["state"], FAILED)
        self.assertEqual((child["session_id"], child["worktree"]), ("s2", "/wt/child"))  # evidence kept
        self.assertIn("#%d" % self.parent, child["last_error"])
        self.db.update_task(self.parent, state=DONE)
        self.tick(ex)
        self.assertEqual(ex.calls, [])                               # the parent finishing does not rerun it
        self.assertEqual(self.states(self.child), [FAILED])


class QueueDoesNotLockTest(QueueCase):
    def test_a_withheld_child_ahead_of_the_queue_does_not_stop_independent_work_in_the_same_iteration(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent), "--priority", "9")
        orphan = self.add("orphan", "--after", str(parent), "--priority", "8")
        independent = self.add("independent")
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom")]})
        self.tick(ex)                                                # parent fails
        # older writers: the child retried by an older CLI, the orphan's parent removed under it
        self.db.update_task(child, state=RUNNABLE, blocked_until=None)
        self.db.update_task(orphan, depends_on=999, state=RUNNABLE)
        self.assertEqual(self.states(child, orphan, independent), [RUNNABLE, RUNNABLE, RUNNABLE])

        out = self.tick(ex)                                          # one iteration: both withheld, independent runs
        self.assertEqual(ex.labels(), ["parent", "independent"])
        self.assertIn("independent", [t["prompt"] for t in self.db.list_tasks(include_done=True) if t["state"] == DONE])
        self.assertEqual(self.states(child, orphan), [PENDING, FAILED])
        self.assertEqual(self.db.get_task(orphan)["last_error"], "dependency missing")
        self.assertIn("done", out)

    def test_only_withheld_work_in_the_queue_is_reported_and_then_settles_to_idle(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom")]})
        self.tick(ex)
        self.db.update_task(child, state=RUNNABLE, blocked_until=None)
        self.assertIn(WAITING, self.tick(ex))                        # said once, in words
        self.assertEqual(self.tick(ex), "idle")                      # and the queue is not stuck on it
        self.assertEqual(ex.labels(), ["parent"])

    def test_a_withheld_child_costs_no_slot_and_no_tool_switch(self):
        # A task that cannot start is settled before a tool is chosen: it must not make the queue report the
        # runner as busy, and a task that may use any tool must not be logged as switched to one it never ran on.
        from timetrace import limits
        from timetrace.dispatch import coding_slot_lock
        from timetrace.models import Sample

        parent = self.add("parent")
        child = self.add("child", "--after", str(parent), "--any-tool", "--priority", "9")
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom")]})
        self.tick(ex)
        limits.record_samples(self.db, [Sample("claude:five_hour", "claude", 100)], at=self.clock)  # claude is out
        self.db.update_task(child, state=RUNNABLE, blocked_until=None, tool="claude")
        held = coding_slot_lock(self.home).acquire()                  # the slot is taken by someone else
        try:
            out = self.tick(ex, {"claude": ex, "codex": FixedExecutor()})
        finally:
            held.release()
        self.assertIn(WAITING, out)
        self.assertNotIn("busy", out)
        self.assertEqual(self.states(child), [PENDING])
        self.assertEqual(self.events("tool_switched"), [])

    def test_waiting_children_do_not_count_as_failures_or_trip_the_breaker(self):
        parent = self.add("parent")
        for n in range(5):
            self.add("child%d" % n, "--after", str(parent))
        independent = self.add("independent")
        ex = FixedExecutor({"parent": [RunResult(exit_code=1, error="boom")]})
        for _ in range(8):
            self.tick(ex)
        self.assertEqual(self.db.failed_runs_since(0), 1)            # only the parent's own failed run
        self.assertEqual(self.states(independent), [DONE])
        self.assertEqual(self.events("circuit_open"), [])
        self.assertEqual(self.db.list_events(limit=50, type_="task_failed")[0]["payload"]["task_id"], parent)

    def test_a_dependency_cycle_fails_for_that_reason_and_does_not_stop_the_other_dependencies(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        loop = self.add("loop-a")
        other = self.add("loop-b", "--after", str(loop))
        self.db.update_task(loop, state=PENDING, depends_on=other)    # a cycle only a hand-edited database has
        ex = FixedExecutor()
        for _ in range(4):
            self.tick(ex)
        self.assertEqual(self.states(parent, child), [DONE, DONE])
        self.assertEqual(ex.labels(), ["parent", "child"])
        self.assertEqual(self.states(loop, other), [FAILED, FAILED])
        self.assertEqual(self.db.get_task(loop)["last_error"], "dependency cycle")

    def test_a_check_that_raises_for_one_row_is_recorded_and_the_rest_still_advance(self):
        from timetrace import prerequisites
        parent = self.add("parent")
        bad = self.add("bad", "--after", str(parent))
        good = self.add("good", "--after", str(parent))
        self.db.update_task(parent, state=DONE)
        real = prerequisites.check

        def check(db, task):
            if task["id"] == bad:
                raise RuntimeError("unreadable row")
            return real(db, task)

        with patch.object(prerequisites, "check", check):
            scheduler._advance_dependencies(self.db, 50)
        self.assertEqual(self.states(bad, good), [PENDING, RUNNABLE])
        errors = self.events("loop_error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["payload"]["task_id"], bad)


class DeepChainTest(QueueCase):
    def test_a_chain_waits_as_one_piece_and_runs_each_task_once_in_order_after_the_root_is_retried(self):
        a = self.add("a")
        b = self.add("b", "--after", str(a))
        c = self.add("c", "--after", str(b))
        d = self.add("d")
        ex = FixedExecutor({"a": [RunResult(exit_code=1, error="boom"), RunResult(ok=True)]})
        self.tick(ex)
        self.tick(ex)
        self.assertEqual(self.states(a, b, c, d), [FAILED, PENDING, PENDING, DONE])
        self.tick(ex)
        self.assertEqual(self.states(a, b, c, d), [FAILED, PENDING, PENDING, DONE])

        code, out, _ = self.cli("ls")
        lines = {int(line.split()[0]): line for line in out.splitlines()[2:]}
        self.assertIn(WAITING, lines[b])
        self.assertIn(WAITING, lines[c])
        self.assertIn("timetrace retry %d" % a, lines[c])            # the deep task points at the ROOT to retry

        self.cli("retry", str(a))
        for _ in range(5):
            self.tick(ex)
        self.assertEqual(ex.labels(), ["a", "d", "a", "b", "c"])
        self.assertEqual(self.states(a, b, c, d), [DONE] * 4)
        self.assertEqual([t["id"] for t in self.db.list_tasks(include_done=True)], [a, b, c, d])

    def test_removing_the_root_fails_the_whole_chain_as_missing_not_as_waiting_forever(self):
        a = self.add("a")
        b = self.add("b", "--after", str(a))
        c = self.add("c", "--after", str(b))
        self.cli("rm", str(a))
        ex = FixedExecutor()
        self.tick(ex)
        self.assertEqual(self.states(b, c), [FAILED, FAILED])
        self.assertIn("dependency", self.db.get_task(b)["last_error"])
        self.assertIn("missing", self.db.get_task(c)["last_error"])


class WhatHappensToSiblingsAndOwnFailuresTest(QueueCase):
    def test_a_retry_runs_only_the_retried_task_and_its_waiting_descendants_not_finished_siblings(self):
        p = self.add("p")
        s1 = self.add("s1", "--after", str(p))
        s2 = self.add("s2", "--after", str(p))
        s3 = self.add("s3", "--after", str(s2))
        ex = FixedExecutor({"s2": [RunResult(exit_code=1, error="boom"), RunResult(ok=True)]})
        for _ in range(4):
            self.tick(ex)
        self.assertEqual(self.states(p, s1, s2, s3), [DONE, DONE, FAILED, PENDING])
        self.assertEqual(ex.labels(), ["p", "s1", "s2"])
        self.cli("retry", str(s2))
        for _ in range(3):
            self.tick(ex)
        self.assertEqual(ex.labels(), ["p", "s1", "s2", "s2", "s3"])  # p and s1 were not run again
        self.assertEqual(self.states(p, s1, s2, s3), [DONE] * 4)

    def test_a_child_that_failed_on_its_own_stays_failed_when_the_parent_is_retried_and_passes(self):
        p = self.add("p")
        k = self.add("k", "--after", str(p))
        w = self.add("w", "--after", str(p))
        # k really ran once (after an older version let it past the parent) and failed by itself
        self.db.update_task(p, state=FAILED, session_id="sp", worktree="/wt/p", branch="timetrace/p")
        self.db.update_task(k, state=FAILED, session_id="sk", worktree="/wt/k", last_error="its own error")
        self.db.finish_run(self.db.add_run(k, "claude", "sk", "/log", now=10), 1, False, "its own error", now=20)
        ex = FixedExecutor()
        self.cli("retry", str(p))
        for _ in range(4):
            self.tick(ex)
        self.assertEqual(ex.labels(), ["p", "w"])                    # k was not started by the parent passing
        self.assertEqual(self.states(p, k, w), [DONE, FAILED, DONE])
        self.assertEqual(self.db.get_task(k)["last_error"], "its own error")

    def test_dependency_failures_an_older_version_wrote_come_back_to_waiting_when_nothing_ever_ran(self):
        p = self.add("p")
        unrun = self.add("unrun", "--after", str(p))
        ran = self.add("ran", "--after", str(p))
        mine = self.add("mine", "--after", str(p))
        self.db.update_task(p, state=FAILED, session_id="sp", worktree="/wt/p")
        self.db.update_task(unrun, state=FAILED, last_error="dependency %d failed" % p)      # exactly what it wrote
        self.db.update_task(ran, state=FAILED, last_error="dependency %d failed" % p, session_id="sr")
        self.db.finish_run(self.db.add_run(ran, "claude", "sr", "/log", now=10), 1, False, "x", now=20)
        self.db.update_task(mine, state=FAILED, last_error="a failure of its own")
        ex = FixedExecutor()
        self.tick(ex)
        self.assertEqual(self.states(unrun, ran, mine), [PENDING, FAILED, FAILED])
        self.assertIsNone(self.db.get_task(unrun)["last_error"])
        self.cli("retry", str(p))
        for _ in range(3):
            self.tick(ex)
        self.assertEqual(ex.labels(), ["p", "unrun"])
        self.assertEqual(self.states(p, unrun, ran, mine), [DONE, DONE, FAILED, FAILED])


class RetryRefusalTest(QueueCase):
    def test_retry_of_a_child_that_already_ran_before_its_parent_is_done_is_refused_and_changes_nothing(self):
        p = self.add("p")
        k = self.add("k", "--after", str(p))
        self.db.update_task(p, state=FAILED)
        self.db.update_task(k, state=FAILED, session_id="sk", worktree="/wt/k", last_error="its own error")
        self.db.finish_run(self.db.add_run(k, "claude", "sk", "/log", now=10), 1, False, "x", now=20)
        code, out, err = self.cli("retry", str(k))
        self.assertEqual(code, 1)
        self.assertIn("#%d" % p, err)
        self.assertEqual(self.states(k), [FAILED])
        self.assertEqual(self.db.get_task(k)["last_error"], "its own error")

    def test_retry_of_a_task_whose_parent_was_removed_is_refused(self):
        p = self.add("p")
        k = self.add("k", "--after", str(p))
        self.db.update_task(k, state=FAILED, last_error="x")
        self.cli("rm", str(p))
        code, out, err = self.cli("retry", str(k))
        self.assertEqual(code, 1)
        self.assertIn("#%d" % p, err)
        self.assertEqual(self.states(k), [FAILED])

    def test_retry_of_a_child_whose_parent_is_done_is_runnable_as_before(self):
        p = self.add("p")
        k = self.add("k", "--after", str(p))
        self.db.update_task(p, state=DONE)
        self.db.update_task(k, state=FAILED, last_error="x")
        code, out, err = self.cli("retry", str(k))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(k), [RUNNABLE])
        self.assertIn("runnable", out)

    def test_retry_of_a_task_with_no_parent_is_unchanged(self):
        t = self.add("t")
        self.db.update_task(t, state=FAILED, session_id="s", worktree="/wt", last_error="x")
        code, out, _ = self.cli("retry", str(t), "--fresh")
        self.assertEqual(code, 0)
        self.assertIn("runnable", out)
        self.assertEqual((self.states(t), self.db.get_task(t)["session_id"]), ([RUNNABLE], None))
        self.assertEqual(self.db.get_task(t)["worktree"], "/wt")


class DisplayTest(QueueCase):
    def test_ls_says_what_each_waiting_task_is_waiting_for(self):
        done_parent = self.add("done parent")
        waiting_on_done = self.add("w-done", "--after", str(done_parent))
        failed = self.add("failed parent")
        waits_failed = self.add("w-failed", "--after", str(failed))
        running = self.add("running parent")
        waits_running = self.add("w-running", "--after", str(running))
        paused = self.add("paused parent")
        waits_paused = self.add("w-paused", "--after", str(paused))
        self.db.update_task(done_parent, state=DONE)
        self.db.update_task(failed, state=FAILED, last_error="boom")
        self.db.update_task(running, state=RUNNING)
        self.db.update_task(paused, state=BLOCKED, blocked_until=2000000000)

        tasks = self.ls_json()
        self.assertEqual(tasks[waits_failed]["prerequisite"]["blocker"], failed)
        self.assertEqual(tasks[waits_failed]["prerequisite"]["blocker_state"], FAILED)
        self.assertIsNone(tasks[failed].get("prerequisite"))
        code, out, _ = self.cli("ls")
        lines = {int(line.split()[0]): line for line in out.splitlines()[2:]}
        self.assertIn(WAITING, lines[waits_failed])
        self.assertIn("timetrace retry %d" % failed, lines[waits_failed])
        self.assertIn(WAITING, lines[waits_running])
        self.assertIn("#%d" % running, lines[waits_running])
        self.assertIn(WAITING, lines[waits_paused])
        self.assertIn("#%d" % paused, lines[waits_paused])
        for line in lines.values():
            self.assertNotIn("dependency", line)                     # no 'failed' wording for work that never ran

    def test_add_after_a_failed_parent_says_it_waits(self):
        p = self.add("p")
        self.db.update_task(p, state=FAILED)
        code, out, err = self.cli("add", "--repo", str(self.repo), "--after", str(p), "next")
        self.assertEqual(code, 0, err)
        self.assertIn("task 2 added (pending)", out)
        self.assertIn(WAITING, out)


if __name__ == "__main__":
    unittest.main()
