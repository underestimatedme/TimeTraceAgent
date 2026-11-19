import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.checkpoints import (
    Checkpoint,
    ResumeInputs,
    resume_allowed,
    resume_decision,
)
from timetrace.db import Database


class ResumeAllowedTest(unittest.TestCase):
    def test_resume_keeps_tool(self):
        self.assertTrue(resume_allowed("claude-personal", "claude-personal", True))
        # Cross-tool resume is impossible: session context does not travel.
        self.assertFalse(resume_allowed("claude-personal", "codex-personal", True))
        # Without native resume support there is no honest continuation.
        self.assertFalse(resume_allowed("claude-personal", "claude-personal", False))
        self.assertFalse(resume_allowed("", "", True))


def _inputs(**kw):
    base = dict(
        cancelled=False, original_profile="claude-personal", requested_profile="claude-personal",
        native_resume=True, auto_resume_enabled=True, zero_spend_verified=True,
        availability="available", reliable_reset_passed=False, probe_used=False,
        has_native_session=True,
    )
    base.update(kw)
    return ResumeInputs(**base)


class ResumeDecisionTest(unittest.TestCase):
    def test_cancel_wins(self):
        self.assertEqual(resume_decision(_inputs(cancelled=True, availability="available")), "no_resume")

    def test_missing_session_needs_input(self):
        self.assertEqual(resume_decision(_inputs(has_native_session=False)), "waiting_input")

    def test_cross_tool_needs_input(self):
        self.assertEqual(resume_decision(_inputs(requested_profile="codex-personal")), "waiting_input")

    def test_auto_resume_off_refreshes_only(self):
        self.assertEqual(resume_decision(_inputs(auto_resume_enabled=False)), "refresh_only")

    def test_unverified_billing_waits(self):
        self.assertEqual(resume_decision(_inputs(zero_spend_verified=False)), "wait")

    def test_available_resumes(self):
        self.assertEqual(resume_decision(_inputs(availability="available")), "resume")

    def test_blocked_waits(self):
        self.assertEqual(resume_decision(_inputs(availability="blocked")), "wait")

    def test_unknown_with_reliable_reset_probes_once(self):
        self.assertEqual(resume_decision(_inputs(availability="unknown", reliable_reset_passed=True)), "probe")
        # A second probe for the same pool/window is not allowed.
        self.assertEqual(resume_decision(_inputs(availability="unknown", reliable_reset_passed=True, probe_used=True)), "wait")

    def test_unknown_without_reliable_reset_waits(self):
        self.assertEqual(resume_decision(_inputs(availability="unknown", reliable_reset_passed=False)), "wait")


class CheckpointPersistenceTest(unittest.TestCase):
    def test_atomic_save_load_delete(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cp = Checkpoint(
                plan_id="plan-1", job_id="job-1", attempt_id="att-1",
                tool_profile_id="claude-personal", provider_session_id="sess-9",
                canonical_workspace="/repo", git_head="abc123", dirty_paths_digest="d1",
                last_output_offset=42, completed_criteria=["a"], side_effect_summary="edited x",
                reason="waiting_quota",
            )
            db.save_checkpoint(cp)
            loaded = db.get_checkpoint("plan-1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.provider_session_id, "sess-9")
            self.assertEqual(loaded.completed_criteria, ["a"])
            self.assertEqual(loaded.schema_version, cp.schema_version)
            # Re-saving replaces in place (one checkpoint per plan).
            db.save_checkpoint(cp)
            self.assertEqual(len(db.list_checkpoints()), 1)
            db.delete_checkpoint("plan-1")
            self.assertIsNone(db.get_checkpoint("plan-1"))

    def test_agent_sessions_lists_recent_checkpoints_without_transcript_or_paths(self):
        def listing(*args):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["agent", "sessions", *args]), 0)
            return out.getvalue()

        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, TIMETRACE_HOME=d):
            self.assertEqual(json.loads(listing("--json")), [])
            self.assertIn("还没有保存", listing())
            db = Database(Path(d) / "timetrace.db")
            self.addCleanup(db.close)
            plan = Checkpoint(plan_id="p", job_id="j", attempt_id="a", tool_profile_id="profile",
                              provider_session_id="private-native-id", canonical_workspace="/private/work",
                              git_head="h", dirty_paths_digest="d", last_output_offset=0, completed_criteria=[],
                              side_effect_summary="private transcript", reason="waiting_quota", schema_version=1)
            db.save_checkpoint(plan, now=1)
            work = replace(plan, plan_id="work-session:w", job_id="j2", reason="work_session", provider="codex")
            db.save_checkpoint(work, now=2)
            result = listing("--json")
            rows = json.loads(result)
            self.assertEqual([r["id"] for r in rows], ["w", "p"])
            self.assertEqual(rows[0], dict(kind="work_session", id="w", provider="codex", job_id="j2",
                                           tool_profile_id="profile", reason="work_session"))
            self.assertEqual(rows[1]["kind"], "plan")
            self.assertNotIn("private", result)
            self.assertIn("等待额度恢复", listing())
            self.assertEqual(db.get_checkpoint("p"), plan)
            self.assertEqual(db.get_checkpoint("work-session:w"), work)

    def test_checkpoint_row_carries_no_secrets(self):
        cp = Checkpoint(plan_id="p", job_id="j", attempt_id="a", tool_profile_id="claude-personal",
                        provider_session_id="s", canonical_workspace="/r", git_head="h",
                        dirty_paths_digest="d", last_output_offset=0, completed_criteria=[],
                        side_effect_summary="", reason="waiting_quota")
        row = cp.to_row()
        self.assertNotIn("prompt", row)
        self.assertNotIn("env", row)
        self.assertNotIn("token", row)


if __name__ == "__main__":
    unittest.main()
