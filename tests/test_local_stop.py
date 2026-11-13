"""`timetrace run` receives SIGINT or SIGTERM while a tool is executing (the local queue, not the cloud agent).

The reported failure: the tool was gone, yet the task and the run stayed `running` for ever. A SIGTERM killed the
scheduler on the spot (no `finally`, no database write, the slot's crash marker left behind) and a Ctrl-C only
unwound it. What is pinned here, with the real CLI (`python -m timetrace`) in a real subprocess, a real SQLite
database and real signals, against a tool that only writes files under its test directory
(tests/fake_queue_tool.py):

  * the tool's whole process group is stopped, within a bound, before the scheduler exits, and nothing else is
    signalled;
  * the run gets its end, the task an explainable state (`failed`, "interrupted (SIGTERM): ..."), and the stop is
    not counted as a failure by the circuit breaker;
  * a restart runs the other, independent tasks but never the interrupted one; `timetrace retry` resumes its
    original session, once; what waited for it keeps waiting;
  * a tool whose exit is not confirmed is never reported as stopped (the slot stays fenced: the existing lock
    protection); only the kernel's "no such process group" (ESRCH) confirms an exit, never EPERM;
  * a hard kill (SIGKILL) leaves a task `running` with no record: the restart ends that record as "待核对" (no exit
    code) and never runs the task again by itself, with or without a crash marker on the slot (a missing marker is
    not an authorization). Only an explicit `timetrace retry` does, in the same session and worktree, and the
    slot's lock still refuses a tool that may be running;
  * a stop that races a normal completion never turns a finished task into an interrupted one; repeated signals
    are one stop.

No test sends a signal to a process it did not start itself: the CLI under test gets the signal, and the fake
tools end by themselves (a `release` file) or are stopped by the code under test.
"""
import contextlib
import errno
import fcntl
import gc
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from timetrace import cli, process, scheduler
from timetrace.db import Database
from timetrace.dispatch import DispatchDenied, UnclearedOwner, coding_slot_lock
from timetrace.models import DONE, FAILED, PENDING, RUNNABLE, RUNNING, RunResult

from tests.queue_fixture import CFG, QueueCase, git

ROOT = Path(__file__).resolve().parents[1]
FAKE_TOOL = Path(__file__).with_name("fake_queue_tool.py")
EPERM_CLI = Path(__file__).with_name("eperm_cli.py")
STOP_BOUND = 25  # seconds the whole stop may take: 2 s grace + 5 s confirmation + joins, with a wide margin


def open_descriptors():
    gc.collect()  # a connection some earlier step left unreferenced closes its file now, not in the middle of a block
    return len(os.listdir("/dev/fd"))


@contextlib.contextmanager
def owned_tools(release, wait=10):
    """The tools `run_streaming` starts inside this block all end before the block is left, whatever happened in it.

    A test that makes the exit of its tool unconfirmable leaves that tool running on purpose, so the block ends it
    (the `release` file every such tool waits for), waits for each process it started, lets the reader threads
    `run_streaming` left behind see the end of their pipes, and closes the pipes: nothing is signalled by pid, no
    process, thread or descriptor outlives the test, and the temporary directory is removed only afterwards.
    A tool that did not end by itself is killed (it is this block's own child, never anyone else's) and reported.
    Leaving the block normally with anything leaked fails the test."""
    before = SimpleNamespace(threads=set(threading.enumerate()), fds=open_descriptors())
    spawned, real_popen = [], subprocess.Popen

    def popen(*args, **kwargs):
        spawned.append(real_popen(*args, **kwargs))
        return spawned[-1]
    stragglers = []
    try:
        with patch("timetrace.process.subprocess.Popen", popen):
            yield spawned
    finally:
        Path(release).touch()
        for proc in spawned:
            try:
                proc.wait(wait)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                stragglers.append(proc.pid)
        readers = set(threading.enumerate()) - before.threads
        for thread in readers:
            thread.join(wait)
        running = [t.name for t in readers if t.is_alive()]
        if not running:  # a pipe is closed only once nothing reads it any more
            for proc in spawned:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
    problems = ["tool %s had to be killed" % pid for pid in stragglers]
    problems += ["thread %s is still running" % name for name in running]
    after = open_descriptors()
    if after > before.fds:
        problems.append("%d descriptors open before, %d after" % (before.fds, after))
    if problems:
        raise AssertionError("the block leaked: " + "; ".join(problems))


