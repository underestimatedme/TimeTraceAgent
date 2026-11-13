"""P0-1 first part: a result batch Valley keeps refusing (HTTP 409) stops being re-sent on its own, and stays findable.

What these tests pin down:

* a generic 409 (Valley's 40900 also covers a lost lease, a finished attempt and a sequence gap) is confirmed a bounded
  number of times, one retry wait apart, before automatic sending of that batch stops; one 409 is never taken as final;
* the stop is stored in the database, so a restart, an hour or a month later does not start the retries again; a
  database an older Runner already filled with failures stops nothing at the upgrade and gets one fresh request for
  its long-failed batches first (test_outbox_legacy_check);
* the events, sequences and occurrence times stay byte for byte as queued, are never marked sent or set aside, and can
  be put back into service by hand (`agent retry-results`) or set aside by hand (`agent discard-results`);
* 401, network errors, lost receipts, 5xx and rate limits are never counted as a refusal, and a later ACK is the only
  thing that marks events as delivered;
* the stopped batch never holds back other batches, the claim, the heartbeat or the approval fast path.

Time is a fixed clock patched into `time.time`; nothing sleeps. Synthetic ids only.
"""
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from timetrace import agent as agent_module
from timetrace import audit, cli, config
from timetrace.agent import OUTBOX_ERROR_RETRY_SECONDS, OUTBOX_REFUSED_RETRY_SECONDS, Agent
from timetrace.cloud import CloudError
from timetrace.db import Database

from tests.test_outbox_isolation import CONFLICT, READ_TIMEOUT, SERVER_ERROR, OutboxFailureFixture

# getattr: the module also loads on the code before the change, where these tests have to fail for the right reason.
PAUSE_AFTER = getattr(agent_module, "OUTBOX_REFUSED_PAUSE_AFTER", 5)
REFUSED = OUTBOX_REFUSED_RETRY_SECONDS
RETRY = OUTBOX_ERROR_RETRY_SECONDS
HOUR, DAY = 3600, 24 * 3600
RATE_LIMITED = lambda: CloudError("slow down", status=429)
BAD_GATEWAY = lambda: CloudError("bad gateway", status=502)
GARBLED_REPLY = lambda: CloudError("reply was not JSON", status=0)
NETWORK_DOWN = lambda: OSError("network is unreachable")


class RefusalFixture(OutboxFailureFixture):
    """The shared outbox fixture, plus what the new behaviour needs: the bodies Valley received and a process restart."""

    def fresh(self, parallel=False):
        super().fresh(parallel)
        self.bodies = []  # (job, attempt, epoch, events) of every upload, whatever Valley answered
        inner = self.agent.cloud.append_events

        def spy(token, job, attempt, epoch, events):
            self.bodies.append((job, attempt, epoch, list(events)))
            return inner(token, job, attempt, epoch, events)
        self.agent.cloud.append_events = spy
        self.queued = {}  # job → the events enqueue() stored

    def enqueue(self, job):
        events = super().enqueue(job)
        self.queued[job] = events
        return events

    def restart(self):
        """What a restart of the Runner does: a new connection on the same file, a new Agent, no memory."""
        cloud, home, path = self.agent.cloud, self.agent.home, self.db.path
        self.db.close()
        self.db = Database(path)
        self.addCleanup(self.db.close)
        self.agent = Agent(self.db, cloud, {}, home, lambda: "token", report_protocol=True,
                           inventory=lambda: ([], []), log=self.logs.append)

    def snapshot(self):
        return [tuple(row) for row in self.db.conn.execute("SELECT * FROM remote_outbox ORDER BY id")]

    def key(self, job):
        return (job, job + "-attempt", 3)

    def claim(self, job):
        return {"job": {"id": job}, "attempt_id": job + "-attempt", "lease_epoch": 3}

    def retry(self, job):
        return self.db.remote_event_retry(*self.key(job))

    def refuse_until_paused(self, job="old", start=1000):
        """Flush each time the batch is due until it has been refused PAUSE_AFTER times. Returns the clock."""
        if job not in self.queued:
            self.enqueue(job)
        self.failing[job] = CONFLICT
        now = start
        for _ in range(PAUSE_AFTER):
            self.flush_at(now)
            now = self.retry(job)["retry_at"]
        return now

    def hold_rows(self):
        try:
            return [dict(row) for row in self.db.conn.execute("SELECT * FROM remote_outbox_hold")]
        except sqlite3.OperationalError:
            return []


