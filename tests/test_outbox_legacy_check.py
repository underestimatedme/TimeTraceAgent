"""P0-1 review fix: a database an older Runner filled with failures is not stopped at the upgrade, it is checked once.

The older Runner kept three facts per batch (`remote_outbox_retry`): the status of the LAST failure, the TOTAL number of
failures of every kind, and when to try again. That is not a run of refusals: 4 server errors and one 409 are 5 failures
whose last status is 409, and so are 13,181 refusals. So the upgrade must not turn the old numbers into a stop. What
these tests pin down, on SQLite files written the way the older Runner wrote them (its two tables, its SQL, no
`remote_outbox_hold`):

* opening such a file stops nothing; a batch with many failures and a last answer of 409 only gets an explicit
  "to be checked" mark, a young or mixed one is counted from zero like a new batch;
* the first fresh request decides: an ACK delivers the original events as they were; a real 409 stops the batch (one
  extra request in all) and says what the old and the new evidence are; a network error, 401, 429 or 5xx is never a
  refusal and keeps the old backoff;
* events, sequences, `observed_at`, queue time and the sent / set-aside marks stay exactly as the older Runner left them;
* a person's resume is not undone by a reopen, and the older Runner can still write its seven-column table.

Fixed clock, no sleeps, synthetic ids.
"""
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from timetrace import audit, cli, config
from timetrace import db as db_module
from timetrace.agent import OUTBOX_ERROR_RETRY_SECONDS
from timetrace.cloud import CloudError

from tests.test_outbox_isolation import CONFLICT, READ_TIMEOUT, SERVER_ERROR
from tests.test_outbox_refusal_pause import (BAD_GATEWAY, DAY, GARBLED_REPLY, HOUR, NETWORK_DOWN, PAUSE_AFTER, RATE_LIMITED,
                                             REFUSED, RefusalFixture)

# getattr: the module also loads on the code before the change, where these tests have to fail for the right reason.
CHECK_AFTER = getattr(db_module, "OUTBOX_LEGACY_CHECK_AFTER", 60)

EVENTS = [{"seq": 1, "type": "running", "observed_at": "2026-10-30T00:32:00Z"},
          {"seq": 2, "type": "completed", "result_summary": "kept exactly as queued", "observed_at": "2026-10-30T00:32:34Z"}]
APPROVAL_EVENTS = [{"seq": 1, "type": "running", "observed_at": "2026-10-30T00:32:00Z"},
                   {"seq": 2, "type": "approval_requested", "request_id": "r1", "tool": "Bash", "summary": "echo fixture",
                    "observed_at": "2026-10-30T00:32:10Z"}]

# What an older Runner recorded per failed upload: the HTTP status, 0 when no usable answer came back.
LONG_409 = [409] * 13181                                  # one computer's real number: refused once a minute for nine days
ALL_409 = [409] * 1373                                    # the other one
MOSTLY_NOT_409 = [0] * 1000 + [500] * 372 + [409]         # 1373 failures, but only the last one was a refusal
MIXED = [500, 500, 500, 500, 409]                         # a few failures, the last being 409

# The two tables as the older Runner (before remote_outbox_hold) created them, and the statement it used to record a failure.
OLD_RUNNER_TABLES = """
CREATE TABLE remote_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    sent_at     INTEGER,
    created_at  INTEGER NOT NULL,
    abandoned_at INTEGER,
    UNIQUE(job_id, attempt_id, seq)
);
CREATE TABLE remote_outbox_retry (
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    status INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    last_failed_at REAL NOT NULL,
    retry_at REAL NOT NULL,
    PRIMARY KEY (job_id, attempt_id, lease_epoch)
);
"""
OLD_RUNNER_FAILURE = ("INSERT INTO remote_outbox_retry VALUES (?,?,?,?,1,?,?)"
                      " ON CONFLICT(job_id,attempt_id,lease_epoch) DO UPDATE SET"
                      " status=excluded.status, failures=failures+1,"
                      " last_failed_at=excluded.last_failed_at, retry_at=excluded.retry_at")