class RealCliCase(unittest.TestCase):
    """A real `python -m timetrace` in a temporary home, driving tests/fake_queue_tool.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home, self.repo, self.fake = root / "home", root / "repo", root / "fake"
        user = root / "user"
        for d in (self.home, self.repo, self.fake, user):
            d.mkdir()
        git("init", "-q", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "a").write_text("a")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        wrapper = root / "claude"
        wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_TOOL))
        wrapper.chmod(0o755)
        (self.home / "config.json").write_text(json.dumps({
            "interval_sec": 1, "jitter_sec": 0, "claude": {"bin": str(wrapper), "quota_probe": False}}))
        self.env = {"PATH": os.environ["PATH"], "HOME": str(user), "TIMETRACE_HOME": str(self.home),
                    "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "TT_FAKE_HOME": str(self.fake)}
        self.procs = []
        self.addCleanup(self.cleanup_processes)
        self.script()

    # ---- the real CLI -----------------------------------------------------------------------
    def timetrace(self, *argv, timeout=60):
        done = subprocess.run([sys.executable, "-m", "timetrace", *argv], env=self.env, cwd=self.tmp.name,
                              capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr

    def spawn(self, *argv):
        return self.spawn_command([sys.executable, "-m", "timetrace", *argv])

    def spawn_command(self, command):
        out, err = open(self.fake / "cli.out", "a"), open(self.fake / "cli.err", "a")
        proc = subprocess.Popen(command, env=self.env, cwd=self.tmp.name, stdout=out, stderr=err, text=True)
        out.close()
        err.close()
        self.procs.append(proc)
        return proc

    def cleanup_processes(self):
        (self.fake / "release").touch()  # every fake tool ends by itself; no process is ever signalled by pid
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()  # our own child: the CLI under test
            proc.wait()
        # A tool the CLI left running on purpose (a hard kill, an unconfirmed exit) ends by itself now; the
        # temporary directory is removed only once every tool tree is gone.
        for lock_file in sorted(self.fake.glob("alive-*.lock")):
            self.wait_for(lambda: not self.tree_alive(lock_file), "%s to end" % lock_file.name, timeout=15)

    def add(self, label, *flags):
        code, out, err = self.timetrace("add", "--repo", str(self.repo), "--tool", "claude", *(list(flags) + [label]))
        self.assertEqual(code, 0, err)
        return int(re.search(r"task (\d+) added", out).group(1))

    def script(self, **plans):
        (self.fake / "scenario.json").write_text(json.dumps(plans))

    # ---- evidence -----------------------------------------------------------------------------
    def db(self):
        db = Database(self.home / "timetrace.db")
        self.addCleanup(db.close)
        return db

    def exec_log(self):
        path = self.fake / "exec.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def runs_of(self, label):
        return [e for e in self.exec_log() if e["label"] == label]

    def tool_tree_alive(self, label):
        """True while the tool or any process it forked lives (they all hold the flock)."""
        return self.tree_alive(self.fake / ("alive-%s.lock" % label))

    @staticmethod
    def tree_alive(path):
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False

    def wait_for(self, predicate, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for " + what)

    def working(self, label):
        return (self.fake / ("running-%s" % label)).exists()

    def stop_with(self, proc, *signums, gap=0.0):
        started = time.monotonic()
        for i, signum in enumerate(signums):
            if i:
                time.sleep(gap)
            proc.send_signal(signum)  # to the CLI we started, nothing else
        try:
            proc.wait(STOP_BOUND)
        except subprocess.TimeoutExpired:
            self.fail("the scheduler did not exit within %ds of the signal" % STOP_BOUND)
        return time.monotonic() - started

    def cli_text(self):
        parts = [self.fake / "cli.out", self.fake / "cli.err"]
        return "".join(p.read_text() for p in parts if p.exists())

    def slot_marker(self):
        path = self.home / "locks" / "coding-slot.lock"
        return path.read_text() if path.exists() else ""


class InterruptedRunMixin:
    """The real-CLI cases, run once for SIGINT and once for SIGTERM (the two concrete classes below)."""
    SIGNUM = None

    def start_working(self, label="alpha", **plan):
        """Queue `label` and start the real scheduler; return once the tool is executing."""
        self.script(**{label: dict(hang=True, grandchild=True, **plan)})
        task = self.add(label)
        proc = self.spawn("run", "--interval", "1")
        self.wait_for(lambda: self.working(label), "the tool to be executing")
        return task, proc

    def test_the_signal_stops_the_tool_and_ends_the_run(self):
        task, proc = self.start_working()
        took = self.stop_with(proc, self.SIGNUM)
        self.assertEqual(proc.returncode, 0, self.cli_text())
        self.assertLess(took, STOP_BOUND)
        self.assertFalse(self.tool_tree_alive("alpha"), "the tool's process group outlived the scheduler")
        db = self.db()
        row, run = db.get_task(task), db.latest_run(task)
        self.assertEqual(row["state"], FAILED)
        self.assertTrue(row["last_error"].startswith("interrupted (%s)" % self.SIGNUM.name), row["last_error"])
        self.assertEqual(row["session_id"], self.runs_of("alpha")[0]["session"])
        self.assertIsNotNone(run["ended_at"], "the run was left without an end")
        self.assertEqual(run["interrupted"], 1)
        self.assertIn("interrupted by %s" % self.SIGNUM.name, run["summary"])
        self.assertIn("exit confirmed", run["summary"])
        self.assertEqual([e["payload"]["exit_confirmed"] for e in db.list_events(type_="task_interrupted")], [True])
        self.assertEqual(self.slot_marker(), "", "the slot kept its crash marker")
        self.assertEqual(db.failed_runs_since(0), 0, "a stop counts as a failure for the circuit breaker")
        self.assertIn("interrupted (%s)" % self.SIGNUM.name, self.timetrace("ls")[1])

    def test_a_restart_runs_the_other_tasks_never_the_interrupted_one_and_retry_resumes_it_once(self):
        self.script(alpha=dict(hang=True, grandchild=True))
        alpha = self.add("alpha")
        child = self.add("child-of-alpha", "--after", str(alpha))
        bravo = self.add("bravo")
        proc = self.spawn("run", "--interval", "1")
        self.wait_for(lambda: self.working("alpha"), "alpha to be executing")
        self.stop_with(proc, self.SIGNUM)
        session = self.runs_of("alpha")[0]["session"]
        db = self.db()

        def states():
            return [db.get_task(i)["state"] for i in (alpha, child, bravo)]
        self.assertEqual(states(), [FAILED, PENDING, RUNNABLE])

        # a restart: the independent task runs; the interrupted one and what waits for it do not
        self.script()  # were alpha started again, it would now finish at once, and the log would show it
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual(states(), [FAILED, PENDING, DONE])
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual(states(), [FAILED, PENDING, DONE])
        self.assertEqual([e["label"] for e in self.exec_log()], ["alpha", "bravo"])
        self.assertIn("timetrace retry %d" % alpha, self.timetrace("ls")[1])

        # an explicit retry resumes the original session: one more run of alpha, nothing else
        code, out, err = self.timetrace("retry", str(alpha))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual([(e["event"], e["label"], e["session"]) for e in self.exec_log()],
                         [("start", "alpha", session), ("start", "bravo", db.get_task(bravo)["session_id"]),
                          ("resume", "alpha", session)])
        self.assertEqual(states(), [DONE, PENDING, DONE])
        self.assertEqual(self.timetrace("run", "--once")[0], 0)  # the child follows its finished parent, once
        self.assertEqual(states(), [DONE, DONE, DONE])
        self.assertEqual([e["label"] for e in self.exec_log()], ["alpha", "bravo", "alpha", "child-of-alpha"])

    def test_repeated_signals_are_one_stop(self):
        # SIGTERM is ignored by this tool, so the stop needs its SIGKILL escalation: a window for more signals
        task, proc = self.start_working(ignore_term=True)
        other = signal.SIGTERM if self.SIGNUM == signal.SIGINT else signal.SIGINT
        self.stop_with(proc, self.SIGNUM, other, self.SIGNUM, gap=0.4)
        self.assertEqual(proc.returncode, 0, self.cli_text())
        self.assertFalse(self.tool_tree_alive("alpha"))
        db = self.db()
        self.assertEqual(len(db.runs_for_task(task)), 1)
        self.assertEqual(len(db.list_events(type_="task_interrupted")), 1)
        self.assertEqual(db.get_task(task)["state"], FAILED)
        self.assertTrue(db.get_task(task)["last_error"].startswith("interrupted (%s)" % self.SIGNUM.name))
        self.assertEqual(self.slot_marker(), "")

    def test_a_tool_that_completes_as_it_is_stopped_stays_done(self):
        task, proc = self.start_working(finish_on_term=True)
        self.stop_with(proc, self.SIGNUM)
        self.assertEqual(proc.returncode, 0, self.cli_text())
        db = self.db()
        self.assertEqual(db.get_task(task)["state"], DONE)
        self.assertIsNone(db.get_task(task)["last_error"])
        run = db.latest_run(task)
        self.assertEqual((run["exit_code"], run["interrupted"]), (0, 0))
        self.assertEqual(db.list_events(type_="task_interrupted"), [])
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual([e["event"] for e in self.runs_of("alpha")], ["start"])  # and never run again

    def test_a_stop_while_idle_exits_at_once(self):
        proc = self.spawn("run", "--interval", "30")
        self.wait_for(lambda: "daemon" in self.cli_text(), "the scheduler to be up")
        time.sleep(0.5)
        self.assertLess(self.stop_with(proc, self.SIGNUM), 5)  # not after the 30 s sleep
        self.assertEqual(proc.returncode, 0, self.cli_text())


class SigintRunTest(InterruptedRunMixin, RealCliCase):
    SIGNUM = signal.SIGINT


class SigtermRunTest(InterruptedRunMixin, RealCliCase):
    SIGNUM = signal.SIGTERM


def claims_stopped(text):
    """Whether a text says the tool was stopped / its exit confirmed: nothing may say that about a task whose
    scheduler ended without a record."""
    return any(claim in text for claim in ("tool stopped", "exit confirmed", "process group was stopped",
                                           "stopped (SIG"))


class HardKillTest(RealCliCase):
    def kill_the_scheduler_mid_run(self, label="alpha"):
        self.script(**{label: dict(hang=True)})
        task = self.add(label)
        proc = self.spawn("run", "--interval", "1")
        self.wait_for(lambda: self.working(label), "the tool to be executing")
        proc.kill()  # our own child, the scheduler
        proc.wait()
        self.assertTrue(self.tool_tree_alive(label), "the tool is orphaned, as a hard kill leaves it")
        return task

    def test_a_hard_kill_is_left_to_the_slot_fence_and_marked_for_checking(self):
        """SIGKILL gives the scheduler no chance to write anything: that boundary is the slot's crash marker, which
        must keep a second tool from starting while the first may still be running. The restart says that what became
        of the tool is unverified (待核对), keeps the fence exactly as it was, claims nothing is stopped, and does
        not run the task: only an explicit `timetrace retry` does, and the fence still refuses until a person
        clears it."""
        task = self.kill_the_scheduler_mid_run()
        marker = self.slot_marker()
        self.assertNotEqual(marker, "")
        db = self.db()
        before = db.get_task(task)
        self.assertEqual(before["state"], RUNNING)
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.runs_of("alpha")), 1, "a second tool was started over a possibly running one")
        self.assertEqual(self.slot_marker(), marker, "the restart touched the crash fence")
        row = db.get_task(task)
        self.assertEqual(row["state"], FAILED, "the restart must not decide to run it again")
        self.assertIn("待核对", row["last_error"])
        self.assertIn("unverified", row["last_error"])
        self.assertFalse(claims_stopped(row["last_error"] + out + err), row["last_error"] + out + err)
        self.assertEqual([row[k] for k in ("session_id", "worktree", "branch")],
                         [before[k] for k in ("session_id", "worktree", "branch")])
        runs = db.runs_for_task(task)
        self.assertEqual(len(runs), 1)
        self.assertIsNotNone(runs[0]["ended_at"])
        self.assertIn("待核对", runs[0]["summary"])
        self.assertFalse(claims_stopped(runs[0]["summary"]), runs[0]["summary"])
        self.assertEqual(runs[0]["interrupted"], 1)
        self.assertIsNone(runs[0]["exit_code"])  # nothing says how it ended
        self.assertEqual([e["payload"]["exit_confirmed"] for e in db.list_events(type_="task_interrupted")], [False])

        # a second restart changes nothing
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual((len(db.runs_for_task(task)), len(self.runs_of("alpha")), self.slot_marker()), (1, 1, marker))

        # an explicit retry is the decision to run it again; the fence still answers for a tool that may be running
        code, out, err = self.timetrace("retry", str(task))
        self.assertEqual(code, 0, err)
        self.assertEqual(db.get_task(task)["state"], RUNNABLE)
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("manual recovery required", out)  # the existing lock protection answered
        self.assertEqual(len(self.runs_of("alpha")), 1)
        self.assertEqual(db.get_task(task)["state"], RUNNABLE)
        self.assertEqual(self.slot_marker(), marker)

        # a person verified nothing is writing any more and cleared the marker: now it resumes, once, in place
        (self.fake / "release").touch()
        self.wait_for(lambda: not self.tool_tree_alive("alpha"), "the orphaned tool to end")
        (self.home / "locks" / "coding-slot.lock").write_text("")
        (self.fake / "release").unlink()
        self.script()  # the resumed run finishes at once
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual([(e["event"], e["session"]) for e in self.runs_of("alpha")],
                         [("start", before["session_id"]), ("resume", before["session_id"])])
        self.assertEqual(os.path.realpath(self.runs_of("alpha")[1]["cwd"]), os.path.realpath(before["worktree"]))
        self.assertEqual(db.get_task(task)["state"], DONE)


class UnrecordedExitTest(RealCliCase):
    """A task found `running` whose scheduler left no record, and no crash marker on the slot (a database from before,
    a marker a person cleared, a home that was copied): the missing marker is not an authorization. The restart
    must not run the task again by itself: the original record ends as 待核对 with no exit code, the task waits for an
    explicit `timetrace retry`, and the independent tasks go on."""

    def old_database(self):
        """alpha `running` in session sess-old and its worktree with its run unfinished; a child waits for it; bravo
        is independent. Nothing in locks/."""
        alpha = self.add("alpha")
        child = self.add("child-of-alpha", "--after", str(alpha))
        bravo = self.add("bravo")
        worktree = self.home / "worktrees" / "alpha"
        worktree.mkdir(parents=True)
        git("branch", "timetrace/alpha", cwd=self.repo)  # the child builds on its parent's branch
        db = self.db()
        db.update_task(alpha, state=RUNNING, tool="claude", session_id="sess-old", worktree=str(worktree),
                       branch="timetrace/alpha")
        run = db.add_run(alpha, "claude", "sess-old", str(self.home / "logs" / "alpha.log"), now=100)
        self.assertEqual(self.slot_marker(), "")
        self.assertFalse((self.home / "locks").exists())
        return alpha, child, bravo, run, worktree

    def test_a_restart_never_runs_it_again_by_itself(self):
        alpha, child, bravo, run, worktree = self.old_database()
        db = self.db()

        def states():
            return [db.get_task(i)["state"] for i in (alpha, child, bravo)]
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual([e["label"] for e in self.exec_log()], ["bravo"], "the old task was run again by the restart")
        self.assertEqual(states(), [FAILED, PENDING, DONE])
        row = db.get_task(alpha)
        self.assertIn("待核对", row["last_error"])
        self.assertIn("unverified", row["last_error"])
        self.assertFalse(claims_stopped(row["last_error"] + out), row["last_error"] + out)
        self.assertIn("timetrace retry %d" % alpha, out)
        self.assertEqual((row["session_id"], row["worktree"], row["branch"]), ("sess-old", str(worktree), "timetrace/alpha"))
        runs = db.runs_for_task(alpha)
        self.assertEqual([(r["id"], r["exit_code"], r["interrupted"]) for r in runs], [(run, None, 1)])
        self.assertIsNotNone(runs[0]["ended_at"])
        self.assertIn("待核对", runs[0]["summary"])
        self.assertEqual(db.failed_runs_since(0), 0, "an unverified end counts as a tool failure for the breaker")
        self.assertEqual(self.slot_marker(), "", "a marker appeared that nobody wrote")

        # nothing changes however often it restarts; the waiting child keeps waiting
        for _ in range(2):
            self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual(([e["label"] for e in self.exec_log()], states(), len(db.runs_for_task(alpha))),
                         (["bravo"], [FAILED, PENDING, DONE], 1))
        self.assertEqual(len(db.list_events(type_="task_interrupted")), 1)
        self.assertIn("timetrace retry %d" % alpha, self.timetrace("ls")[1])

        # the explicit retry resumes the same session in the same worktree, once; the child follows its parent
        code, out, err = self.timetrace("retry", str(alpha))
        self.assertEqual(code, 0, err)
        self.assertEqual(states(), [RUNNABLE, PENDING, DONE])
        for _ in range(2):
            self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual([(e["event"], e["label"], e["session"]) for e in self.exec_log()],
                         [("start", "bravo", db.get_task(bravo)["session_id"]), ("resume", "alpha", "sess-old"),
                          ("start", "child-of-alpha", db.get_task(child)["session_id"])])
        self.assertEqual(os.path.realpath(self.runs_of("alpha")[0]["cwd"]), os.path.realpath(worktree))
        self.assertEqual(states(), [DONE, DONE, DONE])
        self.assertEqual(len(db.runs_for_task(alpha)), 2)

    def test_the_lock_still_refuses_a_tool_that_may_be_running_after_the_retry(self):
        alpha, child, bravo, run, worktree = self.old_database()
        db = self.db()
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual(self.timetrace("retry", str(alpha))[0], 0)
        # something holds the slot right now (a live tool, the agent's job): the retry is refused, not forced
        held = coding_slot_lock(self.home).acquire()
        try:
            code, out, err = self.timetrace("run", "--once")
        finally:
            held.release()
        self.assertEqual(code, 0, err)
        self.assertIn("deferred (runner busy)", out)
        self.assertEqual(db.get_task(alpha)["state"], RUNNABLE)
        self.assertEqual([e["label"] for e in self.exec_log()], ["bravo"])
        # and the same with a crash fence nobody cleared
        (self.home / "locks" / "coding-slot.lock").write_text("4242")
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("manual recovery required", out)
        self.assertEqual(([e["label"] for e in self.exec_log()], db.get_task(alpha)["state"]), (["bravo"], RUNNABLE))
        # cleared by a person: it resumes the original session
        (self.home / "locks" / "coding-slot.lock").write_text("")
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        self.assertEqual([(e["event"], e["label"], e["session"]) for e in self.exec_log()][1:],
                         [("resume", "alpha", "sess-old")])

    def test_retry_says_that_the_earlier_end_was_never_verified(self):
        alpha = self.old_database()[0]
        self.assertEqual(self.timetrace("run", "--once")[0], 0)
        code, out, err = self.timetrace("retry", str(alpha))
        self.assertEqual(code, 0, err)
        self.assertIn("待核对", out + err)
        self.assertIn("sess-old", out + err)


class NotPermittedCliTest(RealCliCase):
    """The real CLI, a real SIGTERM and a real tool, in a kernel that answers EPERM to the group existence probe
    (tests/eperm_cli.py). The tool really ends; the scheduler still may not say so."""

    def test_eperm_keeps_the_exit_unconfirmed_and_the_slot_fenced(self):
        self.script(alpha=dict(hang=True))
        task = self.add("alpha")
        proc = self.spawn_command([sys.executable, str(EPERM_CLI), "run", "--interval", "1"])
        self.wait_for(lambda: self.working("alpha"), "the tool to be executing")
        took = self.stop_with(proc, signal.SIGTERM)
        self.assertLess(took, STOP_BOUND)
        self.assertEqual(proc.returncode, 1, self.cli_text())
        text = self.cli_text()
        self.assertIn("待核对", text)
        self.assertNotIn("stopped (SIGTERM)", text)  # what a confirmed stop prints
        self.assertFalse(self.tool_tree_alive("alpha"), "SIGTERM really ended the tool")
        db = self.db()
        row, run = db.get_task(task), db.latest_run(task)
        self.assertEqual(row["state"], FAILED)
        self.assertIn("待核对", row["last_error"])
        self.assertFalse(claims_stopped(row["last_error"] + run["summary"]), row["last_error"] + run["summary"])
        self.assertIsNone(run["exit_code"])
        self.assertIn("NOT confirmed", run["summary"])
        self.assertEqual([e["payload"]["exit_confirmed"] for e in db.list_events(type_="task_interrupted")], [False])
        self.assertEqual(db.failed_runs_since(0), 0)
        marker = self.slot_marker()
        self.assertNotEqual(marker, "", "the slot lost its crash marker: nothing keeps a second tool out")
        # the ordinary CLI, after an explicit retry, is refused by that marker
        self.assertEqual(self.timetrace("retry", str(task))[0], 0)
        code, out, err = self.timetrace("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("manual recovery required", out)
        self.assertEqual((len(self.runs_of("alpha")), self.slot_marker()), (1, marker))


class RecoverRunningTest(QueueCase):
    """`cli._recover_running`, directly: what it writes for each kind of leftover row."""

    def recover(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._recover_running(self.db)
        return out.getvalue()

    def running(self, label, session="sess-%s", runs=1):
        task = self.add(label)
        self.db.update_task(task, state=RUNNING, tool="claude", session_id=session % label,
                            worktree="/w/%s" % label, branch="timetrace/%s" % label)
        return task, [self.db.add_run(task, "claude", session % label, "", now=10 + n) for n in range(runs)]

    def test_every_running_task_ends_as_unverified_and_waits_for_a_retry(self):
        one, runs_one = self.running("one", runs=2)
        two, runs_two = self.running("two", runs=0)  # an even older row: not a single run
        finished = self.add("finished")
        self.db.update_task(finished, state=DONE, session_id="sess-finished")
        done_run = self.db.add_run(finished, "claude", "sess-finished", "", now=5)
        self.db.finish_run(done_run, 0, blocked=False, summary="all done", now=6)
        waiting = self.add("waiting")
        text = self.recover()
        for task, label in ((one, "one"), (two, "two")):
            row = self.db.get_task(task)
            self.assertEqual(row["state"], FAILED)
            self.assertIn("待核对", row["last_error"])
            self.assertEqual((row["session_id"], row["worktree"], row["branch"]),
                             ("sess-%s" % label, "/w/%s" % label, "timetrace/%s" % label))
            self.assertIn("timetrace retry %d" % task, text)
        for run in self.db.runs_for_task(one):
            self.assertEqual((run["exit_code"], run["interrupted"], run["blocked"]), (None, 1, 0))
            self.assertIsNotNone(run["ended_at"])
            self.assertIn("待核对", run["summary"])
        self.assertEqual(self.db.runs_for_task(two), [])
        self.assertFalse(claims_stopped(text + self.db.get_task(one)["last_error"]), text)
        self.assertNotIn("will resume", text)
        # rows that were not running are untouched
        self.assertEqual(self.db.get_task(finished)["state"], DONE)
        self.assertEqual([(r["exit_code"], r["summary"], r["interrupted"]) for r in self.db.runs_for_task(finished)],
                         [(0, "all done", 0)])
        self.assertEqual(self.db.get_task(waiting)["state"], RUNNABLE)
        self.assertEqual(self.db.failed_runs_since(0), 0)
        events = self.events("task_interrupted")
        self.assertEqual(sorted(e["payload"]["task_id"] for e in events), [one, two])
        self.assertEqual({e["payload"]["exit_confirmed"] for e in events}, {False})
        # and once is enough
        self.assertEqual(self.recover(), "")
        self.assertEqual(len(self.events("task_interrupted")), 2)

    def test_a_row_that_changed_since_it_was_read_is_left_alone(self):
        task, runs = self.running("one")
        stale = self.db.tasks_in_state(RUNNING)
        self.db.finish_run(runs[0], 0, blocked=False, summary="finished meanwhile", now=20)
        self.db.update_task(task, state=DONE)
        with patch.object(self.db, "tasks_in_state", return_value=stale):
            self.assertEqual(self.recover(), "")
        self.assertEqual(self.db.get_task(task)["state"], DONE)
        self.assertEqual([(r["exit_code"], r["summary"], r["interrupted"]) for r in self.db.runs_for_task(task)],
                         [(0, "finished meanwhile", 0)])
        self.assertEqual(self.events("task_interrupted"), [])


class StopRequestProcessTest(unittest.TestCase):
    """`run_streaming` with the queue's stop request, on real process groups."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()

    def test_it_stops_the_group_in_a_bound_and_signals_nothing_but_its_own_group(self):
        spawned, sent = [], []
        real_popen, real_killpg = subprocess.Popen, os.killpg

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc.pid)
            return proc

        def killpg(pgid, sig):
            sent.append((pgid, sig))
            return real_killpg(pgid, sig)
        stop = process.StopRequest()
        threading.Timer(0.5, stop.request, args=("SIGTERM",)).start()
        alive = self.d / "alive.lock"
        script = ("import fcntl,os,time\nf=open(%r,'a+');fcntl.flock(f,fcntl.LOCK_EX)\n"
                  "if os.fork()==0:\n time.sleep(30)\n os._exit(0)\ntime.sleep(30)\n" % str(alive))
        started = time.monotonic()
        with patch("timetrace.process.subprocess.Popen", popen), patch("timetrace.process.os.killpg", killpg):
            code, _ = process.run_streaming([sys.executable, "-c", script], str(self.d), str(self.d / "run.log"),
                                            cancel_event=stop)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertTrue(stop.tool_signalled)
        with open(alive, "a+") as fh:  # the leader and the forked member are both gone
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(sent and {pgid for pgid, _ in sent} == set(spawned), sent)
        self.assertTrue(all(pgid > 1 for pgid, _ in sent))
        self.assertIn("stop requested", (self.d / "run.log").read_text())

    def test_a_tool_that_ended_before_the_signal_is_not_reported_as_signalled(self):
        class Late(process.StopRequest):
            def wait(self, timeout=None):  # the request lands after the tool has already finished by itself
                time.sleep(1.0)
                self.request("SIGINT")
                return True
        stop = Late()
        code, lines = process.run_streaming(["sh", "-c", "echo finished; sleep 0.3"], str(self.d),
                                            str(self.d / "run.log"), cancel_event=stop)
        self.assertEqual((code, lines, stop.tool_signalled), (0, ["finished"], False))

    def test_an_exit_that_cannot_be_confirmed_raises_and_is_never_reported_as_stopped(self):
        stop = process.StopRequest()
        release = self.d / "release"
        script = "import os,time\nwhile not os.path.exists(%r): time.sleep(0.05)\n" % str(release)
        with patch.object(process, "_signal_group", lambda proc, signum: False), \
                patch.object(process, "STOP_GRACE_SECONDS", 0.3), patch.object(process, "STOP_CONFIRM_SECONDS", 0.5), \
                owned_tools(release) as spawned:
            threading.Timer(0.5, stop.request, args=("SIGTERM",)).start()
            started = time.monotonic()
            with self.assertRaises(process.ToolExitUnconfirmed):
                process.run_streaming([sys.executable, "-c", script], str(self.d), str(self.d / "run.log"),
                                      cancel_event=stop)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(len(spawned), 1)
            self.assertIsNone(spawned[0].poll(), "the tool is still running, so the unconfirmed exit was honest")
        # owned_tools has now released the tool, waited for it, finished the reader threads and closed the pipes;
        # only here does the temporary directory (self.tmp) go
        self.assertIsNotNone(spawned[0].returncode)
        log = (self.d / "run.log").read_text()
        self.assertNotIn("--- exit", log)
        self.assertIn("NOT confirmed", log)

    def test_the_helper_that_waits_for_the_tools_fails_a_test_that_leaks_one(self):
        """The guard itself: a tool that does not end by itself is killed and reported, never left behind."""
        with self.assertRaises(AssertionError) as caught:
            with owned_tools(self.d / "never-looked-at", wait=0.5):
                subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],  # recorded: Popen is patched
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertIn("had to be killed", str(caught.exception))

    def test_the_helper_also_fails_a_test_that_leaks_a_descriptor_or_a_thread(self):
        release = self.d / "release"
        keep = []
        with self.assertRaises(AssertionError) as caught:
            with owned_tools(release):
                keep.append(open(self.d / "left-open", "w"))
        self.addCleanup(keep[0].close)
        self.assertIn("descriptors open", str(caught.exception))
        stop = threading.Event()
        with self.assertRaises(AssertionError) as caught:
            with owned_tools(release, wait=0.3):
                worker = threading.Thread(target=stop.wait, args=(30,), daemon=True)
                worker.start()
        stop.set()
        worker.join(5)
        self.assertIn("is still running", str(caught.exception))

    def test_without_a_stop_request_nothing_changes_for_the_cloud_agent(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        code, _ = process.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], str(self.d),
                                        str(self.d / "run.log"), cancel_event=cancel)
        self.assertEqual(code, -signal.SIGTERM)


