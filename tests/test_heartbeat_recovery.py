import tempfile
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