RETRY_COLUMNS = ["job_id", "attempt_id", "lease_epoch", "status", "failures", "last_failed_at", "retry_at"]


def write_old_runner_database(path, histories, events=EVENTS, epoch=3):
    """A file as an older Runner leaves it: for each job its queued events and one retry row per failure it recorded."""
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_RUNNER_TABLES)
    for job, history in histories.items():
        for event in events:
            conn.execute("INSERT INTO remote_outbox (job_id,attempt_id,lease_epoch,seq,payload,created_at) VALUES (?,?,?,?,?,?)",
                         (job, job + "-attempt", epoch, event["seq"], json.dumps(event, ensure_ascii=False, sort_keys=True), 800))
        failed_at = 900.0 - 60 * (len(history) - 1)  # one failure a minute, the last one at t=900
        for status in history:
            conn.execute(OLD_RUNNER_FAILURE, (job, job + "-attempt", epoch, status, failed_at, failed_at + (60 if status == 409 else 30)))
            failed_at += 60
    conn.commit()
    conn.close()


def raw_rows(path, table):
    """The table as it is on disk, read with plain sqlite3: the older Runner's view, not this code's."""
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return [tuple(row) for row in conn.execute("SELECT * FROM %s ORDER BY 1,2,3,4" % table)]
    finally:
        conn.close()


class OldFormatFixture(RefusalFixture):
    def open_old_database(self, histories, events=EVENTS):
        """Replace the fixture's database by a file an older Runner wrote, then start the upgraded Runner on it."""
        path = self.db.path
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(str(path) + suffix):
                os.remove(str(path) + suffix)
        write_old_runner_database(path, histories, events)
        self.old_outbox, self.old_retry = raw_rows(path, "remote_outbox"), raw_rows(path, "remote_outbox_retry")
        self.restart()
        self.queued = {job: list(events) for job in histories}

    def hold(self, job):
        rows = [row for row in self.hold_rows() if row["job_id"] == job]
        return rows[0] if rows else None

    def paused(self):
        return [row["job_id"] for row in self.hold_rows() if row["paused_at"] is not None]

    def outbox_state(self, job):
        return [(row["seq"], json.loads(row["payload"]), row["created_at"], row["sent_at"], row["abandoned_at"])
                for row in self.db.conn.execute("SELECT * FROM remote_outbox WHERE job_id=? ORDER BY seq", (job,))]


class UpgradeStopsNothingTest(OldFormatFixture, unittest.TestCase):
    def test_opening_an_older_database_stops_nothing_and_marks_only_the_long_failed_batches_for_a_check(self):
        self.open_old_database({
            "long": LONG_409, "all-409": ALL_409, "mostly-not-409": MOSTLY_NOT_409, "at-limit": [409] * CHECK_AFTER,
            "below-limit": [409] * (CHECK_AFTER - 1), "mixed": MIXED, "last-500": [409] * 500 + [500]})
        self.assertEqual(self.paused(), [], "the old counters are evidence of persistence, not of refusals")
        self.assertEqual(self.db.paused_remote_batches(), [])
        marked = {row["job_id"]: row for row in self.hold_rows()}
        self.assertEqual(sorted(marked), ["all-409", "at-limit", "long", "mostly-not-409"])
        for job, failures in (("long", 13181), ("all-409", 1373), ("mostly-not-409", 1373), ("at-limit", CHECK_AFTER)):
            with self.subTest(job=job):
                self.assertEqual(marked[job]["basis"], "legacy_check", "an explicit, separate origin: to be checked")
                self.assertEqual((marked[job]["refused"], marked[job]["legacy_failures"]), (0, failures))
        # Not one byte of what the older Runner stored is changed by the upgrade.
        self.assertEqual(raw_rows(self.db.path, "remote_outbox"), self.old_outbox)
        self.assertEqual(raw_rows(self.db.path, "remote_outbox_retry"), self.old_retry)
        self.assertEqual([column[1] for column in self.db.conn.execute("PRAGMA table_info(remote_outbox_retry)")], RETRY_COLUMNS)
        self.assertEqual(self.db.abandoned_remote_event_count(), 0)

    def test_reopening_changes_no_mark(self):
        self.open_old_database({"long": LONG_409, "mixed": MIXED})
        first = self.hold_rows()
        for _ in range(3):
            self.restart()
        self.assertEqual(self.hold_rows(), first)

    def test_the_mark_is_dropped_when_an_older_runner_got_the_batch_acknowledged(self):
        self.open_old_database({"long": LONG_409})
        self.db.conn.execute("UPDATE remote_outbox SET sent_at=1 WHERE job_id='long'")  # the way it always acknowledged
        self.db.conn.execute("DELETE FROM remote_outbox_retry WHERE job_id='long'")
        self.restart()
        self.assertEqual(self.hold_rows(), [])