def kernel_says(*answers):
    """A stand-in for `os.killpg` that answers each call as scripted (the last answer repeats): an exception is
    raised, None means the signal was delivered. Returns (function, calls)."""
    calls = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
    return killpg, calls


NOT_FOUND = ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))
NOT_PERMITTED = PermissionError(errno.EPERM, os.strerror(errno.EPERM))


class GroupConfirmationTest(unittest.TestCase):
    """The tool's process group counts as gone only when the kernel says it does not exist (ESRCH). EPERM says the
    group exists and may not be signalled: that is the opposite of gone, so the exit stays unconfirmed (the slot
    stays fenced). Nothing here signals a real process: `os.killpg` is the scripted stand-in."""
    PROC = SimpleNamespace(pid=424242)

    def emptied(self, *answers, bound=0.3):
        killpg, calls = kernel_says(*answers)
        with patch.object(process, "GROUP_EMPTY_SECONDS", bound), patch("timetrace.process.os.killpg", killpg):
            return process._group_emptied(self.PROC), calls

    def test_only_not_found_confirms_that_the_group_is_gone(self):
        gone, calls = self.emptied(NOT_FOUND)
        self.assertTrue(gone)
        self.assertEqual(calls, [(424242, 0)])  # one existence probe, nothing delivered

    def test_not_permitted_is_not_confirmation_however_long_it_is_asked(self):
        gone, calls = self.emptied(NOT_PERMITTED)
        self.assertFalse(gone)
        self.assertGreater(len(calls), 1, "it should keep looking until the bound for a not-found answer")
        self.assertEqual({sig for _, sig in calls}, {0})

    def test_any_other_answer_is_not_confirmation_either(self):
        for answer in (None, OSError(errno.EINVAL, "invalid"), OSError(errno.EIO, "i/o"), TimeoutError("slow")):
            with self.subTest(answer=answer):
                self.assertFalse(self.emptied(answer)[0])

    def test_not_found_after_not_permitted_is_still_a_confirmation(self):
        gone, calls = self.emptied(NOT_PERMITTED, NOT_PERMITTED, None, NOT_FOUND)
        self.assertTrue(gone)
        self.assertEqual(len(calls), 4)

    def test_a_pid_that_is_not_a_group_of_ours_is_never_confirmed_or_probed(self):
        killpg, calls = kernel_says(NOT_FOUND)
        with patch.object(process, "GROUP_EMPTY_SECONDS", 0.2), patch("timetrace.process.os.killpg", killpg):
            self.assertFalse(process._group_emptied(SimpleNamespace(pid=1)))
            self.assertFalse(process._group_emptied(SimpleNamespace(pid=0)))
        self.assertEqual(calls, [])


