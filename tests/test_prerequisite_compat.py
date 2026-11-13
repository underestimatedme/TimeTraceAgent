"""Old and new local-queue schedulers over one database, as real processes.

`tests/queue_compat_worker.py` is a scheduler process: ROLE `new` is this tree, ROLE `legacy` is the scheduler of
commit c1b577b (what is being upgraded from). Both use the real process locks and the same slot files; the "tool" is
a scripted executor that only appends to HOME/exec.log. The CLI is the real one, run as `python -m timetrace`.

What is pinned here: the reported behaviour really happens with the old scheduler on a real database (so the new
tests would have caught it); the new scheduler does not do it; the two never run together and never pass each
other; a task runs exactly once and never before its parent, even with several schedulers racing (guards off) and
with older writers (a direct RUNNABLE write, as the old `timetrace retry` did) hitting the rows meanwhile.
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from timetrace.db import Database
from timetrace.models import DONE, FAILED, PENDING, RUNNABLE

from tests.queue_fixture import QueueCase

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("queue_compat_worker.py")
WAITING = "等待前置工作"
REFUSED = "another timetrace scheduler is already running"


class CompatCase(QueueCase):
    def setUp(self):
        super().setUp()
        self.env = {**os.environ, "TIMETRACE_HOME": str(self.home), "PYTHONPATH": str(ROOT),
                    "PYTHONUNBUFFERED": "1"}
        self.procs = []
        self.addCleanup(self.stop_workers)

    # ---- real processes ---------------------------------------------------------------------------
    def timetrace(self, *argv):
        done = subprocess.run([sys.executable, "-m", "timetrace", *argv], env=self.env, cwd=self.tmp.name,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def add(self, label, *flags):
        code, out, err = self.timetrace("add", "--repo", str(self.repo), "--tool", "claude", *(list(flags) + [label]))
        self.assertEqual(code, 0, err)
        return int(out.split()[1])

    def worker(self, role, *flags, timeout=90):
        done = subprocess.run([sys.executable, str(WORKER), str(self.home), role, *flags], env=self.env,
                              cwd=self.tmp.name, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr

    def spawn(self, role, *flags):
        proc = subprocess.Popen([sys.executable, str(WORKER), str(self.home), role, *flags], env=self.env,
                                cwd=self.tmp.name, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def stop_workers(self):
        (self.home / "release").touch()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def finish(self, proc, timeout=90):
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err

    def wait_for(self, predicate, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def script(self, **answers):
        (self.home / "script.json").write_text(json.dumps(answers))

    def exec_log(self):
        path = self.home / "exec.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def started(self):
        return [e["label"] for e in self.exec_log() if e["event"] == "start"]

    def fresh_db(self):
        db = Database(self.home / "timetrace.db")
        self.addCleanup(db.close)
        return db


class ReportedBehaviourTest(CompatCase):
    """The measurement that started this: parent, child --after parent, independent."""

    def test_the_old_scheduler_fails_the_child_and_lets_a_retried_child_pass_its_failed_parent(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        independent = self.add("independent")
        self.script(parent=[1, 0])
        db = self.fresh_db()
        self.assertEqual(self.states(parent, child, independent), [RUNNABLE, PENDING, RUNNABLE])

        code, out, err = self.worker("legacy")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(parent, child, independent), [FAILED, FAILED, DONE])   # the child is lost
        self.assertEqual(db.get_task(child)["last_error"], "dependency %d failed" % parent)
        session = db.get_task(parent)["session_id"]
        self.assertEqual(self.timetrace("retry", str(parent))[0], 0)
        self.assertEqual(self.worker("legacy")[0], 0)
        self.assertEqual(self.states(parent, child), [DONE, FAILED])                         # and stays lost
        self.assertEqual(self.started(), ["parent", "independent", "parent"])
        self.assertEqual([e["session"] for e in self.exec_log() if e["label"] == "parent" and e["event"] == "start"],
                         [session, session])

    def test_the_old_retry_of_a_child_lets_the_old_scheduler_start_it_before_its_failed_parent(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent), "--priority", "5")
        self.script(parent=[1, 0])
        self.assertEqual(self.worker("legacy", "--ticks", "1")[0], 0)                         # the parent fails
        db = self.fresh_db()
        self.assertEqual(self.states(parent), [FAILED])
        db.update_task(child, state=RUNNABLE, blocked_until=None)   # what `timetrace retry <child>` used to write
        self.assertEqual(self.worker("legacy")[0], 0)
        self.assertEqual(self.started(), ["parent", "child"])                                 # ahead of the parent
        self.assertEqual(self.states(parent, child), [FAILED, DONE])

    def test_the_new_scheduler_keeps_the_child_and_runs_everything_once_in_order(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        independent = self.add("independent")
        self.script(parent=[1, 0])
        db = self.fresh_db()

        code, out, err = self.worker("new")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(parent, child, independent), [FAILED, PENDING, DONE])
        session = db.get_task(parent)["session_id"]
        worktree = db.get_task(parent)["worktree"]
        code, out, err = self.timetrace("ls")
        self.assertIn(WAITING, out)
        self.assertIn("timetrace retry %d" % parent, out)

        code, out, err = self.timetrace("retry", str(child))                                  # cannot jump ahead
        self.assertEqual(code, 0, err)
        self.assertIn(WAITING, out)
        self.assertEqual(self.states(child), [PENDING])
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "independent"])

        self.assertEqual(self.timetrace("retry", str(parent))[0], 0)
        self.assertEqual(self.states(parent, child), [RUNNABLE, PENDING])
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "independent", "parent", "child"])
        again = [e for e in self.exec_log() if e["label"] == "parent" and e["event"] == "start"][1]
        self.assertEqual((again["kind"], again["session"], again["cwd"]), ("resume", session, worktree))
        self.assertEqual(self.states(parent, child, independent), [DONE, DONE, DONE])


class ProcessLockTest(CompatCase):
    def holder(self, role):
        self.add("holder")
        proc = self.spawn(role, "--hold", "holder")
        self.assertEqual(proc.stdout.readline().strip(), "ready %s" % role)
        self.wait_for(lambda: self.started() == ["holder"], "the held task to start")
        return proc

    def check_nothing_starts_beside(self, holder_role):
        holder = self.holder(holder_role)
        for role in ("new", "legacy"):
            code, out, err = self.worker(role)
            self.assertEqual((code, out), (1, ""), role)
            self.assertIn(REFUSED, err)
        code, out, err = self.timetrace("run", "--once")                                      # the real command too
        self.assertEqual(code, 1)
        self.assertIn(REFUSED, err)
        # even with the process locks bypassed, the running task holds the slot: nothing else starts beside it
        self.add("other")
        for role in ("new", "legacy"):
            code, out, err = self.worker(role, "--no-guard", "--ticks", "5")
            self.assertEqual(code, 0, err)
            self.assertIn("deferred (runner busy)", out, role)
        self.assertEqual(self.started(), ["holder"])
        (self.home / "release").touch()
        code, out, err = self.finish(holder)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started(), ["holder", "other"])
        code, out, err = self.worker("legacy")                                                 # the lock is free again
        self.assertEqual(code, 0, err)

    def test_no_scheduler_starts_beside_a_running_new_one(self):
        self.check_nothing_starts_beside("new")

    def test_no_scheduler_starts_beside_a_running_old_one(self):
        self.check_nothing_starts_beside("legacy")


class CrashRecoveryCompatTest(CompatCase):
    """A scheduler killed hard leaves its task `running` and its slot fenced. This tree's restart no longer resumes
    such a task by itself (it ends the record as 待核对 and waits for an explicit retry); the older scheduler, which
    takes only overall slot 0 (`locks/coding-slot.lock`), must still be refused by the fence this tree left there,
    must not start a task that is waiting for a retry, and may run it, once, after a person cleared the fence."""

    def setUp(self):
        super().setUp()
        user = Path(self.tmp.name) / "user"  # the real CLI below must not look at this machine's tool installs
        self.env["HOME"] = str(user)
        self.locks = self.home / "locks"

    def kill_a_scheduler_mid_run(self):
        task = self.add("alpha")
        worker = self.spawn("new", "--hold", "alpha")
        self.assertEqual(worker.stdout.readline().strip(), "ready new")
        self.wait_for(lambda: self.started() == ["alpha"], "alpha to be executing")
        worker.kill()  # our own child
        worker.communicate()
        self.assertEqual(self.states(task), ["running"])
        return task

    def test_the_older_scheduler_is_fenced_and_a_retried_task_resumes_once_after_the_fence_is_cleared(self):
        task = self.kill_a_scheduler_mid_run()
        db = self.fresh_db()
        before = db.get_task(task)
        fence = (self.locks / "coding-slot.lock").read_text()
        self.assertNotEqual(fence, "")
        first = self.exec_log()[0]

        code, out, err = self.timetrace("run", "--once")                                      # the restart
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(task), [FAILED])
        self.assertIn("待核对", db.get_task(task)["last_error"])
        self.assertEqual(self.started(), ["alpha"])                                           # not run again
        self.assertEqual((self.locks / "coding-slot.lock").read_text(), fence)                # the fence is untouched
        run = db.runs_for_task(task)[0]
        self.assertEqual((run["exit_code"], run["interrupted"]), (None, 1))
        for role in ("legacy", "new"):                                                        # nobody picks it up
            code, out, err = self.worker(role, "--ticks", "3")
            self.assertEqual(code, 0, err)
            self.assertNotIn("task %d" % task, out, role)
        self.assertEqual(self.started(), ["alpha"])

        self.assertEqual(self.timetrace("retry", str(task))[0], 0)                            # a person's decision
        for role in ("legacy", "new"):                                                        # and the fence answers
            code, out, err = self.worker(role, "--ticks", "3")
            self.assertEqual(code, 0, err)
            self.assertIn("deferred (runner busy)", out, role)
        self.assertEqual(self.started(), ["alpha"])
        self.assertEqual(sorted(p.name for p in self.locks.iterdir()), ["coding-slot.lock"])  # the only slot it takes
        self.assertEqual((self.locks / "coding-slot.lock").read_text(), fence)

        (self.locks / "coding-slot.lock").write_text("")                                      # verified and cleared
        code, out, err = self.worker("legacy")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.started(), ["alpha", "alpha"])
        again = [e for e in self.exec_log() if e["event"] == "start"][1]
        self.assertEqual((again["kind"], again["session"], again["cwd"]),
                         ("resume", before["session_id"], first["cwd"]))
        self.assertEqual(self.states(task), [DONE])
        self.assertEqual(sorted(p.name for p in self.locks.iterdir()), ["coding-slot.lock"])
        self.assertEqual((self.locks / "coding-slot.lock").read_text(), "")                   # released, not left behind

    def test_without_a_marker_the_restart_still_does_not_resume_and_the_older_scheduler_never_picks_it_up(self):
        task = self.kill_a_scheduler_mid_run()
        db = self.fresh_db()
        (self.locks / "coding-slot.lock").write_text("")                                      # no marker at all now
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.states(task), [FAILED])
        self.assertIn("待核对", db.get_task(task)["last_error"])
        for role in ("legacy", "new"):
            self.assertEqual(self.worker(role, "--ticks", "3")[0], 0)
        self.assertEqual(self.started(), ["alpha"])                                           # one run, ever
        self.assertEqual(self.states(task), [FAILED])


class HandoffTest(CompatCase):
    def test_a_database_passed_from_the_new_scheduler_to_an_old_one_and_back_loses_nothing(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        grandchild = self.add("grandchild", "--after", str(child))
        independent = self.add("independent")
        self.script(parent=[1, 0])
        db = self.fresh_db()

        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.states(parent, child, grandchild, independent), [FAILED, PENDING, PENDING, DONE])
        # Downgrade: the old scheduler fails the waiting tasks the way it always did. It never starts them.
        self.assertEqual(self.worker("legacy")[0], 0)
        self.assertEqual(self.states(parent, child, grandchild, independent), [FAILED, FAILED, FAILED, DONE])
        self.assertEqual(self.started(), ["parent", "independent"])
        # Upgrade again: nothing of theirs ever ran, so they are put back to waiting.
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.states(parent, child, grandchild, independent), [FAILED, PENDING, PENDING, DONE])
        self.assertIsNone(db.get_task(child)["last_error"])
        self.assertEqual(self.started(), ["parent", "independent"])

        self.assertEqual(self.timetrace("retry", str(parent))[0], 0)
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "independent", "parent", "child", "grandchild"])
        self.assertEqual(self.states(parent, child, grandchild, independent), [DONE] * 4)
        self.assertEqual(len(db.list_tasks(include_done=True)), 4)                            # nothing duplicated

    def test_a_task_that_ran_before_a_downgrade_is_not_rerun_by_the_upgrade(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent))
        self.script(parent=[1, 0], child=[1])
        db = self.fresh_db()
        # the old scheduler lets a retried child run ahead of its failed parent, and it fails on its own
        self.assertEqual(self.worker("legacy", "--ticks", "1")[0], 0)
        db.update_task(child, state=RUNNABLE, blocked_until=None)
        self.assertEqual(self.worker("legacy", "--ticks", "1")[0], 0)
        self.assertEqual(self.states(parent, child), [FAILED, FAILED])
        db.update_task(child, last_error="dependency %d failed" % parent)   # worst case: carries the old marker
        self.assertEqual(self.timetrace("retry", str(parent))[0], 0)
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "child", "parent"])                       # child: never again
        self.assertEqual(self.states(parent, child), [DONE, FAILED])


class RaceTest(CompatCase):
    def test_racing_schedulers_run_each_task_once_and_never_a_child_before_its_parent(self):
        # Only the process locks keep two schedulers apart in real use. With them off, the slot and the single-run
        # checks must still hold: this tree's scheduler never starts a task twice, whoever else is running. (The old
        # scheduler is left out on purpose: its promotion of a waiting task is an unconditional write, which is
        # exactly why it relied on the process lock alone.)
        parent = self.add("parent")
        kids = [self.add("kid%d" % n, "--after", str(parent)) for n in range(2)]
        grandkid = self.add("grandkid", "--after", str(kids[0]))
        others = [self.add("other%d" % n) for n in range(3)]
        procs = [self.spawn("new", "--no-guard") for _ in range(4)]
        for proc in procs:
            code, out, err = self.finish(proc)
            self.assertEqual(code, 0, err)
        ids = [parent] + kids + [grandkid] + others
        self.assertEqual(self.states(*ids), [DONE] * len(ids))
        started = self.started()
        self.assertEqual(sorted(started), sorted(["parent", "kid0", "kid1", "grandkid", "other0", "other1", "other2"]))
        log = self.exec_log()
        position = {(e["label"], e["event"]): i for i, e in enumerate(log)}
        for label in ("kid0", "kid1"):
            self.assertGreater(position[(label, "start")], position[("parent", "end")], label)
        self.assertGreater(position[("grandkid", "start")], position[("kid0", "end")])

    def test_writers_hitting_the_rows_meanwhile_never_start_a_child_ahead_of_its_failed_parent(self):
        parent = self.add("parent")
        child = self.add("child", "--after", str(parent), "--priority", "9")
        deep = self.add("deep", "--after", str(child), "--priority", "9")
        other = self.add("other")
        self.script(parent=[1, 0])
        holder = self.spawn("new", "--hold", "parent")
        self.assertEqual(holder.stdout.readline().strip(), "ready new")
        self.wait_for(lambda: self.started() == ["parent"], "the parent to start")

        stop = threading.Event()
        writes = []

        def old_writers():
            db = Database(self.home / "timetrace.db")                                         # its own connection
            try:
                while not stop.is_set():
                    for task_id in (child, deep):                                             # the old `retry` write
                        db.update_task(task_id, state=RUNNABLE, blocked_until=None)
                        writes.append(task_id)
                    time.sleep(0.005)
            finally:
                db.close()

        thread = threading.Thread(target=old_writers)
        thread.start()
        try:
            time.sleep(0.3)
            (self.home / "release").touch()                                                   # the parent fails
            self.wait_for(lambda: "other" in self.started(), "independent work to run past the withheld rows")
            for _ in range(3):
                self.timetrace("retry", str(child))                                           # the real CLI, meanwhile
            time.sleep(0.3)
        finally:
            stop.set()
            thread.join()
        code, out, err = self.finish(holder)
        self.assertEqual(code, 0, err)
        self.assertGreater(len(writes), 10)
        self.assertEqual(self.started(), ["parent", "other"])                                 # child, deep: never
        self.assertEqual(self.states(parent, other), [FAILED, DONE])
        # the last old write may land after the worker's last pass: it is still only a row, never a start
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "other"])
        self.assertEqual(self.states(child, deep), [PENDING, PENDING])

        (self.home / "script.json").write_text(json.dumps({"parent": [1, 0]}))
        self.assertEqual(self.timetrace("retry", str(parent))[0], 0)
        self.assertEqual(self.worker("new")[0], 0)
        self.assertEqual(self.started(), ["parent", "other", "parent", "child", "deep"])
        self.assertEqual(self.states(parent, child, deep, other), [DONE] * 4)


if __name__ == "__main__":
    unittest.main()