class FirstFreshRequestTest(OldFormatFixture, unittest.TestCase):
    def test_a_high_old_total_with_a_last_409_is_delivered_when_valley_accepts_the_next_request(self):
        for name, history in (("long", LONG_409), ("all-409", ALL_409), ("mostly-not-409", MOSTLY_NOT_409)):
            with self.subTest(history=name):
                self.fresh()
                self.open_old_database({name: history})
                before = self.outbox_state(name)
                self.flush_at(5000)
                self.assertEqual(self.attempts, [name], "exactly one request")
                self.assertEqual(self.sent, [name])
                self.assertEqual(self.bodies, [(name, name + "-attempt", 3, EVENTS)], "the original events, not rebuilt ones")
                self.assertEqual(self.pending_jobs(), [])
                self.assertIsNone(self.retry(name))
                self.assertEqual(self.hold_rows(), [])
                after = self.outbox_state(name)
                self.assertEqual([(seq, payload, created) for seq, payload, created, _, _ in after],
                                 [(seq, payload, created) for seq, payload, created, _, _ in before])
                self.assertTrue(all(sent is not None and abandoned is None for _, _, _, sent, abandoned in after))
                self.flush_at(5000 + DAY)
                self.assertEqual(self.attempts, [name], "delivered once")

    def test_a_low_count_mixed_old_batch_is_delivered_and_never_marked(self):
        self.open_old_database({"mixed": MIXED})
        self.assertEqual(self.hold_rows(), [])
        self.flush_at(5000)
        self.assertEqual(self.sent, ["mixed"])
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(self.hold_rows(), [])
        self.assertTrue(all(sent is not None for _, _, _, sent, _ in self.outbox_state("mixed")))

    def test_a_low_count_mixed_old_batch_that_is_refused_is_counted_from_zero_like_a_new_one(self):
        self.open_old_database({"mixed": MIXED})
        self.failing["mixed"] = CONFLICT
        now = 5000
        for _ in range(PAUSE_AFTER + 3):
            self.flush_at(now)
            now += REFUSED
        self.assertEqual(self.attempts, ["mixed"] * PAUSE_AFTER, "the old 4 server errors and 1 refusal count for nothing")
        self.assertEqual((self.hold("mixed")["basis"], self.hold("mixed")["refused"]), ("confirmed", PAUSE_AFTER))

    def test_a_long_old_409_batch_gets_exactly_one_extra_request_and_then_stops_with_its_evidence_untouched(self):
        self.open_old_database({"long": LONG_409})
        self.failing["long"] = CONFLICT
        before = self.outbox_state("long")
        self.flush_at(5000)
        for later in (5000, 5000 + REFUSED, 5000 + HOUR, 5000 + DAY):
            self.flush_at(later)
        self.restart()
        self.flush_at(5000 + 40 * DAY)
        self.assertEqual(self.bodies, [("long", "long-attempt", 3, EVENTS)], "one extra request in all, with the original events")
        hold = self.hold("long")
        self.assertIsNotNone(hold["paused_at"])
        self.assertEqual((hold["basis"], hold["refused"], hold["legacy_failures"]), ("legacy_refused", 1, 13181))
        self.assertEqual(self.paused(), ["long"])
        # payload, seq, observed_at and queue time are the older Runner's; not sent, not set aside.
        self.assertEqual(self.outbox_state("long"), before)
        self.assertEqual([state[1]["observed_at"] for state in self.outbox_state("long")], [e["observed_at"] for e in EVENTS])
        self.assertEqual(raw_rows(self.db.path, "remote_outbox"), self.old_outbox)
        self.assertEqual(self.db.abandoned_remote_event_count(), 0)
        self.assertEqual(self.pending_jobs(), ["long"])
        self.assertEqual(self.retry("long")["status"], 409)

    def test_the_check_is_one_request_in_all_across_the_flush_and_the_approval_lane(self):
        self.open_old_database({"long": LONG_409}, events=APPROVAL_EVENTS)
        self.failing["long"] = CONFLICT
        with patch("timetrace.agent.time.time", return_value=5000):
            self.assertTrue(self.agent._upload_due(self.claim("long")))
            self.agent._upload_attempt(self.claim("long"), True)
            self.agent._refuse_batch(self.key("long"))  # a second request still in flight comes back 409 too
        for later in (5000 + REFUSED, 5000 + HOUR):
            with patch("timetrace.agent.time.time", return_value=later):
                self.assertFalse(self.agent._upload_due(self.claim("long")))
                self.agent._upload_attempt(self.claim("long"), True)
            self.flush_at(later)
        self.assertEqual(self.attempts, ["long"])
        self.assertEqual((self.hold("long")["basis"], self.hold("long")["refused"]), ("legacy_refused", 1))

    def test_the_approval_lane_check_delivers_when_valley_accepts(self):
        self.open_old_database({"long": LONG_409}, events=APPROVAL_EVENTS)
        with patch("timetrace.agent.time.time", return_value=5000):
            self.assertTrue(self.agent._upload_due(self.claim("long")))
            self.agent._upload_attempt(self.claim("long"), True)
        self.assertEqual(self.sent, ["long"])
        self.assertEqual(self.bodies, [("long", "long-attempt", 3, APPROVAL_EVENTS)])
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual((self.hold_rows(), self.retry("long")), ([], None))

    def test_a_refusal_after_a_person_resumed_the_batch_is_an_ordinary_bounded_round(self):
        self.open_old_database({"long": LONG_409})
        self.failing["long"] = CONFLICT
        self.flush_at(5000)
        self.assertEqual(self.db.resume_paused_remote_events("long", "long-attempt"), 2)
        self.restart()
        self.restart()
        now = 9000
        for _ in range(PAUSE_AFTER + 3):
            self.flush_at(now)
            now += REFUSED
        self.assertEqual(len(self.attempts), 1 + PAUSE_AFTER)
        self.assertEqual((self.hold("long")["basis"], self.hold("long")["refused"]), ("confirmed", PAUSE_AFTER))


