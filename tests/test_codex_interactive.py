import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from timetrace.adapters.codex import CodexAdapter
from timetrace.adapters.codex_interactive import COMMAND_APPROVAL, FILE_APPROVAL, CodexTurn
from timetrace.billing import StaticBilling

class CodexInteractiveTest(unittest.TestCase):
    def test_approval_command_is_unwrapped_and_broader_requests_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            turn = CodexTurn([], tmp, None, threading.Event(), 3, False)
            local = {"cwd": tmp, "command": "/bin/zsh -lc 'touch marker'"}
            self.assertEqual(turn._permission_input(COMMAND_APPROVAL, local)["input"]["command"], "touch marker")
            for extra in ({"additionalPermissions": {"fileSystem": {"write": ["/"]}}},
                          {"networkApprovalContext": {"host": "example.test"}}, {"kind": "writeStdin"}):
                self.assertIsNone(turn._permission_input(COMMAND_APPROVAL, dict(local, **extra)))
            turn.file_changes["patch"] = ["present.txt", ".github/workflows/test.yml"]
            self.assertIsNone(turn._permission_input(FILE_APPROVAL, {"itemId": "patch"}))

    def test_cancel_interrupts_the_expected_native_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "codex"
            fake.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fake_codex_interactive.py").read_text())
            fake.chmod(0o700)
            cancel = threading.Event()
            host = Host()
            host.attach_interactive = lambda _: cancel.set()
            adapter = CodexAdapter({"bin": str(fake), "timeout_seconds": 3}, billing=StaticBilling(True, "test"))
            result = adapter.work_session("initial", tmp, "old-thread", str(root / "run.log"),
                                          cancel, control=host, resuming=True, read_only=False)
            self.assertFalse(result.ok)
            self.assertEqual(result.session_id, "old-thread")
            self.assertEqual(json.loads((root / "interrupt.json").read_text()),
                             {"threadId": "old-thread", "turnId": "native-turn"})

    def test_native_ack_steer_and_one_time_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "codex"
            fake.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fake_codex_interactive.py").read_text())
            fake.chmod(0o700)
            adapter = CodexAdapter({"bin": str(fake), "timeout_seconds": 3}, billing=StaticBilling(True, "test"))
            host = Host()
            self.assertTrue(adapter.capabilities().get("can_interact"))
            result = adapter.work_session("initial", tmp, "old-thread", str(root / "run.log"),
                                          threading.Event(), control=host, resuming=True, read_only=False)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.session_id, "old-thread")
            self.assertEqual(result.output, "User-facing answer")
            self.assertEqual(host.delivery, ["applied", ("control-1", "applied")])
            self.assertEqual(host.replies, ["User-facing answer"])
            self.assertEqual(host.requests[0]["input"]["command"], "touch marker")
            data = json.loads((root / "transcript.json").read_text())
            self.assertEqual(data["approval"], {"id": "permission-1", "result": {"decision": "accept"}})
            self.assertEqual(data["steer"]["expectedTurnId"], "native-turn")
            self.assertEqual(data["steer"]["clientUserMessageId"], "control-1")
            self.assertEqual(data["resume"]["threadId"], "old-thread")
            self.assertEqual(data["start"]["clientUserMessageId"], "message-1")


class Host:
    message_id = "message-1"

    def __init__(self):
        self.delivery, self.replies, self.requests = [], [], []

    def provider_input_allowed(self, *, check_gate=True):
        return True

    def provider_session(self, session_id):
        self.session_id = session_id

    def initial_delivery(self, status):
        self.delivery.append(status)

    def assistant_message(self, reply):
        self.replies.append(reply)

    def attach_interactive(self, send):
        send("control-1", "additional", lambda status: self.delivery.append(("control-1", status)))

    def detach(self):
        pass

    def permission(self, request, cancelled, *, respond):
        self.requests.append(request)
        respond({"behavior": "allow"})


if __name__ == "__main__":
    unittest.main()
