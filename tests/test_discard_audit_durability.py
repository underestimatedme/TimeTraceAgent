"""A manual discard of a refused (409) batch is a decision of the person at this computer. It must never be carried out
or acknowledged without a complete, durable entry in ~/.timetrace/audit.log that tells the truth about it.

Fake identifiers and isolated temporary directories only."""
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from timetrace import audit, cli
from timetrace.db import Database

JOB, ATTEMPT = "synthetic-job", "synthetic-attempt"


class DiscardAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="timetrace-discard-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.refuse(JOB, ATTEMPT, 1)

    def refuse(self, job, attempt, seq):
        self.db.queue_remote_event(job, attempt, 1, {"seq": seq, "type": "failed", "message": "synthetic"}, now=100)
        self.db.defer_remote_events(job, attempt, 1, 409, 200, 260)

    def rows(self):
        return [tuple(r) for r in self.db.conn.execute("SELECT payload, sent_at, abandoned_at FROM remote_outbox ORDER BY id")]

    def retry_rows(self):
        return self.db.conn.execute("SELECT COUNT(*) FROM remote_outbox_retry WHERE status=409").fetchone()[0]

    def discard(self, yes=True, job=JOB, attempt=ATTEMPT):
        out = io.StringIO()
        args = SimpleNamespace(job=job, attempt=attempt, yes=yes)
        with patch.object(cli, "_open", return_value=(self.home, None, self.db)), contextlib.redirect_stdout(out):
            code = cli.cmd_agent_discard_results(args)
        return code, out.getvalue()

    def log_lines(self):
        target = audit.path(self.home)
        return target.read_bytes().split(b"\n") if target.exists() else []

    def entries(self, event=None):
        found = audit.read(self.home)
        return [e for e in found if event is None or e["event"] == event]

    # ---- the path that works ------------------------------------------------------------------------------------

    def test_control_a_confirmed_discard_writes_the_request_then_the_outcome_under_one_id(self):
        before = self.rows()
        code, text = self.discard()
        self.assertEqual(code, 0, text)
        after = self.rows()
        self.assertEqual([r[0] for r in after], [r[0] for r in before], "the payloads are untouched")
        self.assertTrue(all(r[1] is None for r in after), "setting aside is not delivering")
        self.assertTrue(all(r[2] is not None for r in after))
        requested, done = self.entries("discard_requested"), self.entries("outbox_discard")
        self.assertEqual(len(requested), 1)
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["job_id"], done[0]["attempt_id"], done[0]["events"]), (JOB, ATTEMPT, 1))
        self.assertEqual(requested[0]["discard_id"], done[0]["discard_id"])
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested", "outbox_discard"], "the request comes first")

    def test_without_the_explicit_yes_nothing_is_written_anywhere(self):
        code, _ = self.discard(yes=False)
        self.assertEqual(code, 1)
        self.assertFalse(audit.path(self.home).exists())
        self.assertTrue(all(r[2] is None for r in self.rows()))

    def test_a_batch_that_was_not_refused_is_not_audited_or_discarded(self):
        self.db.queue_remote_event("slow", "slow-attempt", 1, {"seq": 1, "type": "running"}, now=100)
        self.db.defer_remote_events("slow", "slow-attempt", 1, 500, 200, 260)
        code, _ = self.discard(job="slow", attempt="slow-attempt")
        self.assertEqual(code, 2)
        self.assertFalse(audit.path(self.home).exists())

    # ---- the audit file cannot take the entry --------------------------------------------------------------------

    def assert_nothing_was_discarded(self):
        self.assertTrue(all(r[2] is None for r in self.rows()), "the discard must not be committed")
        self.assertEqual(self.retry_rows(), 1, "the 409 stays recorded, so a later explicit retry can still discard it")
        self.assertEqual(self.entries("outbox_discard"), [])

    def test_an_unwritable_audit_file_stops_the_discard_before_it_changes_anything(self):
        (self.home / audit.FILE_NAME).mkdir()
        code, text = self.discard()
        self.assertNotEqual(code, 0, "it must not report success")
        self.assertIn("没有丢弃", text)
        self.assert_nothing_was_discarded()

    def test_a_write_that_fails_halfway_stops_the_discard_and_leaves_no_merged_entry_behind(self):
        real = os.write
        calls = {"n": 0}

        def half(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(fd, bytes(data)[:10])  # ten bytes reach the file...
            raise OSError(errno.ENOSPC, "no space left")  # ...then the disk is full

        with patch.object(audit.os, "write", side_effect=half):
            code, text = self.discard()
        self.assertNotEqual(code, 0)
        self.assertIn("没有丢弃", text)
        self.assert_nothing_was_discarded()
        # The disk is fine again. The fragment is closed off, so the next entry is a line of its own.
        code, text = self.discard()
        self.assertEqual(code, 0, text)
        parsed = [json.loads(line) for line in self.log_lines() if line.strip().startswith(b'{"') and line.strip().endswith(b"}")]
        self.assertEqual([e["event"] for e in parsed], ["discard_requested", "outbox_discard"])
        self.assertTrue(all(r[2] is not None for r in self.rows()))

    def test_a_write_that_only_ever_takes_a_few_bytes_still_ends_up_complete(self):
        real = os.write
        with patch.object(audit.os, "write", side_effect=lambda fd, data: real(fd, bytes(data)[:10])):
            code, text = self.discard()
        self.assertEqual(code, 0, text)
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested", "outbox_discard"])
        self.assertTrue(all(line == b"" or line.startswith(b"{") and line.endswith(b"}") for line in self.log_lines()))

    def test_the_entry_is_forced_to_disk_before_the_discard_goes_on(self):
        synced = []
        real = audit._flush
        with patch.object(audit, "_flush", side_effect=lambda fd: (synced.append(fd), real(fd))[1]):
            code, _ = self.discard()
        self.assertEqual(code, 0)
        self.assertEqual(len(synced), 2, "the request entry and the outcome entry are each forced to disk")

    # ---- the outcome entry cannot be written, or the process ends in between ------------------------------------

    def test_when_only_the_outcome_cannot_be_written_the_discard_stands_but_is_not_reported_as_complete(self):
        real = audit.record_durable

        def fail_the_outcome(home, event, **fields):
            if event == "outbox_discard":
                raise OSError(errno.EIO, "synthetic outcome failure")
            return real(home, event, **fields)

        with patch.object(audit, "record_durable", side_effect=fail_the_outcome):
            code, text = self.discard()
        self.assertNotEqual(code, 0, "the person is told the record is incomplete")
        self.assertIn("已丢弃", text)
        self.assertIn("审计", text)
        self.assertTrue(all(r[2] is not None for r in self.rows()), "it was committed, and says so")
        self.assertEqual(len(self.entries("discard_requested")), 1, "the request is on disk: the decision is not unrecorded")
        self.assertEqual(self.entries("outbox_discard"), [])

    def test_ending_between_the_request_entry_and_the_commit_changes_nothing(self):
        with patch.object(self.db, "discard_conflicted_remote_events", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.discard()
        self.assertTrue(all(r[2] is None for r in self.rows()))
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested"])

    def test_ending_between_the_commit_and_the_outcome_entry_leaves_the_request_on_disk(self):
        real = audit.record_durable

        def die_on_outcome(home, event, **fields):
            if event == "outbox_discard":
                raise KeyboardInterrupt
            return real(home, event, **fields)

        with patch.object(audit, "record_durable", side_effect=die_on_outcome):
            with self.assertRaises(KeyboardInterrupt):
                self.discard()
        self.assertTrue(all(r[2] is not None for r in self.rows()))
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested"])

    def test_a_failing_database_leaves_the_request_and_says_that_it_failed(self):
        with patch.object(self.db, "discard_conflicted_remote_events", side_effect=RuntimeError("synthetic db error")):
            with self.assertRaises(RuntimeError):
                self.discard()
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested", "discard_failed"])
        self.assertTrue(all(r[2] is None for r in self.rows()))

    # ---- two decisions at once ---------------------------------------------------------------------------------

    def test_a_second_confirmation_of_the_same_attempt_does_not_claim_a_second_discard(self):
        refused = self.db.remote_outbox_diagnostics()
        with patch.object(self.db, "remote_outbox_diagnostics", return_value=refused):
            first_code, _ = self.discard()
            second_code, text = self.discard()  # it also saw the batch as refused before the first one committed
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 2)
        self.assertIn("没有可丢弃", text)
        done = self.entries("outbox_discard")
        self.assertEqual([e["events"] for e in done], [1], "one discard happened, so one outcome says so")
        self.assertEqual([e["event"] for e in self.entries()], ["discard_requested", "outbox_discard", "discard_requested", "discard_unchanged"])
        self.assertEqual(self.db.abandoned_remote_event_count(), 1)

    # ---- the best-effort entries keep their promise ------------------------------------------------------------

    def test_the_other_audit_entries_still_never_raise_and_never_merge_into_a_broken_line(self):
        (self.home / audit.FILE_NAME).mkdir()
        audit.record(self.home, "pause", source="local")  # must not raise
        (self.home / audit.FILE_NAME).rmdir()
        audit.path(self.home).write_bytes(b'{"at": "x", "event": "tru')  # a previous process died mid-line
        audit.record(self.home, "resume", source="local")
        events = [e["event"] for e in self.entries()]
        self.assertEqual(events, ["resume"])


class ConcurrentWritersTest(unittest.TestCase):
    """Two processes append long entries at once through a write that only takes a few bytes per call."""

    WORKER = textwrap.dedent("""
        import os, sys, time
        from pathlib import Path
        from timetrace import audit
        real = os.write
        def slow(fd, data):
            time.sleep(0.0005)
            return real(fd, bytes(data)[:7])
        audit.os.write = slow
        for i in range(12):
            audit.record_durable(Path(sys.argv[1]), "synthetic", writer=sys.argv[2], n=i, pad="x" * 60)
    """)

    def test_entries_of_two_processes_never_interleave(self):
        with tempfile.TemporaryDirectory(prefix="timetrace-audit-race-") as tmp:
            repo = str(Path(__file__).resolve().parents[1])
            env = dict(os.environ, PYTHONPATH=repo)
            procs = [subprocess.Popen([sys.executable, "-c", self.WORKER, tmp, name], env=env) for name in ("a", "b")]
            self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0])
            lines = [l for l in Path(tmp, audit.FILE_NAME).read_bytes().split(b"\n") if l]
            parsed = [json.loads(l) for l in lines]
            self.assertEqual(len(parsed), 24)
            for name in ("a", "b"):
                self.assertEqual([e["n"] for e in parsed if e["writer"] == name], list(range(12)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
