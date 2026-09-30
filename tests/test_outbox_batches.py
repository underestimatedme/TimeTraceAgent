import tempfile
import sqlite3
import unittest
from pathlib import Path

from timetrace.agent import Agent
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
        with self.assertRaises(OSError): self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], second)
        self.db.close()
        self.db = Database(self.path)
        self.agent.db = self.db
        self.cloud.append_events = append
        self.agent.flush_outbox()
        self.assertEqual(self.sent, [("a-job", "a1", 3, first), ("b-job", "b1", 3, second), ("b-job", "b1", 3, second)])
        self.assertEqual(self.db.pending_remote_events(), [])

    def test_409_keeps_entire_batch_and_is_not_silently_discarded(self):
        events = self.enqueue("job", "attempt")
        def conflict(*args): raise CloudError("lease/attempt conflict", status=409)
        self.cloud.append_events = conflict
        with self.assertRaises(CloudError): self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], events)

    def test_ack_failure_is_atomic_not_a_partially_sent_batch(self):
        events = self.enqueue("job", "attempt")
        self.db.conn.execute("CREATE TRIGGER reject_second_ack BEFORE UPDATE OF sent_at ON remote_outbox WHEN OLD.seq=2 BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.agent.flush_outbox()
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], events)