class OtherFailuresAreNotRefusalsTest(OldFormatFixture, unittest.TestCase):
    ERRORS = (("network", NETWORK_DOWN), ("timeout", READ_TIMEOUT), ("500", SERVER_ERROR), ("502", BAD_GATEWAY),
              ("429", RATE_LIMITED), ("unreadable reply", GARBLED_REPLY))

    def test_a_failure_that_is_not_a_refusal_is_not_recorded_as_one_and_keeps_the_old_backoff(self):
        for name, error in self.ERRORS:
            with self.subTest(failure=name):
                self.fresh()
                self.open_old_database({"long": LONG_409})
                self.failing["long"] = error
                self.flush_at(5000)
                retry = self.retry("long")
                self.assertEqual(retry["failures"], 13182, "`failures` still counts every kind")
                self.assertEqual(retry["retry_at"] - 5000, OUTBOX_ERROR_RETRY_SECONDS, "the existing 5 s..30 s backoff")
                self.assertNotEqual(retry["status"], 409)
                hold = self.hold("long")
                self.assertEqual((hold["paused_at"], hold["refused"], hold["basis"]), (None, 0, "legacy_check"),
                                 "no refusal was seen, and the question is still open")
                self.flush_at(5000 + OUTBOX_ERROR_RETRY_SECONDS - 1)
                self.assertEqual(len(self.attempts), 1, "inside the backoff nothing is sent")

    def test_such_failures_are_retried_for_ever_and_the_ack_that_finally_comes_delivers_the_original_batch(self):
        for name, error in self.ERRORS:
            with self.subTest(failure=name):
                self.fresh()
                self.open_old_database({"long": LONG_409})
                self.failing["long"] = error
                now = 5000
                rounds = 3 * PAUSE_AFTER + 4
                for _ in range(rounds):
                    self.flush_at(now)
                    now = self.retry("long")["retry_at"]
                self.assertEqual(len(self.attempts), rounds)
                self.assertEqual(self.paused(), [])
                self.failing.clear()
                self.flush_at(now)
                self.assertEqual(self.sent, ["long"])
                self.assertEqual(self.bodies[-1][3], EVENTS)
                self.assertEqual((self.pending_jobs(), self.hold_rows(), self.retry("long")), ([], [], None))

    def test_401_records_nothing_against_the_batch(self):
        self.open_old_database({"long": LONG_409})
        retry, hold = dict(self.retry("long")), self.hold("long")
        self.failing["long"] = lambda: CloudError("unauthorized", status=401)
        for step in range(3):
            with self.assertRaises(CloudError):
                self.flush_at(5000 + step * REFUSED)
        self.assertEqual((dict(self.retry("long")), self.hold("long")), (retry, hold))

    def test_the_first_real_409_after_such_failures_stops_the_batch_on_the_old_and_the_new_evidence(self):
        self.open_old_database({"long": LONG_409})
        self.failing["long"] = SERVER_ERROR
        self.flush_at(5000)
        self.failing["long"] = CONFLICT
        self.flush_at(self.retry("long")["retry_at"])
        for later in (9000, 9000 + DAY):
            self.flush_at(later)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual((self.hold("long")["basis"], self.hold("long")["refused"]), ("legacy_refused", 1))


