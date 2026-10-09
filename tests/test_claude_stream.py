"""Probe tests: the stream-json control protocol against a fake `claude`
process that speaks it exactly as Claude Code 2.1.285 does."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace.adapters import claude
from timetrace.claude_stream import StreamSession, permission_response, user_message

FAKE = str(Path(__file__).with_name("fake_claude_stream.py"))


class Recorder:
    """A host that answers every permission prompt with `decide(request)`."""

    def __init__(self, decide=None, delay=0.0):
        self.decide = decide or (lambda request: {"behavior": "allow"})
        self.delay = delay
        self.requests = []
        self.cancelled = []
        self.send = None
        self.detached = threading.Event()

    def attach(self, send):
        self.send = send

    def detach(self):
        self.detached.set()

    def permission(self, request, cancelled):
        self.requests.append(request)
        if self.delay:
            if cancelled.wait(self.delay):
                self.cancelled.append(request)
                return {"behavior": "deny", "message": "gone"}
        return self.decide(request)


class FakeClaudeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.transcript = self.d / "transcript.jsonl"
        self.log = str(self.d / "run.log")

    def tearDown(self):
        self.tmp.cleanup()

    def scenario(self, turns, exit_code=0):
        path = self.d / "scenario.json"
        path.write_text(json.dumps({"turns": turns, "exit_code": exit_code}))
        return {"TT_FAKE_SCENARIO": str(path), "TT_FAKE_TRANSCRIPT": str(self.transcript)}

    def cmd(self, *extra):
        return [sys.executable, FAKE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--session-id", "11111111-2222-3333-4444-555555555555"] + list(extra)

    def records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def received(self):
        return [r["received"] for r in self.records() if "received" in r]

    def run_session(self, turns, host=None, timeout=20, cancel=None, idle_grace=5.0, prompt="do it", exit_code=0):
        with patch.dict(os.environ, self.scenario(turns, exit_code)):
            session = StreamSession(self.cmd(), str(self.d), self.log, prompt, host=host, timeout=timeout,
                                    cancel_event=cancel, idle_grace=idle_grace)
            code, lines = session.run()
        return code, lines


class ProtocolShapeTest(unittest.TestCase):
    def test_user_message_shape(self):
        self.assertEqual(user_message("hi"), {
            "type": "user", "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}})

    def test_permission_response_shapes(self):
        allow = permission_response("r1", "toolu_1", {"behavior": "allow"}, {"command": "ls"})
        self.assertEqual(allow, {"type": "control_response", "response": {
            "subtype": "success", "request_id": "r1",
            "response": {"behavior": "allow", "updatedInput": {"command": "ls"}, "toolUseID": "toolu_1"}}})
        deny = permission_response("r2", "toolu_2", {"behavior": "deny", "message": "no"}, {})
        self.assertEqual(deny["response"]["response"], {"behavior": "deny", "message": "no", "toolUseID": "toolu_2"})


class StreamSessionTest(FakeClaudeCase):
    def test_prompt_goes_over_stdin_after_initialize_and_the_run_ends_on_its_result(self):
        code, lines = self.run_session([[{"result": "ACK"}]], prompt="--settings={\"hooks\":{}}")
        self.assertEqual(code, 0)
        got = self.received()
        self.assertEqual(got[0]["type"], "control_request")
        self.assertEqual(got[0]["request"], {"subtype": "initialize"})
        self.assertEqual(got[1], user_message("--settings={\"hooks\":{}}"))
        result = claude.parse_stream(lines)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "ACK")
        self.assertEqual(result.session_id, "11111111-2222-3333-4444-555555555555")

    def test_permission_prompt_is_answered_by_the_host(self):
        host = Recorder(lambda request: {"behavior": "allow"} if request["tool_name"] == "Bash"
                        else {"behavior": "deny", "message": "not that"})
        code, lines = self.run_session([[{"permission": {"tool": "Bash", "input": {"command": "npm test"}}},
                                         {"permission": {"tool": "WebFetch", "input": {"url": "https://x"}}},
                                         {"result": "done"}]], host=host)
        self.assertEqual(code, 0)
        self.assertEqual([r["tool_name"] for r in host.requests], ["Bash", "WebFetch"])
        self.assertEqual(host.requests[0]["input"], {"command": "npm test"})
        decisions = [r["decision"] for r in self.records() if "decision" in r]
        first = decisions[0]["reply"]
        self.assertEqual(first["subtype"], "success")
        self.assertEqual(first["response"]["behavior"], "allow")
        self.assertEqual(first["response"]["updatedInput"], {"command": "npm test"})
        self.assertTrue(first["response"]["toolUseID"].startswith("toolu_"))
        second = decisions[1]["reply"]["response"]
        self.assertEqual((second["behavior"], second["message"]), ("deny", "not that"))

    def test_without_a_host_every_prompt_is_denied(self):
        self.run_session([[{"permission": {"tool": "Bash", "input": {"command": "rm -rf x"}}}, {"result": "ok"}]])
        reply = [r["decision"] for r in self.records() if "decision" in r][0]["reply"]["response"]
        self.assertEqual(reply["behavior"], "deny")

    def test_appended_message_is_a_second_turn(self):
        host = Recorder()
        def append_when_attached():
            while host.send is None:
                time.sleep(.01)
            self.assertTrue(host.send("also add a test"))
        threading.Thread(target=append_when_attached, daemon=True).start()
        code, lines = self.run_session([[{"sleep": 0.5}, {"result": "first"}], [{"result": "second"}]], host=host)
        self.assertEqual(code, 0)
        users = [m for m in self.received() if m.get("type") == "user"]
        self.assertEqual([m["message"]["content"][0]["text"] for m in users], ["do it", "also add a test"])
        self.assertEqual(claude.parse_stream(lines).output, "second")
        self.assertTrue(host.detached.is_set())

    def test_a_message_after_stdin_closed_is_refused(self):
        host = Recorder()
        self.run_session([[{"result": "only"}]], host=host)
        self.assertFalse(host.send("too late"))

    def test_folded_mid_turn_message_closes_after_the_quiet_grace(self):
        # One result for two messages: stdin still closes, after idle_grace.
        host = Recorder()
        def append():
            while host.send is None:
                time.sleep(.01)
            host.send("folded")
        threading.Thread(target=append, daemon=True).start()
        started = time.monotonic()
        code, _ = self.run_session([[{"sleep": 0.4}, {"result": "one"}], []], host=host, idle_grace=0.5)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 10)

    def test_withdrawn_prompt_cancels_the_host_wait(self):
        host = Recorder(delay=30)
        code, _ = self.run_session([[{"cancel_permission": {"tool": "Bash", "input": {"command": "x"}, "after": 0.3}},
                                     {"result": "ok"}]], host=host)
        self.assertEqual(code, 0)
        self.assertEqual(len(host.cancelled), 1)
        self.assertFalse([r for r in self.records() if "decision" in r])

    def test_waiting_for_a_decision_does_not_count_toward_the_timeout(self):
        host = Recorder(delay=1.5)  # answers (allow) after 1.5 s; timeout is 1 s
        host.decide = lambda request: {"behavior": "allow"}
        code, lines = self.run_session([[{"permission": {"tool": "Bash", "input": {"command": "x"}}},
                                         {"result": "ok"}]], host=host, timeout=1.0)
        self.assertEqual(code, 0)
        self.assertTrue(claude.parse_stream(lines).ok)

    def test_timeout_still_applies_to_the_model_itself(self):
        started = time.monotonic()
        code, _ = self.run_session([[{"sleep": 30}, {"result": "late"}]], timeout=0.5)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 10)

    def test_cancel_kills_the_process_group(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        code, _ = self.run_session([[{"sleep": 30}, {"result": "late"}]], cancel=cancel)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 10)

    def test_already_cancelled_run_never_spawns(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_session([[{"result": "x"}]], cancel=cancel)
        self.assertFalse(self.transcript.exists())

    def test_unsupported_control_request_gets_an_error_response(self):
        # Hook callbacks, MCP messages etc. are never registered by timetrace.
        session = StreamSession(["true"], str(self.d), self.log, "x")
        written = []
        session._stdin_open = True
        session._write = lambda obj: written.append(obj) or True
        session._on_control_request({"type": "control_request", "request_id": "r9",
                                     "request": {"subtype": "hook_callback", "callback_id": "h"}})
        self.assertEqual(written[0]["response"]["subtype"], "error")
        self.assertEqual(written[0]["response"]["request_id"], "r9")


if __name__ == "__main__":
    unittest.main()
