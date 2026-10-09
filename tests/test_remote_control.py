"""RunControl: controls and approvals from the lease, and the audit log."""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from timetrace import audit
from timetrace.remote_control import TIMEOUT_DENY, USER_DENY, RunControl

GOLDEN_CONTROLS = {
    "attempt_id": "96f63154e8a1b6930bbe7db99df77fd5", "lease_epoch": 1,
    "lease_expires_at": "2026-09-30T12:18:19.080275Z",
    "controls": [
        {"id": "9e16b7bd6ae37c440850df07e1d3f2a7", "action": "append", "text": "顺便补一个单元测试",
         "idempotency_key": "append-1"},
        {"id": "005af5d549e56cdb76e73244e9f73ecc", "action": "interrupt",
         "idempotency_key": "control:a27b8b7565596a63b67a41b2709e60b6"}],
    "renew_after_seconds": 10}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "tt"
        self.root = Path(self.tmp.name).resolve() / "wt"
        self.root.mkdir()
        self.cancel = threading.Event()
        self.control = RunControl("job1", self.home, self.cancel, root=str(self.root),
                                  user_home=str(Path(self.tmp.name) / "user"))

    def tearDown(self):
        self.tmp.cleanup()

    def ask_async(self, request):
        box = {}
        cancelled = threading.Event()
        def run():
            box["answer"] = self.control.permission(request, cancelled)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return box, t, cancelled

    def wait_event(self, kind, timeout=5):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            seen += self.control.take_events()
            found = [e for e in seen if e["type"] == kind]
            if found:
                return found[0], seen
            time.sleep(.02)
        self.fail("no %s event: %s" % (kind, seen))


class ControlTest(Base):
    def test_golden_lease_controls(self):
        live = []
        self.control.attach(lambda text: live.append(text) or True)
        self.control.apply_lease(GOLDEN_CONTROLS)
        self.assertEqual(live, ["顺便补一个单元测试"])
        self.assertEqual(self.control.renew_after, 10)
        self.assertTrue(self.cancel.is_set())
        self.assertTrue(self.control.interrupted)
        self.assertEqual(self.control.interrupt_id, "005af5d549e56cdb76e73244e9f73ecc")
        events = self.control.take_events()
        self.assertEqual(events, [{"type": "control_applied", "control_id": "9e16b7bd6ae37c440850df07e1d3f2a7",
                                   "status": "applied", "message": ""}])

    def test_repeated_controls_are_handled_once(self):
        live = []
        self.control.attach(lambda text: live.append(text) or True)
        lease = {"controls": [GOLDEN_CONTROLS["controls"][0]]}
        for _ in range(3):
            self.control.apply_lease(lease)
        self.assertEqual(len(live), 1)
        self.assertEqual(len(self.control.take_events()), 1)

    def test_append_without_a_live_session_is_queued_for_the_next_turn(self):
        self.control.apply_lease({"controls": [{"id": "c1", "action": "append", "text": "next"}]})
        self.assertEqual(self.control.take_events()[0]["status"], "queued_next_turn")
        self.assertEqual(self.control.next_followup(ok=True), ("c1", "next"))
        self.control.followup_started("c1")
        self.assertEqual(self.control.take_events()[0]["status"], "applied")
        self.assertIsNone(self.control.next_followup(ok=True))
        self.assertTrue(self.control.finished)

    def test_failed_turn_runs_no_followup(self):
        self.control.apply_lease({"controls": [{"id": "c1", "action": "append", "text": "next"}]})
        self.assertIsNone(self.control.next_followup(ok=False))

    def test_interrupt_after_the_run_finished_is_ignored(self):
        self.assertIsNone(self.control.next_followup(ok=True))
        self.control.apply_lease({"controls": [{"id": "c2", "action": "interrupt"}]})
        self.assertFalse(self.cancel.is_set())
        self.assertFalse(self.control.interrupted)

    def test_empty_append_is_rejected(self):
        self.control.apply_lease({"controls": [{"id": "c3", "action": "append", "text": "  "}]})
        self.assertEqual(self.control.take_events()[0]["status"], "rejected")

    def test_audit_log_is_private_and_append_only(self):
        self.control.apply_lease({"controls": [{"id": "c4", "action": "interrupt"}]})
        path = audit.path(self.home)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        entries = audit.read(self.home)
        self.assertEqual(entries[-1]["event"], "interrupt")
        self.assertEqual(entries[-1]["control_id"], "c4")
        audit.record(self.home, "pause")
        self.assertEqual([e["event"] for e in audit.read(self.home)], ["interrupt", "pause"])


