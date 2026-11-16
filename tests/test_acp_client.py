"""Probe tests: the generic ACP client against a fake agent (tests/fake_acp_agent.py) that speaks ACP v1 as the
official schema describes it. No real Gemini CLI or Cursor is ever started."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import process
from timetrace.acp_client import AcpSession, choose_option, host_request
from timetrace.billing import billing_env_keys
from timetrace.dispatch import DispatchDenied
from timetrace.mcp_access import McpAccess

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
TOKEN = "ttm_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"
MCP_URL = "https://valley.example.test/timetrace/mcp"
OPTIONS = [
    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "never", "name": "Never allow", "kind": "reject_always"},
    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
]
EDIT_CALL = {"toolCallId": "call-1", "title": "Edit app.py", "kind": "edit", "status": "pending",
             "locations": [{"path": "app.py"}],
             "rawInput": {"file_path": "app.py", "old_string": "a", "new_string": "b"}}


class Host:
    """Answers every permission prompt with `behavior`, optionally after waiting `delay` seconds (or until the
    client withdraws the prompt)."""

    def __init__(self, behavior="allow", delay=0.0, fail=False):
        self.behavior, self.delay, self.fail = behavior, delay, fail
        self.requests, self.withdrawn = [], []

    def permission(self, request, cancelled):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("host exploded")
        if self.delay and cancelled.wait(self.delay):
            self.withdrawn.append(request)
            return {"behavior": "deny", "message": "withdrawn"}
        return {"behavior": self.behavior}


class AcpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()
        self.transcript = self.d / "transcript.jsonl"
        self.log = self.d / "run.log"

    def run_session(self, scenario, prompt="do it", cmd=None, environ=None, **kwargs):
        path = self.d / "scenario.json"
        path.write_text(json.dumps(scenario))
        env = {"TT_ACP_SCENARIO": str(path), "TT_ACP_TRANSCRIPT": str(self.transcript)}
        env.update(environ or {})
        with patch.dict(os.environ, env):
            return AcpSession(cmd or [sys.executable, FAKE], str(self.d), str(self.log), prompt, **kwargs).run()

    def records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def received(self, method=None):
        return [r["received"] for r in self.records()
                if "received" in r and (method is None or r["received"].get("method") == method)]

    def answers(self, method=None):
        return [r["answer"]["response"] for r in self.records()
                if "answer" in r and (method is None or r["answer"]["method"] == method)]

    def log_text(self):
        return self.log.read_text()


class NormalTurnTest(AcpCase):
    def test_a_turn_is_initialize_new_session_and_one_prompt_and_its_updates_become_the_result(self):
        plan = {"sessionUpdate": "plan", "entries": [
            {"content": "read the code", "priority": "high", "status": "completed"},
            {"content": "write the fix", "priority": "high", "status": "in_progress"}]}
        result = self.run_session({"turn": [
            {"thought": "Let me look.\n"},
            {"chunk": "Looking at the code.\n"},
            {"update": {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read app.py", "kind": "read",
                        "status": "pending"}},
            {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"}},
            {"update": plan},
            {"update": {"sessionUpdate": "available_commands_update", "availableCommands": []}},
            {"update": {"sessionUpdate": "a_variant_from_a_later_schema", "whatever": 1}},
            {"chunk": "Done: "}, {"chunk": "all good."},
            {"update": {"sessionUpdate": "usage_update", "used": 1200, "size": 200000}},
        ]}, prompt="fix the bug")
        self.assertIsNone(result.error)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.session_id, "fake-session-1")
        self.assertEqual(result.text, "Looking at the code.\nDone: all good.")
        self.assertEqual(result.last_message, "Done: all good.", "the answer after the last tool call")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual([e["status"] for e in result.plan], ["completed", "in_progress"])
        self.assertEqual(result.usage, {"used": 1200, "size": 200000})
        self.assertEqual(result.exit_code, 0)

        init = self.received("initialize")[0]
        self.assertEqual(init["jsonrpc"], "2.0")
        self.assertEqual(init["params"]["protocolVersion"], 1)
        self.assertEqual(init["params"]["clientCapabilities"],
                         {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False})
        self.assertEqual(init["params"]["clientInfo"]["name"], "timetrace")
        new = self.received("session/new")[0]["params"]
        self.assertEqual(new, {"cwd": str(self.d), "mcpServers": []})
        prompt = self.received("session/prompt")[0]["params"]
        self.assertEqual(prompt, {"sessionId": "fake-session-1", "prompt": [{"type": "text", "text": "fix the bug"}]})
        self.assertEqual(self.received("authenticate"), [], "the client never picks an auth method")

        log = self.log_text()
        for expected in ("-> initialize", "<- session/new: session fake-session-1", "[thought] Let me look.",
                         "\nLooking at the code.\n", "[tool] read: Read app.py (pending)", "[tool] Read app.py: completed",
                         "[plan] [x] read the code | [>] write the fix", "\nDone: all good.\n",
                         "<- session/prompt: stopReason end_turn", "[usage] used 1200 of 200000 tokens",
                         "--- exit 0 ---"):
            self.assertIn(expected, log)
        self.assertTrue(log.startswith("$ "))

    def test_stderr_and_a_stray_stdout_line_are_shown_but_never_acted_on(self):
        result = self.run_session({"garbage_first": True, "turn": [
            {"stderr": "debug: warming up"}, {"garbage": "{not json either"}, {"chunk": "ok"}]})
        self.assertEqual((result.error, result.stop_reason, result.text), (None, "end_turn", "ok"))
        log = self.log_text()
        self.assertIn("[stdout] this is not JSON", log)
        self.assertIn("[stdout] {not json either", log)
        self.assertIn("[stderr] debug: warming up", log)
        self.assertIn("2 non-JSON stdout line(s) ignored", log)


class PermissionTest(AcpCase):
    def turn(self, options=None, tool_call=None):
        return {"turn": [{"permission": {"toolCall": tool_call or EDIT_CALL, "options": options or OPTIONS}},
                         {"chunk": "after"}]}

    def outcome(self):
        return self.answers("session/request_permission")[0]["result"]["outcome"]

    def test_the_host_decides_and_the_answer_is_a_one_time_option(self):
        host = Host("allow")
        self.run_session(self.turn(), host=host)
        self.assertEqual(self.outcome(), {"outcome": "selected", "optionId": "once"})
        request = host.requests[0]
        self.assertEqual((request["tool_name"], request["input"], request["require_phone"]),
                         ("Edit", {"file_path": "app.py"}, False))
        self.assertEqual(request["title"], "Edit app.py")
        self.assertIn("[permission] Edit app.py -> once (host)", self.log_text())

    def test_a_denial_is_reject_once(self):
        self.run_session(self.turn(), host=Host("deny"))
        self.assertEqual(self.outcome(), {"outcome": "selected", "optionId": "no"})

    def test_without_a_host_or_with_a_broken_one_every_prompt_is_rejected_once(self):
        for host in (None, Host(fail=True)):
            with self.subTest(host=host):
                if self.transcript.exists():
                    self.transcript.unlink()
                self.run_session(self.turn(), host=host)
                self.assertEqual(self.outcome(), {"outcome": "selected", "optionId": "no"})

    def test_an_always_option_is_never_chosen(self):
        lasting = [o for o in OPTIONS if o["kind"].endswith("_always")]
        for behavior in ("allow", "deny"):
            with self.subTest(behavior=behavior):
                if self.transcript.exists():
                    self.transcript.unlink()
                self.run_session(self.turn(options=lasting), host=Host(behavior))
                # Only lasting options on offer: neither is taken, the prompt is withdrawn instead.
                self.assertEqual(self.outcome(), {"outcome": "cancelled"})
        for allow in (True, False):
            for subset in ([OPTIONS[0], OPTIONS[3]], [OPTIONS[2], OPTIONS[1]], OPTIONS, OPTIONS[::-1]):
                chosen = choose_option(subset, allow).get("optionId")
                self.assertNotIn(chosen, ("always", "never"))

    def test_a_mode_switch_is_refused_without_asking_anyone(self):
        host = Host("allow")
        self.run_session(self.turn(tool_call={"toolCallId": "m", "title": "Switch to auto-accept", "kind": "switch_mode"}),
                         host=host)
        self.assertEqual(host.requests, [])
        self.assertEqual(self.outcome(), {"outcome": "selected", "optionId": "no"})

    def test_time_spent_waiting_for_a_decision_does_not_count_toward_the_timeout(self):
        result = self.run_session(self.turn(), host=Host("allow", delay=1.5), timeout=1.0)
        self.assertEqual((result.error, result.stop_reason), (None, "end_turn"))

    def test_tool_calls_are_described_in_the_rule_tables_words(self):
        root = str(self.d)
        bash = host_request({"kind": "execute", "title": "ls", "rawInput": {"command": "ls -la"}}, root)
        self.assertEqual((bash["tool_name"], bash["input"], bash["require_phone"]), ("Bash", {"command": "ls -la"}, False))
        here = host_request({"kind": "execute", "rawInput": {"command": "ls", "dir_path": "."}}, root)
        self.assertFalse(here["require_phone"])
        elsewhere = host_request({"kind": "execute", "rawInput": {"command": "cat x", "dir_path": "/etc"}}, root)
        self.assertTrue(elsewhere["require_phone"], "relative paths of a command run elsewhere cannot be judged")
        subdir = host_request({"kind": "execute", "rawInput": {"command": "cat ../../x", "dir_path": "src/a"}}, root)
        self.assertTrue(subdir["require_phone"])
        read = host_request({"kind": "read", "rawInput": {"file_path": "a.py"}}, root)
        self.assertEqual((read["tool_name"], read["require_phone"]), ("Read", True))
        many = host_request({"kind": "edit", "locations": [{"path": p} for p in "abcd"]}, root)
        self.assertEqual((many["input"], many["require_phone"]),
                         ({"file_path": "a", "path": "b", "notebook_path": "c"}, True))
        delete = host_request({"kind": "delete", "rawInput": {"path": "a.py"}}, root)
        self.assertEqual((delete["tool_name"], delete["require_phone"]), ("Edit", True))
        unknown = host_request({"kind": "other", "title": "get_task (timetrace)", "rawInput": {"id": 3}}, root)
        self.assertEqual((unknown["tool_name"], unknown["input"]), ("acp:other", {"id": 3, "title": "get_task (timetrace)"}))
        no_command = host_request({"kind": "execute", "title": "Shell"}, root)
        self.assertEqual(no_command["tool_name"], "acp:execute")
        self.assertIsNone(host_request({"kind": "switch_mode"}, root))

    def test_the_rule_table_refuses_a_write_outside_the_worktree_and_allows_one_inside(self):
        from timetrace import approvals
        root = str(self.d)
        inside = host_request(EDIT_CALL, root)
        self.assertEqual(approvals.evaluate(inside["tool_name"], inside["input"], root).action, approvals.ALLOW)
        outside = host_request(dict(EDIT_CALL, locations=[{"path": "/etc/hosts"}], rawInput={}), root)
        self.assertEqual(approvals.evaluate(outside["tool_name"], outside["input"], root).action, approvals.DENY)
        push = host_request({"kind": "execute", "rawInput": {"command": "git push origin main"}}, root)
        self.assertEqual(approvals.evaluate(push["tool_name"], push["input"], root).action, approvals.DENY)


class RefusedRequestsTest(AcpCase):
    def test_fs_terminal_and_unknown_requests_are_method_not_found(self):
        result = self.run_session({"turn": [
            {"request": {"method": "fs/read_text_file", "params": {"path": str(self.d / "x"), "sessionId": "s"}}},
            {"request": {"method": "terminal/create", "params": {"command": "ls"}}},
            {"request": {"method": "_vendor/something", "params": {}}},
            {"chunk": "still fine"}]})
        self.assertEqual((result.error, result.text), (None, "still fine"))
        errors = [a["error"]["code"] for a in self.answers()]
        self.assertEqual(errors, [-32601, -32601, -32601])
        self.assertIn("refused fs/read_text_file (this client advertises no fs capability)", self.log_text())


class ResumeTest(AcpCase):
    REPLAY = [{"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "earlier ask"}},
              {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "earlier answer"}},
              {"sessionUpdate": "tool_call", "toolCallId": "old", "title": "old edit", "kind": "edit"}]

    def test_load_replays_history_that_is_not_counted_as_this_turn(self):
        result = self.run_session({"initialize": {"loadSession": True}, "replay": self.REPLAY,
                                   "turn": [{"chunk": "continuing"}]}, session_id="sess-9")
        self.assertEqual(self.received("session/new"), [])
        load = self.received("session/load")[0]["params"]
        self.assertEqual(load, {"sessionId": "sess-9", "cwd": str(self.d), "mcpServers": []})
        self.assertEqual((result.session_id, result.text, result.tool_calls), ("sess-9", "continuing", 0))
        self.assertIn("3 earlier updates replayed", self.log_text())
        self.assertNotIn("earlier answer", self.log_text())

    def test_resume_is_preferred_when_offered(self):
        result = self.run_session({"initialize": {"loadSession": True, "resume": True}, "turn": [{"chunk": "x"}]},
                                  session_id="sess-9")
        self.assertEqual(len(self.received("session/resume")), 1)
        self.assertEqual(self.received("session/load"), [])
        self.assertEqual((result.error, result.session_id), (None, "sess-9"))

    def test_an_agent_that_cannot_resume_fails_and_never_starts_a_new_session(self):
        result = self.run_session({"turn": [{"chunk": "x"}]}, session_id="sess-9")
        self.assertTrue(result.error.startswith("resume_unsupported"))
        self.assertIsNone(result.session_id)
        self.assertEqual(self.received("session/new") + self.received("session/prompt"), [])


class CancelTest(AcpCase):
    def test_a_cancel_mid_turn_sends_session_cancel_and_ends_the_group(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        result = self.run_session({"turn": [{"chunk": "working"}, {"wait_cancel": 20}]}, cancel_event=cancel)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(len(self.received("session/cancel")), 1)
        self.assertEqual(self.received("session/cancel")[0]["params"], {"sessionId": "fake-session-1"})
        self.assertTrue(result.cancelled)
        self.assertEqual((result.error, result.stop_reason, result.text), ("cancelled", "cancelled", "working"))

    def test_the_queue_stop_request_is_bounded_and_reports_the_signal(self):
        stop = process.StopRequest()
        threading.Timer(0.5, stop.request, args=("SIGTERM",)).start()
        result = self.run_session({"turn": [{"wait_cancel": 20}]}, cancel_event=stop)
        self.assertTrue(stop.tool_signalled)
        self.assertEqual(result.error, "cancelled")
        self.assertIn("stop requested (SIGTERM): signalling the agent's process group", self.log_text())
        self.assertIn("--- exit", self.log_text())

    def test_an_open_permission_prompt_is_withdrawn_on_cancel(self):
        cancel = threading.Event()
        host = Host("allow", delay=20)
        threading.Timer(0.5, cancel.set).start()
        result = self.run_session({"turn": [{"permission": {"toolCall": EDIT_CALL, "options": OPTIONS}},
                                            {"wait_cancel": 20}]}, host=host, cancel_event=cancel)
        self.assertEqual(self.answers("session/request_permission")[0]["result"]["outcome"], {"outcome": "cancelled"})
        self.assertEqual(len(host.withdrawn), 1, "the host stopped waiting")
        self.assertEqual(result.stop_reason, "cancelled")

    def test_the_timeout_cancels_and_stops_an_agent_that_does_not_answer(self):
        started = time.monotonic()
        result = self.run_session({"turn": [{"sleep": 30}]}, timeout=1.0)
        self.assertLess(time.monotonic() - started, 9)
        self.assertTrue(result.cancelled)
        self.assertEqual((result.error, result.stop_reason), ("timed out after 1s", ""))
        self.assertIn("timeout: stopping the agent's process group", self.log_text())

    def test_an_already_cancelled_run_never_spawns(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DispatchDenied):
            self.run_session({"turn": []}, cancel_event=cancel)
        self.assertEqual(self.records(), [])


class FailureTest(AcpCase):
    def test_a_crash_mid_turn_is_an_error_with_the_exit_code(self):
        result = self.run_session({"turn": [{"chunk": "partial"}, {"crash": 3}]})
        self.assertIn("closed its output before answering session/prompt", result.error)
        self.assertEqual((result.failed_method, result.exit_code, result.text), ("session/prompt", 3, "partial"))

    def test_a_slow_initialize_times_out_and_the_agent_is_stopped(self):
        started = time.monotonic()
        result = self.run_session({"initialize": {"delay": 30}}, request_timeout=0.5)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.error, "initialize: no answer within 0.5s")
        self.assertEqual(self.received("session/new"), [])
        self.assertIsNotNone(result.exit_code)

    def test_another_protocol_version_is_refused(self):
        result = self.run_session({"initialize": {"protocolVersion": 2}})
        self.assertIn("speaks ACP protocol 2", result.error)
        self.assertEqual(self.received("session/new"), [])

    def test_an_agent_that_wants_authentication_is_never_authenticated_for_the_user(self):
        result = self.run_session({"new_session": {"error": {"code": -32000, "message": "Authentication required"}}})
        self.assertEqual(result.error, "session/new failed (-32000): Authentication required")
        self.assertEqual(self.received("authenticate"), [])

    def test_a_missing_binary_is_an_error_not_an_exception(self):
        result = self.run_session({}, cmd=[str(self.d / "no-such-agent"), "--acp"])
        self.assertEqual((result.exit_code, result.failed_method), (127, "spawn"))
        self.assertIn("could not start no-such-agent", result.error)

    def test_whatever_the_agent_started_is_killed_with_its_group(self):
        result = self.run_session({"spawn_child": True, "ignore_eof": True, "turn": [{"chunk": "x"}]})
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertLess(result.exit_code, 0, "an agent that stays after EOF is stopped by a signal")
        child = next(r["child"] for r in self.records() if "child" in r)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail("the agent's child is still running")


class SecretsTest(AcpCase):
    def access(self):
        return McpAccess(url=MCP_URL, token=TOKEN, expires_at=time.time() + 600)

    def test_the_mcp_token_travels_only_inside_the_protocol(self):
        result = self.run_session({"initialize": {"mcpHttp": True}, "env_probe": ["TIMETRACE_MCP_TOKEN"],
                                   "turn": [{"chunk": "I can see " + TOKEN + " here\n"}]}, mcp=self.access())
        servers = self.received("session/new")[0]["params"]["mcpServers"]
        self.assertEqual(servers, [{"type": "http", "name": "timetrace", "url": MCP_URL,
                                    "headers": [{"name": "Authorization", "value": "Bearer " + TOKEN}]}])
        self.assertTrue(result.mcp_attached)
        argv = next(r["argv"] for r in self.records() if "argv" in r)
        self.assertNotIn(TOKEN, json.dumps(argv))
        env = next(r["env"] for r in self.records() if "env" in r)
        self.assertIsNone(env["TIMETRACE_MCP_TOKEN"], "the token is not in the agent's environment")
        log = self.log_text()
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("Bearer", log, "a header value is never written")
        self.assertIn("mcp: timetrace attached (http, %s)" % MCP_URL, log)
        self.assertNotIn(TOKEN, result.text)

    def test_an_agent_without_http_mcp_runs_without_it(self):
        result = self.run_session({"turn": [{"chunk": "x"}]}, mcp=self.access())
        self.assertEqual(self.received("session/new")[0]["params"]["mcpServers"], [])
        self.assertFalse(result.mcp_attached)
        self.assertIn("mcp: skipped (the agent does not advertise http MCP servers)", self.log_text())
        self.assertNotIn(TOKEN, self.transcript.read_text())

    def test_billing_variables_never_reach_the_agent(self):
        leak = {"GEMINI_API_KEY": "g", "GOOGLE_API_KEY": "k", "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_APPLICATION_CREDENTIALS": "/sa.json", "GOOGLE_CLOUD_PROJECT": "p", "TT_ACP_KEEP": "1"}
        self.run_session({"env_probe": sorted(leak), "turn": []}, environ=leak,
                         drop_env=billing_env_keys("gemini"))
        env = next(r["env"] for r in self.records() if "env" in r)
        self.assertEqual({k: v for k, v in env.items() if v is not None}, {"TT_ACP_KEEP": "1"})


if __name__ == "__main__":
    unittest.main()
