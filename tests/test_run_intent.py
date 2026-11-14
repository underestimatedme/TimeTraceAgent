"""AI 思路上报: `progress` and `intent` events of a task run (run canvas phase 2).

Gating by the claim, the throttle and de-duplication, intent parsing and its
cap, Claude's stream session and Codex's output lines, contiguous sequence
numbers with the terminal event, and job kinds that must not change."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import process, run_intent
from timetrace.models import RunResult
from timetrace.remote_control import RunControl
from tests.test_remote_jobs import Adapter, Base, ControlCloud

FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude_stream.py"))


def tool_use(name, tool_input):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}]}}


def text(value):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": value}]}}


def codex_line(kind, item):
    return json.dumps({"type": kind, "item": item})


class ClaimFlagsTest(unittest.TestCase):
    def test_only_a_json_true_counts(self):
        self.assertEqual(run_intent.claim_flags({}), (False, False))
        self.assertEqual(run_intent.claim_flags({"report_progress": True}), (True, False))
        self.assertEqual(run_intent.claim_flags({"report_progress": True, "report_intent": True}), (True, True))
        # Intent without progress: an older server would reject the type, so neither.
        self.assertEqual(run_intent.claim_flags({"report_intent": True}), (False, False))
        for value in ("true", 1, "1", None, {}):
            self.assertEqual(run_intent.claim_flags({"report_progress": value, "report_intent": True}), (False, False))
            self.assertEqual(run_intent.claim_flags({"report_progress": True, "report_intent": value}), (True, False))
        self.assertEqual(run_intent.claim_flags(None), (False, False))
        both = {"report_progress": True, "report_intent": True}
        self.assertEqual(run_intent.claim_flags(dict(both, kind="task")), (True, True))
        for kind in ("chat_turn", "review_turn", "check", "planning", "import_parse"):
            self.assertEqual(run_intent.claim_flags(dict(both, kind=kind)), (False, False))

    def test_instruction_is_chinese_and_names_the_prefix_and_the_limit(self):
        prompt = run_intent.with_instruction("fix it")
        self.assertTrue(prompt.startswith("fix it\n\n"))
        self.assertIn("INTENT:", run_intent.INTENT_INSTRUCTION)
        self.assertIn("120", run_intent.INTENT_INSTRUCTION)
        self.assertIn("在改动任何内容之前", run_intent.INTENT_INSTRUCTION)


class ProgressLineTest(unittest.TestCase):
    def test_action_level_lines_from_the_approval_summary(self):
        self.assertEqual(run_intent.progress_line("Bash", {"command": "npm test", "description": "run tests"}),
                         "运行 npm test")
        self.assertEqual(run_intent.progress_line("Read", {"file_path": "/wt/web/src/Today.tsx"}, root="/wt"),
                         "读取 web/src/Today.tsx")
        self.assertEqual(run_intent.progress_line("Write", {"file_path": "/wt/a.py", "content": "SECRET BODY"},
                                                  root="/wt/"), "修改文件 a.py")
        self.assertEqual(run_intent.progress_line("Read", {"file_path": "/Users/u/notes.md"}, home="/Users/u"),
                         "读取 ~/notes.md")

    def test_no_file_contents_command_output_or_todo_text(self):
        heredoc = "cat > notes.txt <<'EOF'\nprivate line one\nprivate line two\nEOF"
        line = run_intent.progress_line("Bash", {"command": heredoc})
        self.assertEqual(line, "运行 cat > notes.txt <<'EOF' …")
        self.assertNotIn("private", line)
        self.assertEqual(run_intent.progress_line("TodoWrite", {"todos": [{"content": "my plan"}]}), "")
        self.assertEqual(run_intent.progress_line("Bash", {"command": "  "}), "")

    def test_secrets_are_masked_and_the_line_is_at_most_200_characters(self):
        key = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        line = run_intent.progress_line("Bash", {"command": "curl -H 'Authorization: token %s' x" % key})
        self.assertNotIn(key, line)
        long = run_intent.progress_line("Bash", {"command": "echo " + "字" * 400})
        self.assertEqual(len(long), 200)
        self.assertTrue(long.endswith("…"))


class ParseTest(unittest.TestCase):
    def test_claude_tool_use_and_intent_lines_in_order(self):
        msg = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "好的。\nINTENT: 先读代码再改\n其他说明"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "rg token"}},
            {"type": "thinking", "thinking": "INTENT: never read"},
        ]}}
        self.assertEqual(run_intent.from_claude(msg), [("intent", "先读代码再改"), ("progress", "运行 rg token")])
        self.assertEqual(run_intent.from_claude({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "INTENT: output"}]}}), [])
        self.assertEqual(run_intent.from_claude({"type": "result", "result": "INTENT: x"}), [])

    def test_intent_line_variants(self):
        self.assertEqual(run_intent.intent_lines("INTENT: a\n  **INTENT:** b\nINTENT：c\nnot INTENT: d\nintent: e"),
                         ["a", "b", "c"])
        self.assertEqual(run_intent.intent_lines(None), [])

    def test_codex_commands_file_changes_and_agent_messages(self):
        started = codex_line("item.started", {"id": "i0", "type": "command_execution",
                                              "command": "bash -lc 'npm test -- --watch=false'",
                                              "status": "in_progress"})
        self.assertEqual(run_intent.from_codex(started), [("progress", "运行 npm test -- --watch=false")])
        listed = codex_line("item.started", {"type": "command_execution", "command": ["/bin/zsh", "-lc", "ls src"]})
        self.assertEqual(run_intent.from_codex(listed), [("progress", "运行 ls src")])
        # Completed commands carry their output: never read.
        done = codex_line("item.completed", {"type": "command_execution", "command": "cat .env",
                                             "aggregated_output": "INTENT: KEY=1\n", "exit_code": 0})
        self.assertEqual(run_intent.from_codex(done), [])
        change = codex_line("item.completed", {"type": "file_change", "status": "completed", "changes": [
            {"path": "/wt/src/a.py", "kind": "update"}, {"path": "/wt/src/b.py", "kind": "add"}]})
        self.assertEqual(run_intent.from_codex(change, root="/wt"), [("progress", "修改文件 src/a.py 等 2 个文件")])
        message = codex_line("item.completed", {"type": "agent_message", "text": "INTENT: 先跑测试\n然后修"})
        self.assertEqual(run_intent.from_codex(message), [("intent", "先跑测试")])
        reasoning = codex_line("item.completed", {"type": "reasoning", "text": "INTENT: private reasoning"})
        self.assertEqual(run_intent.from_codex(reasoning), [])
        for raw in ("", "not json", "{broken", "[1]", json.dumps({"type": "turn.completed"})):
            self.assertEqual(run_intent.from_codex(raw), [])


class NarratorTest(unittest.TestCase):
    def test_first_line_goes_at_once_then_one_per_window_and_the_latest_wins(self):
        n = run_intent.Narrator(report_intent=False, interval=10)
        self.assertEqual(n.observe([("progress", "运行 a")], 0), [{"type": "progress", "message": "运行 a"}])
        self.assertEqual(n.observe([("progress", "运行 b")], 1), [])
        self.assertEqual(n.observe([("progress", "运行 c")], 2), [])
        self.assertEqual(n.tick(9.9), [])
        self.assertEqual(n.tick(10), [{"type": "progress", "message": "运行 c"}])
        self.assertEqual(n.tick(30), [])
        # A new line after a quiet window goes at once and supersedes nothing.
        self.assertEqual(n.observe([("progress", "运行 d")], 31), [{"type": "progress", "message": "运行 d"}])

    def test_exact_repeats_are_skipped(self):
        n = run_intent.Narrator(report_intent=False, interval=10)
        n.observe([("progress", "运行 a")], 0)
        # a (shown), then b, a within its window: what is shown is already the latest.
        n.observe([("progress", "运行 b")], 1)
        n.observe([("progress", "运行 a")], 2)
        self.assertEqual(n.tick(40), [])
        # The same line again after a quiet window is still a repeat.
        self.assertEqual(n.observe([("progress", "运行 a")], 50), [])

    def test_a_waiting_line_is_dropped_when_the_run_stops(self):
        n = run_intent.Narrator(report_intent=False, interval=10)
        n.observe([("progress", "运行 a"), ("progress", "运行 b")], 0)
        n.stop()
        self.assertEqual(n.tick(100), [])
        self.assertEqual(n.observe([("progress", "运行 c"), ("intent", "x")], 200), [])

    def test_intents_need_consent_change_and_stop_at_five(self):
        quiet = run_intent.Narrator(report_intent=False)
        self.assertEqual(quiet.observe([("intent", "先读代码")], 0), [])
        n = run_intent.Narrator(report_intent=True, interval=10)
        self.assertEqual(n.observe([("intent", "先读代码")], 0), [{"type": "intent", "message": "先读代码"}])
        self.assertEqual(n.observe([("intent", "先读代码")], 1), [])          # unchanged
        self.assertEqual(n.observe([("intent", "`rm -rf x`")], 1), [])        # empty after filtering
        sent = [n.observe([("intent", "方案 %d" % i)], 2 + i) for i in range(10)]
        self.assertEqual(sum(len(e) for e in sent), 4)
        self.assertEqual(n.intents_sent, 5)
        # Intents are not throttled by the progress window.
        n2 = run_intent.Narrator(report_intent=True, interval=10)
        self.assertEqual(len(n2.observe([("intent", "一"), ("intent", "二"), ("progress", "运行 a")], 0)), 3)

    def test_intents_are_filtered_and_cut_to_120(self):
        n = run_intent.Narrator(report_intent=True)
        events = n.observe([("intent", "先联系 alex@example.com，" + "改" * 300)], 0)
        self.assertNotIn("alex@example.com", events[0]["message"])
        self.assertEqual(len(events[0]["message"]), 120)


class RunControlNarrationTest(unittest.TestCase):
    def control(self, **kw):
        self.now = [0.0]
        control = RunControl("j1", Path(tempfile.mkdtemp()), threading.Event(), root="/wt",
                             clock=lambda: self.now[0])
        if kw.get("narrate", True):
            control.narrate(kw.get("intent", True), interval=10)
        return control

    def test_without_narrate_nothing_is_observed(self):
        control = self.control(narrate=False)
        control.observe(tool_use("Bash", {"command": "npm test"}))
        control.observe_line(codex_line("item.started", {"type": "command_execution", "command": "ls"}))
        control.tick()
        self.assertEqual(control.take_events(), [])
        self.assertFalse(control.wakeup.is_set())

    def test_progress_wakes_the_job_loop_but_not_the_approval_pump(self):
        control = self.control()
        control.observe(tool_use("Read", {"file_path": "/wt/src/a.py"}))
        self.assertTrue(control.wakeup.is_set())
        self.assertFalse(control.pending.is_set())
        self.assertEqual(control.take_events(), [{"type": "progress", "message": "读取 src/a.py"}])

    def test_tick_sends_the_waiting_line_and_finishing_drops_it(self):
        control = self.control()
        control.observe(tool_use("Bash", {"command": "a"}))
        control.observe(tool_use("Bash", {"command": "b"}))
        self.now[0] = 10
        control.tick()
        self.assertEqual([e["message"] for e in control.take_events()], ["运行 a", "运行 b"])
        control.observe(tool_use("Bash", {"command": "c"}))  # waits for its window
        self.assertIsNone(control.next_followup(ok=True))    # the run is over
        self.now[0] = 100
        control.tick()
        control.observe(text("INTENT: 迟到的思路"))
        self.assertEqual(control.take_events(), [])

    def test_five_intents_per_attempt(self):
        control = self.control()
        for i in range(8):
            control.observe(text("INTENT: 第 %d 个方案" % i))
        self.assertEqual([e["message"] for e in control.take_events()], ["第 %d 个方案" % i for i in range(5)])


class RunStreamingObserverTest(unittest.TestCase):
    def test_each_stdout_line_is_shown_masked_and_a_failing_observer_changes_nothing(self):
        token = "ttm_" + "A" * 30
        script = "import sys; print('one'); print('tok %s'); sys.stderr.write('err\\n')" % token
        with tempfile.TemporaryDirectory() as d:
            seen = []
            code, lines = process.run_streaming([sys.executable, "-c", script], d, str(Path(d) / "log"),
                                                on_line=seen.append)
            self.assertEqual(code, 0)
            self.assertEqual(seen, ["one", "tok [REDACTED:timetrace]"])
            def boom(line):
                raise RuntimeError("observer broke")
            code, lines = process.run_streaming([sys.executable, "-c", script], d, str(Path(d) / "log"), on_line=boom)
            self.assertEqual((code, lines), (0, ["one", "tok [REDACTED:timetrace]"]))


class ClaudeStreamTest(unittest.TestCase):
    """The fake stream-json CLI's tool_use / text steps reach host.observe, and a RunControl turns them into events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, turns):
        scenario = self.d / "s.json"
        scenario.write_text(json.dumps({"turns": turns}))
        return {"TT_FAKE_SCENARIO": str(scenario), "TT_FAKE_TRANSCRIPT": str(self.d / "t.jsonl")}

    TURN = [{"text": "INTENT: 先跑测试，再改失败的断言"},
            {"tool_use": {"name": "Bash", "input": {"command": "npm test"}}},
            {"tool_use": {"name": "Edit", "input": {"file_path": "PLACEHOLDER/src/a.ts", "old_string": "SECRET OLD",
                                                    "new_string": "SECRET NEW"}}},
            {"result": "done"}]

    def turn(self):
        steps = json.loads(json.dumps(self.TURN))
        steps[2]["tool_use"]["input"]["file_path"] = str(self.d) + "/src/a.ts"
        return steps

    def test_session_shows_assistant_messages_to_a_host_with_observe(self):
        from timetrace.claude_stream import StreamSession
        from tests.test_claude_stream import Recorder

        class Watching(Recorder):
            def __init__(self):
                super().__init__()
                self.seen = []
            def observe(self, msg):
                self.seen.append(msg)
                raise RuntimeError("an observer that fails does not stop the reader")
        host = Watching()
        cmd = [sys.executable, FAKE_CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
        with patch.dict(os.environ, self.env([self.turn()])):
            code, lines = StreamSession(cmd, str(self.d), str(self.d / "run.log"), "go", host=host, timeout=20).run()
        self.assertEqual(code, 0)
        blocks = [b for m in host.seen for b in m["message"]["content"]]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use", "tool_use", "text"])
        self.assertEqual(blocks[1]["input"], {"command": "npm test"})
        self.assertTrue(any('"type": "result"' in line for line in lines))
        # A host without observe (every earlier host) still works.
        with patch.dict(os.environ, self.env([self.turn()])):
            code, _ = StreamSession(cmd, str(self.d), str(self.d / "run.log"), "go", host=Recorder(), timeout=20).run()
        self.assertEqual(code, 0)

    def test_claude_adapter_with_a_narrating_control_reports_progress_and_intent(self):
        from timetrace.adapters import claude
        from timetrace.billing import StaticBilling
        adapter = claude.ClaudeAdapter({"bin": FAKE_CLAUDE, "allowed_tools": []}, billing=StaticBilling(True),
                                       credentials=lambda: {})
        for consent in (True, False):
            control = RunControl("j1", self.d / "home", threading.Event(), root=str(self.d), user_home=str(self.d / "u"))
            control.narrate(consent)
            with patch.dict(os.environ, self.env([self.turn()])):
                result = adapter.start("build it", str(self.d), "11111111-2222-3333-4444-555555555555",
                                       str(self.d / "run.log"), threading.Event(), control=control)
            self.assertTrue(result.ok, result.error)
            events = control.take_events()
            expected = [{"type": "progress", "message": "运行 npm test"}]  # the edit waits for its window
            if consent:
                expected.insert(0, {"type": "intent", "message": "先跑测试，再改失败的断言"})
            self.assertEqual(events, expected)
            self.assertNotIn("SECRET", json.dumps(events))


class CodexAdapterTest(unittest.TestCase):
    LINES = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "INTENT: 不该读到的推理"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "INTENT: 先看 README 再补测试"}},
        {"type": "item.started", "item": {"type": "command_execution", "command": "bash -lc 'cat README.md'"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "bash -lc 'cat README.md'",
                                            "aggregated_output": "file body\n", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed"},
    ]

    def run_codex(self, control):
        from timetrace.adapters import codex
        from timetrace.billing import StaticBilling
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "codex"
            script.write_text("#!%s\nimport json\nfor m in %r:\n    print(json.dumps(m), flush=True)\n"
                              % (sys.executable, self.LINES))
            script.chmod(0o755)
            adapter = codex.CodexAdapter({"bin": str(script), "sandbox": "workspace-write"}, billing=StaticBilling(True))
            with patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                kw = {"control": control} if control is not None else {}
                result = adapter.start("go", d, "tid", str(Path(d) / "run.log"), threading.Event(), **kw)
                folder = adapter.start_folder("go", d, d, "tid", str(Path(d) / "run2.log"), threading.Event(), **kw)
        return result, folder

    def test_codex_lines_become_progress_and_intent(self):
        control = RunControl("j1", Path(tempfile.mkdtemp()), threading.Event(), root="/wt")
        control.narrate(True)
        result, folder = self.run_codex(control)
        self.assertTrue(result.ok and folder.ok)
        self.assertEqual(result.output, "done")
        events = control.take_events()
        # The folder run repeats the same intent and line: both are skipped as unchanged.
        self.assertEqual(events, [{"type": "intent", "message": "先看 README 再补测试"},
                                  {"type": "progress", "message": "运行 cat README.md"}])

    def test_without_a_control_the_run_is_what_it_was(self):
        result, folder = self.run_codex(None)
        self.assertTrue(result.ok and folder.ok)
        self.assertEqual(result.output, "done")


class Watched(Adapter):
    """A Claude-like adapter (interactive): shows `messages` to the control the way a stream session does."""
    interactive_runs = True

    def __init__(self, messages, between=0.0, after=0.0):
        super().__init__()
        self.messages, self.between, self.after = messages, between, after

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
        self.calls.append(("start", prompt))
        for msg in self.messages:
            control.observe(msg)
            time.sleep(self.between)
        time.sleep(self.after)
        return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)


