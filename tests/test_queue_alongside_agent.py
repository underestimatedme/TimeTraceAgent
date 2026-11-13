"""The local queue (`timetrace run`) beside an online agent (`timetrace agent run`).

Both daemons used to take the process-wide agent.lock exclusively, so the queue could never start while
the agent was online. Now the agent daemon holds agent.lock SHARED (plus its own agent-role.lock, so there is
still one agent) and the queue may run beside it by holding agent.lock shared too, but only inside the SAME
coding-slot limits the agent's own jobs obey, so the combined concurrency is never higher than before.

Every older `timetrace run` / `timetrace agent run` takes agent.lock EXCLUSIVELY and without waiting. While any
newer participant is online that lock attempt fails, so an older queue can neither reset a running task
(`_recover_running`) nor run it again, and there is no gap when the agent leaves. An older agent that already
holds the lock exclusively keeps the new queue out ("upgrade the agent first"): fail closed.
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
# an up-to-date `timetrace agent run`: agent.lock shared + agent-role.lock exclusive
SHARED_AGENT = ("import sys,time\n"
                "sys.path.insert(0,sys.argv[2])\n"
                "from pathlib import Path\n"
                "from timetrace import cli\n"
                "lock=cli._acquire_agent_lock(Path(sys.argv[1]))\n"
                "assert lock is not None\n"
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

    def agent_online(self):
        """An up-to-date `timetrace agent run` daemon in its own process."""
        p = subprocess.Popen([sys.executable, "-I", "-c", SHARED_AGENT, str(self.home), str(Path(cli.__file__).resolve().parents[1])],
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
        self.agent_online()
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
                self.agent_online() if held == "all0" else None
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

    def test_an_older_queue_cannot_even_start_while_this_queue_runs_beside_the_agent_that_just_left(self):
        # the older `timetrace run` takes agent.lock exclusively and only then resets RUNNING tasks and dispatches.
        # Whatever the agent does, this queue keeps agent.lock shared, so that first step fails and nothing of the
        # older queue (reset, stale read, second run) can happen to the task being run
        self.queue_one_task()
        agent = self.agent_online()
        older = {}

        def agent_leaves_then_older_queue_starts():
            agent.kill()
            agent.wait()
            older["lock"] = cli._acquire_execution_lock(self.home)        # exactly what the older cmd_run does first
            older["state_during"] = self.task_state()
        adapter = FakeAdapter("claude", agent_leaves_then_older_queue_starts)
        with patch("timetrace.cli._adapters", lambda cfg: {"claude": adapter}):
            code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIsNone(older["lock"])
        self.assertEqual(older["state_during"], "running")                  # never reset to runnable
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.task_state(), DONE)

    # --- who may run beside whom -------------------------------------------------------------

    def test_queue_runs_beside_an_online_agent(self):
        self.agent_online()
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("beside an online agent", out)

    def test_queue_is_refused_by_an_older_agent_that_holds_the_lock_exclusively(self):
        # the installed 0.6.4 agent: it cannot tell it has company, so the queue stays out until the agent is upgraded
        self.hold("agent.lock", "-m", "timetrace", "agent", "run")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace agent is already running", err)
        self.assertIn("upgrade", err)

    def test_queue_is_refused_when_an_older_queue_holds_the_lock(self):
        self.hold("agent.lock", "-m", "timetrace", "run")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace agent is already running", err)

    def test_two_queues_never_run_at_once(self):
        self.hold("scheduler.lock", "-m", "timetrace", "run")
        code, _, err = self.run_cli("run", "--once")
        self.assertEqual(code, 1)
        self.assertIn("another timetrace scheduler is already running", err)

    def test_a_queue_alone_still_keeps_the_agent_out(self):
        # nobody online: the queue takes agent.lock exclusively, exactly as before, so an agent cannot start beside it
        guard = cli._SchedulerGuard(self.home)
        self.assertIsNone(guard.acquire())
        try:
            self.assertEqual(guard.mode, "exclusive")
            self.assertIsNone(cli._acquire_agent_lock(self.home))
            self.assertIsNone(cli._acquire_execution_lock(self.home))
        finally:
            guard.release()
        again = cli._acquire_agent_lock(self.home)
        self.assertIsNotNone(again)
        again.close()

    def test_a_queue_beside_an_agent_keeps_every_older_process_out_with_no_gap_when_the_agent_leaves(self):
        agent = self.agent_online()
        guard = cli._SchedulerGuard(self.home)
        self.assertIsNone(guard.acquire())
        try:
            self.assertEqual(guard.mode, "beside-agent")
            self.assertIsNone(cli._acquire_execution_lock(self.home))
            agent.kill()
            agent.wait()
            self.assertIsNone(cli._acquire_execution_lock(self.home))         # still shut out: no window for an older queue
        finally:
            guard.release()
        again = cli._acquire_execution_lock(self.home)
        self.assertIsNotNone(again)                                            # and free again once the queue is gone
        again.close()

    def test_agent_and_queue_may_start_in_either_order_and_there_is_only_one_of_each(self):
        queue = cli._SchedulerGuard(self.home)
        agent = cli._acquire_agent_lock(self.home)
        self.assertIsNotNone(agent)
        try:
            self.assertIsNone(cli._acquire_agent_lock(self.home))              # a second agent
            self.assertIsNone(queue.acquire())
            self.assertEqual(queue.mode, "beside-agent")
            second = cli._SchedulerGuard(self.home)
            self.assertEqual(second.acquire(), "error: another timetrace scheduler is already running")
            agent.close()
            self.assertIsNone(cli._acquire_execution_lock(self.home))          # the queue alone still fences older ones
            agent = cli._acquire_agent_lock(self.home)
            self.assertIsNotNone(agent)                                        # a new agent may join the running queue
        finally:
            queue.release()
            if agent:
                agent.close()

    def test_a_stale_task_row_read_before_the_slot_was_free_is_not_run_again(self):
        # run_once reads the runnable rows, then waits for a slot (the queue may be told "busy" and read again later);
        # when the slot comes the task may long be done: the row is re-read under the slot
        db = Database(self.home / "timetrace.db")
        try:
            tid = db.add_task("do it", "/repo", tool="claude")
            stale = db.get_task(tid)
            db.update_task(tid, state=DONE)
            ad = FakeAdapter("claude")
            out = scheduler._dispatch(db, ad, "claude", stale, {}, self.home, 100, lambda s: None, fake_worktree,
                                      lambda *a: None, lambda: 0.0, lambda: 100, SlotLimits(1, {}))
            self.assertEqual(ad.calls, [])
            self.assertIn("no longer runnable", out)
            self.assertEqual(set(self.slot_states().values()), {"free"})
        finally:
            db.close()

    # --- a task that really lost its owner ---------------------------------------------------

    def test_a_task_left_running_by_a_dead_queue_is_recovered_beside_an_agent(self):
        # "recovered" no longer means "resumed by the restart": a missing slot marker is not an authorization. The
        # restart marks the original record 待核对 and waits; the explicit retry then runs it, beside the agent
        self.queue_one_task()
        db = Database(self.home / "timetrace.db")
        db.update_task(1, state="running", session_id="sess-1")
        db.close()
        self.agent_online()
        adapter = FakeAdapter("claude")
        with patch("timetrace.cli._adapters", lambda cfg: {"claude": adapter}):
            code, out, err = self.run_cli("run", "--once")
            self.assertEqual(code, 0, err)
            self.assertIn("was running when the scheduler ended without a record", out)
            self.assertIn("待核对", out)
            self.assertEqual(adapter.calls, [])                                 # not run again by itself
            self.assertEqual(self.task_state(), "failed")
            code, out, err = self.run_cli("retry", "1")                         # a person's decision
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(adapter.calls[0].startswith("You were interrupted"), adapter.calls[0])  # the recorded session
        self.assertEqual(self.task_state(), DONE)

    def test_a_crash_fence_left_by_the_dead_owner_defers_until_cleared_then_resumes(self):
        self.queue_one_task()
        db = Database(self.home / "timetrace.db")
        db.update_task(1, state="running", session_id="sess-1")
        db.close()
        marker = tool_slot_lock(self.home, "claude", 0)
        marker.acquire()
        os.close(marker._fd)                    # the owner died: the OS lock is gone, the marker stays
        marker._fd = None
        fence = Path(marker.path).read_text()
        self.agent_online()
        adapter = FakeAdapter("claude")
        with patch("timetrace.cli._adapters", lambda cfg: {"claude": adapter}):
            code, out, err = self.run_cli("run", "--once")
            self.assertEqual(code, 0, err)
            self.assertEqual(adapter.calls, [])                                 # the restart does not run it
            self.assertEqual(self.task_state(), "failed")                       # 待核对, waiting for a retry
            self.assertEqual(Path(marker.path).read_text(), fence)              # and does not touch the fence
            code, out, err = self.run_cli("retry", "1")
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli("run", "--once")
            self.assertEqual(code, 0, err)
            self.assertIn("deferred", out)                                      # the fence still refuses the retry
            self.assertEqual(adapter.calls, [])
            self.assertEqual(self.task_state(), "runnable")                     # recoverable, not lost
            Path(marker.path).write_text("")                                    # the operator verified and cleared it
            code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(self.task_state(), DONE)


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
