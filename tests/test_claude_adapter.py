import json
import unittest
from pathlib import Path

from timetrace.adapters import claude
from timetrace.adapters.base import SAFETY_RULES

FIXTURES = Path(__file__).parent / "fixtures"


def load_lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class ParseStreamTest(unittest.TestCase):
    def test_success_run_yields_samples_and_session(self):
        res = claude.parse_stream(load_lines("claude_stream.jsonl"))
        self.assertTrue(res.ok)
        self.assertFalse(res.blocked)
        self.assertEqual(res.session_id, "d84c4ed6-6c86-42bf-ba75-21c6e635b3dc")
        self.assertEqual(res.output, "ACK")
        keys = {s.bucket_key: s for s in res.samples}
        self.assertEqual(set(keys), {"claude:five_hour", "claude:seven_day"})
        self.assertEqual(keys["claude:five_hour"].used_pct, 13.0)
        self.assertEqual(keys["claude:five_hour"].reset_at, 1788370200)
        self.assertEqual(keys["claude:five_hour"].window_mins, 300)
        self.assertTrue(keys["claude:five_hour"].is_representative)
        self.assertFalse(keys["claude:seven_day"].is_representative)
        self.assertEqual(keys["claude:seven_day"].used_pct, 3.0)
        self.assertEqual(res.reset_at, 1788370200)

    def test_rejected_status_marks_blocked(self):
        lines = load_lines("claude_stream.jsonl")
        lines = [l.replace('"status":"allowed"', '"status":"rejected"') for l in lines]
        res = claude.parse_stream(lines)
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.reset_at, 1788370200)

    def test_api_error_429_marks_blocked(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "API Error: 429", "session_id": "s", "api_error_status": 429}
        res = claude.parse_stream([json.dumps(result)])
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)

    def test_limit_text_with_error_marks_blocked(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "You've hit your limit · resets 9pm", "session_id": "s",
                  "api_error_status": None}
        res = claude.parse_stream([json.dumps(result)])
        self.assertTrue(res.blocked)

    def test_plain_error_is_failure_not_block(self):
        result = {"type": "result", "subtype": "error_max_turns", "is_error": True,
                  "result": "stopped", "session_id": "s", "api_error_status": None}
        res = claude.parse_stream([json.dumps(result)])
        self.assertFalse(res.blocked)
        self.assertFalse(res.ok)
        self.assertIn("error_max_turns", res.error)

    def test_garbage_lines_ignored(self):
        res = claude.parse_stream(["", "not json", "{broken"])
        self.assertFalse(res.ok)
        self.assertEqual(res.samples, [])


