"""A real signed-out Claude (`claude auth status` exits 1 and prints loggedIn=false) gets the definite login
instruction from `agent doctor`, and doctor only ever ASKS for the status: it never logs in, bills or reports."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import billing, cli
from timetrace.adapters.claude import ClaudeAdapter
from timetrace.billing import BillingVerifier

SIGNED_OUT = json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})


def fake_claude(path: Path, log: Path, code: int, stdout: str) -> str:
    path.write_text("#!/bin/sh\necho \"$@\" >> %s\nprintf '%%s\\n' '%s'\nexit %d\n" % (log, stdout, code))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class SignedOutDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "invocations.log"
        self.addCleanup(self.tmp.cleanup)

    def doctor_lines(self, code, stdout):
        binary = fake_claude(self.dir / "claude", self.log, code, stdout)
        cfg = {"bin": binary}
        verifier = BillingVerifier(lambda now: billing.verify_claude(cfg, env={}, settings_path=str(self.dir / "no-settings.json"), now=now))
        adapter = ClaudeAdapter(cfg, billing=verifier, credentials=lambda: None)
        with patch("timetrace.cloud.CloudClient", side_effect=AssertionError("doctor must not report")):
            return cli._tool_check("claude", {"claude": cfg}, {"claude": adapter})

    def invocations(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_signed_out_claude_gets_the_definite_login_instruction(self):
        lines, verified = self.doctor_lines(1, SIGNED_OUT)
        text = "\n".join(lines)
        self.assertFalse(verified)
        self.assertIn("not_logged_in", text)
        self.assertIn("未登录或登录已失效", text)
        self.assertIn("auth login", text)
        self.assertIn("使用 Claude.ai 订阅账号登录", text)
        self.assertNotIn("不能据此断定未登录或过期", text)
        self.assertIn("该工具不会被派发任务", text)           # the gate stays closed

    def test_doctor_only_asks_for_the_status_it_never_logs_in(self):
        self.doctor_lines(1, SIGNED_OUT)
        self.assertEqual(self.invocations(), ["auth status"])

    def test_an_unknown_failure_keeps_the_cautious_wording_and_no_definite_login_claim(self):
        lines, verified = self.doctor_lines(1, "boom")
        text = "\n".join(lines)
        self.assertFalse(verified)
        self.assertIn("auth_status_unavailable:exit_1", text)
        self.assertIn("不能据此断定未登录或过期", text)
        self.assertNotIn("未登录或登录已失效", text)
        self.assertEqual(self.invocations(), ["auth status"])


if __name__ == "__main__":
    unittest.main()
