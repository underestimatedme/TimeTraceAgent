"""L2: reset credits (read only), self-check, sleep prevention, local pause."""
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import health, pause
from timetrace.adapters import codex
from timetrace.agent import PROTOCOL_VERSION, Agent
from timetrace import __version__
from timetrace.billing import BillingVerdict, StaticBilling
from timetrace.db import Database
from timetrace.sleepguard import SleepGuard
from tests.test_parallel import QueueCloud, init_repo

FAKE_APP_SERVER = str(Path(__file__).with_name("fake_codex_app_server.py"))


class ResetCreditsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.auth = self.d / "auth.json"
        self.auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acct-1"}}))
        self.transcript = self.d / "methods.jsonl"
        self.adapter = codex.CodexAdapter({"bin": FAKE_APP_SERVER, "auth_path": str(self.auth)},
                                          billing=StaticBilling(True))

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, mode, timeout=20):
        env = {"TT_FAKE_MODE": mode, "TT_FAKE_TRANSCRIPT": str(self.transcript)}
        with patch.dict(os.environ, env):
            return self.adapter.reset_credits_entry(timeout=timeout)

    def methods(self):
        return [json.loads(l)["method"] for l in self.transcript.read_text().splitlines()]

    def test_reads_reset_credits_through_a_short_lived_app_server(self):
        entry = self.read("ok")
        key = self.adapter.account_key()
        self.assertEqual(entry["pool_id"], "pool-codex-" + key)
        self.assertEqual(entry["tool_profile_id"], "codex-default")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["available_count"], 2)
        self.assertTrue(entry["read_at"].endswith("Z"))
        first, second = entry["credits"]
        self.assertEqual(first, {"id": "cred-1", "reset_type": "codexRateLimits", "status": "available",
                                 "granted_at": "2026-08-29T10:40:00Z", "expires_at": "2026-09-10T00:26:40Z",
                                 "description": "Welcome reset"})
        self.assertEqual(second["status"], "unknown")      # unknown status values are "unknown"
        self.assertNotIn("expires_at", second)
        self.assertEqual(self.methods(), ["initialize", "initialized", "account/rateLimits/read"])

    def test_any_failure_is_unknown_never_zero(self):
        for mode in ("error", "no_credits"):
            with self.subTest(mode=mode):
                entry = self.read(mode)
                self.assertEqual(entry["status"], "unknown")
                self.assertNotIn("available_count", entry)
        started = time.monotonic()
        entry = self.read("silent", timeout=0.5)
        self.assertEqual(entry["status"], "unknown")
        self.assertLess(time.monotonic() - started, 10)

    def test_the_consume_method_is_never_sent(self):
        self.read("ok")
        self.read("error")
        self.assertNotIn("account/rateLimitResetCredit/consume", self.transcript.read_text())
        with self.assertRaises(ValueError):
            codex.app_server_request(FAKE_APP_SERVER, "account/rateLimitResetCredit/consume",
                                     {"idempotencyKey": "x"})
        self.assertNotIn("consume", self.transcript.read_text())
        # Nothing in the package source builds that request either.
        root = Path(codex.__file__).resolve().parents[1]
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "rateLimitResetCredit/consume" in text:
                self.assertIn("FORBIDDEN_METHODS", text, path)

    def test_no_codex_login_reports_nothing(self):
        self.auth.unlink()
        self.assertIsNone(self.read("ok"))


class Verdict:
    def __init__(self, verified, reason=""):
        self.v = BillingVerdict("x", verified, reason, "", 0.0)

    def verdict(self, force=False):
        return self.v


