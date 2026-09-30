"""Daemon shutdown: SIGTERM/SIGINT stop claiming, cancel running jobs, release locks."""
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import agent as agent_module
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.dispatch import coding_slot_lock, plan_lock
from timetrace.models import RunResult
from tests.test_parallel import QueueCloud, init_repo


class WaitingAdapter:
    """start() runs until its cancel event is set (as run_streaming would
    after killing the process group)."""

    def __init__(self):
        self.entered = threading.Event()
        self.cancelled = []
        self.runs = 0
        self.lock = threading.Lock()

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        with self.lock:
            self.runs += 1
        self.entered.set()
        self.cancelled.append(cancel_event.wait(30))
        return RunResult(exit_code=-15, ok=False, error="terminated", session_id=session_id)

    resume = start
    start_folder = None

    def chat(self, prompt, cwd, session_id, log_file, cancel_event=None):
        return self.start(prompt, cwd, session_id, log_file, cancel_event)


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.home = self.d / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, cloud, adapter, **kwargs):
        return Agent(self.db, cloud, {"codex": adapter}, self.home, lambda: "token", **kwargs)

    def assert_locks_free(self, plan="p1"):
        coding_slot_lock(self.home).acquire().release()
        plan_lock(self.home, plan).acquire().release()

    def test_stop_cancels_a_running_job_and_reports_it(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, adapter)
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(agent.run_once()))
        worker.start()
        self.assertTrue(adapter.entered.wait(10))
        started = time.monotonic()
        agent.stop()
        worker.join(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(adapter.cancelled, [True])
        self.assertEqual(outcome, ["job j1 → blocked (runner_stopped)"])
        last = cloud.events[-1][1]
        self.assertEqual(last["type"], "waiting_input")
        self.assertIn("runner_stopped", last["message"])
        self.assert_locks_free()

    def test_a_job_claimed_after_stop_never_spawns(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, adapter)
        agent.stop()
        outcome = agent.handle(cloud.claim("t"))
        self.assertFalse(adapter.entered.is_set())
        self.assertEqual(outcome, "job j1 → blocked (runner_stopped)")
        self.assert_locks_free()

    def test_sequential_loop_returns_once_stopped(self):
        cloud = QueueCloud([])
        agent = self.agent(cloud, WaitingAdapter())
        threading.Timer(.3, agent.stop).start()
        started = time.monotonic()
        agent.run_forever(interval=60, log=lambda message: None)
        self.assertLess(time.monotonic() - started, 5)

    def test_parallel_loop_stops_claiming_and_cancels_workers(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, adapter, max_parallel=2, workspace_wait=10)
        done = threading.Event()

        def run():
            agent.run_forever(interval=60, log=lambda message: None)
            done.set()
        loop = threading.Thread(target=run, daemon=True)
        loop.start()
        for _ in range(200):
            if adapter.runs == 2:
                break
            time.sleep(.05)
        self.assertEqual(adapter.runs, 2)
        started = time.monotonic()
        agent.stop()
        self.assertTrue(done.wait(15))
        self.assertLess(time.monotonic() - started, agent_module.SHUTDOWN_GRACE_SECONDS)
        stopped = [job for job, e in cloud.events if e["type"] == "waiting_input" and "runner_stopped" in e["message"]]
        self.assertEqual(sorted(stopped), ["j1", "j2"])
        self.assert_locks_free("p1")
        self.assert_locks_free("p2")

    def test_ctrl_c_in_parallel_loop_does_not_wait_for_the_job(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, adapter, max_parallel=2)
        original = cloud.claim

        def claim(token):
            answer = original(token)
            if answer is None:
                adapter.entered.wait(10)
                raise KeyboardInterrupt()
            return answer
        cloud.claim = claim
        started = time.monotonic()
        with patch("timetrace.agent.time.sleep"), self.assertRaises(KeyboardInterrupt):
            agent.run_forever(interval=0, log=lambda message: None)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(adapter.cancelled, [True])
        self.assert_locks_free()

    def test_ctrl_c_in_sequential_loop_cancels_the_running_job(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, adapter, heartbeat_interval=.1)
        renew = agent._renew

        def interrupted(*args):
            if adapter.entered.is_set():
                raise KeyboardInterrupt()
            return renew(*args)
        agent._renew = interrupted
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            agent.run_forever(interval=0, log=lambda message: None)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(adapter.cancelled, [True])
        self.assert_locks_free()

    def test_signal_handlers_stop_the_agent(self):
        agent = self.agent(QueueCloud([]), WaitingAdapter())
        previous = agent_module.install_stop_handlers(agent)
        try:
            self.assertEqual(set(previous), {signal.SIGTERM, signal.SIGINT})
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(50):
                if agent.stopping:
                    break
                time.sleep(.02)
            self.assertTrue(agent.stopping)
        finally:
            agent_module.restore_handlers(previous)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous[signal.SIGTERM])

    def test_stopped_real_process_group_is_killed(self):
        from timetrace.process import run_streaming
        cancel = threading.Event()
        log = self.d / "run.log"
        pidfile = self.d / "child.pid"
        threading.Timer(.5, cancel.set).start()
        started = time.monotonic()
        run_streaming(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > '%s'; wait" % pidfile],
                      str(self.d), str(log), cancel_event=cancel)
        self.assertLess(time.monotonic() - started, 6)
        pid = int(pidfile.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("background child survived the cancel")


if __name__ == "__main__":
    unittest.main()
