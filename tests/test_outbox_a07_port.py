"""The a07 candidate: a06's source line plus the a2 line's persistent batch isolation (S2-7) and manual set-aside (S3-24).

These tests pin what the port has to guarantee on a06's own semantics (a refused batch (409) is retried once a minute, named once,
and re-raised at the end of the flush for `_next_claim` to swallow): a failing upload never blocks other batches or the claim, 401
still reaches the revoke path, the waiting time of every batch survives a restart, batches are isolated per attempt and epoch with
their order kept, only a refused (409) batch can be set aside and only by an explicit confirmation, and a database written before
this change opens with its pending events intact.
"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from timetrace import audit, cli
from timetrace.agent import OUTBOX_ERROR_RETRY_FIRST_SECONDS, OUTBOX_REFUSED_RETRY_SECONDS, Agent
from timetrace.cloud import CloudError
from timetrace.db import Database


class Fixture(unittest.TestCase):
    """A temporary synthetic database and a fake cloud; nothing here touches a real Agent home, database or network."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / "timetrace.db"
        self.script = {}   # (job, attempt, epoch) → factory of the error its upload raises
        self.sent = []     # (job, attempt, epoch, [seq...]) in the order Valley received them
        self.claims = []
        self.logs = []
        self.revoked = []
        self.open_agent()

    def open_agent(self):
        """(Re)open the database and start a new Agent over it: what a restart of the process does."""
        outer = self
        db = Database(self.path)
        if getattr(self, "db", None) is not None:
            self.db.close()
        self.db = db

        class Cloud:
            def append_events(_, token, job, attempt, epoch, events):
                factory = outer.script.get((job, attempt, epoch))
                if factory:
                    raise factory()
                outer.sent.append((job, attempt, epoch, [event["seq"] for event in events]))

            def claim(_, token):
                outer.claims.append(token)
                return None

            def update_inventory(_, *args, **kwargs):
                pass

        self.agent = Agent(self.db, Cloud(), {}, self.home, lambda: "token", log=self.logs.append)
        self.agent._on_revoked = lambda: self.revoked.append(1)

    def tearDown(self):
        self.db.close()

    def enqueue(self, job, attempt="attempt", epoch=3, seqs=(1, 2)):
        for seq in seqs:
            self.db.queue_remote_event(job, attempt, epoch, {"seq": seq, "type": "running" if seq == 1 else "completed"})

    def flush_at(self, now):
        with patch("timetrace.agent.time.time", return_value=now):
            try:
                self.agent.flush_outbox()
            except CloudError as exc:
                if exc.status != 409:   # a06 re-raises a refusal after the flush; nothing else may escape
                    raise

    def pending(self):
        return sorted({(row["job_id"], row["attempt_id"], row["lease_epoch"]) for row in self.db.pending_remote_events()})


SERVER_ERROR = lambda: CloudError("unavailable", status=503)
CONFLICT = lambda: CloudError("conflict", status=409)
UNAUTHORIZED = lambda: CloudError("unauthorized", status=401)
NO_RESPONSE = lambda: TimeoutError("The read operation timed out")


class FailuresNeverBlockTheClaimTest(Fixture):
    def test_a_503_batch_does_not_stop_other_batches_or_the_claim(self):
        self.enqueue("bad")
        self.enqueue("good")
        self.script[("bad", "attempt", 3)] = SERVER_ERROR
        with patch("timetrace.agent.time.time", return_value=1000):
            claim = self.agent._next_claim()
        self.assertEqual(claim, "idle", "the claim is reached although a batch cannot be uploaded")
        self.assertEqual(self.claims, ["token"])
        self.assertEqual(self.sent, [("good", "attempt", 3, [1, 2])])
        self.assertEqual(self.pending(), [("bad", "attempt", 3)])

    def test_a_refused_batch_still_does_not_stop_the_claim(self):
        self.enqueue("dead")
        self.script[("dead", "attempt", 3)] = CONFLICT
        with patch("timetrace.agent.time.time", return_value=1000):
            self.assertEqual(self.agent._next_claim(), "idle")
        self.assertEqual(self.pending(), [("dead", "attempt", 3)])

    def test_no_response_ends_the_round_but_the_claim_is_still_made(self):
        self.enqueue("a")
        self.enqueue("b")
        self.script[("a", "attempt", 3)] = NO_RESPONSE
        self.script[("b", "attempt", 3)] = NO_RESPONSE
        with patch("timetrace.agent.time.time", return_value=1000):
            self.assertEqual(self.agent._next_claim(), "idle")
        self.assertEqual(self.db.remote_event_retry("a", "attempt", 3)["status"], 0)
        self.assertIsNone(self.db.remote_event_retry("b", "attempt", 3), "the round ended at the first batch")
        self.assertEqual(len(self.pending()), 2)

    def test_401_still_reaches_the_revoke_path_and_is_not_filed_as_a_batch_failure(self):
        self.enqueue("job")
        self.script[("job", "attempt", 3)] = UNAUTHORIZED
        with patch("timetrace.agent.time.time", return_value=1000):
            self.assertEqual(self.agent._next_claim(), "revoked")
        self.assertEqual(self.revoked, [1])
        self.assertEqual(self.claims, [], "a revoked computer does not claim")
        self.assertIsNone(self.db.remote_event_retry("job", "attempt", 3), "401 is not this batch's fault, nothing is deferred")
        self.assertEqual(self.pending(), [("job", "attempt", 3)])


