"""Installing the Claude Code statusLine hook during setup / agent install.

Fake HOME and TIMETRACE_HOME throughout: the real ~/.claude/settings.json is
never touched."""
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, config, toolpath
from timetrace.db import Database

PAYLOAD = json.dumps({"model": {"id": "claude-x"},
                      "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 4102444800}}})


class Verified:
    def capabilities(self):
        return {"can_enforce_zero_spend": True}

    def capability_details(self):
        return {"can_enforce_zero_spend": True, "auth_method": "claude.ai/max", "verified_at": 1000}

    def plan_tier(self):
        return None


class HookTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.user = self.root / "home"
        self.user.mkdir()
        self.data = self.root / "data"
        self.settings = self.user / ".claude" / "settings.json"
        env = patch.dict(os.environ, {"HOME": str(self.user), "TIMETRACE_HOME": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        p = patch("timetrace.cli.Path.home", new=lambda: self.user)
        p.start()
        self.addCleanup(p.stop)
        self.tt = " ".join(shlex.quote(part) for part in cli.timetrace_command())

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_settings(self, doc):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(doc, indent=2))

    def read_settings(self):
        return json.loads(self.settings.read_text())

    def command(self):
        return self.read_settings()["statusLine"]["command"]


class StatuslineInstallTest(HookTestBase):
    def test_fresh_install_uses_the_absolute_timetrace_path(self):
        code, out, err = self.run_cli("statusline", "--install")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_settings()["statusLine"], {"type": "command", "command": self.tt + " statusline"})
        self.assertTrue(os.path.isabs(shlex.split(self.command())[0]))

    def test_install_is_idempotent_and_keeps_other_settings(self):
        self.write_settings({"theme": "dark", "statusLine": {"type": "command", "command": "tta statusline"}})
        self.run_cli("statusline", "--install")
        first = self.settings.read_text()
        self.run_cli("statusline", "--install")
        self.assertEqual(self.settings.read_text(), first)
        doc = self.read_settings()
        self.assertEqual(doc["theme"], "dark")
        self.assertEqual(doc["statusLine"]["command"], self.tt + " statusline")  # stale path refreshed

    def test_the_pre_rename_keji_hook_is_replaced_not_chained(self):
        self.write_settings({"statusLine": {"type": "command", "command": "/old/checkout/cli/bin/keji statusline"}})
        self.run_cli("statusline", "--install")
        self.assertEqual(self.command(), self.tt + " statusline")
        self.assertFalse((self.data / "statusline-chain.sh").exists())

    def test_existing_command_is_chained_not_overwritten(self):
        seen = self.root / "seen.json"
        original = {"type": "command", "command": "cat > %s; echo mine" % shlex.quote(str(seen)), "padding": 0}
        self.write_settings({"statusLine": original, "model": "opus"})
        before = self.settings.read_text()
        code, out, err = self.run_cli("statusline", "--install")
        self.assertEqual(code, 0, err)
        doc = self.read_settings()
        self.assertEqual(doc["model"], "opus")
        self.assertEqual(doc["statusLine"]["padding"], 0)
        wrapper = shlex.split(doc["statusLine"]["command"])[0]
        self.assertTrue(os.access(wrapper, os.X_OK))
        backups = list(self.settings.parent.glob("settings.json.timetrace-backup*"))
        self.assertEqual([b.read_text() for b in backups], [before])
        # Both run: the user's command sees the same stdin and its output is shown,
        # and our hook stored the quota sample.
        proc = subprocess.run([wrapper], input=PAYLOAD, capture_output=True, text=True, timeout=60,
                              env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mine")
        self.assertEqual(seen.read_text(), PAYLOAD)
        rows = Database(self.data / "timetrace.db").latest_samples()
        self.assertEqual([(r["bucket_key"], r["source"]) for r in rows], [("claude:five_hour", "statusline")])

    def test_a_failing_timetrace_never_breaks_the_users_status_line(self):
        self.write_settings({"statusLine": {"type": "command", "command": "echo mine"}})
        with patch("timetrace.cli.timetrace_command", return_value=["/bin/false"]):
            self.run_cli("statusline", "--install")
        wrapper = shlex.split(self.command())[0]
        proc = subprocess.run([wrapper], input=PAYLOAD, capture_output=True, text=True, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "mine"))

    def test_rerunning_does_not_double_wrap(self):
        original = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": original})
        self.run_cli("statusline", "--install")
        wrapped = self.read_settings()
        wrapper_text = Path(shlex.split(self.command())[0]).read_text()
        self.run_cli("statusline", "--install")
        self.assertEqual(self.read_settings(), wrapped)
        self.assertEqual(Path(shlex.split(self.command())[0]).read_text(), wrapper_text)
        self.assertEqual(wrapper_text.count("echo mine"), 1)
        self.assertEqual(len(list(self.settings.parent.glob("settings.json.timetrace-backup*"))), 1)

    def test_uninstall_restores_the_original(self):
        original = {"type": "command", "command": "echo mine", "padding": 2}
        self.write_settings({"statusLine": original, "theme": "light"})
        self.run_cli("statusline", "--install")
        wrapper = shlex.split(self.command())[0]
        code, out, err = self.run_cli("statusline", "--uninstall")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_settings(), {"statusLine": original, "theme": "light"})
        self.assertFalse(os.path.exists(wrapper))
        # and without an original, uninstall just removes ours
        self.run_cli("statusline", "--install")
        self.run_cli("statusline", "--uninstall")
        self.run_cli("statusline", "--uninstall")  # nothing left to do: still fine
        self.write_settings({"theme": "light"})
        self.run_cli("statusline", "--install")
        self.run_cli("statusline", "--uninstall")
        self.assertEqual(self.read_settings(), {"theme": "light"})

    def test_invalid_settings_json_is_left_alone(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{not json")
        code, out, err = self.run_cli("statusline", "--install")
        self.assertEqual(code, 1)
        self.assertEqual(self.settings.read_text(), "{not json")

    def test_statusline_run_records_when_it_was_last_called(self):
        from timetrace import runner_state
        self.run_cli("statusline", stdin=PAYLOAD)
        self.assertIn("statusline_seen_at", runner_state.load(self.data))


class AutoInstallTest(HookTestBase):
    def setUp(self):
        super().setUp()
        self.claude = self.user / ".local" / "bin" / "claude"
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text("#!/bin/sh\necho 2.1.3\n")
        self.claude.chmod(0o755)
        self.found = True
        self.installed = []

        def discover(home, user_home=None):
            res = {"claude": toolpath.Resolution("claude", path=str(self.claude) if self.found else None,
                                                 source="login_shell"),
                   "codex": toolpath.Resolution("codex")}
            return config.load(home), res

        for target, value in (
            ("timetrace.cli._discover_tools", discover),
            ("timetrace.cli.install_launch_agent", lambda *a, **k: self.installed.append(k) or Path("/x.plist")),
            ("timetrace.cli._adapters", lambda cfg: {"claude": Verified()}),
            ("timetrace.cli.CredentialStore.load", lambda store, account="default": {"runner": {"name": "Mac"}}),
            ("timetrace.cli._tool_version", lambda binary, node=None: "2.1.3"),
            ("timetrace.toolpath.launch_agent_state", lambda *a, **k: {"loaded": False, "running": False, "pid": None}),
        ):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)

    def wizard(self, argv, answers):
        answers, asked, out = list(answers), [], []

        def ask(prompt=""):
            asked.append(prompt)
            return answers.pop(0)

        args = cli.build_parser().parse_args(["setup"] + list(argv))
        code = cli.run_setup(args, input_fn=ask, print_fn=lambda *a: out.append(" ".join(map(str, a))))
        self.assertEqual(answers, [])
        return code, "\n".join(out), asked

    def test_agent_install_installs_the_hook_when_claude_is_found(self):
        code, out, err = self.run_cli("agent", "install")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.command(), self.tt + " statusline")
        self.assertIn("状态栏钩子", out)

    def test_agent_install_no_statusline_flag_and_missing_claude_skip_it(self):
        self.run_cli("agent", "install", "--no-statusline")
        self.assertFalse(self.settings.exists())
        self.found = False
        self.run_cli("agent", "install")
        self.assertFalse(self.settings.exists())

    def test_setup_asks_once_and_defaults_to_yes(self):
        # answers: repo (none), keep binding, install agent, install hook
        code, out, asked = self.wizard([], ["", "n", "", ""])
        self.assertEqual(asked[-1], "  让刻迹读取 Claude 额度（安装状态栏钩子）？[Y/n] ")
        self.assertEqual(sum("状态栏钩子" in q for q in asked), 1)
        self.assertEqual(self.command(), self.tt + " statusline")
        # already installed: not asked again
        code, out, asked = self.wizard([], ["", "n", ""])
        self.assertFalse(any("状态栏钩子" in q for q in asked))
        self.assertIn("状态栏钩子：已安装", out)

    def test_setup_no_answer_and_flags(self):
        self.wizard([], ["", "n", "", "n"])
        self.assertFalse(self.settings.exists())
        self.wizard(["--no-statusline", "--yes"], [])
        self.assertFalse(self.settings.exists())
        self.wizard(["--yes"], [])
        self.assertEqual(self.command(), self.tt + " statusline")

    def test_doctor_reports_the_hook_and_the_last_sample(self):
        code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("Claude 状态栏钩子: 未安装 → 运行 timetrace statusline --install", out)
        self.run_cli("statusline", "--install")
        code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("Claude 状态栏钩子: 已安装；还没有收到额度样本", out)
        with patch("timetrace.cli.time.time", return_value=1790000000):
            self.run_cli("statusline", stdin=PAYLOAD)
        code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("Claude 状态栏钩子: 已安装；上次额度样本 %s" % cli._iso_local(1790000000), out)


if __name__ == "__main__":
    unittest.main()