class NotPermittedStopTest(unittest.TestCase):
    """A real tool is stopped (SIGTERM really ends it) while the kernel answers EPERM to the existence probe: its
    exit is NOT confirmed, `run_streaming` raises, and nothing says it stopped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()

    @staticmethod
    def eperm_on_probe():
        real = os.killpg

        def killpg(pgid, sig):
            if sig == 0:
                raise NOT_PERMITTED
            return real(pgid, sig)
        return killpg

    def test_run_streaming_raises_and_writes_no_exit_line(self):
        stop = process.StopRequest()
        with patch.object(process, "GROUP_EMPTY_SECONDS", 0.3), patch("timetrace.process.os.killpg", self.eperm_on_probe()), \
                owned_tools(self.d / "release") as spawned:
            threading.Timer(0.3, stop.request, args=("SIGTERM",)).start()
            with self.assertRaises(process.ToolExitUnconfirmed):
                process.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], str(self.d),
                                      str(self.d / "run.log"), cancel_event=stop)
            self.assertTrue(stop.tool_signalled)
            self.assertIsNotNone(spawned[0].poll(), "SIGTERM really ended the tool; what is unproven is its group")
        log = (self.d / "run.log").read_text()
        self.assertIn("NOT confirmed", log)
        self.assertNotIn("--- exit", log)

    def test_a_stop_that_does_not_need_the_probe_is_unchanged(self):
        """The existence probe only matters after a stop request signalled the tool: a tool that ran to its end, and
        the cloud agent's cancel, never ask it."""
        with patch("timetrace.process.os.killpg", self.eperm_on_probe()):
            code, lines = process.run_streaming(["sh", "-c", "echo hi"], str(self.d), str(self.d / "run.log"),
                                                cancel_event=process.StopRequest())
        self.assertEqual((code, lines), (0, ["hi"]))


