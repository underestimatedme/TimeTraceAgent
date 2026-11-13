"""The explicit parent of a queue task is checked again where the task is launched, not only before the worktree.

A child's parent is confirmed DONE under the execution slot, and then the worktree is prepared. That can take
long enough for another process to run `timetrace rm` on the finished parent. The start used to go on and call
the tool. These tests run the REAL `timetrace rm` (a separate process on the same SQLite file) at two moments
and pin what must follow:

  * while the worktree is being prepared (the scheduler holds its slot but no database write lock);
  * at the adapter's spawn boundary (the check `run_streaming` makes right before it creates the process).

With the parent gone the tool is never called; the child never ran, so nothing says it did (no run row, no
session, no worktree on the row) and the prepared worktree is kept on disk and named in the event; independent
work goes on. No model, no network, no real queue: a scripted executor and a temporary TIMETRACE_HOME.
"""
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from timetrace import scheduler
from timetrace.dispatch import enforce_spawn_authority
from timetrace.models import DONE, FAILED, PENDING, RUNNABLE, RunResult
from timetrace.process import run_streaming

from tests.queue_fixture import CFG, FixedExecutor, QueueCase, Worktrees

ROOT = Path(__file__).resolve().parents[1]


class RemoveDuring(Worktrees):
    """`ensure_worktree` that, once the directory exists, lets `action` happen (another process acts meanwhile)."""

    def __init__(self, task_id, action):
        super().__init__()
        self.task_id, self.action = task_id, action

    def __call__(self, repo, task_id, home, base="HEAD"):
        result = super().__call__(repo, task_id, home, base)
        if task_id == self.task_id:
            self.action()
        return result


