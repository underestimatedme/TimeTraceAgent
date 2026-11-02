import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace.agent import Agent
from timetrace.db import Database
from timetrace import pause, runner_state


class HeartbeatRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.inventories = []
        class Cloud:
            def update_inventory(_, *args, **kwargs):
                self.inventories.append(kwargs)
        self.cloud = Cloud()
        self.agent = Agent(self.db, self.cloud, {}, self.home, lambda: "token",
                           report_protocol=True, inventory=lambda: ([], []))

    def test_unchanged_paused_agent_republishes_presence_every_30_seconds(self):
        pause.set_paused(self.home, True)
        for now in [1000, 1029, 1030, 1059, 1060]:
            self.agent.maintain(now=now)
        self.assertEqual(len(self.inventories), 3)
        self.assertTrue(all(not x["extras"]["accepting_local"] for x in self.inventories))
        self.assertEqual(runner_state.load(self.home)["inventory_pushed_at"], 1060)

    def test_failed_inventory_does_not_advance_heartbeat_success_time(self):
        self.agent.maintain(now=1000)
        upload = self.cloud.update_inventory
        def offline(*args, **kwargs):
            raise OSError("offline")
        self.cloud.update_inventory = offline
        self.agent.maintain(now=1030)
        self.assertEqual(runner_state.load(self.home)["inventory_pushed_at"], 1000)
        self.cloud.update_inventory = upload
        self.agent.maintain(now=1031)
        self.assertEqual(runner_state.load(self.home)["inventory_pushed_at"], 1031)

    def test_both_loops_maintain_presence_before_repeated_outbox_errors(self):
        for parallel in [False, True]:
            with self.subTest(parallel=parallel):
                calls = []
                agent = Agent(self.db, self.cloud, {}, self.home, lambda: "token",
                              max_parallel=2 if parallel else 1)
                def maintain(*args, **kwargs):
                    calls.append("heartbeat")
                def fail():
                    calls.append("outbox")
                    if calls.count("outbox") == 3:
                        agent.stop()
                    raise OSError("old result could not be delivered")
                agent.maintain = maintain
                agent.flush_outbox = fail
                with patch("timetrace.runtime_preview.RuntimePreviews.start"):
                    agent.run_forever(interval=0, log=lambda message: None)
                self.assertEqual(calls, ["heartbeat", "outbox"] * 3)