class SchedulerStopTest(QueueCase):
    """The scheduler's records for a stop, with a scripted tool that answers as run_streaming would."""

    def tick_stop(self, tool, stop, hook_runner=None):
        self.clock += 1
        now = self.clock
        return scheduler.run_once(
            self.db, {"claude": tool}, CFG, self.home, now=now, log=lambda s: None,
            ensure_worktree=self.worktrees, hook_runner=hook_runner or (lambda *a, **k: None), rng=lambda: 0.0,
            clock=lambda: now, stop=stop)

    @staticmethod
    def tool(behaviour):
        class Tool:
            name = "claude"

            def read_limits(self):
                return None

            def capabilities(self):
                return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return behaviour(cancel_event, session_id)
            resume = start
        return Tool()

    def test_a_signalled_tool_that_did_not_finish_is_an_interrupted_task(self):
        task = self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGTERM")
            event.tool_signalled = True
            return RunResult(exit_code=-15, ok=False, error="claude exited with -15", session_id=session)
        out = self.tick_stop(self.tool(behaviour), stop)
        self.assertEqual(out, "task %d → interrupted (SIGTERM)" % task)
        row, run = self.db.get_task(task), self.db.latest_run(task)
        self.assertEqual((row["state"], run["exit_code"], run["interrupted"], run["blocked"]), (FAILED, -15, 1, 0))
        self.assertEqual(row["session_id"], run["session_id"])
        self.assertEqual(self.db.failed_runs_since(0), 0)

    def test_a_signalled_tool_that_finished_ok_is_done_and_never_goes_back(self):
        task = self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGINT")
            event.tool_signalled = True  # it was signalled and still completed its work
            return RunResult(exit_code=0, ok=True, output="all done", session_id=session)
        out = self.tick_stop(self.tool(behaviour), stop)
        self.assertEqual(out, "task %d → done" % task)
        self.assertEqual(self.db.get_task(task)["state"], DONE)
        self.assertEqual(self.db.latest_run(task)["interrupted"], 0)
        self.assertEqual(self.events("task_interrupted"), [])
        self.assertEqual(self.tick_stop(self.tool(behaviour), stop), "stopping")  # and it is not run again
        self.assertEqual(self.db.get_task(task)["state"], DONE)

    def test_a_tool_that_failed_on_its_own_while_a_stop_arrived_is_a_failure_not_an_interruption(self):
        task = self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGTERM")  # tool_signalled stays False: it had already exited by itself
            return RunResult(exit_code=2, ok=False, error="claude exited with 2", session_id=session)
        self.tick_stop(self.tool(behaviour), stop)
        row = self.db.get_task(task)
        self.assertEqual((row["state"], row["last_error"]), (FAILED, "claude exited with 2"))
        self.assertEqual(self.db.latest_run(task)["interrupted"], 0)

    def test_nothing_starts_once_a_stop_is_requested(self):
        task = self.add("one")
        stop = process.StopRequest()
        stop.request("SIGTERM")
        tool = self.tool(lambda e, s: self.fail("a tool was started after the stop request"))
        self.assertEqual(self.tick_stop(tool, stop), "stopping")
        self.assertEqual(self.db.get_task(task)["state"], RUNNABLE)
        self.assertEqual(self.db.runs_for_task(task), [])

    def test_a_stop_between_the_claim_and_the_spawn_takes_the_claim_back(self):
        task = self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGTERM")
            raise DispatchDenied("cancelled")  # what run_streaming's spawn gate raises for a set event
        out = self.tick_stop(self.tool(behaviour), stop)
        self.assertIn("not started", out)
        row = self.db.get_task(task)
        self.assertEqual((row["state"], row["session_id"], row["worktree"]), (RUNNABLE, None, None))
        self.assertEqual(self.db.runs_for_task(task), [])

    def test_an_unconfirmed_exit_is_recorded_for_checking_and_keeps_the_slot_fenced(self):
        task = self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGTERM")
            raise process.ToolExitUnconfirmed("the tool did not exit")
        with self.assertRaises(process.ToolExitUnconfirmed):
            self.tick_stop(self.tool(behaviour), stop)
        row, run = self.db.get_task(task), self.db.latest_run(task)
        self.assertEqual(row["state"], FAILED)
        self.assertIn("待核对", row["last_error"])
        self.assertNotIn("stopped", row["last_error"])
        self.assertIn("NOT confirmed", run["summary"])
        self.assertNotIn("exit confirmed", run["summary"])
        self.assertIsNone(run["exit_code"])
        self.assertEqual(run["interrupted"], 1)
        self.assertEqual([e["payload"]["exit_confirmed"] for e in self.events("task_interrupted")], [False])
        with self.assertRaises(UnclearedOwner):  # the existing lock protection: a person must clear it
            coding_slot_lock(self.home).acquire()

    def test_eperm_from_the_kernel_is_an_unconfirmed_exit_that_keeps_the_slot_fenced(self):
        """The real process runner under the real scheduler: the tool really ends on SIGTERM, but the kernel answers
        EPERM when asked whether its group still exists. That is not "gone": the task and run say 待核对 (no exit
        code, nothing stopped) and the slot keeps its crash marker."""
        task = self.add("one")
        stop = process.StopRequest()

        class Tool:
            name = "claude"

            def read_limits(self):
                return None

            def capabilities(self):
                return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                threading.Timer(0.3, cancel_event.request, args=("SIGTERM",)).start()
                code, _ = process.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], cwd, log_file,
                                                cancel_event=cancel_event)
                return RunResult(exit_code=code, ok=code == 0, session_id=session_id, error="exit %d" % code)
            resume = start
        with patch.object(process, "GROUP_EMPTY_SECONDS", 0.3), \
                patch("timetrace.process.os.killpg", NotPermittedStopTest.eperm_on_probe()), \
                owned_tools(Path(self.home) / "release"):
            with self.assertRaises(process.ToolExitUnconfirmed):
                self.tick_stop(Tool(), stop)
        row, run = self.db.get_task(task), self.db.latest_run(task)
        self.assertEqual(row["state"], FAILED)
        self.assertIn("待核对", row["last_error"])
        self.assertFalse(claims_stopped(row["last_error"] + run["summary"]), row["last_error"] + run["summary"])
        self.assertIsNone(run["exit_code"])
        self.assertEqual([e["payload"]["exit_confirmed"] for e in self.events("task_interrupted")], [False])
        with self.assertRaises(UnclearedOwner):
            coding_slot_lock(self.home).acquire()

    def test_an_adapter_that_takes_no_cancel_event_is_called_exactly_as_before(self):
        task = self.add("one")

        class Plain:
            name = "claude"
            calls = []

            def read_limits(self):
                return None

            def capabilities(self):
                return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

            def start(self, prompt, cwd, session_id, log_file):
                self.calls.append(prompt)
                return RunResult(ok=True, session_id=session_id)
        tool = Plain()
        self.assertEqual(self.tick_stop(tool, process.StopRequest(), hook_runner=lambda *a: None),
                         "task %d → done" % task)
        self.assertEqual(len(tool.calls), 1)

    def test_the_slot_stays_fenced_even_when_the_unconfirmed_stop_cannot_be_written_down(self):
        self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            raise process.ToolExitUnconfirmed("the tool did not exit")
        with patch.object(scheduler, "_record_unconfirmed_stop", side_effect=RuntimeError("database is locked")), \
                self.assertRaises(process.ToolExitUnconfirmed):
            self.tick_stop(self.tool(behaviour), stop)
        with self.assertRaises(UnclearedOwner):
            coding_slot_lock(self.home).acquire()

    def add_with_follow_up(self, label):
        code, out, err = self.cli("add", "--repo", str(self.repo), "--tool", "claude", "--on-success", "list more",
                                  label)
        self.assertEqual(code, 0, err)
        return int(re.search(r"task (\d+) added", out).group(1))

    def test_no_follow_up_tool_is_started_once_a_stop_is_requested(self):
        task = self.add_with_follow_up("one")
        stop = process.StopRequest()
        hooks = []

        def behaviour(event, session):
            stop.request("SIGTERM")  # the stop arrives as the task completes
            return RunResult(exit_code=0, ok=True, output="done", session_id=session)
        out = self.tick_stop(self.tool(behaviour), stop, hook_runner=lambda *a, **k: hooks.append(1))
        self.assertEqual((out, hooks), ("task %d → done" % task, []))
        self.assertEqual(self.db.get_task(task)["state"], DONE)
        self.assertIn("stop requested", self.events("hook_rejected")[0]["payload"]["reason"])

    def test_the_follow_up_tool_is_given_the_stop_request_and_an_unconfirmed_exit_still_fences_the_slot(self):
        task = self.add_with_follow_up("one")
        stop = process.StopRequest()
        seen = []

        def hook(db, adapter, row, home, cfg, now, log_file, cancel_event=None):
            seen.append(cancel_event)
            raise process.ToolExitUnconfirmed("the follow-up tool did not exit")
        with self.assertRaises(process.ToolExitUnconfirmed):
            self.tick_stop(self.tool(lambda e, s: RunResult(exit_code=0, ok=True, session_id=s)), stop, hook_runner=hook)
        self.assertEqual(seen, [stop])
        self.assertEqual(self.db.get_task(task)["state"], DONE)  # the task itself finished
        self.assertEqual([(e["payload"]["phase"], e["payload"]["exit_confirmed"]) for e in self.events("task_interrupted")],
                         [("on_success hook", False)])
        with self.assertRaises(UnclearedOwner):
            coding_slot_lock(self.home).acquire()

    def test_the_hook_does_not_swallow_an_unconfirmed_exit_as_a_crash(self):
        from timetrace import hooks

        class Adapter:
            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                raise process.ToolExitUnconfirmed("still there")
        row = {"id": 1, "on_success": "more", "generation": 0, "worktree": "/w", "session_id": "s", "tool": "claude"}
        with self.assertRaises(process.ToolExitUnconfirmed):
            hooks.run_hook(self.db, Adapter(), row, self.home, CFG, 1, "/dev/null", cancel_event=process.StopRequest())
        self.assertEqual(self.events("hook_rejected"), [])

    def test_a_confirmed_stop_releases_the_slot(self):
        self.add("one")
        stop = process.StopRequest()

        def behaviour(event, session):
            stop.request("SIGTERM")
            event.tool_signalled = True
            return RunResult(exit_code=-15, ok=False, error="x", session_id=session)
        self.tick_stop(self.tool(behaviour), stop)
        coding_slot_lock(self.home).acquire().release()

    def test_three_interrupted_runs_do_not_open_the_circuit(self):
        for n in range(4):
            self.add("task-%d" % n)

        def interrupted(event, session):
            event.tool_signalled = True
            return RunResult(exit_code=-15, ok=False, error="x", session_id=session)
        for _ in range(3):
            self.assertIn("interrupted", self.tick_stop(self.tool(interrupted), process.StopRequest()))
        self.assertEqual(self.db.failed_runs_since(0), 0)
        # the fourth task still starts: three failures in the window would have opened the circuit
        self.assertEqual(self.tick_stop(self.tool(lambda e, s: RunResult(ok=True, session_id=s)),
                                        process.StopRequest()), "task 4 → done")


