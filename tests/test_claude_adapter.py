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


if __name__ == "__main__":
    unittest.main()
