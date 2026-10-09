import tempfile
import unittest
from pathlib import Path

from timetrace import config
from timetrace.db import Database
from timetrace.models import PENDING, RUNNABLE, Sample


class PlanTombstoneTest(unittest.TestCase):
    def open(self, path):
        db = Database(path); self.addCleanup(db.close)
        return db

    def test_reopening_releases_plans_whose_failure_was_already_delivered(self):
        # Failures delivered by an older agent left the tombstone behind.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.sqlite"
            db = Database(path)
            db.mark_plan_started("failed-plan", "j1", "a1")
            db.queue_remote_event("j1", "a1", 1, {"seq": 2, "type": "failed"})
            db.conn.execute("UPDATE remote_outbox SET sent_at=1 WHERE job_id='j1'")
            db.mark_plan_started("undelivered-plan", "j2", "a2")
            db.queue_remote_event("j2", "a2", 1, {"seq": 2, "type": "failed"})
            db.mark_plan_started("running-plan", "j3", "a3")
            db.queue_remote_event("j3", "a3", 1, {"seq": 1, "type": "running"})
            db.conn.execute("UPDATE remote_outbox SET sent_at=1 WHERE job_id='j3'")
            db.close()
            db = self.open(path)
            self.assertFalse(db.plan_started("failed-plan"))
            self.assertTrue(db.plan_started("undelivered-plan"))
            self.assertTrue(db.plan_started("running-plan"))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "timetrace.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_sent_outbox_rows_are_pruned_after_the_retention_window(self):
        # Output tails are not kept on disk longer than needed for retries.
        self.db.queue_remote_event("j", "a", 1, {"seq": 1, "output_tail": "old"}, now=100)
        self.db.queue_remote_event("j", "a", 1, {"seq": 2, "output_tail": "recent"}, now=100)
        self.db.queue_remote_event("j", "a", 1, {"seq": 3, "output_tail": "unsent"}, now=100)
        rows = self.db.pending_remote_events()
        self.db.mark_remote_events_sent([rows[0]["id"]], now=1000)
        self.db.mark_remote_events_sent([rows[1]["id"]], now=5000)
        self.assertEqual(self.db.prune_sent_remote_events(before=2000), 1)
        payloads = [r[0] for r in self.db.conn.execute("SELECT payload FROM remote_outbox").fetchall()]
        self.assertEqual(len(payloads), 2)
        self.assertFalse(any("old" in p for p in payloads))
        self.assertEqual([r["seq"] for r in self.db.pending_remote_events()], [3])

    def test_add_task_states(self):
        a = self.db.add_task("first", "/repo")
        b = self.db.add_task("second", "/repo", depends_on=a)
        self.assertEqual(self.db.get_task(a)["state"], RUNNABLE)
        self.assertEqual(self.db.get_task(b)["state"], PENDING)
        self.assertEqual(self.db.get_task(b)["depends_on"], a)

    def test_update_and_list(self):
        a = self.db.add_task("x", "/repo", tool="claude", any_tool=True, priority=5)
        self.db.update_task(a, state="done", session_id="s1")
        t = self.db.get_task(a)
        self.assertEqual(t["state"], "done")
        self.assertEqual(t["session_id"], "s1")
        self.assertEqual(t["any_tool"], 1)
        self.assertEqual(self.db.list_tasks(), [])
        self.assertEqual(len(self.db.list_tasks(include_done=True)), 1)

    def test_bucket_upsert_is_idempotent(self):
        b1 = self.db.upsert_bucket("codex", "codex:codex:primary", 300, False)
        b2 = self.db.upsert_bucket("codex", "codex:codex:primary", None, True)
        self.assertEqual(b1, b2)
        bucket = self.db.get_bucket("codex:codex:primary")
        self.assertEqual(bucket["window_mins"], 300)  # COALESCE keeps old value
        self.assertEqual(bucket["is_representative"], 1)

    def test_latest_samples_one_per_bucket(self):
        s = Sample("claude:five_hour", "claude", 10.0, reset_at=100, window_mins=300)
        self.db.add_sample(s, at=1000)
        s2 = Sample("claude:five_hour", "claude", 20.0, reset_at=100, window_mins=300)
        self.db.add_sample(s2, at=2000)
        self.db.add_sample(Sample("claude:seven_day", "claude", 3.0), at=1500)
        rows = self.db.latest_samples()
        self.assertEqual(len(rows), 2)
        by_key = {r["bucket_key"]: r for r in rows}
        self.assertEqual(by_key["claude:five_hour"]["used_pct"], 20.0)
        self.assertEqual(by_key["claude:five_hour"]["at"], 2000)
        self.assertEqual(by_key["claude:seven_day"]["used_pct"], 3.0)

    def test_failed_runs_since_excludes_blocked_and_success(self):
        t = self.db.add_task("x", "/repo")
        r1 = self.db.add_run(t, "claude", "s", "/log1", now=10)
        self.db.finish_run(r1, exit_code=1, blocked=False, now=20)
        r2 = self.db.add_run(t, "claude", "s", "/log2", now=30)
        self.db.finish_run(r2, exit_code=1, blocked=True, now=40)
        r3 = self.db.add_run(t, "claude", "s", "/log3", now=50)
        self.db.finish_run(r3, exit_code=0, blocked=False, now=60)
        r4 = self.db.add_run(t, "codex", "s", "/log4", now=5)
        self.db.finish_run(r4, exit_code=2, blocked=False, now=6)
        self.assertEqual(self.db.failed_runs_since(10), 1)
        self.assertEqual(self.db.failed_runs_since(0), 2)
        self.assertEqual(self.db.latest_run(t)["id"], r4)

    def test_events_roundtrip(self):
        self.db.add_event("task_done", tool="claude", payload={"task_id": 1}, at=5)
        self.db.add_event("rate_limit", tool="codex", payload={"pct": 100}, at=6)
        ev = self.db.list_events(limit=10)
        self.assertEqual([e["type"] for e in ev], ["rate_limit", "task_done"])
        self.assertEqual(ev[1]["payload"], {"task_id": 1})
        self.assertEqual(len(self.db.list_events(type_="rate_limit")), 1)


class ConfigTest(unittest.TestCase):
    def test_defaults_and_merge(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cfg = config.load(home)
            self.assertEqual(cfg["interval_sec"], 30)
            (home / "config.json").write_text(
                '{"interval_sec": 5, "claude": {"permission_mode": "bypassPermissions"}}'
            )
            cfg = config.load(home)
            self.assertEqual(cfg["interval_sec"], 5)
            self.assertEqual(cfg["claude"]["permission_mode"], "bypassPermissions")
            self.assertEqual(cfg["claude"]["bin"], "claude")
            self.assertEqual(cfg["codex"]["sandbox"], "workspace-write")


if __name__ == "__main__":
    unittest.main()
