"""`agent doctor` for a Codex whose status command fails: only an explicit "Not logged in" gets the definite
re-login instruction; any other failing exit (even one printing the success phrase) stays cautious and closed.
Uses a stand-in `codex` in a temp dir; nothing real is invoked or read."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import billing, cli
from timetrace.adapters.codex import CodexAdapter
from timetrace.billing import BillingVerifier


class CodexDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "invocations.log"
        self.addCleanup(self.tmp.cleanup)

    def doctor_lines(self, code, stdout):
        binary = self.dir / "codex"
        binary.write_text("#!/bin/sh\necho \"$@\" >> %s\nprintf '%%s\\n' '%s'\nexit %d\n" % (self.log, stdout, code))
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        cfg = {"bin": str(binary)}
        auth = self.dir / "auth.json"
        auth.write_text('{"auth_mode": "chatgpt", "OPENAI_API_KEY": null}')
        verifier = BillingVerifier(lambda now: billing.verify_codex(cfg, env={}, auth_path=str(auth), now=now))
        adapter = CodexAdapter(cfg, billing=verifier)
        with patch("timetrace.cloud.CloudClient", side_effect=AssertionError("doctor must not report")):
            lines, verified = cli._tool_check("codex", {"codex": cfg}, {"codex": adapter})
        return "\n".join(lines), verified

    def test_explicit_not_logged_in_gets_the_login_instruction(self):
        text, verified = self.doctor_lines(1, "Not logged in")
        self.assertFalse(verified)
        self.assertIn("not_logged_in", text)
        self.assertIn("未登录或登录已失效", text)
        self.assertIn(" login，使用 ChatGPT 订阅账号登录", text)
        self.assertIn("该工具不会被派发任务", text)

    def test_other_failing_exit_is_unavailable_not_a_relogin_prompt(self):
        for code, stdout in ((1, "boom"), (2, "boom"), (1, "Logged in using ChatGPT")):
            text, verified = self.doctor_lines(code, stdout)
            self.assertFalse(verified, (code, stdout))
            self.assertIn("auth_status_unavailable:exit_%d" % code, text)
            self.assertIn("不能据此断定未登录或过期", text)
            self.assertNotIn("未登录或登录已失效", text)
            self.assertNotIn("使用 ChatGPT 订阅账号登录", text)   # the definite re-login line is for "Not logged in" only
            self.assertIn("该工具不会被派发任务", text)

    def test_doctor_only_asks_for_login_status(self):
        self.doctor_lines(1, "boom")
        self.assertEqual(set(self.log.read_text().splitlines()), {"login status"})


if __name__ == "__main__":
    unittest.main()