class HealthTest(unittest.TestCase):
    def test_login_states(self):
        ok = BillingVerdict("c", True, "", "", 0)
        self.assertEqual(health.login_state(ok, True), "ok")
        gone = BillingVerdict("c", False, "not_logged_in", "", 0)
        self.assertEqual(health.login_state(gone, True), "expired")
        self.assertEqual(health.login_state(gone, False), "missing")
        self.assertEqual(health.login_state(BillingVerdict("c", False, "auth_status_unavailable:FileNotFoundError", "", 0), True),
                         "missing")

    def test_check_matches_the_protocol_shape(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            repo = d / "repo"; init_repo(repo)
            (repo / "tracked").write_text("x")
            subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
            workspaces = [{"id": "ws1", "path": str(repo), "kind": "git"},
                          {"id": "ws2", "path": str(d / "gone"), "kind": "git"},
                          {"id": "ws3", "path": str(d), "kind": "folder"}]

            class Claude:
                billing = Verdict(True)
                def credentials_path(self):
                    return d / "creds.json"

            class Codex:
                billing = Verdict(False, "not_logged_in")
                def _auth_path(self):
                    return d / "auth.json"

            report = health.check({"claude": Claude(), "codex": Codex()}, workspaces, d,
                                  usage=lambda p: type("U", (), {"free": 42_500_000_000})())
        self.assertEqual(report["claude_login"], "ok")
        self.assertEqual(report["codex_login"], "missing")
        self.assertEqual(report["disk_free_gb"], 42.5)
        self.assertEqual(report["workspaces"], [{"id": "ws1", "exists": True, "git": True, "clean": False},
                                                {"id": "ws2", "exists": False, "git": False},
                                                {"id": "ws3", "exists": True, "git": False}])
        self.assertRegex(report["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class FakeSleep:
    def __init__(self):
        self.on = False
        self.starts = 0

    def start(self):
        self.on = True
        self.starts += 1

    def stop(self):
        self.on = False

    def active(self):
        return self.on


class InventoryCloud(QueueCloud):
    def __init__(self, jobs=()):
        super().__init__(list(jobs))
        self.inventories = []
        self.claims = 0

    def claim(self, token):
        self.claims += 1
        return super().claim(token)

    def update_inventory(self, token, workspaces, tools, max_parallel=None, max_parallel_per_tool=None, extras=None):
        self.inventories.append(extras)
        return {}


class AgentL2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.home = self.d / "home"
        self.home.mkdir()
        self.checks = 0

    def tearDown(self):
        self.tmp.cleanup()

    def health(self, free=42.0):
        def check():
            self.checks += 1
            return {"claude_login": "ok", "disk_free_gb": free, "workspaces": [], "checked_at": "2026-09-30T12:00:00Z"}
        return check

    def agent(self, cloud, adapter=None, free=42.0, **kw):
        return Agent(self.db, cloud, {"codex": adapter} if adapter else {}, self.home, lambda: "t",
                     inventory=lambda: ([], []), report_protocol=True, health_check=self.health(free),
                     reset_credits=lambda: [{"pool_id": "pool-codex-ab", "status": "unknown"}], **kw)

    def test_inventory_carries_protocol_version_health_pause_and_reset_credits(self):
        cloud = InventoryCloud()
        agent = self.agent(cloud, sleep_guard=FakeSleep())
        agent.maintain(now=1000.0, force=True)
        extras = cloud.inventories[-1]
        self.assertEqual(extras["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(extras["agent_version"], __version__)
        self.assertEqual(__version__, "0.4.0")
        self.assertIs(extras["accepting_local"], True)
        self.assertEqual(extras["health"]["sleep_prevention"], "inactive")
        self.assertEqual(extras["health"]["disk_free_gb"], 42.0)
        self.assertEqual(extras["reset_credits"], [{"pool_id": "pool-codex-ab", "status": "unknown"}])
        # Within 10 minutes: no new self-check; a local pause is pushed at once.
        agent.maintain(now=1100.0)
        self.assertEqual(self.checks, 1)
        self.assertEqual(len(cloud.inventories), 1)
        pause.set_paused(self.home, True)
        agent.maintain(now=1110.0)
        self.assertIs(cloud.inventories[-1]["accepting_local"], False)
        agent.maintain(now=1700.0)
        self.assertEqual(self.checks, 2)

    def test_local_pause_stops_claiming_but_not_upkeep(self):
        cloud = InventoryCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud)
        pause.set_paused(self.home, True)
        self.assertEqual(stat.S_IMODE(os.stat(self.home / pause.FILE_NAME).st_mode), 0o600)
        self.assertEqual(agent.run_once(), "paused")
        self.assertEqual(cloud.claims, 0)
        pause.set_paused(self.home, False)
        self.assertNotEqual(agent.run_once(), "paused")
        self.assertEqual(cloud.claims, 1)

    def test_disk_below_one_gb_stops_claiming(self):
        cloud = InventoryCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, free=0.6)
        agent.maintain(now=1000.0, force=True)
        self.assertEqual(agent.run_once(), "disk_full")
        self.assertEqual(cloud.claims, 0)

    def test_a_failed_job_triggers_a_new_self_check(self):
        from timetrace.models import RunResult

        class Failing:
            def capabilities(self):
                return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                        "can_resume": True, "can_enforce_zero_spend": True}
            def start(self, *a, **k):
                return RunResult(exit_code=1, ok=False, error="boom")
        cloud = InventoryCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, Failing())
        agent.maintain(now=time.time(), force=True)
        self.assertEqual(self.checks, 1)
        self.assertEqual(agent.run_once(), "job j1 → failed")
        self.assertEqual(self.checks, 2)  # the run's own maintain re-checked

    def test_sleep_prevention_runs_only_while_a_job_runs(self):
        from timetrace.models import RunResult
        guard = FakeSleep()
        seen = []

        class Watching:
            def capabilities(self):
                return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                        "can_resume": True, "can_enforce_zero_spend": True}
            def start(self, *a, **k):
                seen.append(guard.active())
                return RunResult(exit_code=0, ok=True, output="ok", session_id="s")
        cloud = InventoryCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, Watching(), sleep_guard=guard)
        self.assertFalse(guard.active())
        agent.run_once()
        self.assertEqual(seen, [True])
        self.assertFalse(guard.active())
        self.assertEqual(guard.starts, 1)


class SleepGuardTest(unittest.TestCase):
    def test_caffeinate_arguments_and_stop(self):
        calls = []

        class Proc:
            def __init__(self, argv, **kw):
                calls.append(argv)
                self.done = False
            def poll(self):
                return 0 if self.done else None
            def terminate(self):
                self.done = True
            def wait(self, timeout=None):
                return 0
        guard = SleepGuard(pid=4242, popen=Proc)
        guard.start()
        guard.start()  # already running: not started twice
        self.assertEqual(calls, [["/usr/bin/caffeinate", "-i", "-w", "4242"]])
        self.assertTrue(guard.active())
        guard.stop()
        self.assertFalse(guard.active())

    def test_missing_caffeinate_is_inactive(self):
        guard = SleepGuard(binary="/nonexistent/caffeinate")
        guard.start()
        self.assertFalse(guard.active())


if __name__ == "__main__":
    unittest.main()