class LaunchGateCase(QueueCase):
    def setUp(self):
        super().setUp()
        self.parent = self.add("parent")
        self.child = self.add("child", "--after", str(self.parent), "--priority", "20")
        self.independent = self.add("independent", "--priority", "-5")
        # The parent finished earlier; the child was released the way the scheduler releases it.
        self.db.update_task(self.parent, state=DONE, session_id="parent-session", worktree="/wt/parent",
                            branch="timetrace/%d" % self.parent)
        self.db.update_task(self.child, state=RUNNABLE)

    def real_rm(self, task_id):
        """`timetrace rm` as a person runs it: its own process, its own connection, the same database file."""
        env = dict(os.environ, TIMETRACE_HOME=str(self.home), PYTHONPATH=str(ROOT))
        done = subprocess.run([sys.executable, "-m", "timetrace", "rm", str(task_id)], env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("removed", done.stdout)

    def kept_worktree(self):
        return self.home / "worktrees" / str(self.child)

    def assert_child_never_ran(self, executor):
        child = self.db.get_task(self.child)
        self.assertNotIn("child", executor.labels())                    # the tool was never called for it
        self.assertFalse(self.db.task_has_run(self.child))              # no run row: nothing is recorded as a call
        self.assertIsNone(child["session_id"])                          # no session was ever handed out
        self.assertIsNone(child["worktree"])                            # the row does not say it was prepared...
        self.assertTrue(self.kept_worktree().is_dir())                  # ...but the directory is kept to look at
        self.assertEqual(self.db.failed_runs_since(0), 0)               # and it is no failure for the breaker
        for event in self.db.list_events(limit=100):
            self.assertNotEqual((event["type"], event["payload"].get("task_id")), ("task_resumed", self.child))
            self.assertNotEqual((event["type"], event["payload"].get("task_id")), ("task_done", self.child))
        return child

    def assert_failed_as_missing(self):
        child = self.db.get_task(self.child)
        self.assertEqual(child["state"], FAILED)
        self.assertEqual(child["last_error"], "dependency missing")
        failed = [e for e in self.events("task_failed") if e["payload"].get("task_id") == self.child]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"]["reason"], "dependency missing")
        self.assertEqual(failed[0]["payload"]["worktree"], str(self.kept_worktree()))   # named, for a person
        self.assertTrue(failed[0]["payload"]["worktree_kept"])


class WorktreePreparationTest(LaunchGateCase):
    def test_a_real_rm_of_the_finished_parent_while_the_worktree_is_prepared_starts_nothing(self):
        self.worktrees = RemoveDuring(self.child, lambda: self.real_rm(self.parent))
        ex = FixedExecutor()

        out = self.tick(ex)

        self.assertIsNone(self.db.get_task(self.parent))               # the parent really is gone
        self.assertEqual(ex.labels(), ["independent"])                 # the queue behind the child went on
        self.assertIn("done", out)
        self.assertEqual(self.db.get_task(self.independent)["state"], DONE)
        self.assert_child_never_ran(ex)
        self.assert_failed_as_missing()

    def test_the_outcome_says_what_was_kept_and_nothing_starts_later_either(self):
        self.worktrees = RemoveDuring(self.child, lambda: self.real_rm(self.parent))
        ex = FixedExecutor()
        self.tick(ex)
        self.assertEqual(self.tick(ex), "idle")                         # the child is settled, not retried
        self.assertEqual(self.tick(ex), "idle")
        self.assertEqual(ex.labels(), ["independent"])
        failed = [e for e in self.events("task_failed") if e["payload"].get("task_id") == self.child]
        self.assertEqual(len(failed), 1)                                # settled once, not again every pass

    def test_a_parent_that_is_no_longer_done_after_the_worktree_sends_the_child_back_to_waiting_unrun(self):
        self.worktrees = RemoveDuring(self.child, lambda: self.db.update_task(self.parent, state=FAILED))
        ex = FixedExecutor()

        self.tick(ex)

        self.assertEqual(ex.labels(), ["independent"])
        self.assertEqual(self.db.get_task(self.child)["state"], PENDING)    # waiting, not failed, not lost
        self.assert_child_never_ran(ex)
        waiting = [e for e in self.events("task_waiting") if e["payload"].get("task_id") == self.child]
        self.assertEqual(waiting[-1]["payload"]["worktree"], str(self.kept_worktree()))

        # The parent passes later: the child then starts once, in the worktree that was kept.
        self.worktrees = Worktrees()
        self.db.update_task(self.parent, state=DONE)
        self.tick(ex)
        self.tick(ex)
        self.assertEqual(ex.labels(), ["independent", "child"])
        self.assertEqual(self.db.get_task(self.child)["state"], DONE)
        self.assertEqual(len(self.db.runs_for_task(self.child)), 1)

    def test_the_child_whose_parent_stays_done_starts_exactly_once_with_its_record(self):
        ex = FixedExecutor()
        self.tick(ex)
        child = self.db.get_task(self.child)
        self.assertEqual(ex.labels(), ["child"])                       # the higher priority goes first
        self.assertEqual(child["state"], DONE)
        self.assertTrue(child["session_id"])
        self.assertEqual(child["worktree"], str(self.kept_worktree()))
        self.assertEqual(len(self.db.runs_for_task(self.child)), 1)


class SpawnBoundaryTest(LaunchGateCase):
    def run_with_rm_at_spawn(self, executor_cls):
        marker = self.home / "spawned"
        ex = executor_cls(self, marker)
        out = self.tick(ex)
        return ex, marker, out

    class AtTheBoundary(FixedExecutor):
        """What an adapter does: prepare, then reach the OS boundary where `run_streaming` creates the process.
        The real `timetrace rm` lands just before it, as the process is about to be created. `entered` records
        that the adapter was reached; `spawned` that a process was created (it must stay empty)."""
        def __init__(self, case, marker):
            super().__init__()
            self.case, self.marker, self.entered, self.spawned = case, marker, [], []

        def start(self, prompt, cwd, session_id, log_file):
            label = prompt.rsplit("\n", 1)[-1]
            if label != "child":
                return super().start(prompt, cwd, session_id, log_file)
            self.entered.append(label)
            self.case.real_rm(self.case.parent)
            code, _ = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(
                self.marker)], cwd, log_file)
            self.spawned.append(label)
            return RunResult(ok=code == 0, exit_code=code)

    def test_a_real_rm_at_the_adapter_spawn_boundary_creates_no_process_and_records_no_call(self):
        ex, marker, out = self.run_with_rm_at_spawn(self.AtTheBoundary)

        self.assertIsNone(self.db.get_task(self.parent))
        self.assertEqual(ex.entered, ["child"])                        # the adapter was at its boundary...
        self.assertFalse(marker.exists())                              # ...and no process was created
        self.assertEqual(ex.spawned, [])                               # run_streaming raised before Popen
        self.assertEqual(ex.labels(), ["independent"])
        self.assertIn("done", out)                                      # the independent task still ran
        self.assertEqual(self.db.get_task(self.independent)["state"], DONE)
        self.assert_child_never_ran(ex)
        self.assert_failed_as_missing()
        self.assertEqual(len(self.db.runs_for_task(self.independent)), 1)

    def test_the_claim_is_taken_back_without_a_trace_when_the_gate_refuses(self):
        ex, marker, out = self.run_with_rm_at_spawn(self.AtTheBoundary)
        runs = self.db.conn.execute("SELECT task_id FROM run ORDER BY id").fetchall()
        self.assertEqual([r["task_id"] for r in runs], [self.independent])   # the child's run row is gone
        later = FixedExecutor()
        self.assertEqual(self.tick(later), "idle")                      # and it is not retried or re-run later
        self.assertEqual(later.calls, [])

    def test_a_spawn_boundary_that_does_not_use_run_streaming_is_refused_the_same_way(self):
        # An adapter that calls the shared check itself, as the Claude stream adapter does before its own Popen.
        class Explicit(FixedExecutor):
            def __init__(self, case):
                super().__init__()
                self.case, self.spawned = case, []

            def start(self, prompt, cwd, session_id, log_file):
                if prompt.rsplit("\n", 1)[-1] == "child":
                    self.case.real_rm(self.case.parent)
                    enforce_spawn_authority()
                    self.spawned.append("child")
                return super().start(prompt, cwd, session_id, log_file)

        ex = Explicit(self)
        self.tick(ex)
        self.assertEqual(ex.spawned, [])
        self.assertEqual(ex.labels(), ["independent"])
        self.assert_child_never_ran(ex)
        self.assert_failed_as_missing()

    def test_a_resumed_child_whose_parent_goes_before_the_launch_keeps_its_old_evidence_and_gains_none(self):
        # It really ran once before (a session, a worktree, a run row). Its parent is removed in the instant between
        # the claim and the first launch check (the `log` call). Nothing new is recorded for the attempt.
        self.db.update_task(self.child, session_id="child-session", worktree=str(self.kept_worktree()),
                            branch="timetrace/%d" % self.child)
        self.kept_worktree().mkdir(parents=True)
        old_run = self.db.add_run(self.child, "claude", "child-session", "/old.log", now=50)
        self.db.finish_run(old_run, 0, False, "earlier", now=60)
        ex = FixedExecutor()
        removed = []

        def log(line):
            if "task %d: resume" % self.child in line and not removed:
                removed.append(True)
                self.real_rm(self.parent)

        self.clock += 1
        now = self.clock
        out = scheduler.run_once(self.db, {"claude": ex}, CFG, self.home, now=now, log=log,
                                 ensure_worktree=self.worktrees, hook_runner=lambda *a: None, rng=lambda: 0.0,
                                 clock=lambda: now)

        self.assertTrue(removed)
        self.assertEqual(ex.labels(), ["independent"])                  # the child was not resumed
        self.assertIn("done", out)
        self.assertEqual([r["id"] for r in self.db.runs_for_task(self.child)], [old_run])   # no new call recorded
        child = self.db.get_task(self.child)
        self.assertEqual((child["session_id"], child["worktree"]), ("child-session", str(self.kept_worktree())))
        self.assertEqual(child["state"], FAILED)
        self.assertEqual(child["last_error"], "dependency missing")
        self.assertEqual([e for e in self.events("task_resumed") if e["payload"].get("task_id") == self.child], [])