class CodexLike(Adapter):
    """A non-interactive adapter whose runs can be watched line by line."""
    observes_runs = True

    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
        self.calls.append(("start", prompt, control is not None))
        if control is not None:
            for line in self.lines:
                control.observe_line(line)
        return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)


class Plain(Adapter):
    """Neither interactive nor observable: start() takes no control at all."""

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append(("start", prompt))
        return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)


MESSAGES = [text("我先看看。\nINTENT: 先找出失败的测试，再只改相关断言"),
            tool_use("Bash", {"command": "npm test"}),
            tool_use("Read", {"file_path": "src/a.ts"})]


class AgentGatingTest(Base):
    def run_job(self, adapter, provider="claude", **flags):
        job = {"id": "j1", "plan_id": "p1", "provider": provider, "tool_profile_id": provider + "-default"}
        job.update(flags)
        cloud = ControlCloud([job])
        outcome = self.agent(cloud, adapter, provider=provider).run_once()
        self.assertEqual(outcome, "job j1 → awaiting_review")
        events = self.events(cloud)
        self.assert_contiguous(events)
        return events

    def test_without_report_progress_nothing_new_is_sent_and_the_prompt_is_unchanged(self):
        for flags in ({}, {"report_intent": True}, {"report_progress": "true", "report_intent": True}):
            with self.subTest(flags=flags):
                self.setUp()
                adapter = Watched(MESSAGES)
                events = self.run_job(adapter, **flags)
                self.assertEqual([e["type"] for e in events], ["running", "completed"])
                self.assertEqual(adapter.calls, [("start", "p")])

    def test_progress_only_sends_progress_and_no_intent_or_instruction(self):
        adapter = Watched(MESSAGES)
        events = self.run_job(adapter, report_progress=True)
        self.assertEqual([e["type"] for e in events], ["running", "progress", "completed"])
        self.assertEqual(events[1]["message"], "运行 npm test")
        self.assertIn("observed_at", events[1])
        self.assertEqual(adapter.calls, [("start", "p")])

    def test_both_flags_send_intent_and_add_the_instruction(self):
        adapter = Watched(MESSAGES)
        events = self.run_job(adapter, report_progress=True, report_intent=True)
        self.assertEqual([e["type"] for e in events], ["running", "intent", "progress", "completed"])
        self.assertEqual(events[1]["message"], "先找出失败的测试，再只改相关断言")
        self.assertEqual(adapter.calls, [("start", "p" + run_intent.INTENT_INSTRUCTION)])

    def test_the_resume_prompt_gets_the_instruction_too(self):
        from timetrace.agent import RESUME_PROMPT, Agent
        job = {"resume_of_job_id": "j0", "resume_note": "继续"}
        self.assertEqual(Agent._run_prompt(job, True), RESUME_PROMPT + "\n\nAdditional instruction from the user:\n继续")
        # _execute_tracked appends the instruction to whatever _run_prompt returned (see the test above).

    def test_throttled_line_is_sent_by_the_job_loop_once_its_window_passes(self):
        adapter = Watched([tool_use("Bash", {"command": "a"}), tool_use("Bash", {"command": "b"}),
                           tool_use("Bash", {"command": "c"})], after=1.0)
        with patch.object(run_intent, "PROGRESS_INTERVAL_SECONDS", .3):
            events = self.run_job(adapter, report_progress=True)
        self.assertEqual([(e["type"], e.get("message")) for e in events],
                         [("running", "started"), ("progress", "运行 a"), ("progress", "运行 c"),
                          ("completed", events[-1].get("message"))])

    def test_a_line_still_waiting_at_the_end_is_dropped_and_the_terminal_event_comes_last(self):
        adapter = Watched([tool_use("Bash", {"command": "a"}), tool_use("Bash", {"command": "b"})])
        events = self.run_job(adapter, report_progress=True)
        self.assertEqual([(e["type"], e.get("message")) for e in events][:2],
                         [("running", "started"), ("progress", "运行 a")])
        self.assertEqual([e["type"] for e in events], ["running", "progress", "completed"])

    def test_progress_and_approvals_share_one_contiguous_sequence(self):
        class Asking(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                control.observe(tool_use("Read", {"file_path": "src/a.ts"}))
                control.permission({"tool_name": "Bash", "input": {"command": "npm install left-pad"}},
                                   threading.Event())
                control.observe(text("INTENT: 装好依赖后再跑测试"))
                return RunResult(exit_code=0, ok=True, output="ok", session_id=session_id)
        def lease_extra():
            asked = [e for _, e in cloud.events if e["type"] == "approval_requested"]
            if asked:
                return {"approvals": [{"request_id": asked[0]["request_id"], "decision": "approve"}]}
            return {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default",
                               "report_progress": True, "report_intent": True}], lease_extra)
        self.assertEqual(self.agent(cloud, Asking(), provider="claude").run_once(), "job j1 → awaiting_review")
        events = self.events(cloud)
        self.assert_contiguous(events)
        self.assertEqual([e["type"] for e in events],
                         ["running", "progress", "approval_requested", "approval_resolved", "intent", "completed"])

    def test_codex_like_adapter_gets_the_control_only_when_asked(self):
        lines = [codex_line("item.completed", {"type": "agent_message", "text": "INTENT: 先补测试"}),
                 codex_line("item.started", {"type": "command_execution", "command": "bash -lc 'pytest -q'"})]
        adapter = CodexLike(lines)
        events = self.run_job(adapter, provider="codex")
        self.assertEqual([e["type"] for e in events], ["running", "completed"])
        self.assertEqual(adapter.calls, [("start", "p", False)])
        self.setUp()
        adapter = CodexLike(lines)
        events = self.run_job(adapter, provider="codex", report_progress=True, report_intent=True)
        self.assertEqual([(e["type"], e.get("message")) for e in events[1:3]],
                         [("intent", "先补测试"), ("progress", "运行 pytest -q")])
        self.assertEqual(adapter.calls, [("start", "p" + run_intent.INTENT_INSTRUCTION, True)])

    def test_an_adapter_that_cannot_be_watched_is_left_alone(self):
        adapter = Plain()
        events = self.run_job(adapter, provider="codex", report_progress=True, report_intent=True)
        self.assertEqual([e["type"] for e in events], ["running", "completed"])
        self.assertEqual(adapter.calls, [("start", "p")])


class OtherJobKindsTest(unittest.TestCase):
    def test_a_chat_turn_with_the_flags_sends_what_it_always_did(self):
        from tests.test_chat_turn import AgentChatTurnTest, ChatAdapter, ChatCloud
        cloud = ChatCloud()
        cloud.job.update({"report_progress": True, "report_intent": True})
        adapter = ChatAdapter(RunResult(exit_code=0, ok=True, output="INTENT: 不该上报\n答案", session_id="s"))
        case = AgentChatTurnTest()
        outcome, repo = case.run_turn(cloud, adapter)
        self.assertEqual(outcome, "job c1 → replied")
        self.assertEqual([e["type"] for e in cloud.events], ["running", "completed"])
        self.assertEqual([e["seq"] for e in cloud.events], [1, 2])
        self.assertEqual(adapter.calls[0][0], "explain the repo")


if __name__ == "__main__":
    unittest.main()
