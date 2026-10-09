import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.db import Database


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)


class Verified:
    def __init__(self, ok=True):
        self.ok = ok

    def capabilities(self):
        return {"can_enforce_zero_spend": self.ok}

    def capability_details(self):
        if self.ok:
            return {"can_enforce_zero_spend": True, "auth_method": "claude.ai/max", "verified_at": 1000}
        return {"can_enforce_zero_spend": False, "unsupported_reason": "not_logged_in"}

    def plan_tier(self):
        return "max"


class FakeValley:
    """Answers the device-authorization flow; records every call."""

    def __init__(self, approve=True):
        self.calls = []
        self.approve = approve

    def request(self, client, method, path, body=None, token=None):
        self.calls.append(path)
        if path == "/iphone/device-authorizations":
            return {"user_code": "WXYZ5678", "device_code": "dev-secret", "expires_in": 600, "interval": 1, "verification_uri": "timetrace://pair?code=WXYZ5678&platform=darwin&v=2"}
        if path == "/device-authorizations/token":
            return {"status": "waiting_phone"} if self.approve else {"status": "expired"}
        if path == "/iphone/device-authorizations/activate":
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 900,
                    "runner": {"id": "r1", "name": "Test Mac"}}
        return {}


class SetupWizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.repo = root / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-qm", "i", cwd=self.repo)
        env = patch.dict(os.environ, {"TIMETRACE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.valley = FakeValley()
        self.saved = {}
        self.installed = []
        self.paired = None
        self.adapters = {"claude": Verified(True), "codex": Verified(False)}
        for target, value in (
            ("timetrace.cloud.CloudClient.request", lambda client, *a, **k: self.valley.request(client, *a, **k)),
            ("timetrace.cli.CredentialStore.save", lambda store, creds: self.saved.update(creds)),
            ("timetrace.cli.CredentialStore.load", lambda store, account="default": self.paired),
            ("timetrace.cli.install_launch_agent", lambda *a, **k: self.installed.append(True) or Path("/x.plist")),
            ("timetrace.cli._adapters", lambda cfg: self.adapters),
            ("timetrace.cli.shutil.which", lambda name: "/usr/local/bin/" + name),
            ("timetrace.cli.time.sleep", lambda s: None),
            ("timetrace.cli._discover_tools", lambda home, user_home=None: (cli.config.load(home), {})),
            ("timetrace.cli.Path.home", lambda: root / "user"),
        ):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)

    def wizard(self, argv, answers=()):
        answers = list(answers)
        asked, out = [], []

        def fake_input(prompt=""):
            asked.append(prompt)
            if not answers:
                raise AssertionError("unexpected question: %s" % prompt)
            return answers.pop(0)

        def fake_print(*args, **kwargs):
            out.append(" ".join(str(a) for a in args))

        args = cli.build_parser().parse_args(["setup"] + list(argv))
        code = cli.run_setup(args, input_fn=fake_input, print_fn=fake_print)
        self.assertEqual(answers, [], "not every answer was used")
        return code, "\n".join(out), asked

    def workspaces(self):
        return Database(self.home / "timetrace.db").list_workspaces()

    def test_interactive_happy_path(self):
        code, out, asked = self.wizard([], [str(self.repo), "", "y", "0000", "y"])
        self.assertEqual(code, 0, out)
        # tool check reuses the doctor verdicts
        self.assertIn("claude", out)
        self.assertIn("零付费核验: 通过", out)
        self.assertIn("not_logged_in", out)
        # repo registered through the workspace logic
        self.assertEqual([w["path"] for w in self.workspaces()], [str(self.repo.resolve())])
        # pairing reused the cloud login flow, including the QR code
        self.assertIn("WXYZ5678", out)
        self.assertTrue(any(set(line) <= set("█▀▄ ") and len(line) > 20 for line in out.splitlines()))
        self.assertNotIn("dev-secret", out)
        self.assertEqual(self.saved.get("refresh_token"), "rt")
        self.assertEqual(self.installed, [True])

    def test_invalid_repo_is_asked_again_and_empty_answer_moves_on(self):
        not_repo = Path(self.tmp.name) / "plain"
        not_repo.mkdir()
        code, out, asked = self.wizard([], [str(not_repo), "", "n", "n"])
        self.assertEqual(code, 0, out)
        self.assertIn("git", out)
        self.assertEqual(self.workspaces(), [])
        self.assertEqual(self.saved, {})
        self.assertEqual(self.installed, [])
        self.assertEqual(self.valley.calls, [])

    def test_yes_skips_choices_but_phone_code_is_still_required(self):
        code, out, asked = self.wizard(["--repo", str(self.repo), "--yes"], ["0000"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(asked), 1)
        self.assertIn("4 位", asked[0])
        self.assertEqual(len(self.workspaces()), 1)
        self.assertEqual(self.saved.get("refresh_token"), "rt")
        self.assertEqual(self.installed, [True])

    def test_skip_flags(self):
        code, out, asked = self.wizard(["--repo", str(self.repo), "--no-pair", "--no-agent", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.valley.calls, [])
        self.assertEqual(self.installed, [])

    def test_already_paired_is_kept_unless_confirmed(self):
        self.paired = {"refresh_token": "old", "runner": {"id": "r0", "name": "Old Mac"}}
        code, out, asked = self.wizard(["--repo", str(self.repo), "--no-agent"], ["n"])
        self.assertEqual(code, 0, out)
        self.assertIn("Old Mac", out)
        self.assertEqual(self.valley.calls, [])

    def test_failed_pairing_is_reported_and_exit_code_is_nonzero(self):
        self.valley.approve = False
        code, out, asked = self.wizard(["--repo", str(self.repo), "--yes", "--no-agent"])
        self.assertNotEqual(code, 0)
        self.assertIn("timetrace cloud login", out)

    def test_warns_when_no_tool_passes_the_zero_spend_check(self):
        self.adapters = {"claude": Verified(False), "codex": Verified(False)}
        code, out, asked = self.wizard(["--no-pair", "--no-agent", "--yes"])
        self.assertIn("不会派发", out)

    def test_eof_on_input_means_skip(self):
        def eof(prompt=""):
            raise EOFError

        args = cli.build_parser().parse_args(["setup"])
        code = cli.run_setup(args, input_fn=eof, print_fn=lambda *a, **k: None)
        self.assertEqual(code, 0)
        self.assertEqual(self.valley.calls, [])
        self.assertEqual(self.installed, [])


if __name__ == "__main__":
    unittest.main()