class ApprovalTest(Base):
    def test_local_rules_answer_without_the_phone(self):
        allow = self.control.permission({"tool_name": "Edit", "input": {"file_path": str(self.root / "a.py")}},
                                        threading.Event())
        self.assertEqual(allow, {"behavior": "allow"})
        deny = self.control.permission({"tool_name": "Bash", "input": {"command": "git push origin main"}},
                                       threading.Event())
        self.assertEqual(deny["behavior"], "deny")
        self.assertEqual(self.control.take_events(), [])

    def test_phone_approval_with_remember_covers_the_same_kind_in_this_job(self):
        request = {"tool_name": "Bash", "input": {"command": "npm install left-pad"}}
        box, thread, _ = self.ask_async(request)
        asked, _ = self.wait_event("approval_requested")
        self.assertEqual(asked["tool"], "Bash")
        self.assertEqual(asked["summary"], "运行 npm install left-pad")
        self.assertEqual(asked["input"], {"command": "npm install left-pad"})
        self.assertTrue(asked["request_id"].startswith("perm-") and len(asked["request_id"]) <= 80)
        self.control.apply_lease({"approvals": [{"request_id": asked["request_id"], "decision": "approve",
                                                 "remember": True}]})
        thread.join(5)
        self.assertEqual(box["answer"], {"behavior": "allow"})
        resolved, _ = self.wait_event("approval_resolved")
        self.assertEqual(resolved, {"type": "approval_resolved", "request_id": asked["request_id"],
                                    "outcome": "approved"})
        # Same kind again: no new request.
        again = self.control.permission({"tool_name": "Bash", "input": {"command": "npm install lodash"}},
                                        threading.Event())
        self.assertEqual(again, {"behavior": "allow"})
        self.assertEqual(self.control.take_events(), [])
        # Repeats of the decision on later leases change nothing.
        self.control.apply_lease({"approvals": [{"request_id": asked["request_id"], "decision": "approve",
                                                 "remember": True}]})
        self.assertEqual(self.control.take_events(), [])

    def test_remember_never_widens_past_a_deny_rule(self):
        request = {"tool_name": "Bash", "input": {"command": "git status --porcelain"}}
        self.control._remembered.add("Bash:git push")
        deny = self.control.permission({"tool_name": "Bash", "input": {"command": "git push"}}, threading.Event())
        self.assertEqual(deny["behavior"], "deny")
        self.assertEqual(self.control.permission(request, threading.Event()), {"behavior": "allow"})

    def test_phone_denial_and_server_expiry(self):
        for decision, reason, outcome, message in (("deny", None, "denied", USER_DENY),
                                                   ("deny", "expired", "expired", TIMEOUT_DENY)):
            with self.subTest(outcome=outcome):
                box, thread, _ = self.ask_async({"tool_name": "WebFetch", "input": {"url": "https://x.test"}})
                asked, _ = self.wait_event("approval_requested")
                answer = {"request_id": asked["request_id"], "decision": decision, "remember": False}
                if reason:
                    answer["reason"] = reason
                self.control.apply_lease({"approvals": [answer]})
                thread.join(5)
                self.assertEqual(box["answer"], {"behavior": "deny", "message": message})
                resolved, _ = self.wait_event("approval_resolved")
                self.assertEqual(resolved["outcome"], outcome)

    def test_no_decision_within_the_timeout_denies_with_the_spec_message(self):
        self.control.approval_timeout = 0.3
        answer = self.control.permission({"tool_name": "WebFetch", "input": {"url": "https://x.test"}},
                                         threading.Event())
        self.assertEqual(answer, {"behavior": "deny", "message": "用户未批准，请换一种不需要该操作的做法或结束并说明"})
        events = self.control.take_events()
        self.assertEqual([e["type"] for e in events], ["approval_requested", "approval_resolved"])
        self.assertEqual(events[1]["outcome"], "expired")
        self.assertEqual(audit.read(self.home)[-1]["source"], "timeout")

    def test_the_end_of_the_job_releases_a_waiting_prompt(self):
        box, thread, _ = self.ask_async({"tool_name": "WebFetch", "input": {"url": "https://x.test"}})
        self.wait_event("approval_requested")
        self.control.close()
        thread.join(5)
        self.assertEqual(box["answer"]["behavior"], "deny")

    def test_requested_input_is_redacted(self):
        box, thread, cancelled = self.ask_async({"tool_name": "Bash",
                                                 "input": {"command": "deploy --token ghp_" + "b" * 36}})
        asked, _ = self.wait_event("approval_requested")
        self.assertNotIn("ghp_" + "b" * 36, json.dumps(asked))
        cancelled.set()
        thread.join(5)


if __name__ == "__main__":
    unittest.main()