class RefusalPauseTest(RefusalFixture, unittest.TestCase):
    def test_one_409_is_never_taken_as_final(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        self.flush_at(1000)
        self.failing.clear()
        self.flush_at(1000 + REFUSED)
        self.assertEqual(self.sent, ["old"])
        self.assertEqual(self.pending_jobs(), [])

    def test_confirmed_refusals_stop_automatic_sending_and_keep_the_evidence_untouched(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        before = self.snapshot()
        now = 1000
        for _ in range(PAUSE_AFTER + 3):
            self.flush_at(now)
            now += REFUSED
        self.assertEqual(self.attempts, ["old"] * PAUSE_AFTER, "exactly the confirmations, then no more")
        for later in (now + HOUR, now + 40 * DAY):
            self.flush_at(later)
        self.assertEqual(len(self.attempts), PAUSE_AFTER)
        # Every confirmation carried the whole original batch, in order.
        self.assertEqual([body[3] for body in self.bodies], [self.queued["old"]] * PAUSE_AFTER)
        # Nothing about the events changed: payload, seq, times, not sent, not set aside.
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([row["payload"] for row in self.db.pending_remote_events()], self.queued["old"])
        self.assertEqual(self.retry("old")["status"], 409)
        self.assertEqual(self.db.abandoned_remote_event_count(), 0)

    def test_the_stop_survives_a_restart_and_a_long_idle(self):
        self.refuse_until_paused()
        self.failing.clear()  # even a healthy Valley is not asked: the person decides
        for step in (HOUR, 40 * DAY):
            self.restart()
            self.flush_at(1000 + step)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.attempts), PAUSE_AFTER)
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_the_count_of_confirmations_survives_a_restart(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        now = 1000
        for _ in range(PAUSE_AFTER - 1):
            self.flush_at(now)
            now = self.retry("old")["retry_at"]
            self.restart()  # a restart after every refusal must not lose the count
        self.assertEqual(len(self.attempts), PAUSE_AFTER - 1)
        self.flush_at(now)
        self.assertEqual(len(self.attempts), PAUSE_AFTER)
        self.flush_at(now + REFUSED)
        self.assertEqual(len(self.attempts), PAUSE_AFTER, "stopped by the last confirmation")

    def test_two_uploads_in_one_wait_window_are_one_confirmation(self):
        # The normal flush and the approval fast path can both be refused for the same wait window.
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        with patch("timetrace.agent.time.time", return_value=1000):
            for _ in range(PAUSE_AFTER + 2):
                self.agent._refuse_batch(self.key("old"))
        self.flush_at(1000 + REFUSED)
        self.assertEqual(self.attempts, ["old"], "the repeated reports counted once, so the batch is still being retried")

    def test_the_pause_is_named_once_in_the_log_and_points_at_the_way_out(self):
        self.refuse_until_paused()
        self.flush_at(5000)
        self.flush_at(9000)
        lines = [line for line in self.logs if "已停止自动重试" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("old-attempt", lines[0])
        self.assertIn("agent doctor", lines[0])
        self.assertIn("未标记为已送达", lines[0])

    def test_a_late_answer_for_an_already_stopped_batch_changes_nothing(self):
        self.refuse_until_paused()
        before_retry, before_holds = dict(self.retry("old")), self.hold_rows()
        key = self.key("old")
        rows = self.db.pending_remote_events_for(*key)
        with patch("timetrace.agent.time.time", return_value=50000):
            self.agent._refuse_batch(key)                      # a request that was still in flight comes back 409
            self.agent._defer_failed_batch(key, rows, SERVER_ERROR(), self.retry("old"))  # or comes back 500
        self.assertEqual(self.retry("old"), before_retry)
        self.assertEqual(self.hold_rows(), before_holds)

    def test_another_failure_between_refusals_breaks_the_run(self):
        # "Consecutive": 409 ×(N-1), one 500, 409 ×(N-1) is not N refusals in a row.
        self.enqueue("old")
        now = 1000
        for status in [CONFLICT] * (PAUSE_AFTER - 1) + [SERVER_ERROR] + [CONFLICT] * (PAUSE_AFTER - 1):
            self.failing["old"] = status
            self.flush_at(now)
            now = self.retry("old")["retry_at"]
        self.assertEqual(len(self.attempts), 2 * PAUSE_AFTER - 1)
        self.failing["old"] = CONFLICT
        self.flush_at(now)  # the N-th in a row only now
        self.assertEqual(len(self.attempts), 2 * PAUSE_AFTER)
        self.flush_at(now + DAY)
        self.assertEqual(len(self.attempts), 2 * PAUSE_AFTER)

    def test_the_total_failure_count_keeps_its_old_meaning(self):
        # `failures` counts every failed upload of the batch, whatever the kind; it is not the count of refusals.
        self.enqueue("old")
        now = 1000
        for status in (SERVER_ERROR, CONFLICT, READ_TIMEOUT, CONFLICT):
            self.failing["old"] = status
            self.flush_at(now)
            now = self.retry("old")["retry_at"]
        self.assertEqual(self.retry("old")["failures"], 4)


class StoppedBatchIsolationTest(RefusalFixture, unittest.TestCase):
    def test_new_batches_the_claim_and_the_flush_go_on_while_the_old_one_is_stopped(self):
        self.refuse_until_paused("old")
        self.enqueue("new")
        with patch("timetrace.agent.time.time", return_value=5000):
            self.agent.flush_outbox()  # must not raise: nothing new was refused
            self.assertEqual(self.agent.run_once(), "idle")
        self.assertEqual(self.sent, ["new"])
        self.assertEqual(self.claims, ["token"])
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_a_stopped_attempt_does_not_stop_its_own_job_s_other_attempts(self):
        self.refuse_until_paused("old")
        for event in ({"seq": 1, "type": "running"}, {"seq": 2, "type": "completed"}):
            self.db.queue_remote_event("old", "old-attempt-2", 3, event)
        self.failing.clear()
        self.flush_at(5000)
        self.assertEqual([body[1] for body in self.bodies][-1:], ["old-attempt-2"])
        self.assertEqual(self.sent, ["old"])
        self.assertEqual([row["attempt_id"] for row in self.db.pending_remote_events()], ["old-attempt"] * 2)

    def test_the_approval_fast_path_skips_the_stopped_attempt_but_serves_the_others(self):
        self.refuse_until_paused("old")
        self.enqueue("new")
        self.failing.clear()
        with patch("timetrace.agent.time.time", return_value=1000 + 2 * HOUR):
            self.assertFalse(self.agent._upload_due(self.claim("old")), "the pump must not keep waking for it")
            before = len(self.attempts)
            self.agent._upload_attempt(self.claim("old"), True)
            self.agent._upload_attempt(self.claim("old"), False)
            self.assertEqual(len(self.attempts), before, "a new event of a stopped attempt waits in the queue")
            self.assertTrue(self.agent._upload_due(self.claim("new")))
            self.agent._upload_attempt(self.claim("new"), True)
        self.assertEqual(self.attempts[before:], ["new"])
        self.assertEqual(self.sent, ["new"])
        self.assertEqual([(row["job_id"], row["seq"]) for row in self.db.pending_remote_events()],
                         [("old", 1), ("old", 2), ("new", 2)], "only the approval-path event of 'new' was acknowledged")

    def test_the_fast_path_confirms_refusals_too(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        now = 1000
        for _ in range(PAUSE_AFTER + 2):
            with patch("timetrace.agent.time.time", return_value=now):
                self.agent._upload_attempt(self.claim("old"), True)
            now = self.retry("old")["retry_at"]
        self.assertEqual(self.attempts, ["old"] * PAUSE_AFTER, "stopped on the fast path as well")


class OtherFailuresAreNeverPermanentTest(RefusalFixture, unittest.TestCase):
    """401, network errors, 5xx, rate limits and lost receipts keep being retried, however long they last."""

    def test_failures_that_are_not_a_refusal_are_retried_forever_and_never_stopped(self):
        rounds = 3 * PAUSE_AFTER + 4
        for name, error in (("network", NETWORK_DOWN), ("timeout", READ_TIMEOUT), ("500", SERVER_ERROR),
                            ("502", BAD_GATEWAY), ("429", RATE_LIMITED), ("unreadable reply", GARBLED_REPLY)):
            with self.subTest(failure=name):
                self.fresh()
                self.failing["old"] = error
                self.enqueue("old")
                now = 1000
                for _ in range(rounds):
                    self.flush_at(now)
                    now = self.retry("old")["retry_at"]
                self.assertEqual(len(self.attempts), rounds, "every round whose wait has run out sends the batch again")
                self.assertEqual([row for row in self.hold_rows() if row["paused_at"] is not None], [])
                # Valley recovers: the very same events go out and only the ACK marks them delivered.
                self.failing.clear()
                self.assertEqual(self.pending_jobs(), ["old"])
                self.flush_at(now)
                self.assertEqual(self.sent, ["old"])
                self.assertEqual(self.bodies[-1][3], self.queued["old"])
                self.assertEqual(self.pending_jobs(), [])
                self.assertIsNone(self.retry("old"))
                self.assertEqual(self.db.abandoned_remote_event_count(), 0)

    def test_401_is_not_a_refusal_and_is_not_recorded_against_the_batch(self):
        self.failing["old"] = lambda: CloudError("unauthorized", status=401)
        self.enqueue("old")
        for step in range(3 * PAUSE_AFTER):
            with self.assertRaises(CloudError):
                self.flush_at(1000 + step * REFUSED)
        self.assertEqual(len(self.attempts), 3 * PAUSE_AFTER)
        self.assertIsNone(self.retry("old"))
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_a_lost_receipt_is_sent_again_after_its_wait_and_delivered_only_on_ack(self):
        # The server stores the batch, the answer never arrives; the Runner cannot know and must send it again.
        received = []

        def commit_then_lose_the_answer(token, job, attempt, epoch, events):
            received.append(list(events))
            raise OSError("connection reset after the server committed")
        self.agent.cloud.append_events = commit_then_lose_the_answer
        self.enqueue("old")
        now = 1000
        for _ in range(2 * PAUSE_AFTER):
            self.flush_at(now)
            self.assertEqual(self.pending_jobs(), ["old"], "no ACK, so not delivered")
            self.flush_at(now + 1)  # still inside the wait: nothing is sent
            now = self.retry("old")["retry_at"]
        self.assertEqual(len(received), 2 * PAUSE_AFTER)
        self.assertTrue(all(body == self.queued["old"] for body in received))
        delivered = []
        self.agent.cloud.append_events = lambda token, job, attempt, epoch, events: delivered.append(list(events))
        self.flush_at(now)
        self.assertEqual(delivered, [self.queued["old"]])
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual([row["sent_at"] is not None for row in self.db.conn.execute("SELECT sent_at FROM remote_outbox")], [True, True])

    def test_success_clears_whatever_was_recorded_against_the_batch(self):
        self.failing["old"] = CONFLICT
        self.enqueue("old")
        now = 1000
        for _ in range(PAUSE_AFTER - 1):
            self.flush_at(now)
            now = self.retry("old")["retry_at"]
        self.failing.clear()
        self.flush_at(now)
        self.assertEqual(self.pending_jobs(), [])
        self.assertIsNone(self.retry("old"))
        self.assertEqual(self.hold_rows(), [])
        # The same attempt later queues one more event: nothing from the old refusals applies to it.
        self.db.queue_remote_event("old", "old-attempt", 3, {"seq": 3, "type": "completed"})
        self.failing["old"] = CONFLICT
        self.flush_at(now + REFUSED)
        self.flush_at(now + 2 * REFUSED)
        self.assertEqual(self.attempts[-2:], ["old", "old"])


class OlderDatabaseTest(RefusalFixture, unittest.TestCase):
    """A database an older Runner filled with refusals (same tables, no record of a stop)."""

    def make_old_database(self):
        for job in ("stuck-a", "stuck-b", "few", "flaky"):
            self.enqueue(job)
        self.db.conn.execute("DROP TABLE IF EXISTS remote_outbox_hold")  # the older Runner never had it
        # (job, status, failures): the numbers of the two batches found on a real computer, and two that must not stop
        for job, status, failures in (("stuck-a", 409, 13181), ("stuck-b", 409, 1373), ("few", 409, 2), ("flaky", 500, 13181)):
            self.db.conn.execute("INSERT INTO remote_outbox_retry VALUES (?,?,?,?,?,?,?)",
                                 (job, job + "-attempt", 3, status, failures, 900, 960))
        self.restart()  # the upgraded Runner starts on that file

    def test_batches_refused_thousands_of_times_get_one_check_then_stop_and_the_others_carry_on(self):
        self.make_old_database()
        before = self.snapshot()
        self.assertEqual([row for row in self.hold_rows() if row["paused_at"] is not None], [],
                         "opening the database stops nothing: the old counter is not proof of a run of refusals")
        self.enqueue("new")
        self.failing.update({"stuck-a": CONFLICT, "stuck-b": CONFLICT, "few": CONFLICT, "flaky": SERVER_ERROR})
        now = 5000
        for _ in range(PAUSE_AFTER + 3):
            self.flush_at(now)
            now += HOUR
        self.assertEqual(self.attempts.count("stuck-a"), 1, "one fresh request to check, then the stop")
        self.assertEqual(self.attempts.count("stuck-b"), 1)
        self.assertEqual(self.attempts.count("new"), 1)
        self.assertEqual(self.attempts.count("few"), PAUSE_AFTER, "a young refusal gets the same bounded confirmation")
        self.assertEqual(self.attempts.count("flaky"), PAUSE_AFTER + 3, "a server error is never stopped, however often it failed")
        # Their events are as the older Runner left them.
        self.assertEqual([r for r in self.snapshot() if r[1].startswith("stuck")], [r for r in before if r[1].startswith("stuck")])
        self.assertEqual(self.db.abandoned_remote_event_count(), 0)

    def test_the_upgrade_is_idempotent_and_a_restart_does_not_undo_a_person_s_decision(self):
        self.make_old_database()
        first = self.hold_rows()
        self.restart()
        self.restart()
        self.assertEqual(self.hold_rows(), first)
        # The one check is refused: the batch stops. A person resumes it; later restarts must not mark it again on
        # the strength of the old counter.
        self.failing["stuck-a"] = CONFLICT
        self.flush_at(5000)
        self.assertEqual(self.attempts.count("stuck-a"), 1)
        self.assertEqual(self.db.resume_paused_remote_events("stuck-a", "stuck-a-attempt"), 2)
        self.restart()
        self.restart()
        stuck = [row for row in self.hold_rows() if row["job_id"] == "stuck-a"][0]
        self.assertEqual((stuck["paused_at"], stuck["basis"], stuck["refused"]), (None, None, 0))
        self.flush_at(9000)
        self.assertEqual(self.attempts.count("stuck-a"), 2, "the resumed batch is sent, not stopped again by the old count")
        self.flush_at(9000 + REFUSED)
        self.assertEqual(self.attempts.count("stuck-a"), 3, "an ordinary run of confirmations from zero")

    def test_a_set_aside_batch_is_not_stopped_again_and_not_counted_twice(self):
        self.make_old_database()
        self.assertEqual(self.db.discard_conflicted_remote_events("stuck-b", "stuck-b-attempt", now=2000), 2)
        self.restart()
        self.assertEqual([row["job_id"] for row in self.hold_rows()], ["stuck-a"])
        self.assertEqual(self.db.abandoned_remote_event_count(), 2)

    def test_an_older_runner_can_still_use_the_file(self):
        # Downgrade: the older Runner writes the retry table by position and never learns of the new table.
        self.refuse_until_paused("old")
        conn = self.db.conn
        conn.execute("INSERT INTO remote_outbox_retry VALUES (?,?,?,?,1,?,?)", ("x", "x-attempt", 3, 500, 1.0, 2.0))
        conn.execute("INSERT INTO remote_outbox_retry VALUES (?,?,?,?,1,?,?)"
                     " ON CONFLICT(job_id,attempt_id,lease_epoch) DO UPDATE SET status=excluded.status,"
                     " failures=failures+1, last_failed_at=excluded.last_failed_at, retry_at=excluded.retry_at",
                     ("x", "x-attempt", 3, 409, 3.0, 4.0))
        old_view = conn.execute("SELECT * FROM remote_outbox WHERE sent_at IS NULL AND abandoned_at IS NULL ORDER BY id").fetchall()
        self.assertEqual([json.loads(r["payload"]) for r in old_view], self.queued["old"], "its events are all still there")
        # It acknowledges the batch the way it always did (retry row only); the next open sweeps the orphaned stop.
        conn.execute("UPDATE remote_outbox SET sent_at=1 WHERE job_id='old'")
        conn.execute("DELETE FROM remote_outbox_retry WHERE job_id='old'")
        self.restart()
        self.assertEqual([row["job_id"] for row in self.hold_rows()], [])


class RecoveryAndDiagnosticsTest(RefusalFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.home = self.agent.home
        env = patch.dict(os.environ, {"TIMETRACE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        config.ensure_dirs(config.home())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def doctor(self):
        with patch("timetrace.cli._adapters", return_value={}), patch("timetrace.cli.CredentialStore.load", return_value=None):
            return self.run_cli("agent", "doctor")[1]

    # ---- doctor -----------------------------------------------------------------------------------------------
    def test_doctor_says_the_automatic_retry_stopped_what_is_kept_and_what_to_do(self):
        self.enqueue("old")
        self.refuse_until_paused("old")
        out = self.doctor()
        text = out
        self.assertIn("已停止自动重试", text)
        self.assertIn("HTTP 409", text)
        self.assertIn("%d 次" % PAUSE_AFTER, text)
        self.assertIn("2 条", text)
        self.assertIn("序列 1", text)
        self.assertIn("2026-10-30T00:32:00Z", text, "the occurrence time recorded in the events")
        self.assertIn("没有标记为已送达", text)
        self.assertIn("timetrace agent retry-results old old-attempt --yes", text)
        self.assertIn("timetrace agent discard-results old old-attempt --yes", text)
        self.assertNotIn("待恢复", text)

    def test_doctor_does_not_call_a_retried_failure_stopped(self):
        self.enqueue("slow")
        self.failing["slow"] = SERVER_ERROR
        self.flush_at(1000)
        out = self.doctor()
        self.assertIn("结果上报待恢复: 任务 slow", out)
        self.assertNotIn("已停止自动重试", out)
        self.assertNotIn("retry-results", out)

    def test_doctor_names_a_batch_that_awaits_its_check_by_its_old_failure_count_and_does_not_call_it_stopped(self):
        OlderDatabaseTest.make_old_database(self)
        out = self.doctor()
        stuck = [line for line in out.splitlines() if "stuck-a-attempt" in line]
        self.assertIn("待核对", stuck[0])
        self.assertIn("13181", stuck[0])
        self.assertIn("升级前", stuck[0])
        self.assertNotIn("已停止自动重试", out)
        self.assertNotIn("一直", out, "the last answer being 409 is not 'it always said 409'")
        self.assertIn("1373", out)
        self.assertNotIn("待核对: 任务 few", out)

    # ---- the controlled way back ------------------------------------------------------------------------------
    def test_retry_results_without_yes_changes_nothing(self):
        self.refuse_until_paused("old")
        code, out, _ = self.run_cli("agent", "retry-results", "old", "old-attempt")
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertIn("2 条", out)
        self.failing.clear()
        self.flush_at(9000)
        self.assertEqual(self.sent, [])
        self.assertFalse(audit.path(self.home).exists())

    def test_retry_results_lets_the_original_batch_go_out_once_more_and_only_an_ack_delivers_it(self):
        self.refuse_until_paused("old")
        before = self.snapshot()
        code, out, err = self.run_cli("agent", "retry-results", "old", "old-attempt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before, "resuming touches no event")
        entries = [e for e in audit.read(self.home) if e["event"] == "retry_requested"]
        self.assertEqual([(e["job_id"], e["attempt_id"], e["events"]) for e in entries], [("old", "old-attempt", 2)])
        self.failing.clear()
        self.flush_at(9000)
        self.assertEqual(self.sent, ["old"])
        self.assertEqual(self.bodies[-1][3], self.queued["old"], "the original events, not rebuilt ones")
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(self.hold_rows(), [])
        self.assertIsNone(self.retry("old"))
        self.assertNotIn("old-attempt", self.doctor())
        self.flush_at(9000 + DAY)
        self.assertEqual(self.sent, ["old"], "delivered once")

    def test_when_valley_still_refuses_the_resumed_batch_gets_one_more_bounded_round(self):
        self.refuse_until_paused("old")
        self.assertEqual(self.run_cli("agent", "retry-results", "old", "old-attempt", "--yes")[0], 0)
        now = 9000
        for _ in range(PAUSE_AFTER + 3):
            self.flush_at(now)
            now += REFUSED
        self.assertEqual(len(self.attempts), 2 * PAUSE_AFTER)
        self.flush_at(now + 40 * DAY)
        self.assertEqual(len(self.attempts), 2 * PAUSE_AFTER)
        self.assertEqual(self.pending_jobs(), ["old"])

    def test_retry_results_only_resumes_what_was_stopped(self):
        self.enqueue("slow")
        self.failing["slow"] = SERVER_ERROR
        self.flush_at(1000)
        for job, attempt in (("slow", "slow-attempt"), ("nobody", "nothing")):
            code, out, err = self.run_cli("agent", "retry-results", job, attempt, "--yes")
            self.assertEqual(code, 2)
            self.assertIn("409", out + err)
        self.assertFalse(audit.path(self.home).exists())

    def test_retry_results_needs_its_audit_entry_first(self):
        self.refuse_until_paused("old")
        (self.home / audit.FILE_NAME).mkdir()
        code, out, _ = self.run_cli("agent", "retry-results", "old", "old-attempt", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("没有", out)
        self.failing.clear()
        self.flush_at(9000)
        self.assertEqual(self.sent, [], "still stopped")

    def test_a_stopped_batch_can_still_be_set_aside_by_hand_and_is_then_never_sent_or_counted_delivered(self):
        self.refuse_until_paused("old")
        code, out, err = self.run_cli("agent", "discard-results", "old", "old-attempt", "--yes")
        self.assertEqual(code, 0, err)
        rows = self.db.conn.execute("SELECT sent_at, abandoned_at FROM remote_outbox").fetchall()
        self.assertEqual([(r["sent_at"], r["abandoned_at"] is not None) for r in rows], [(None, True)] * 2)
        self.assertEqual(self.hold_rows(), [])
        self.failing.clear()
        self.flush_at(9000)
        self.assertEqual(self.sent, [])
        self.assertIn("已手动丢弃的结果: 2 条", self.doctor())

    # ---- the structure the web part will read -----------------------------------------------------------------
    def test_the_health_summary_is_counts_and_times_only(self):
        self.assertEqual(self.db.remote_outbox_health(), {
            "schema": 1, "paused_batches": 0, "paused_events": 0, "paused_reason": None, "paused_since": None,
            "oldest_paused_event_at": None, "retrying_batches": 0, "retrying_events": 0, "set_aside_events": 0})
        self.refuse_until_paused("job-aaa")
        self.enqueue("job-bbb")
        self.failing["job-bbb"] = SERVER_ERROR
        self.flush_at(5000)
        self.enqueue("job-ccc")
        self.db.defer_remote_events("job-ccc", "job-ccc-attempt", 3, 409, 1000, 1060)
        self.db.discard_conflicted_remote_events("job-ccc", "job-ccc-attempt", now=2000)
        health = self.db.remote_outbox_health()
        self.assertEqual((health["paused_batches"], health["paused_events"], health["paused_reason"]), (1, 2, "http_409"))
        self.assertEqual((health["retrying_batches"], health["retrying_events"], health["set_aside_events"]), (1, 2, 2))
        self.assertIsInstance(health["paused_since"], int)
        self.assertIsInstance(health["oldest_paused_event_at"], int)
        text = json.dumps(health)
        for private in ("job-aaa", "job-bbb", "job-ccc", "attempt", "running", "completed", "2026-10-30"):
            self.assertNotIn(private, text, "no ids, payloads or times of the events")


if __name__ == "__main__":
    unittest.main()
