"""The local queue (`timetrace run`) beside an online agent (`timetrace agent run`).

Both daemons used to take the process-wide agent.lock, so the queue could never start while the
agent was online. The agent daemon keeps agent.lock; the queue may run beside it, but only inside
the SAME coding-slot limits the agent's own jobs obey (tool slot, then overall slot), so the
combined concurrency is never higher than before. Anything that is not a running `agent run`
holding agent.lock still keeps the queue out, and two queues never run at once.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, scheduler
from timetrace.db import Database
from timetrace.dispatch import (LockBusy, SlotLimits, UnclearedOwner, coding_slot_lock, tool_slot_lock)
from timetrace.models import DONE, RunResult

HOLDER = ("import fcntl,sys,time\n"
          "f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
          "print('held',flush=True);time.sleep(600)\n")


class FakeAdapter:
    def __init__(self, name, probe=None):
        self.name, self.calls, self.probe = name, [], probe

    def read_limits(self):
        return None

    def capabilities(self):
        return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

    def start(self, prompt, cwd, session_id, log_file):
        self.calls.append(prompt)
        if self.probe:
            self.probe()
        return RunResult(ok=True, session_id=session_id)

    resume = start


def fake_worktree(repo, task_id, home, base="HEAD"):
    p = Path(home) / "worktrees" / str(task_id)
    p.mkdir(parents=True, exist_ok=True)
    return str(p), "timetrace/%d" % task_id


class QueueAlongsideAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "codex": {"bin": "/nonexistent/codex"}, "claude": {"bin": "/nonexistent/claude"},
            "max_parallel": 4, "max_parallel_per_tool": {"claude": 2, "codex": 2}}))
        self._old = os.environ.get("TIMETRACE_HOME")
        os.environ["TIMETRACE_HOME"] = str(self.home)
        self.holders = []
        for target, value in (("timetrace.cli.CredentialStore.load", lambda self, account="default": None),
                              ("timetrace.cli._discover_tools", lambda home, user_home=None: (cli.config.load(home), {}))):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for p in self.holders:
            p.kill()
            p.wait()
            p.stdout.close()
        if self._old is None:
            os.environ.pop("TIMETRACE_HOME", None)
        else:
            os.environ["TIMETRACE_HOME"] = self._old
        self.tmp.cleanup()

    def hold(self, lock_name, *argv_tail):
        """A separate process holding `lock_name` the way a daemon would; its command line carries argv_tail."""
        p = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(self.home / lock_name), *argv_tail],
                             stdout=subprocess.PIPE, text=True)
        self.holders.append(p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def queue_one_task(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        for argv in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
            subprocess.run(["git", *argv], cwd=repo, check=True)
        (repo / "a").write_text("a")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        db = Database(self.home / "timetrace.db")
        db.add_task("do it", str(repo), tool="claude")
        db.close()

    def task_state(self):
        db = Database(self.home / "timetrace.db")
        try:
            return db.get_task(1)["state"]
        finally:
            db.close()

    def slot_states(self):
        out = {}
        for name, lock in (("tool0", tool_slot_lock(self.home, "claude", 0)), ("tool1", tool_slot_lock(self.home, "claude", 1)),
                           ("all0", coding_slot_lock(self.home, 0)), ("all1", coding_slot_lock(self.home, 1)),
                           ("all3", coding_slot_lock(self.home, 3))):
            try:
                lock.acquire().release()
                out[name] = "free"
            except LockBusy:
                out[name] = "held"
        return out

    def test_a_queued_task_beside_an_agent_holds_exactly_tool_0_and_overall_0(self):
        # whatever limits are configured (here 8 / 4 per tool), the queue never reaches for a higher slot: the
        # agent's limits can be lowered remotely while it runs, and slot 0 is what an older queue fences on
        cfg = json.loads((self.home / "config.json").read_text())
        cfg.update({"max_parallel": 8, "max_parallel_per_tool": {"claude": 4, "codex": 4}})
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.queue_one_task()
        self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        seen = {}
        with patch("timetrace.cli._adapters", lambda cfg: {"claude": FakeAdapter("claude", lambda: seen.update(self.slot_states()))}):
            code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, {"tool0": "held", "tool1": "free", "all0": "held", "all1": "free", "all3": "free"})
        self.assertEqual(self.task_state(), DONE)
        self.assertEqual(set(self.slot_states().values()), {"free"})          # and everything is given back

    def test_the_queue_waits_while_an_agent_job_holds_overall_slot_0_or_the_tools_slot_0(self):
        for held in ("all0", "tool0"):
            with self.subTest(agent_holds=held):
                self.queue_one_task() if held == "all0" else None
                db = Database(self.home / "timetrace.db")
                db.update_task(1, state="runnable")
                db.close()
                self.hold("agent.lock", "-m", "timetrace", "agent", "run") if held == "all0" else None
                lock = (coding_slot_lock(self.home, 0) if held == "all0" else tool_slot_lock(self.home, "claude", 0)).acquire()
                started = []
                try:
                    with patch("timetrace.cli._adapters", lambda cfg: {"claude": FakeAdapter("claude", lambda: started.append(1))}):
                        code, out, err = self.run_cli("run", "--once")
                finally:
                    lock.release()
                self.assertEqual(code, 0, err)
                self.assertEqual(started, [])                                  # nothing ran beside the agent's job
                self.assertIn("deferred", out)
                self.assertEqual(self.task_state(), "runnable")

    def test_an_older_queue_cannot_run_the_task_the_queue_is_running_beside_the_agent(self):
        # the older `timetrace run` resets RUNNING tasks first and then fences on overall slot 0 only: while this
        # queue holds slot 0 (the agent having just left), that older queue defers instead of running the task again
        self.queue_one_task()
        self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        older = {}

        def older_queue_starts():
            db = Database(self.home / "timetrace.db")
            for t in db.tasks_in_state("running"):                              # what the older _recover_running does
                db.update_task(t["id"], state="runnable", last_error="daemon restarted mid-run")
            ad = FakeAdapter("claude")
            older["out"] = scheduler.run_once(db, {"claude": ad}, {"interval_sec": 1, "jitter_sec": 0, "hook_max_tasks": 5,
                                              "default_block_sleep_sec": 600, "circuit_breaker_failures": 3, "circuit_window_mins": 300},
                                              self.home, log=lambda s: None)
            older["calls"] = ad.calls
            db.close()
        with patch("timetrace.cli._adapters", lambda cfg: {"claude": FakeAdapter("claude", older_queue_starts)}):
            code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(older["out"], "task 1 → deferred (runner busy)")
        self.assertEqual(older["calls"], [])
        self.assertEqual(self.task_state(), DONE)       # the reset did not stick: this queue's result is the record

    # --- who may run beside whom -------------------------------------------------------------

    def test_queue_runs_beside_an_online_agent(self):
        self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("beside an online agent", out)

    def test_queue_is_refused_when_the_lock_holder_is_not_an_agent(self):
        # e.g. a 0.6.x `timetrace run` daemon: its agent.lock must keep the queue out, as before
        self.hold("agent.lock", "-m", "timetrace", "run")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace agent is already running", err)

    def test_queue_is_refused_when_an_unrelated_process_holds_the_lock(self):
        self.hold("agent.lock", "something-else")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace agent is already running", err)

    def test_two_queues_never_run_at_once(self):
        self.hold("scheduler.lock", "-m", "timetrace", "run")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace scheduler is already running", err)

    def test_a_queue_alone_still_keeps_the_agent_out(self):
        # nobody online: the queue takes agent.lock itself, exactly as before, so an agent cannot start beside it
        guard = cli._SchedulerGuard(self.home)
        self.assertIsNone(guard.acquire())
        try:
            self.assertEqual(guard.mode, "exclusive")
            self.assertIsNone(cli._acquire_execution_lock(self.home))
        finally:
            guard.release()
        again = cli._acquire_execution_lock(self.home)
        self.assertIsNotNone(again)
        again.close()

    def test_a_queue_beside_an_agent_takes_over_agent_lock_when_the_agent_leaves(self):
        agent = self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        guard = cli._SchedulerGuard(self.home)
        self.assertIsNone(guard.acquire())
        try:
            self.assertEqual(guard.mode, "beside-agent")
            agent.kill()
            agent.wait()
            self.assertIsNone(guard.refresh())
            self.assertEqual(guard.mode, "exclusive")        # an old scheduler cannot slip in behind the leaving agent
            self.assertIsNone(cli._acquire_execution_lock(self.home))
        finally:
            guard.release()

    def test_beside_an_agent_a_new_non_agent_holder_stops_the_queue(self):
        agent = self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        guard = cli._SchedulerGuard(self.home)
        self.assertIsNone(guard.acquire())
        try:
            agent.kill()
            agent.wait()
            self.hold("agent.lock", "-m", "timetrace", "run")      # something else took the lock in between
            self.assertEqual(guard.refresh(), "error: another timetrace agent is already running")
        finally:
            guard.release()


class QueueSlotsTest(unittest.TestCase):
    """The queue fences a task through the same tool slot + overall slot the agent's jobs use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.cfg = {"interval_sec": 1, "jitter_sec": 0, "default_block_sleep_sec": 600,
                    "circuit_breaker_failures": 3, "circuit_window_mins": 300, "hook_max_tasks": 5}
        self.limits = SlotLimits(overall=4, per_tool={"claude": 2, "codex": 2})
        self.held = []

    def tearDown(self):
        for lock in self.held:
            lock.release()
        self.db.close()
        self.tmp.cleanup()

    def take(self, lock):
        self.held.append(lock.acquire())
        return lock

    def run_once(self, adapters, slots="default"):
        return scheduler.run_once(self.db, adapters, self.cfg, self.home, now=100, log=lambda s: None,
                                  ensure_worktree=fake_worktree, hook_runner=lambda *a: None,
                                  rng=lambda: 0.0, clock=lambda: 100,
                                  **({} if slots == "default" else {"slots": slots}))

    def test_without_limits_the_queue_still_uses_slot_zero_only(self):
        self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude")
        self.take(coding_slot_lock(self.home))
        self.assertEqual(self.run_once({"claude": ad}), "task 1 → deferred (runner busy)")
        self.assertEqual(ad.calls, [])

    def test_a_task_takes_one_tool_slot_and_one_overall_slot_the_agent_shares(self):
        self.db.add_task("do", "/repo", tool="claude")
        seen = {}

        def probe():
            for name, lock in (("tool0", tool_slot_lock(self.home, "claude", 0)), ("tool1", tool_slot_lock(self.home, "claude", 1)),
                               ("all0", coding_slot_lock(self.home, 0)), ("all1", coding_slot_lock(self.home, 1))):
                try:
                    lock.acquire().release()
                    seen[name] = "free"
                except LockBusy:
                    seen[name] = "held"
        ad = FakeAdapter("claude", probe)
        self.assertIn("done", self.run_once({"claude": ad}, self.limits))
        self.assertEqual(seen, {"tool0": "held", "tool1": "free", "all0": "held", "all1": "free"})
        # released afterwards: nothing is left taken
        for lock in (tool_slot_lock(self.home, "claude", 0), coding_slot_lock(self.home, 0)):
            lock.acquire().release()

    def test_the_queue_defers_when_the_agent_has_every_slot_of_the_tool(self):
        self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude")
        for i in range(2):
            self.take(tool_slot_lock(self.home, "claude", i))
        self.assertEqual(self.run_once({"claude": ad}, self.limits), "task 1 → deferred (tool busy: claude)")
        self.assertEqual(ad.calls, [])
        # and it did not leave a slot taken behind it
        coding_slot_lock(self.home, 0).acquire().release()

    def test_the_queue_defers_when_the_agent_has_every_overall_slot(self):
        self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude")
        for i in range(4):
            self.take(coding_slot_lock(self.home, i))
        self.assertEqual(self.run_once({"claude": ad}, self.limits), "task 1 → deferred (runner busy)")
        self.assertEqual(ad.calls, [])
        for i in range(2):                                    # the tool slot taken first was given back
            tool_slot_lock(self.home, "claude", i).acquire().release()

    def test_the_queue_uses_the_first_free_overall_slot(self):
        self.db.add_task("do", "/repo", tool="claude")
        self.take(coding_slot_lock(self.home, 0))             # an agent job holds slot 0
        seen = {}

        def probe():
            for i in range(4):
                try:
                    coding_slot_lock(self.home, i).acquire().release()
                    seen[i] = "free"
                except LockBusy:
                    seen[i] = "held"
        self.assertIn("done", self.run_once({"claude": FakeAdapter("claude", probe)}, self.limits))
        self.assertEqual(seen, {0: "held", 1: "held", 2: "free", 3: "free"})

    def test_combined_with_an_agent_the_limits_are_never_exceeded(self):
        # the agent's jobs hold what they hold; the queue only ever adds ONE task inside what is left
        self.db.add_task("do", "/repo", tool="claude")
        self.take(tool_slot_lock(self.home, "claude", 0))
        for i in range(3):
            self.take(coding_slot_lock(self.home, i))
        taken = []

        def probe():
            for i in range(2):
                try:
                    tool_slot_lock(self.home, "claude", i).acquire().release()
                except LockBusy:
                    taken.append(("tool", i))
            for i in range(4):
                try:
                    coding_slot_lock(self.home, i).acquire().release()
                except LockBusy:
                    taken.append(("all", i))
        self.assertIn("done", self.run_once({"claude": FakeAdapter("claude", probe)}, self.limits))
        self.assertEqual(sorted(taken), [("all", 0), ("all", 1), ("all", 2), ("all", 3), ("tool", 0), ("tool", 1)])

    # --- recovery: a crash leaves a fence on the slots it held; the queue goes around it ----------------

    def fence(self, lock):
        """What an unclean exit leaves: the lock file keeps its owner marker while no process holds the lock."""
        lock.acquire()
        os.close(lock._fd)          # the OS lock goes with the descriptor; the marker stays
        lock._fd = None

    def test_a_crash_fence_on_one_slot_is_skipped_for_the_next_free_one(self):
        self.db.add_task("do", "/repo", tool="claude")
        self.fence(tool_slot_lock(self.home, "claude", 0))
        self.fence(coding_slot_lock(self.home, 0))
        seen = {}

        def probe():
            for name, lock in (("tool1", tool_slot_lock(self.home, "claude", 1)), ("all1", coding_slot_lock(self.home, 1))):
                try:
                    lock.acquire().release()
                    seen[name] = "free"
                except LockBusy:
                    seen[name] = "held"
        self.assertIn("done", self.run_once({"claude": FakeAdapter("claude", probe)}, self.limits))
        self.assertEqual(seen, {"tool1": "held", "all1": "held"})
        # the fences are still there for manual recovery: nothing cleared them
        with self.assertRaises(UnclearedOwner):
            tool_slot_lock(self.home, "claude", 0).acquire()

    def test_when_every_slot_of_the_tool_is_fenced_the_queue_names_the_fence(self):
        self.db.add_task("do", "/repo", tool="claude")
        for i in range(2):
            self.fence(tool_slot_lock(self.home, "claude", i))
        ad = FakeAdapter("claude")
        out = self.run_once({"claude": ad}, self.limits)
        self.assertTrue(out.startswith("task 1 → deferred (tool busy: claude)"), out)
        self.assertIn("manual recovery required", out)
        self.assertEqual(ad.calls, [])


if __name__ == "__main__":
    unittest.main()
