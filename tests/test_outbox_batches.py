import tempfile
import sqlite3
import unittest
from pathlib import Path

from timetrace.agent import Agent
from timetrace.cloud import CloudError
from timetrace.db import Database


class OutboxFixture:
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


class OutboxBatchTest(OutboxFixture, unittest.TestCase):
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



class OutboxConflictIsolationTest(OutboxFixture, unittest.TestCase):
    """A-01: one attempt Valley refuses forever must not block everything else."""

    def conflict_on(self, dead_job):
        sent = self.sent
        def append(token, job, attempt, epoch, events):
            if job == dead_job:
                raise CloudError("resource state conflict", status=409, code=40900)
            sent.append((job, attempt, epoch, events))
        self.cloud.append_events = append

    def test_conflicting_batch_does_not_block_later_batches(self):
        dead = self.enqueue("dead-job", "dead-attempt")
        live = self.enqueue("live-job", "live-attempt")
        self.conflict_on("dead-job")
        with self.assertRaises(CloudError) as caught:
            self.agent.flush_outbox()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.sent, [("live-job", "live-attempt", 3, live)])
        # The refused batch is kept durably, never discarded.
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], dead)

    def test_non_conflict_failure_still_stops_the_flush(self):
        self.enqueue("a-job", "a1")
        self.enqueue("b-job", "b1")
        def down(*args): raise OSError("network down")
        self.cloud.append_events = down
        with self.assertRaises(OSError): self.agent.flush_outbox()
        self.assertEqual(len(self.db.pending_remote_events()), 4)

    def test_refused_batch_is_not_resent_on_every_tick(self):
        self.enqueue("dead-job", "dead-attempt")
        calls = []
        def conflict(token, job, attempt, epoch, events):
            calls.append(job)
            raise CloudError("resource state conflict", status=409, code=40900)
        self.cloud.append_events = conflict
        for _ in range(3):
            try: self.agent.flush_outbox()
            except CloudError: pass
        self.assertEqual(calls, ["dead-job"])

    def test_next_claim_still_claims_and_names_the_refused_attempt_once(self):
        self.enqueue("dead-job", "dead-attempt")
        self.conflict_on("dead-job")
        logs = []
        self.agent.log = logs.append
        wanted = {"job": {"id": "next-job"}}
        self.cloud.claim = lambda token: wanted
        for _ in range(3):
            self.assertIs(self.agent._next_claim(), wanted)
        refused = [line for line in logs if "dead-attempt" in line]
        self.assertEqual(len(refused), 1)
        self.assertIn("409", refused[0])

    def test_loop_errors_are_logged_once_per_distinct_error(self):
        logs = []
        self.agent.log = logs.append
        for _ in range(3):
            self.agent._note_loop_error(CloudError("resource state conflict", status=409))
        self.agent._note_loop_error(OSError("network down"))
        self.assertEqual(len(logs), 2)