class ExistingHeartbeatContractTest(unittest.TestCase):
    """S2-7's heartbeat fixes, pinned on the three places the review found missing from the a07 candidate (fake cloud, temporary
    database; no Agent process, runtime, network or real database)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "synthetic.db")
        self.addCleanup(self.db.close)
        self.order = []
        owner = self

        class FakeCloud:
            def append_events(_, *args):
                owner.order.append("upload")

            def claim(_, *args):
                owner.order.append("claim")
                owner.agent.stop()
                return None

            def update_inventory(_, *args, **kwargs):
                owner.order.append("presence")

        self.agent = Agent(self.db, FakeCloud(), {}, self.home, lambda: "synthetic-token",
                           report_protocol=True, inventory=lambda: ([], []), log=lambda _: None)
        self.agent.disk_blocked = lambda: False
        self.agent.locally_paused = lambda: False

    def test_serial_startup_maintains_before_upload_or_claim(self):
        self.db.queue_remote_event("synthetic", "attempt", 3, {"seq": 1, "type": "failed"})
        self.agent.maintain = lambda *a, **k: self.order.append("presence")
        self.agent.run_forever(interval=0)
        self.assertEqual(self.order[0], "presence", self.order)

    def test_parallel_remote_config_hold_does_not_skip_maintenance(self):
        def hold(agent, active):
            self.order.append("config_hold")
            agent.stop()
            return True
        self.agent.remote_config = hold
        self.agent.maintain = lambda *a, **k: self.order.append("presence")
        with patch("timetrace.runtime_preview.RuntimePreviews.start"), patch("timetrace.runtime_preview.RuntimePreviews.stop"):
            self.agent.run_forever(interval=0)
        self.assertEqual(self.order[0], "presence", self.order)

    def test_a_full_parallel_agent_keeps_maintaining_presence_while_its_job_runs(self):
        # Waiting for a slot must not be what keeps the heartbeat from being sent: with one slot taken by a job that does not end,
        # the loop still maintains presence on every round.
        import threading
        release = threading.Event()
        started = threading.Event()
        self.agent.max_parallel = 2
        self.agent.maintain = lambda *a, **k: self.order.append("presence")
        self.agent.disk_blocked = lambda: False
        claims = [{"job": {"id": "synthetic"}, "attempt_id": "attempt"}]
        self.agent.max_parallel = 1

        def next_claim():
            if claims:
                return claims.pop()
            raise AssertionError("a full agent must not claim again")

        def handle(claim):
            started.set()
            release.wait(10)
            return "job synthetic → done"
        self.agent._next_claim = next_claim
        self.agent.handle = handle
        runner = threading.Thread(target=lambda: self._run_parallel(), daemon=True)
        self.addCleanup(release.set)
        self.addCleanup(self.agent.stop)
        with patch("timetrace.runtime_preview.RuntimePreviews.start"), patch("timetrace.runtime_preview.RuntimePreviews.stop"):
            runner.start()
            self.assertTrue(started.wait(5), "the job started")
            deadline = time.time() + 5
            while self.order.count("presence") < 3 and time.time() < deadline:
                time.sleep(0.02)
            presence_while_running = self.order.count("presence")
            release.set()
            self.agent.stop()
            runner.join(10)
        self.assertGreaterEqual(presence_while_running, 3, self.order)

    def test_an_upkeep_asked_for_by_a_worker_thread_is_run_forced_on_the_next_round(self):
        # A worker never runs upkeep itself (maintain() on another thread only sets the flag); the loop must then run it forced.
        calls = []
        self.agent._maintain_due = True

        def maintain(*args, **kwargs):
            calls.append(kwargs.get("force", False))
            self.agent.stop()
        holds = []

        def hold(agent, active):
            holds.append(1)
            if len(holds) >= 3:  # whatever the loop does with upkeep, this test must end by itself
                agent.stop()
            return True
        self.agent.maintain = maintain
        self.agent.remote_config = hold
        with patch("timetrace.runtime_preview.RuntimePreviews.start"), patch("timetrace.runtime_preview.RuntimePreviews.stop"):
            self.agent.run_forever(interval=0)
        self.assertEqual(calls[0], True, "the first upkeep after a worker asked for one is forced")
        self.assertFalse(self.agent._maintain_due, "the request is consumed")

    def _run_parallel(self):
        self.agent.remote_config = None
        self.agent._run_parallel(0.01, lambda message: None)

    def test_unchanged_protocol_two_inventory_still_pushes_after_heartbeat_interval(self):
        self.agent._refresh_health = lambda *a: None
        self.agent._refresh_reset_credits = lambda *a: None
        self.agent.report_quota_counts = lambda *a: {}
        self.agent.inventory_extras = lambda: {"protocol_version": 2}
        self.agent.maintain(now=1000)
        self.agent.maintain(now=1000 + self.agent.heartbeat_interval + 1)
        self.assertEqual(self.order, ["presence", "presence"])

    def test_an_unchanged_inventory_is_not_pushed_again_inside_the_heartbeat_interval(self):
        self.agent._refresh_health = lambda *a: None
        self.agent._refresh_reset_credits = lambda *a: None
        self.agent.report_quota_counts = lambda *a: {}
        self.agent.inventory_extras = lambda: {"protocol_version": 2}
        self.agent.maintain(now=1000)
        self.agent.maintain(now=1000 + self.agent.heartbeat_interval - 1)
        self.assertEqual(self.order, ["presence"])

    def test_a_protocol_one_runner_is_not_given_a_heartbeat_push(self):
        quiet = Agent(self.db, self.agent.cloud, {}, self.home, lambda: "token", report_protocol=False,
                      inventory=lambda: ([], []), log=lambda _: None)
        quiet._refresh_health = lambda *a: None
        quiet._refresh_reset_credits = lambda *a: None
        quiet.report_quota_counts = lambda *a: {}
        quiet.maintain(now=1000)
        quiet.maintain(now=1000 + quiet.heartbeat_interval + 1)
        self.assertEqual(self.order, ["presence"], "only an unchanged protocol-2 inventory is repeated as a heartbeat")

