import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from timetrace import cli


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)


class CliTest(unittest.TestCase):
    def test_inventory_uploads_explicit_zero_spend_capability(self):
        class CapabilityAdapter:
            def __init__(self, value):
                self.value = value

            def capabilities(self):
                return self.value

        # Inspect the payload at the HTTP boundary: an installed binary is not
        # evidence of zero-spend safety.
        for caps, expected in (({}, False), ({"can_enforce_zero_spend": False}, False),
                               ({"can_enforce_zero_spend": True}, True),
                               ({"can_enforce_zero_spend": "true"}, False)):
            with self.subTest(caps=caps), tempfile.TemporaryDirectory() as home:
                payloads = []

                def request(client, method, path, body=None, token=None):
                    if path == "/runner/inventory":
                        payloads.append(json.loads(json.dumps(body)))
                    return {}

                with patch.dict(os.environ, {"TIMETRACE_HOME": home}), \
                     patch("timetrace.cli._adapters", return_value={"codex": CapabilityAdapter(caps)}), \
                     patch("timetrace.cli.SessionManager.token", return_value="test-token"), \
                     patch("timetrace.cloud.CloudClient.request", new=request), \
                     patch("timetrace.cli.Agent.run_once", return_value="idle"), \
                     patch("timetrace.cli.shutil.which", return_value="/test/codex"):
                    code, _, err = self.run_cli("agent", "run", "--once")
                self.assertEqual(code, 0, err)
                self.assertEqual(len(payloads), 1)
                self.assertIs(payloads[0]["tools"][0].get("can_enforce_zero_spend"), expected)

    def test_inventory_uploads_plan_tier(self):
        class TierAdapter:
            def capabilities(self):
                return {"can_enforce_zero_spend": True}

            def plan_tier(self):
                return "max"

        payloads = []

        def request(client, method, path, body=None, token=None):
            if path == "/runner/inventory":
                payloads.append(json.loads(json.dumps(body)))
            return {}

        with patch("timetrace.cli._adapters", return_value={"claude": TierAdapter()}), \
             patch("timetrace.cli.SessionManager.token", return_value="t"), \
             patch("timetrace.cloud.CloudClient.request", new=request), \
             patch("timetrace.cli.Agent.run_once", return_value="idle"), \
             patch("timetrace.cli.shutil.which", return_value="/test/claude"):
            code, _, err = self.run_cli("agent", "run", "--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(payloads[0]["tools"][0]["plan_tier"], "max")

    def test_cloud_login_reports_inventory_and_quota_right_after_pairing(self):
        class Reading:
            def capabilities(self):
                return {"can_read_quota": True, "can_enforce_zero_spend": True}

            def read_limits(self):
                from timetrace.models import Sample
                return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=10.0, reset_at=None, window_mins=300)]

            def plan_tier(self):
                return "plus"

        calls = []

        def request(client, method, path, body=None, token=None):
            calls.append((method, path, token))
            if path == "/device-authorizations":
                return {"user_code": "ABCD1234", "device_code": "dev", "expires_in": 600, "interval": 1}
            if path == "/device-authorizations/token":
                return {"status": "approved", "activation_code": "act"}
            if path == "/device-authorizations/activate":
                return {"access_token": "fresh-token", "refresh_token": "r", "expires_in": 900, "runner": {"id": "r1", "name": "Mac"}}
            return {}

        saved = {}
        with patch("timetrace.cli._adapters", return_value={"codex": Reading()}), \
             patch("timetrace.cli.CredentialStore.save", new=lambda self, creds: saved.update(creds)), \
             patch("timetrace.cloud.CloudClient.request", new=request), \
             patch("timetrace.cli.shutil.which", return_value="/test/codex"), \
             patch("timetrace.cli.time.sleep", return_value=None):
            code, out, err = self.run_cli("cloud", "login")
        self.assertEqual(code, 0, err)
        paths = [c[1] for c in calls]
        self.assertIn("/runner/inventory", paths)
        self.assertIn("/runner/quota/samples", paths)
        self.assertTrue(all(c[2] == "fresh-token" for c in calls if c[1].startswith("/runner/")))
        self.assertIn("已上报", out)

    def test_cloud_login_prints_a_scannable_pair_qr_and_the_fallback_code(self):
        from tests.test_qr import decode

        def request(client, method, path, body=None, token=None):
            if path == "/device-authorizations":
                return {"user_code": "ABCD1234", "device_code": "secret-device-code", "expires_in": 600, "interval": 1}
            return {"status": "expired"}

        with patch("timetrace.cloud.CloudClient.request", new=request), \
             patch("timetrace.cli.platform.node", return_value="Alex 的 Mac"), \
             patch("timetrace.cli.time.sleep", return_value=None), \
             patch("timetrace.cli.time.time", return_value=1790000000):
            code, out, _ = self.run_cli("cloud", "login")
        self.assertEqual(code, 1)  # every code expires in this fake
        self.assertIn("ABCD1234", out)
        self.assertNotIn("secret-device-code", out)
        # Every expired round prints a fresh QR; decode the first block only.
        qr_lines = []
        for line in out.splitlines():
            if line and set(line) <= set("█▀▄ "):
                qr_lines.append(line)
            elif qr_lines:
                break
        self.assertIn("上一个二维码已过期，已生成新的二维码", out)
        self.assertGreater(len(qr_lines), 10)
        # Unpack the half blocks (light drawn, dark blank) and strip the border.
        rows = []
        for line in qr_lines:
            rows.append([ch in " ▄" for ch in line])
            rows.append([ch in " ▀" for ch in line])
        while rows and not any(rows[-1]):
            rows.pop()
        while rows and not any(rows[0]):
            rows.pop(0)
        left = min(row.index(True) for row in rows if any(row))
        size = len(rows)
        matrix = [row[left:left + size] for row in rows]
        text, _, _ = decode(matrix)
        self.assertEqual(text, "timetrace://pair?code=ABCD1234&name=Alex%20%E7%9A%84%20Mac&exp=1790000600&platform=darwin&v=1")

    def test_expired_code_is_replaced_automatically_until_approved(self):
        issued = []

        def request(client, method, path, body=None, token=None):
            if path == "/device-authorizations":
                code = "CODE%04d" % (len(issued) + 1)
                issued.append(code)
                return {"user_code": code, "device_code": "dev-%d" % len(issued), "expires_in": 120, "interval": 1}
            if path == "/device-authorizations/token":
                return {"status": "approved", "activation_code": "act"} if body["device_code"] == "dev-2" else {"status": "expired"}
            if path == "/device-authorizations/activate":
                return {"access_token": "t", "refresh_token": "r", "expires_in": 900, "runner": {"id": "r1", "name": "Mac"}}
            return {}

        with patch("timetrace.cloud.CloudClient.request", new=request), \
             patch("timetrace.cli.CredentialStore.save", new=lambda self, creds: None), \
             patch("timetrace.cli._adapters", return_value={}), \
             patch("timetrace.cli.time.sleep", return_value=None):
            code, out, _ = self.run_cli("cloud", "login")
        self.assertEqual(code, 0)
        self.assertEqual(issued, ["CODE0001", "CODE0002"])
        self.assertIn("已过期，已生成新的二维码", out)
        self.assertIn("2 分钟内有效", out)

    def test_pair_link_drops_the_name_when_it_would_not_fit_the_qr(self):
        link = cli._pair_link("ABCD1234", "很长的名字" * 20)
        self.assertEqual(link, "timetrace://pair?code=ABCD1234&platform=darwin&v=1")
        from timetrace import qr
        qr.encode(link)

    def test_launcher_works_through_a_symlink_from_any_directory(self):
        # README installs `bin/timetrace` with `ln -s` into a PATH directory; the
        # launcher must resolve that link back to the checkout.
        launcher = Path(__file__).resolve().parents[1] / "bin" / "timetrace"
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "timetrace"
            link.symlink_to(launcher)
            result = subprocess.run([str(link), "--version"], cwd=d, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("timetrace "), result.stdout)

    def test_doctor_shows_plan_tier_and_live_quota(self):
        from timetrace.models import Sample

        class Rich:
            def capabilities(self):
                return {"can_read_quota": True, "can_enforce_zero_spend": True}

            def capability_details(self):
                return {"can_enforce_zero_spend": True, "auth_method": "chatgpt", "verified_at": 1000}

            def plan_tier(self):
                return "prolite"

            def read_limits(self):
                return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=11.0, reset_at=4102444800, window_mins=10080)]

        with patch("timetrace.cli._adapters", return_value={"codex": Rich()}), \
             patch("timetrace.cli.shutil.which", return_value="/test/codex"), \
             patch("timetrace.cli.CredentialStore.load", return_value=None):
            code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("套餐: prolite", out)
        self.assertIn("本周 剩余 89%", out)

    def test_doctor_labels_claude_windows_in_chinese(self):
        from timetrace.models import Sample

        class ClaudeLike:
            def capabilities(self):
                return {"can_read_quota": True, "can_enforce_zero_spend": True}

            def capability_details(self):
                return {"can_enforce_zero_spend": True, "auth_method": "claude.ai", "verified_at": 1000}

            def plan_tier(self):
                return "team"

            def read_limits(self):
                return [Sample(bucket_key="claude:five_hour", tool="claude", used_pct=51.0, reset_at=None, window_mins=300),
                        Sample(bucket_key="claude:seven_day", tool="claude", used_pct=73.0, reset_at=None, window_mins=10080)]

        with patch("timetrace.cli._adapters", return_value={"claude": ClaudeLike()}), \
             patch("timetrace.cli.shutil.which", return_value="/test/claude"), \
             patch("timetrace.cli.CredentialStore.load", return_value=None):
            code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("短时 剩余 49%", out)
        self.assertIn("本周 剩余 27%", out)

    def test_cloud_logout_revokes_on_the_server_before_forgetting(self):
        calls, deleted = [], []

        def request(client, method, path, body=None, token=None):
            calls.append((method, path, token))
            return {}

        with patch("timetrace.cli.CredentialStore.load", return_value={"refresh_token": "r", "runner": {"id": "r1"}}), \
             patch("timetrace.cli.CredentialStore.delete", new=lambda self: deleted.append(True)), \
             patch("timetrace.cli.SessionManager.token", return_value="live-token"), \
             patch("timetrace.cloud.CloudClient.request", new=request):
            code, out, err = self.run_cli("cloud", "logout")
        self.assertEqual(code, 0, err)
        self.assertIn(("POST", "/runner/revoke", "live-token"), calls)
        self.assertEqual(deleted, [True])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.repo = root / "repo"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "a").write_text("a")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self._old = os.environ.get("TIMETRACE_HOME")
        os.environ["TIMETRACE_HOME"] = str(self.home)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TIMETRACE_HOME", None)
        else:
            os.environ["TIMETRACE_HOME"] = self._old
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_agent_run_passes_the_output_tail_switch_to_the_agent(self):
        for stored, expected in ((None, True), (False, False)):
            with self.subTest(stored=stored):
                self.home.mkdir(parents=True, exist_ok=True)
                doc = {} if stored is None else {"upload_output_tail": stored}
                (self.home / "config.json").write_text(json.dumps(doc))
                seen = {}

                class FakeAgent:
                    def __init__(self, *args, **kwargs):
                        seen.update(kwargs)

                    def maintain(self, force=False):
                        pass

                    def run_once(self):
                        return "idle"

                with patch("timetrace.cli.Agent", FakeAgent), patch("timetrace.cli._adapters", return_value={}):
                    code, _, err = self.run_cli("agent", "run", "--once")
                self.assertEqual(code, 0, err)
                self.assertIs(seen.get("upload_output_tail"), expected)

    def test_launch_agent_plist_is_valid_even_with_xml_characters_in_paths(self):
        import plistlib
        calls = []
        user_home = Path(self.tmp.name) / "R&D <home>"
        destination = cli.install_launch_agent(user_home, run=lambda cmd, **kw: calls.append(cmd))
        doc = plistlib.loads(destination.read_bytes())
        self.assertEqual(doc["Label"], "com.atlaspaces.timetrace.agent")
        self.assertEqual(doc["ProgramArguments"][1:], ["agent", "run"])
        self.assertTrue(doc["ProgramArguments"][0].endswith("/bin/timetrace"))
        self.assertEqual(doc["EnvironmentVariables"]["TIMETRACE_HOME"], str(user_home / ".timetrace"))
        self.assertEqual(destination, user_home / "Library" / "LaunchAgents" / "com.atlaspaces.timetrace.agent.plist")
        self.assertEqual([c[:2] for c in calls], [["launchctl", "unload"], ["launchctl", "load"]])

    def test_launch_agent_runs_the_installed_module_when_there_is_no_checkout_launcher(self):
        # pipx / pip installs have no bin/timetrace next to the package.
        import plistlib
        with patch("timetrace.cli._LAUNCHER", Path(self.tmp.name) / "missing" / "timetrace"):
            destination = cli.install_launch_agent(Path(self.tmp.name) / "u", run=lambda cmd, **kw: None)
        doc = plistlib.loads(destination.read_bytes())
        self.assertEqual(doc["ProgramArguments"], [sys.executable, "-m", "timetrace", "agent", "run"])

    def test_launch_agent_uses_the_stable_launcher_a_package_manager_announces(self):
        # Homebrew's wrapper points at its opt/ path, which survives `brew upgrade`;
        # the resolved package path is a versioned Cellar directory that does not.
        import plistlib
        stable = Path(self.tmp.name) / "opt" / "bin" / "timetrace"
        stable.parent.mkdir(parents=True)
        stable.write_text("#!/bin/sh\n")
        stable.chmod(0o755)
        with patch.dict(os.environ, {"TIMETRACE_LAUNCHER": str(stable)}):
            destination = cli.install_launch_agent(Path(self.tmp.name) / "u", run=lambda cmd, **kw: None)
        doc = plistlib.loads(destination.read_bytes())
        self.assertEqual(doc["ProgramArguments"], [str(stable), "agent", "run"])

    def test_a_missing_announced_launcher_is_ignored(self):
        with patch.dict(os.environ, {"TIMETRACE_LAUNCHER": str(Path(self.tmp.name) / "gone")}), \
                patch("timetrace.cli._LAUNCHER", Path(self.tmp.name) / "missing" / "timetrace"):
            self.assertEqual(cli.timetrace_command(), [sys.executable, "-m", "timetrace"])

    def test_add_then_ls(self):
        code, out, _ = self.run_cli("add", "fix things", "--repo", str(self.repo), "--tool", "claude",
                                    "--priority", "2")
        self.assertEqual(code, 0)
        self.assertIn("task 1 added (runnable)", out)
        code, out, _ = self.run_cli("add", "then test", "--repo", str(self.repo), "--after", "1",
                                    "--any-tool")
        self.assertIn("task 2 added (pending)", out)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertIn("fix things", out)
        self.assertIn("after #1", out)
        code, out, _ = self.run_cli("ls", "--json")
        tasks = json.loads(out)
        self.assertEqual(tasks[0]["priority"], 2)
        self.assertEqual(tasks[1]["any_tool"], 1)
        self.assertEqual(tasks[1]["repo"], os.path.abspath(str(self.repo)))

    def test_doctor_diagnoses_persistent_lock_without_clearing_it(self):
        path = self.home / "locks" / "coding-slot.lock"
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        with patch("timetrace.cli.CredentialStore") as credentials:
            credentials.return_value.load.return_value = None
            code, out, err = self.run_cli("agent", "doctor")
        self.assertIn("manual", out)
        self.assertIn(str(path), out)
        self.assertIn("descendants", out)
        self.assertEqual(path.read_text(), "1234")

    def test_add_rejects_non_repo_and_missing_dependency(self):
        code, _, err = self.run_cli("add", "x", "--repo", self.tmp.name)
        self.assertEqual(code, 2)
        self.assertIn("git repository", err)
        code, _, err = self.run_cli("add", "x", "--repo", str(self.repo), "--after", "99")
        self.assertEqual(code, 2)
        self.assertIn("no such task", err)

    def test_add_respects_allowed_repos(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"allowed_repos": ["/nowhere"]}))
        code, _, err = self.run_cli("add", "x", "--repo", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("allowed_repos", err)
        (self.home / "config.json").write_text(json.dumps({"allowed_repos": [self.tmp.name]}))
        code, out, _ = self.run_cli("add", "x", "--repo", str(self.repo))
        self.assertEqual(code, 0)

    def test_retry_rm_and_events(self):
        self.run_cli("add", "x", "--repo", str(self.repo))
        code, _, err = self.run_cli("retry", "1")
        self.assertEqual(code, 1)  # runnable tasks cannot be retried
        code, out, _ = self.run_cli("rm", "1")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("ls")
        self.assertIn("no tasks", out)
        code, out, _ = self.run_cli("events")
        self.assertEqual(code, 0)
        self.assertIn("no events", out)
        code, _, err = self.run_cli("logs", "1")
        self.assertEqual(code, 1)

    def test_status_with_no_samples_and_broken_codex(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"codex": {"bin": "/nonexistent/codex"}}))
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("no samples yet", out)
        self.assertIn("live Codex quota read failed", err)
        code, out, _ = self.run_cli("events", "--type", "sample_failure")
        self.assertIn("sample_failure", out)

    def test_statusline_ingests_interactive_rate_limits(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"codex": {"bin": "/nonexistent/codex"}}))
        doc = {"session_id": "s", "model": {"id": "claude-haiku-4-5"}, "version": "2.1.258",
               "rate_limits": {"five_hour": {"used_percentage": 14.0, "resets_at": 1788370200},
                               "seven_day": {"used_percentage": 3, "resets_at": 1788552000}}}
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(doc))
        try:
            code, out, _ = self.run_cli("statusline")
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        self.assertIn("5h 86%", out)
        self.assertIn("7d 97%", out)
        code, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        by_key = {b["bucket_key"]: b for b in data["buckets"]}
        self.assertEqual(by_key["claude:five_hour"]["used_pct"], 14.0)
        self.assertEqual(by_key["claude:five_hour"]["source"], "statusline")
        self.assertTrue(by_key["claude:five_hour"]["is_representative"])
        # a statusline payload without rate_limits (before the first response) is harmless
        sys.stdin = io.StringIO(json.dumps({"session_id": "s"}))
        try:
            code, out, _ = self.run_cli("statusline")
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        self.assertIn("timetrace ·", out)

    def test_run_once_with_no_tasks_is_idle(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"codex": {"bin": "/nonexistent/codex"},
                                                          "claude": {"bin": "/nonexistent/claude"}}))
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 0)
        self.assertIn("daemon", out)


if __name__ == "__main__":
    unittest.main()