class WaitingTimeSurvivesARestartTest(Fixture):
    def test_a_backoff_is_honoured_by_the_restarted_process(self):
        self.enqueue("bad")
        self.script[("bad", "attempt", 3)] = SERVER_ERROR
        self.flush_at(1000)
        self.script.clear()       # the server has recovered...
        self.open_agent()         # ...and the process restarts at once
        self.flush_at(1000 + OUTBOX_ERROR_RETRY_FIRST_SECONDS - 1)
        self.assertEqual(self.sent, [], "the restarted agent does not retry before the stored time")
        self.flush_at(1000 + OUTBOX_ERROR_RETRY_FIRST_SECONDS)
        self.assertEqual(self.sent, [("bad", "attempt", 3, [1, 2])])
        self.assertEqual(self.pending(), [])
        self.assertIsNone(self.db.remote_event_retry("bad", "attempt", 3), "success clears the retry state")

    def test_the_failure_count_continues_after_a_restart(self):
        self.enqueue("bad")
        self.script[("bad", "attempt", 3)] = SERVER_ERROR
        self.flush_at(1000)
        self.open_agent()
        self.flush_at(1005)
        self.assertEqual(self.db.remote_event_retry("bad", "attempt", 3)["failures"], 2)
        self.assertEqual(self.db.remote_event_retry("bad", "attempt", 3)["retry_at"], 1005 + 2 * OUTBOX_ERROR_RETRY_FIRST_SECONDS)

    def test_a_refused_batch_keeps_its_minute_across_a_restart(self):
        self.enqueue("dead")
        self.script[("dead", "attempt", 3)] = CONFLICT
        self.flush_at(1000)
        self.assertEqual(self.db.remote_event_retry("dead", "attempt", 3)["retry_at"], 1000 + OUTBOX_REFUSED_RETRY_SECONDS)
        self.script.clear()
        self.open_agent()
        self.flush_at(1000 + OUTBOX_REFUSED_RETRY_SECONDS - 1)
        self.assertEqual(self.sent, [])
        self.flush_at(1000 + OUTBOX_REFUSED_RETRY_SECONDS)
        self.assertEqual(self.sent, [("dead", "attempt", 3, [1, 2])])

    def test_the_refusal_is_named_once_per_process_not_at_every_retry(self):
        self.enqueue("dead")
        self.script[("dead", "attempt", 3)] = CONFLICT
        for round_ in range(3):
            self.flush_at(1000 + round_ * OUTBOX_REFUSED_RETRY_SECONDS)
        self.assertEqual(len([line for line in self.logs if "HTTP 409" in line]), 1, self.logs)


class OrderAndIsolationPerAttemptAndEpochTest(Fixture):
    def test_every_attempt_and_every_epoch_is_its_own_batch(self):
        # (job, attempt, seq) is unique in the outbox, so a later lease epoch of an attempt carries on counting.
        self.enqueue("job", "a1", 1, seqs=(1, 2))
        self.enqueue("job", "a1", 2, seqs=(3, 4))
        self.enqueue("job", "a2", 1)
        self.script[("job", "a1", 1)] = SERVER_ERROR
        self.flush_at(1000)
        self.assertEqual(self.sent, [("job", "a1", 2, [3, 4]), ("job", "a2", 1, [1, 2])],
                         "a failing (attempt, epoch) holds back neither another epoch nor another attempt")
        self.assertEqual(self.pending(), [("job", "a1", 1)])

    def test_results_that_arrive_for_a_waiting_batch_never_overtake_its_older_ones(self):
        self.enqueue("job", "a1", 1, seqs=(1, 2))
        self.script[("job", "a1", 1)] = SERVER_ERROR
        self.flush_at(1000)
        self.db.queue_remote_event("job", "a1", 1, {"seq": 3, "type": "failed"})
        self.script.clear()
        self.flush_at(1000 + OUTBOX_ERROR_RETRY_FIRST_SECONDS - 1)
        self.assertEqual(self.sent, [], "the new event does not go out alone while the older ones wait")
        self.flush_at(1000 + OUTBOX_ERROR_RETRY_FIRST_SECONDS)
        self.assertEqual(self.sent, [("job", "a1", 1, [1, 2, 3])], "one batch, in sequence order")

    def test_batches_keep_their_durable_enqueue_order(self):
        self.enqueue("z-job")
        self.enqueue("a-job")
        self.flush_at(1000)
        self.assertEqual([item[0] for item in self.sent], ["z-job", "a-job"])

    def test_a_late_failure_of_a_later_batch_does_not_undo_an_earlier_delivery(self):
        self.enqueue("first")
        self.enqueue("second")
        self.script[("second", "attempt", 3)] = SERVER_ERROR
        self.flush_at(1000)
        self.assertEqual(self.sent, [("first", "attempt", 3, [1, 2])])
        self.assertEqual(self.pending(), [("second", "attempt", 3)])


