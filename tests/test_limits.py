import tempfile
import unittest
from pathlib import Path

from timetrace import limits
from timetrace.db import Database
from timetrace.models import Sample


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "timetrace.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed(self):
        limits.record_samples(self.db, [
            Sample("claude:five_hour", "claude", 13.0, reset_at=100, window_mins=300,
                   is_representative=True),
            Sample("claude:seven_day", "claude", 3.0, reset_at=200, window_mins=10080),
            Sample("codex:codex:primary", "codex", 100, reset_at=10 ** 10, window_mins=300),
            Sample("codex:codex:secondary", "codex", 47, reset_at=400, window_mins=10080),
            Sample("codex:base_model_inference:primary", "codex", 6, reset_at=500,
                   window_mins=10080),
        ], at=1000)

    def test_snapshot_and_binding(self):
        self._seed()
        rows = limits.snapshot(self.db)
        self.assertEqual(len(rows), 5)
        b = limits.binding(rows)
        self.assertEqual(b["bucket_key"], "codex:codex:primary")
        self.assertEqual(b["remaining_pct"], 0.0)

    def test_binding_prefers_representative_on_tie(self):
        limits.record_samples(self.db, [
            Sample("claude:seven_day", "claude", 50.0),
            Sample("claude:five_hour", "claude", 50.0, is_representative=True),
        ], at=1)
        b = limits.binding(limits.snapshot(self.db))
        self.assertEqual(b["bucket_key"], "claude:five_hour")

    def test_exhausted_only_at_100(self):
        self._seed()
        rows = limits.snapshot(self.db)
        self.assertTrue(limits.tool_exhausted(rows, "codex"))
        self.assertFalse(limits.tool_exhausted(rows, "claude"))
        self.assertFalse(limits.tool_exhausted(rows, "gemini"))
        limits.record_samples(self.db, [Sample("codex:codex:primary", "codex", 99.9)], at=2000)
        self.assertFalse(limits.tool_exhausted(limits.snapshot(self.db), "codex"))

    def test_exhausted_reading_expires_once_window_reset(self):
        limits.record_samples(self.db, [
            Sample("claude:five_hour", "claude", 100, reset_at=1000, source="statusline"),
        ], at=500)
        rows = limits.snapshot(self.db)
        self.assertTrue(limits.tool_exhausted(rows, "claude", now=999))
        self.assertFalse(limits.tool_exhausted(rows, "claude", now=1001))
        # no reset_at known: stay exhausted until a fresh sample says otherwise
        limits.record_samples(self.db, [Sample("codex:codex:primary", "codex", 100)], at=600)
        self.assertTrue(limits.tool_exhausted(limits.snapshot(self.db), "codex", now=10 ** 9))

    def test_rate_limit_event_written_for_exhausted_bucket(self):
        self._seed()
        ev = self.db.list_events(type_="rate_limit")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["payload"]["bucket_key"], "codex:codex:primary")

    def test_tool_min_remaining(self):
        self._seed()
        rows = limits.snapshot(self.db)
        self.assertEqual(limits.tool_min_remaining(rows, "claude"), 87.0)
        self.assertIsNone(limits.tool_min_remaining(rows, "nope"))
        self.assertIsNone(limits.binding([]))


if __name__ == "__main__":
    unittest.main()
