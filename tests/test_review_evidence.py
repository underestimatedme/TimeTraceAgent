import tempfile
import unittest
from pathlib import Path

from timetrace.db import Database
from timetrace.review_evidence import LOG_BYTES, source_run_evidence


class ReviewEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "db.sqlite")
        self.source = {"job": {"id": "source-1", "workspace_id": "ws1", "tool_profile_id": "codex-default"},
                       "attempt_id": "a1", "lease_epoch": 1}
        self.db.save_remote_claim(self.source, state="reported")
        self.job = {"source_job_id": "source-1", "workspace_id": "ws1"}
        (self.home / "logs").mkdir()
        self.log = self.home / "logs" / "remote-source-1.log"
        self.log.write_text("scope-only-evidence")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def evidence(self):
        return source_run_evidence(self.home, self.db, self.job)

    def test_unrelated_workspace_unknown_source_and_unfinished_run_are_unavailable(self):
        self.job["workspace_id"] = "ws2"
        self.assertNotIn("scope-only-evidence", self.evidence())
        self.job["workspace_id"] = "ws1"
        for value in ("../source-1", "source.1", None, "missing"):
            self.job["source_job_id"] = value
            self.assertIn("不可用", self.evidence())
        self.job["source_job_id"] = "source-1"
        self.db.update_remote_claim("source-1", "running")
        self.assertIn("不可用", self.evidence())

    def test_missing_empty_and_symlink_logs_are_not_evidence(self):
        self.log.unlink()
        self.assertIn("不可用", self.evidence())
        self.log.write_text("")
        self.assertIn("不可用", self.evidence())
        self.log.unlink()
        target = self.home / "private.txt"
        target.write_text("private-evidence")
        self.log.symlink_to(target)
        self.assertNotIn("private-evidence", self.evidence())

    def test_large_or_invalid_utf8_log_is_explicitly_incomplete_and_bounded(self):
        for raw in (b"x" * (LOG_BYTES + 1), b"command\xff"):
            self.log.write_bytes(raw)
            evidence = self.evidence()
            self.assertIn('"log_file_read_completely": false', evidence)
            self.assertLess(len(evidence.encode("utf-8")), LOG_BYTES + 4096)
            self.assertIn("不能推定通过", evidence)

    def test_exact_limit_is_complete(self):
        self.log.write_bytes(b"x" * LOG_BYTES)
        self.assertIn('"log_file_read_completely": true', self.evidence())

    def test_long_evidence_bundle_is_complete_redacted_and_segmented(self):
        import hashlib
        import json
        import re
        raw = "start\n" + "x" * (LOG_BYTES * 3) + "\napi_key=sk-proj-" + "Q" * 40 + "\nEXIT: 0"
        self.log.write_text(raw)
        bundle = self.home / "bundle"
        bundle.mkdir()
        output = source_run_evidence(self.home, self.db, self.job, bundle)
        self.assertIn('"log_file_read_completely": false', output)
        self.assertIn("EXIT: 0", output)
        self.assertNotIn("Q" * 40, output)
        manifest = json.loads((bundle / "manifest.json").read_text())
        chunks = []
        for segment in manifest["segments"]:
            chunk = (bundle / segment["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(chunk).hexdigest(), segment["sha256"])
            chunks.append(chunk)
        data = b"".join(chunks)
        self.assertNotIn(b"Q" * 40, data)
        self.assertTrue(data.endswith(b"EXIT: 0"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["sanitized_sha256"])
        self.assertTrue(manifest["archive_complete"])

    def test_over_archive_limit_never_claims_full_bundle(self):
        from timetrace.review_evidence import ARCHIVE_BYTES
        self.log.write_bytes(b"x" * (ARCHIVE_BYTES + 1))
        bundle = self.home / "bundle"
        bundle.mkdir()
        output = source_run_evidence(self.home, self.db, self.job, bundle)
        self.assertNotIn('"archive_complete": true', output)
        self.assertEqual(list(bundle.iterdir()), [])