class SettingAsideIsManualConfirmedAndLossFreeTest(Fixture):
    def discard(self, job, attempt, yes):
        out = io.StringIO()
        with redirect_stdout(out), patch("timetrace.cli._open", return_value=(self.home, None, self.db)):
            code = cli.cmd_agent_discard_results(SimpleNamespace(job=job, attempt=attempt, yes=yes))
        return code, out.getvalue()

    def refuse(self, job):
        self.enqueue(job)
        self.script[(job, "attempt", 3)] = CONFLICT
        self.flush_at(1000)

    def test_nothing_is_set_aside_without_the_explicit_confirmation(self):
        self.refuse("dead")
        code, text = self.discard("dead", "attempt", yes=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", text)
        self.assertEqual(self.pending(), [("dead", "attempt", 3)])

    def test_only_a_refused_409_batch_can_be_set_aside(self):
        self.enqueue("flaky")
        self.script[("flaky", "attempt", 3)] = SERVER_ERROR
        self.flush_at(1000)
        code, text = self.discard("flaky", "attempt", yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.pending(), [("flaky", "attempt", 3)], "a failure that may pass is left alone, even with --yes")
        self.enqueue("fresh")  # never failed at all
        self.assertEqual(self.discard("fresh", "attempt", yes=True)[0], 2)

    def test_a_confirmed_discard_keeps_the_payloads_audits_the_decision_and_never_counts_it_as_sent(self):
        self.refuse("dead")
        before = [r["payload"] for r in self.db.conn.execute("SELECT payload FROM remote_outbox WHERE job_id='dead' ORDER BY seq")]
        code, _ = self.discard("dead", "attempt", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.pending(), [], "never uploaded again")
        rows = self.db.conn.execute("SELECT payload, sent_at, abandoned_at FROM remote_outbox WHERE job_id='dead' ORDER BY seq").fetchall()
        self.assertEqual([r["payload"] for r in rows], before, "the original payloads are untouched")
        self.assertEqual(len(rows), 2, "both results are still in the database")
        self.assertTrue(all(r["sent_at"] is None for r in rows), "setting aside is not delivering")
        self.assertTrue(all(r["abandoned_at"] is not None for r in rows))
        records = [line for line in (self.home / "audit.log").read_text().splitlines() if "outbox_discard" in line] \
            if (self.home / "audit.log").exists() else []
        self.assertEqual(len(records), 1, "the decision is written to the audit log")
        self.assertEqual(self.db.abandoned_remote_event_count(), 2)

    def test_a_set_aside_batch_stays_set_aside_after_a_restart_and_a_late_conflict(self):
        self.refuse("dead")
        self.discard("dead", "attempt", yes=True)
        self.script.clear()
        self.open_agent()
        self.flush_at(5000)
        self.assertEqual(self.sent, [], "the restarted agent does not upload it")
        self.assertEqual(self.db.abandoned_remote_event_count(), 2)


class AnOlderDatabaseOpensTest(unittest.TestCase):
    """A database written by a06 (before the retry table and the set-aside column existed) opens with its events pending."""

    def make_old_database(self, path):
        db = Database(path)
        for job in ("old-a", "old-b"):
            for seq in (1, 2):
                db.queue_remote_event(job, job + "-attempt", 3, {"seq": seq, "type": "running"})
        db.conn.execute("DROP TABLE remote_outbox_retry")
        db.conn.execute("ALTER TABLE remote_outbox DROP COLUMN abandoned_at")
        db.close()
        with sqlite3.connect(str(path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(remote_outbox)")}
        self.assertNotIn("remote_outbox_retry", tables)
        self.assertNotIn("abandoned_at", columns)

    def test_pending_events_survive_the_migration_and_flush_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.make_old_database(home / "timetrace.db")
            db = Database(home / "timetrace.db")
            self.addCleanup(db.close)
            self.assertEqual(len(db.pending_remote_events()), 4)
            self.assertEqual(db.remote_outbox_diagnostics(), [])
            self.assertEqual(db.abandoned_remote_event_count(), 0)
            sent = []

            class Cloud:
                def append_events(_, token, job, attempt, epoch, events):
                    sent.append((job, [event["seq"] for event in events]))

            Agent(db, Cloud(), {}, home, lambda: "token").flush_outbox()
            self.assertEqual(sent, [("old-a", [1, 2]), ("old-b", [1, 2])])
            self.assertEqual(db.pending_remote_events(), [])

    def test_opening_twice_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetrace.db"
            self.make_old_database(path)
            for _ in range(2):
                db = Database(path)
                self.assertEqual(len(db.pending_remote_events()), 4)
                db.close()


if __name__ == "__main__":
    unittest.main()