class RunLoopTest(QueueCase):
    """`timetrace run` in this process (the signal handlers are installed here), with a scripted tool."""

    def test_the_stop_handlers_are_installed_for_the_run_and_restored_after(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        seen = {}
        original = scheduler.run_once

        def run_once(*args, **kwargs):
            seen.update({s: signal.getsignal(s) for s in before})
            return original(*args, **kwargs)
        with patch("timetrace.cli._adapters", lambda cfg: {}), patch.object(scheduler, "run_once", run_once):
            self.assertEqual(self.cli("run", "--once")[0], 0)
        self.assertTrue(all(seen[s] not in (before[s], signal.SIG_DFL) for s in before))
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)

    def test_an_unconfirmed_exit_ends_the_run_with_a_warning_and_is_not_swallowed_as_a_loop_error(self):
        self.add("one")
        calls = []

        def run_once(*args, **kwargs):
            calls.append(1)
            raise process.ToolExitUnconfirmed("the tool did not exit")
        with patch("timetrace.cli._adapters", lambda cfg: {}), patch.object(scheduler, "run_once", run_once):
            code, out, err = self.cli("run", "--once")  # --once: the loop itself must not be what ends this
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)  # it did not go on to start anything else
        self.assertIn("待核对", out + err)
        self.assertNotIn("stopped", out + err)
        self.assertEqual(self.events("loop_error"), [])


if __name__ == "__main__":
    unittest.main()