class LinearizationTest(LaunchGateCase):
    def other_connection(self):
        conn = sqlite3.connect(str(self.home / "timetrace.db"), timeout=0.05, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def test_the_parent_cannot_be_removed_between_the_last_check_and_the_claim(self):
        # While the claim is being written, another connection (what `timetrace rm` is) cannot delete a task: the
        # database's write lock is held. So rm lands entirely before the claim (the child does not start) or
        # entirely after it (the child started while its parent was DONE).
        other, refused = self.other_connection(), []
        real_add_run = self.db.add_run

        def add_run_then_try_to_remove_the_parent(*args, **kwargs):
            try:
                other.execute("DELETE FROM task WHERE id=?", (self.parent,))
            except sqlite3.OperationalError as exc:
                refused.append(str(exc))
            return real_add_run(*args, **kwargs)

        self.db.add_run = add_run_then_try_to_remove_the_parent
        ex = FixedExecutor()
        self.tick(ex)
        self.assertEqual(len(refused), 1)                              # one claim (the child's) was written
        self.assertIn("locked", refused[0])
        self.assertEqual(self.db.get_task(self.parent)["state"], DONE)  # nothing got through
        self.assertEqual(ex.labels(), ["child"])

    def test_a_removal_after_the_claim_does_not_disturb_the_child_that_already_started(self):
        # The same rule as always: a parent removed while its child runs is not the child's concern.
        class RemoveWhileRunning(FixedExecutor):
            def start(inner, prompt, cwd, session_id, log_file):
                if prompt.rsplit("\n", 1)[-1] == "child":
                    self.real_rm(self.parent)
                return super().start(prompt, cwd, session_id, log_file)

        ex = RemoveWhileRunning()
        # The adapter's own check ran at the spawn gate before start(): the parent was DONE at that moment.
        self.tick(ex)
        self.assertEqual(ex.labels(), ["child"])
        self.assertEqual(self.db.get_task(self.child)["state"], DONE)
        self.assertIsNone(self.db.get_task(self.parent))

    def test_no_database_write_lock_is_held_while_the_tool_runs(self):
        other, during = self.other_connection(), []

        class Probe(FixedExecutor):
            def start(inner, prompt, cwd, session_id, log_file):
                if prompt.rsplit("\n", 1)[-1] == "child":
                    during.append(self.db.conn.in_transaction)
                    other.execute("BEGIN IMMEDIATE")        # would raise "database is locked" if we held it
                    other.execute("ROLLBACK")
                    during.append("writable")
                return super().start(prompt, cwd, session_id, log_file)

        self.tick(Probe())
        self.assertEqual(during, [False, "writable"])

    def test_no_database_write_lock_is_held_while_the_worktree_is_prepared(self):
        other, seen = self.other_connection(), []

        def probe():
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            seen.append(self.db.conn.in_transaction)

        self.worktrees = RemoveDuring(self.child, probe)
        self.tick(FixedExecutor())
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
