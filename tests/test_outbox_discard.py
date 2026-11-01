"""S3-24: a result the server refuses for good (409) stays visible and can be set aside by hand.

Nothing here gives up on its own: a conflict is retried for as long as it lasts, because letting go
of a result by mistake loses it silently. Setting one aside is a person's decision, taken through
`timetrace agent discard-results`, never counted as delivered, and only offered for a batch the
server really refused (409), not for one that failed for a reason that may pass.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import audit, cli, config
from timetrace.db import Database

from tests.test_outbox_isolation import CONFLICT, READ_TIMEOUT, SERVER_ERROR, OutboxFailureFixture

DAY = 24 * 3600


class DiscardConflictedResultsTest(OutboxFailureFixture, unittest.TestCase):
    def conflict(self, job, now=1000):
        self.failing[job] = CONFLICT
        self.enqueue(job)
        self.flush_at(now)

    def test_a_conflict_is_never_given_up_on_its_own(self):
        # The decision behind S3-24: no age or retry count lets the agent drop a refused result.
        self.conflict("old")
        for day in range(1, 40):
            self.flush_at(1000 + day * DAY)
        self.assertEqual(len(self.attempts), 40)
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_discarding_sets_the_batch_aside_without_acknowledging_it(self):
        self.conflict("old")
        self.assertEqual(self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000), 2)
        self.assertEqual(self.pending_jobs(), [])
        self.assertIsNone(self.db.remote_event_retry("old", "old-attempt", 3))
        rows = self.db.conn.execute("SELECT sent_at, abandoned_at FROM remote_outbox").fetchall()
        self.assertEqual([(r["sent_at"], r["abandoned_at"]) for r in rows], [(None, 2000), (None, 2000)])
        # Never uploaded again, and never counted as delivered.
        self.failing.clear()
        self.flush_at(1000 + 10 * DAY)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.attempts), 1)

    def test_a_batch_that_may_still_succeed_is_never_discarded(self):
        for name, error in (("server error", SERVER_ERROR), ("timeout", READ_TIMEOUT)):
            with self.subTest(failure=name):
                self.fresh()
                self.failing["old"] = error
                self.enqueue("old")
                self.flush_at(1000)
                self.assertEqual(self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000), 0)
                self.assertEqual(self.pending_jobs(), ["old"])
        # Nor one that has not failed at all yet.
        self.fresh()
        self.enqueue("old")
        self.assertEqual(self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000), 0)
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_discarding_one_attempt_leaves_the_others_retrying(self):
        self.conflict("a")
        self.failing["b"] = CONFLICT
        self.enqueue("b")
        self.flush_at(1000 + 300)
        self.db.discard_conflicted_remote_events("a", "a-attempt", now=2000)
        self.assertEqual(self.pending_jobs(), ["b"])
        before = len(self.attempts)
        self.flush_at(1000 + 600)
        self.assertEqual(self.attempts[before:], ["b"])

    def test_discarding_one_attempt_of_a_job_leaves_its_other_attempts(self):
        for attempt in ("j-1", "j-2"):
            self.db.queue_remote_event("j", attempt, 3, {"seq": 1, "type": "failed"}, now=500)
            self.db.defer_remote_events("j", attempt, 3, 409, 1000, 1300)
        self.assertEqual(self.db.discard_conflicted_remote_events("j", "j-1", now=2000), 1)
        self.assertEqual([row["attempt_id"] for row in self.db.pending_remote_events()], ["j-2"])
        self.assertEqual(self.db.remote_event_retry("j", "j-2", 3)["status"], 409)

    def test_the_conflict_line_in_the_log_points_to_doctor(self):
        self.conflict("old")
        line = [line for line in self.logs if "result upload conflict" in line][0]
        self.assertIn("agent doctor", line)

    def test_the_oldest_pending_result_and_the_count_of_discarded_ones_are_reported(self):
        self.failing["old"] = CONFLICT
        self.db.queue_remote_event("old", "old-attempt", 3, {"seq": 1, "type": "running"}, now=500)
        self.db.queue_remote_event("old", "old-attempt", 3, {"seq": 2, "type": "completed"}, now=900)
        self.flush_at(1000)
        pending = self.db.remote_outbox_diagnostics()
        self.assertEqual([(p["status"], p["pending_events"], p["oldest_event_at"]) for p in pending], [(409, 2, 500)])
        self.assertEqual(self.db.abandoned_remote_event_count(), 0)
        self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000)
        self.assertEqual(self.db.abandoned_remote_event_count(), 2)
        self.assertEqual(self.db.remote_outbox_diagnostics(), [])


    def test_a_late_conflict_from_an_upload_already_in_flight_does_not_bring_a_discarded_batch_back(self):
        self.conflict("old")
        self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000)
        # The flush that was mid-request when it was set aside now records its own 409.
        self.db.defer_remote_events("old", "old-attempt", 3, 409, 2001, 2301)
        self.assertEqual(self.db.remote_outbox_diagnostics(), [])
        self.assertEqual(self.pending_jobs(), [])


class OldDatabaseTest(unittest.TestCase):
    def test_a_database_from_before_the_column_existed_opens_with_its_events_still_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetrace.db"
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE remote_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,"
                         " attempt_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, seq INTEGER NOT NULL,"
                         " payload TEXT NOT NULL, sent_at INTEGER, created_at INTEGER NOT NULL,"
                         " UNIQUE(job_id, attempt_id, seq))")
            conn.execute("INSERT INTO remote_outbox (job_id,attempt_id,lease_epoch,seq,payload,created_at)"
                         " VALUES ('old','old-attempt',3,1,'{\"seq\": 1}',500)")
            conn.commit()
            conn.close()
            db = Database(path)
            self.addCleanup(db.close)
            self.assertEqual([row["job_id"] for row in db.pending_remote_events()], ["old"])
            self.assertEqual(db.abandoned_remote_event_count(), 0)


class DiscardCommandTest(OutboxFailureFixture, unittest.TestCase):
    """The CLI half: a dry run by default, `--yes` to act, and a doctor line that names the way out."""

    def setUp(self):
        super().setUp()
        self.home_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_dir.cleanup)
        env = patch.dict(os.environ, {"TIMETRACE_HOME": self.home_dir.name})
        env.start()
        self.addCleanup(env.stop)
        config.ensure_dirs(config.home())
        self.store = Database(config.home() / "timetrace.db")
        self.addCleanup(self.store.close)

    def conflict_in_store(self, job, now=1000):
        for seq in (1, 2):
            self.store.queue_remote_event(job, job + "-attempt", 3, {"seq": seq, "type": "running"}, now=now - 100)
        self.store.defer_remote_events(job, job + "-attempt", 3, 409, now, now + 300)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def doctor(self):
        with patch("timetrace.cli._adapters", return_value={}), \
             patch("timetrace.cli.CredentialStore.load", return_value=None):
            return self.run_cli("agent", "doctor")[1]

    def test_without_yes_nothing_changes_and_the_count_is_shown(self):
        self.conflict_in_store("old")
        code, out, _ = self.run_cli("agent", "discard-results", "old", "old-attempt")
        self.assertEqual(code, 1)
        self.assertIn("2 条", out)
        self.assertIn("--yes", out)
        self.assertEqual(len(self.store.pending_remote_events()), 2)
        self.assertEqual(self.store.abandoned_remote_event_count(), 0)

    def test_with_yes_the_results_are_set_aside_and_the_decision_is_audited(self):
        self.conflict_in_store("old")
        code, out, err = self.run_cli("agent", "discard-results", "old", "old-attempt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("2 条", out)
        self.assertIn("保留在本机", out)
        self.assertEqual(self.store.pending_remote_events(), [])
        self.assertEqual(self.store.abandoned_remote_event_count(), 2)
        entry = [e for e in audit.read(config.home()) if e["event"] == "outbox_discard"]
        self.assertEqual([(e["job_id"], e["attempt_id"], e["events"]) for e in entry], [("old", "old-attempt", 2)])

    def test_a_batch_that_was_not_refused_is_not_discarded(self):
        for name, status in (("server error", 500), ("no response", 0)):
            with self.subTest(failure=name):
                self.store.queue_remote_event("slow", "slow-attempt", 3, {"seq": 1, "type": "running"}, now=500)
                self.store.defer_remote_events("slow", "slow-attempt", 3, status, 1000, 1030)
                code, out, err = self.run_cli("agent", "discard-results", "slow", "slow-attempt", "--yes")
                self.assertEqual(code, 2)
                self.assertIn("409", out + err)
                self.assertEqual(len(self.store.pending_remote_events()), 1)
                self.store.clear_remote_event_retry("slow", "slow-attempt", 3)
                self.store.conn.execute("DELETE FROM remote_outbox")

    def test_an_unknown_attempt_is_reported_and_nothing_changes(self):
        self.conflict_in_store("old")
        code, out, err = self.run_cli("agent", "discard-results", "old", "someone-else", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("409", out + err)
        self.assertEqual(len(self.store.pending_remote_events()), 2)

    def test_doctor_names_a_refused_batch_its_age_and_the_command_to_set_it_aside(self):
        self.conflict_in_store("old", now=2 * DAY)
        out = self.doctor()
        line = [line for line in out.splitlines() if "old-attempt" in line]
        self.assertEqual(len(line), 2, out)  # the line itself and the command on the next one
        self.assertIn("被服务器拒收", line[0])
        self.assertIn("2 条", line[0])
        self.assertIn("不会自动丢弃", line[0])
        self.assertIn("timetrace agent discard-results old old-attempt --yes", line[1])
        self.assertIn(cli._iso_local(2 * DAY - 100), line[0])  # the oldest result's own time, not "now"

    def test_doctor_keeps_the_old_wording_for_a_failure_that_may_pass(self):
        self.store.queue_remote_event("slow", "slow-attempt", 3, {"seq": 1, "type": "running"}, now=500)
        self.store.defer_remote_events("slow", "slow-attempt", 3, 500, 1000, 1030)
        out = self.doctor()
        self.assertIn("结果上报待恢复: 任务 slow / 尝试 slow-attempt", out)
        self.assertNotIn("discard-results", out)
        self.assertNotIn("被服务器拒收", out)

    def test_doctor_reports_what_was_set_aside(self):
        self.conflict_in_store("old")
        self.run_cli("agent", "discard-results", "old", "old-attempt", "--yes")
        out = self.doctor()
        self.assertIn("已手动丢弃的结果: 2 条", out)
        self.assertNotIn("被服务器拒收", out)


class DiscardKeepsPlanTombstoneTest(unittest.TestCase):
    """Decided for S3-24: setting a refused result aside does not lift its Plan's 'started' tombstone.

    The tombstone is lifted only once the server holds the job's `failed` event. A discarded batch was
    never accepted, so as far as the server knows the run has no outcome, and the Plan must not be able to
    start again on this computer. Lifting it on discard would turn 'stop retrying this result' into
    'run this Plan again'.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "timetrace.db"
        self.open()
        self.db.mark_plan_started("plan-1", "old", "old-attempt")
        for seq, kind in ((1, "running"), (2, "failed")):
            self.db.queue_remote_event("old", "old-attempt", 3, {"seq": seq, "type": kind}, now=500)

    def open(self):
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def set_aside(self):
        self.db.defer_remote_events("old", "old-attempt", 3, 409, 1000, 1300)
        self.assertEqual(self.db.discard_conflicted_remote_events("old", "old-attempt", now=2000), 2)

    def test_delivering_the_failed_event_does_lift_it(self):
        # The control: the same batch accepted by the server releases the Plan, so the cases below cannot pass vacuously.
        self.assertTrue(self.db.plan_started("plan-1"))
        self.db.mark_remote_events_sent([row["id"] for row in self.db.pending_remote_events()], now=1000)
        self.assertFalse(self.db.plan_started("plan-1"))

    def test_setting_a_refused_batch_aside_keeps_it(self):
        self.set_aside()
        self.assertTrue(self.db.plan_started("plan-1"))

    def test_it_is_still_held_after_the_agent_restarts(self):
        # Opening the database lifts tombstones whose `failed` event was delivered; a set-aside batch was not.
        self.set_aside()
        self.db.close()
        self.open()
        self.assertTrue(self.db.plan_started("plan-1"))


if __name__ == "__main__":
    unittest.main()