class BuildCmdTest(unittest.TestCase):
    cfg = {"bin": "claude", "permission_mode": "acceptEdits", "model": None, "extra_args": [],
           "allowed_tools": ["Bash(git add:*)", "Bash(git commit:*)"]}

    def test_start_uses_session_id(self):
        cmd = claude.build_cmd(self.cfg, "do it", session_id="abc")
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash(git push*)")
        i = cmd.index("--allowedTools")
        self.assertEqual(cmd[i + 1:i + 3], ["Bash(git add:*)", "Bash(git commit:*)"])
        # The variadic --allowedTools must be closed by another option.
        self.assertTrue(cmd[i + 3].startswith("--"), cmd[i + 3])

    def test_no_allowed_tools_flag_when_empty(self):
        cmd = claude.build_cmd(dict(self.cfg, allowed_tools=[]), "x", session_id="abc")
        self.assertNotIn("--allowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], SAFETY_RULES)
        self.assertEqual(cmd[-1], "x")

    def test_resume_uses_resume_flag_and_model(self):
        cfg = dict(self.cfg, model="haiku", extra_args=["--effort", "low"])
        cmd = claude.build_cmd(cfg, "go on", session_id="abc", resume="abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertIn("--effort", cmd)


    def test_prompt_that_looks_like_an_option_stays_a_prompt(self):
        # A phone-supplied prompt is the last argv element. If it starts with
        # "-", Claude's option parser would read e.g. --settings={hooks...}
        # or --permission-mode=bypassPermissions as a flag.
        for prompt in ("--settings={\"hooks\":{}}", "-p", "--permission-mode=bypassPermissions"):
            with self.subTest(prompt=prompt):
                cmd = claude.build_cmd(self.cfg, prompt, session_id="abc")
                self.assertFalse(cmd[-1].startswith("-"))
                self.assertEqual(cmd[-1].strip(), prompt)
        self.assertEqual(claude.build_cmd(self.cfg, "normal", session_id="abc")[-1], "normal")

    def test_project_settings_from_the_worktree_are_not_loaded(self):
        # Worktree content (or a previous run's commit) could carry
        # .claude/settings.json with hooks or a broad allow list; -p mode skips
        # the trust dialog, so only the user's own settings are loaded.
        cmd = claude.build_cmd(self.cfg, "x", session_id="abc")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        cmd = claude.build_cmd(dict(self.cfg, setting_sources="user,project"), "x", session_id="abc")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user,project")


class StreamCmdTest(unittest.TestCase):
    cfg = {"bin": "claude", "permission_mode": "acceptEdits", "model": None, "extra_args": [],
           "allowed_tools": ["Bash(git add:*)"]}

    def test_task_run_is_bidirectional_with_host_permission_prompts(self):
        cmd = claude.build_stream_cmd(self.cfg, session_id="abc")
        self.assertEqual(cmd[:7], ["claude", "-p", "--input-format", "stream-json", "--output-format",
                                   "stream-json", "--verbose"])
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "host")
        self.assertEqual(cmd[cmd.index("--permission-prompt-tool") + 1], "stdio")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "abc")
        i = cmd.index("--disallowedTools")
        self.assertEqual(cmd[i + 1:i + 3], ["Bash(git push:*)", "Bash(git push*)"])
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], SAFETY_RULES)
        # The prompt is never an argv element (it goes over stdin).
        self.assertTrue(cmd[-1] == SAFETY_RULES or cmd[-2].startswith("--"))

    def test_resume_and_explicit_widening_are_kept(self):
        cmd = claude.build_stream_cmd(dict(self.cfg, permission_mode="bypassPermissions"), resume="s1")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")

    def test_folder_stream_run_keeps_restricted_mode_and_never_asks(self):
        cmd = claude.build_stream_folder_cmd(self.cfg, "/ws", session_id="s1")
        self.assertIn("--restricted", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertNotIn("--permission-prompt-tool", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/ws")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash")


class StreamAdapterTest(unittest.TestCase):
    """ClaudeAdapter + RunControl against the fake stream-json CLI."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.transcript = self.d / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, turns, on_event=None):
        import os, threading
        from unittest.mock import patch
        from timetrace.billing import StaticBilling
        from timetrace.remote_control import RunControl
        scenario = self.d / "s.json"
        scenario.write_text(json.dumps({"turns": turns}))
        fake = str(Path(__file__).with_name("fake_claude_stream.py"))
        adapter = claude.ClaudeAdapter({"bin": fake, "allowed_tools": []}, billing=StaticBilling(True),
                                       credentials=lambda: {})
        cancel = threading.Event()
        control = RunControl("j1", self.d / "home", cancel, root=str(self.d), user_home=str(self.d / "user"))
        stop = threading.Event()
        def pump():
            while not stop.is_set():
                for event in control.take_events():
                    if on_event:
                        on_event(control, event)
                stop.wait(.02)
        t = threading.Thread(target=pump, daemon=True); t.start()
        env = {"TT_FAKE_SCENARIO": str(scenario), "TT_FAKE_TRANSCRIPT": str(self.transcript)}
        try:
            with patch.dict(os.environ, env):
                result = adapter.start("build it", str(self.d), "11111111-2222-3333-4444-555555555555",
                                       str(self.d / "run.log"), cancel, control=control)
        finally:
            stop.set(); t.join(2)
        for event in control.take_events():
            if on_event:
                on_event(control, event)
        records = [json.loads(l) for l in self.transcript.read_text().splitlines()]
        return result, records

    def test_phone_approved_command_continues_the_run(self):
        seen = []
        def approve(control, event):
            seen.append(event)
            if event["type"] == "approval_requested":
                control.apply_lease({"approvals": [{"request_id": event["request_id"], "decision": "approve",
                                                    "remember": False}]})
        result, records = self.run_adapter([[{"permission": {"tool": "Bash", "input": {"command": "npm test"}}},
                                             {"permission": {"tool": "Bash", "input": {"command": "git push"}}},
                                             {"result": "all green"}]], approve)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "all green")
        argv = records[0]["argv"]
        self.assertNotIn("build it", argv)
        replies = [r["decision"]["reply"]["response"]["behavior"] for r in records if "decision" in r]
        self.assertEqual(replies, ["allow", "deny"])  # git push: local rule, never asked
        self.assertEqual([e["type"] for e in seen], ["approval_requested", "approval_resolved"])


if __name__ == "__main__":
    unittest.main()