class ReportingTest(OldFormatFixture, unittest.TestCase):
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

    def refused_once(self, job="long", history=LONG_409):
        self.open_old_database({job: history})
        self.failing[job] = CONFLICT
        self.flush_at(5000)

    def test_doctor_before_the_check_says_it_is_to_be_checked_and_keeps_to_what_the_old_record_says(self):
        self.open_old_database({"long": LONG_409})
        out = self.doctor()
        line = [line for line in out.splitlines() if "long-attempt" in line][0]
        for text in ("待核对", "13181", "升级前", "最后一次", "HTTP 409"):
            self.assertIn(text, line)
        self.assertIn("2 条", out)
        self.assertNotIn("已停止自动重试", out)
        self.assertNotIn("一直", out)
        self.assertNotIn("retry-results", out, "it is still sent by itself")

    def test_doctor_after_the_check_names_the_old_and_the_new_evidence_and_never_says_it_always_said_409(self):
        self.refused_once()
        out = self.doctor()
        for text in ("已停止自动重试", "13181", "升级前", "最后一次", "升级后", "HTTP 409", "2 条", "没有标记为已送达",
                     "2026-10-30T00:32:00Z", "timetrace agent retry-results long long-attempt --yes",
                     "timetrace agent discard-results long long-attempt --yes"):
            self.assertIn(text, out)
        self.assertNotIn("一直", out)
        self.assertNotRegex(out, r"连续\s*\d+\s*次", "one fresh refusal is not a run of refusals")

    def test_doctor_for_a_check_that_met_a_server_error_still_says_the_question_is_open(self):
        self.open_old_database({"long": LONG_409})
        self.failing["long"] = SERVER_ERROR
        self.flush_at(5000)
        out = self.doctor()
        line = [line for line in out.splitlines() if "long-attempt" in line][0]
        self.assertIn("待核对", line)
        self.assertIn("HTTP 500", out)
        self.assertNotIn("已停止自动重试", out)

    def test_the_log_names_the_check_the_stop_and_its_evidence_once_without_claiming_it_always_said_409(self):
        self.refused_once()
        self.flush_at(9000)
        text = "\n".join(self.logs)
        stops = [line for line in self.logs if "已停止自动重试" in line]
        self.assertEqual(len(stops), 1, self.logs)
        for needle in ("long-attempt", "升级前", "升级后", "HTTP 409", "未标记为已送达", "agent doctor"):
            self.assertIn(needle, stops[0])
        self.assertNotIn("一直", text)
        # A restarted Runner names the stop again (once), with the same care.
        self.restart()
        self.flush_at(9000 + HOUR)
        self.flush_at(9000 + 2 * HOUR)
        after = [line for line in self.logs if "已停止自动重试" in line][1:]
        self.assertEqual(len(after), 1, self.logs)
        self.assertIn("升级前", after[0])
        self.assertNotIn("一直", "\n".join(self.logs))

    def test_the_log_says_once_that_an_old_batch_will_be_sent_again_to_be_checked(self):
        self.open_old_database({"long": LONG_409})
        self.failing["long"] = SERVER_ERROR
        for step in range(3):
            self.flush_at(5000 + step * HOUR)
        lines = [line for line in self.logs if "核对" in line and "long-attempt" in line]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("13181", lines[0])

    def test_retry_results_puts_a_check_stopped_batch_back_and_only_an_ack_delivers_it(self):
        self.refused_once()
        code, out, err = self.run_cli("agent", "retry-results", "long", "long-attempt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual([e["events"] for e in audit.read(self.home) if e["event"] == "retry_requested"], [2])
        self.assertEqual(raw_rows(self.db.path, "remote_outbox"), self.old_outbox, "resuming touches no event")
        self.failing.clear()
        self.flush_at(9000)
        self.assertEqual(self.sent, ["long"])
        self.assertEqual(self.bodies[-1][3], EVENTS)
        self.assertEqual((self.pending_jobs(), self.hold_rows(), self.retry("long")), ([], [], None))

    def test_retry_results_offers_nothing_for_a_batch_that_is_only_to_be_checked(self):
        self.open_old_database({"long": LONG_409})
        code, out, err = self.run_cli("agent", "retry-results", "long", "long-attempt", "--yes")
        self.assertEqual(code, 2)
        self.assertFalse(audit.path(self.home).exists())

    def test_a_stopped_batch_can_be_set_aside_by_hand_as_before(self):
        self.refused_once()
        code, out, err = self.run_cli("agent", "discard-results", "long", "long-attempt", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.hold_rows(), [])
        self.assertEqual(self.db.abandoned_remote_event_count(), 2)
        self.assertTrue(all(sent is None and abandoned is not None for _, _, _, sent, abandoned in self.outbox_state("long")))

    def test_the_health_summary_counts_a_batch_to_be_checked_as_retrying_and_a_stopped_one_as_paused(self):
        self.open_old_database({"long": LONG_409, "other": LONG_409})
        health = self.db.remote_outbox_health()
        self.assertEqual((health["paused_batches"], health["retrying_batches"], health["retrying_events"]), (0, 2, 4))
        self.failing["long"] = CONFLICT
        self.flush_at(5000)
        health = self.db.remote_outbox_health()
        self.assertEqual((health["paused_batches"], health["paused_events"], health["paused_reason"]), (1, 2, "http_409"))
        self.assertEqual(health["retrying_batches"], 0, "'other' was delivered by the same flush")


if __name__ == "__main__":
    unittest.main()
