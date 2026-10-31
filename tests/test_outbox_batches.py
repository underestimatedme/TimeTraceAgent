import tempfile
import sqlite3
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from timetrace.agent import OUTBOX_ERROR_RETRY_SECONDS, Agent
from timetrace.cloud import CloudError
from timetrace.db import Database


class OutboxBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "old.db"
        self.db = Database(self.path)
        self.sent = []
        class Cloud:
            def append_events(_, token, job, attempt, epoch, events):
                self.sent.append((job, attempt, epoch, events))
        self.cloud = Cloud()
        self.agent = Agent(self.db, self.cloud, {}, Path(self.tmp.name), lambda: "token")

    def enqueue(self, job, attempt):
        events = [{"seq": 1, "type": "running", "observed_at": "2026-09-22T01:00:00Z"},
                  {"seq": 2, "type": "cancelled", "observed_at": "2026-09-22T01:00:02Z"}]
        for event in events:
            self.db.queue_remote_event(job, attempt, 3, event)
        return events

    def test_same_attempt_is_one_ordered_batch_with_original_payloads(self):
        events = self.enqueue("job", "attempt")
        self.agent.flush_outbox()
        self.assertEqual(self.sent, [("job", "attempt", 3, events)])
        self.assertEqual(self.db.pending_remote_events(), [])

    def test_attempts_follow_durable_enqueue_order_not_lexical_ids(self):
        first = self.enqueue("job", "z-attempt")
        second = self.enqueue("job", "a-attempt")
        self.agent.flush_outbox()
        self.assertEqual(self.sent, [("job", "z-attempt", 3, first), ("job", "a-attempt", 3, second)])

    def test_failure_after_one_batch_retries_only_unacked_batch_after_reopen(self):
        first = self.enqueue("a-job", "a1")
        second = self.enqueue("b-job", "b1")
        append = self.cloud.append_events
        def fail_second(token, job, attempt, epoch, events):
            append(token, job, attempt, epoch, events)
            if job == "b-job":
                raise OSError("response lost after server commit")
        self.cloud.append_events = fail_second
        # The lost response no longer propagates (it would stop the claim loop):
        # the batch stays queued and is retried once its delay is over.
        self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], second)
        self.db.close()
        self.db = Database(self.path)
        self.agent.db = self.db
        self.cloud.append_events = append
        with patch("timetrace.agent.time.time", return_value=time.time() + OUTBOX_ERROR_RETRY_SECONDS):
            self.agent.flush_outbox()
        self.assertEqual(self.sent, [("a-job", "a1", 3, first), ("b-job", "b1", 3, second), ("b-job", "b1", 3, second)])
        self.assertEqual(self.db.pending_remote_events(), [])

    def test_409_keeps_entire_batch_and_is_not_silently_discarded(self):
        events = self.enqueue("job", "attempt")
        def conflict(*args): raise CloudError("lease/attempt conflict", status=409)
        self.cloud.append_events = conflict
        self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], events)
        self.assertEqual(self.db.remote_outbox_diagnostics()[0]["status"], 409)

    def test_conflict_does_not_block_other_results_or_claim_heartbeat(self):
        conflict = self.enqueue("old-job", "old-attempt")
        good = self.enqueue("new-job", "new-attempt")
        append = self.cloud.append_events
        def upload(token, job, attempt, epoch, events):
            append(token, job, attempt, epoch, events)
            if job == "old-job":
                raise CloudError("conflict", status=409)
        self.cloud.append_events = upload
        claims = []
        self.cloud.claim = lambda token: claims.append(token)
        self.assertEqual(self.agent.run_once(), "idle")
        self.assertEqual(claims, ["token"])
        self.assertEqual(self.sent[-1], ("new-job", "new-attempt", 3, good))
        self.assertEqual([r["payload"] for r in self.db.pending_remote_events()], conflict)
        self.assertEqual(self.db.remote_outbox_diagnostics()[0]["pending_events"], 2)

    def test_conflict_backoff_survives_restart_and_success_replays_exact_payload(self):
        events = self.enqueue("job", "attempt")
        append = self.cloud.append_events
        def conflict(*args):
            append(*args)
            raise CloudError("conflict", status=409)
        self.cloud.append_events = conflict
        with patch("timetrace.agent.time.time", return_value=1000):
            self.agent.flush_outbox()
        self.db.close()
        self.db = Database(self.path)
        self.agent = Agent(self.db, self.cloud, {}, Path(self.tmp.name), lambda: "token")
        with patch("timetrace.agent.time.time", return_value=1299):
            self.agent.flush_outbox()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([r["payload"] for r in self.db.pending_remote_events()], events)
        with patch("timetrace.agent.time.time", return_value=1300):
            self.agent.flush_outbox()
        self.assertEqual(self.db.remote_outbox_diagnostics()[0]["failures"], 2)
        self.cloud.append_events = append
        with patch("timetrace.agent.time.time", return_value=1600):
            self.agent.flush_outbox()
        self.assertEqual(self.sent, [("job", "attempt", 3, events)] * 3)
        self.assertEqual(self.db.pending_remote_events(), [])
        self.assertEqual(self.db.remote_outbox_diagnostics(), [])
        self.assertIsNone(self.db.remote_event_retry("job", "attempt", 3))

    def test_conflict_never_releases_failed_plan_or_prunes_unsent_payload(self):
        self.db.mark_plan_started("plan", "job", "attempt")
        event = {"seq": 1, "type": "failed", "observed_at": "2026-10-01T00:00:00Z"}
        self.db.queue_remote_event("job", "attempt", 3, event)
        self.cloud.append_events = lambda *args: (_ for _ in ()).throw(CloudError("conflict", status=409))
        self.agent.flush_outbox()
        self.db.prune_sent_remote_events(9999999999)
        self.assertTrue(self.db.plan_started("plan"))
        self.assertEqual([r["payload"] for r in self.db.pending_remote_events()], [event])

    def test_401_still_uses_existing_auth_recovery_and_retains_payload(self):
        events = self.enqueue("job", "attempt")
        revoked = []
        self.agent._on_revoked = lambda: revoked.append(True)
        self.cloud.append_events = lambda *args: (_ for _ in ()).throw(CloudError("unauthorized", status=401))
        self.assertEqual(self.agent.run_once(), "revoked")
        self.assertEqual(revoked, [True])
        self.assertEqual([r["payload"] for r in self.db.pending_remote_events()], events)

    def test_ack_failure_is_atomic_not_a_partially_sent_batch(self):
        events = self.enqueue("job", "attempt")
        self.db.conn.execute("CREATE TRIGGER reject_second_ack BEFORE UPDATE OF sent_at ON remote_outbox WHEN OLD.seq=2 BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], events)
