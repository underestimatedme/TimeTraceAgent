"""S2-7: whatever goes wrong uploading one attempt's results must not stop the agent.

Other batches still upload, claims go on, presence is kept, and the failure is named once in the log.
Valley's 401 keeps its own recovery path (see test_outbox_batches).
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.agent import OUTBOX_ERROR_RETRY_FIRST_SECONDS, OUTBOX_ERROR_RETRY_SECONDS, Agent
from timetrace.cloud import CloudError
from timetrace.db import Database

CONFLICT = lambda: CloudError("conflict", status=409)
SERVER_ERROR = lambda: CloudError("server error", status=500)
READ_TIMEOUT = lambda: TimeoutError("The read operation timed out")
RETRY = OUTBOX_ERROR_RETRY_SECONDS


class OutboxFailureFixture:
    def setUp(self):
        self.fresh()

    def fresh(self, parallel=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.db = Database(home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.failing = {}  # job id → factory of the error its upload raises
        self.attempts, self.sent, self.claims, self.logs = [], [], [], []
        outer = self

        class Cloud:
            def append_events(_, token, job, attempt, epoch, events):
                outer.attempts.append(job)
                if job in outer.failing:
                    raise outer.failing[job]()
                outer.sent.append(job)

            def claim(_, token):
                outer.claims.append(token)
                return None

            def update_inventory(_, *args, **kwargs):
                pass

        self.agent = Agent(self.db, Cloud(), {}, home, lambda: "token", report_protocol=True,
                           inventory=lambda: ([], []), log=self.logs.append,
                           max_parallel=2 if parallel else 1)

    def enqueue(self, job):
        events = [{"seq": 1, "type": "running", "observed_at": "2026-10-30T00:32:00Z"},
                  {"seq": 2, "type": "completed", "observed_at": "2026-10-30T00:32:34Z"}]
        for event in events:
            self.db.queue_remote_event(job, job + "-attempt", 3, event)
        return events

    def pending_jobs(self):
        return sorted({row["job_id"] for row in self.db.pending_remote_events()})

    def flush_at(self, now):
        with patch("timetrace.agent.time.time", return_value=now):
            self.agent.flush_outbox()

    def script_claims(self, steps):
        steps = list(steps)

        def claim(token):
            self.claims.append(token)
            step = steps.pop(0)
            if not steps:
                self.agent.stop()
            if isinstance(step, BaseException):
                raise step
            return step
        self.agent.cloud.claim = claim

    def run_loop(self):
        # Safety stop: a loop that never reaches the scripted end must not hang the suite.
        watchdog = threading.Timer(5, self.agent.stop)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        with patch("timetrace.runtime_preview.RuntimePreviews.start"):
            self.agent.run_forever(interval=0, log=self.logs.append)


class OutboxIsolationTest(OutboxFailureFixture, unittest.TestCase):
    def test_a_server_error_on_one_batch_does_not_stop_other_batches_or_the_claim(self):
        self.failing["old"] = SERVER_ERROR
        self.enqueue("old")
        self.enqueue("new")
        self.assertEqual(self.agent.run_once(), "idle")
        self.assertEqual(self.sent, ["new"])
        self.assertEqual(len(self.claims), 1)
        self.assertEqual(self.pending_jobs(), ["old"])
        self.assertEqual(self.db.remote_event_retry("old", "old-attempt", 3)["status"], 500)

    def test_a_read_timeout_keeps_the_batch_and_the_claim_goes_on(self):
        self.failing["old"] = READ_TIMEOUT
        self.enqueue("old")
        self.assertEqual(self.agent.run_once(), "idle")
        self.assertEqual(len(self.claims), 1)
        self.assertEqual(self.pending_jobs(), ["old"])
        # No HTTP response at all: recorded as status 0.
        self.assertEqual(self.db.remote_event_retry("old", "old-attempt", 3)["status"], 0)

    def test_a_failed_batch_waits_before_its_next_attempt_and_is_cleared_by_success(self):
        first = OUTBOX_ERROR_RETRY_FIRST_SECONDS
        self.failing["old"] = SERVER_ERROR
        events = self.enqueue("old")
        for now, tries in [(1000, 1), (1000 + first - 1, 1), (1000 + first, 2)]:
            self.flush_at(now)
            self.assertEqual(len(self.attempts), tries, "at %s" % now)
        self.failing.clear()
        self.flush_at(1000 + first + RETRY)
        self.assertEqual(self.sent, ["old"])
        self.assertEqual(self.pending_jobs(), [])
        self.assertIsNone(self.db.remote_event_retry("old", "old-attempt", 3))
        self.assertEqual(events[-1]["type"], "completed")

    def test_the_retry_delay_doubles_up_to_its_cap(self):
        # A blip recovers within seconds; a long outage settles at one try per cap.
        self.failing["old"] = SERVER_ERROR
        self.enqueue("old")
        now, delays = 1000, []
        for _ in range(6):
            self.flush_at(now)
            due = self.db.remote_event_retry("old", "old-attempt", 3)["retry_at"]
            delays.append(due - now)
            now = due
        self.assertEqual(delays, [5, 10, 20, 30, 30, 30])

    def test_a_conflict_is_named_once_not_at_every_retry(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        for round_ in range(3):
            self.flush_at(1000 + round_ * 300)
        self.assertEqual(len(self.attempts), 3)
        lines = [line for line in self.logs if "result upload conflict" in line]
        self.assertEqual(len(lines), 1, self.logs)

    def test_a_transport_failure_ends_the_round_instead_of_waiting_out_every_batch(self):
        self.failing["a"] = self.failing["b"] = READ_TIMEOUT
        self.enqueue("a")
        self.enqueue("b")
        self.flush_at(1000)
        self.assertEqual(self.attempts, ["a"])
        self.assertEqual(self.pending_jobs(), ["a", "b"])
        # The next round skips "a" (still waiting) and tries "b".
        self.flush_at(1001)
        self.assertEqual(self.attempts, ["a", "b"])

    def test_an_http_error_does_not_end_the_round(self):
        self.failing["a"] = self.failing["b"] = SERVER_ERROR
        self.enqueue("a")
        self.enqueue("b")
        self.flush_at(1000)
        self.assertEqual(self.attempts, ["a", "b"])

    def test_a_failing_batch_is_named_once_in_the_log(self):
        self.failing["old"] = SERVER_ERROR
        self.enqueue("old")
        for round_ in range(3):
            self.flush_at(1000 + round_ * RETRY)
        lines = [line for line in self.logs if "result upload failed" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("old", lines[0])
        self.assertIn("HTTP 500", lines[0])
        self.assertIn("2 event", lines[0])
        self.assertNotIn("token", lines[0])
        # A different failure of the same batch is news again.
        self.failing["old"] = READ_TIMEOUT
        self.flush_at(1000 + 3 * RETRY)
        lines = [line for line in self.logs if "result upload failed" in line]
        self.assertEqual(len(lines), 2, self.logs)
        self.assertIn("TimeoutError", lines[1])


class ClaimLoopLogTest(OutboxFailureFixture, unittest.TestCase):
    def test_repeated_claim_errors_are_named_once_until_a_claim_succeeds(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                self.fresh(parallel)
                same = TimeoutError("The read operation timed out")
                self.script_claims([same, same, same, None, same])
                self.run_loop()
                lines = [line for line in self.logs if "领取循环出错" in line]
                self.assertEqual(len(lines), 2, self.logs)
                self.assertIn("TimeoutError", lines[0])

    def test_a_different_claim_error_is_named_as_well(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                self.fresh(parallel)
                self.script_claims([TimeoutError("slow"), CloudError("bad gateway", status=502),
                                    CloudError("bad gateway", status=502)])
                self.run_loop()
                lines = [line for line in self.logs if "领取循环出错" in line]
                self.assertEqual(len(lines), 2, self.logs)
                self.assertIn("bad gateway", lines[1])


class StuckUploadNeverStopsTheAgentTest(OutboxFailureFixture, unittest.TestCase):
    def test_claims_presence_and_the_log_survive_a_batch_that_never_uploads(self):
        for parallel in (False, True):
            for name, error in (("409", CONFLICT), ("500", SERVER_ERROR), ("read timeout", READ_TIMEOUT)):
                with self.subTest(parallel=parallel, error=name):
                    self.fresh(parallel)
                    self.failing["old"] = error
                    events = self.enqueue("old")
                    self.script_claims([None, None, None])
                    maintained = []
                    real = self.agent.maintain
                    self.agent.maintain = lambda *a, _real=real, **k: (maintained.append(1), _real(*a, **k))[1]
                    self.run_loop()
                    self.assertEqual(len(self.claims), 3)
                    self.assertGreaterEqual(len(maintained), 3)
                    self.assertEqual(self.attempts, ["old"], "later rounds wait out the retry delay")
                    self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], events)
                    self.assertTrue([line for line in self.logs if "result upload" in line], self.logs)


class OutboxDoctorTextTest(unittest.TestCase):
    def test_a_failure_without_a_response_is_not_reported_as_http_0(self):
        self.assertIn("HTTP 500", cli._outbox_failure_text(500))
        self.assertNotIn("HTTP 0", cli._outbox_failure_text(0))
        self.assertIn("超时", cli._outbox_failure_text(0))


if __name__ == "__main__":
    unittest.main()
