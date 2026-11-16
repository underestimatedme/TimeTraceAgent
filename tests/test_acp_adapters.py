"""Gemini CLI and Cursor through the generic ACP client: capabilities, the zero-spend gate, the Gemini billing
verifier and what a run sends. The agent is always tests/fake_acp_agent.py behind a wrapper script; no real Gemini
CLI or Cursor is started and no model is called."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from timetrace import billing
from timetrace.acp_client import AcpResult
from timetrace.adapters import build_adapters
from timetrace.adapters.acp import run_result
from timetrace.adapters.base import SAFETY_RULES
from timetrace.adapters.cursor import CURSOR_BILLING_REASON, CursorAdapter
from timetrace.adapters.gemini import GeminiAdapter
from timetrace.billing import StaticBilling, gemini_env_file, gemini_workspace_problem, verify_gemini
from timetrace.dispatch import adapter_dispatch_problem
from timetrace.mcp_access import McpAccess

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
TOKEN = "ttm_" + "Qa1Ws2Ed3Rf4Tg5Yh6Uj7Ik8"
PERSONAL = {"security": {"auth": {"selectedType": "oauth-personal"}}}


def wrapper(directory: Path) -> str:
    """An executable standing in for `gemini` / `cursor-agent`: it runs the fake agent with whatever arguments the
    adapter passes, so the launch command is recorded exactly."""
    path = directory / "agent-bin"
    path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()
        self.home = self.d / "home"
        self.home.mkdir()
        self.work = self.d / "worktree"
        self.work.mkdir()
        self.bin = wrapper(self.d)
        self.transcript = self.d / "transcript.jsonl"
        self.log = str(self.d / "run.log")
        # Path.home() reads HOME: the .env search and settings lookups never see the developer's own files.
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def scenario(self, scenario):
        path = self.d / "scenario.json"
        path.write_text(json.dumps(scenario))
        return {"TT_ACP_SCENARIO": str(path), "TT_ACP_TRANSCRIPT": str(self.transcript)}

    def records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def received(self, method):
        return [r["received"] for r in self.records() if r.get("received", {}).get("method") == method]


class CapabilityTest(AdapterCase):
    def test_dispatch_and_resume_follow_the_binary_and_zero_spend_follows_billing_only(self):
        gemini = GeminiAdapter({"bin": self.bin}, billing=StaticBilling(True))
        caps = gemini.capabilities()
        self.assertTrue(caps["can_dispatch"] and caps["can_resume"] and caps["can_enforce_zero_spend"])
        self.assertFalse(caps["can_read_quota"] or caps["can_interact"])
        self.assertEqual(adapter_dispatch_problem(gemini, resuming=True), "")
        unverified = GeminiAdapter({"bin": self.bin}, billing=StaticBilling(False, "settings_missing"))
        self.assertTrue(unverified.capabilities()["can_dispatch"], "the implemented surface is declared honestly")
        self.assertEqual(adapter_dispatch_problem(unverified), "billing_unverified")
        missing = GeminiAdapter({"bin": str(self.d / "no-gemini")}, billing=StaticBilling(True))
        self.assertFalse(missing.capabilities()["can_dispatch"])
        self.assertEqual(adapter_dispatch_problem(missing), "dispatch_unavailable")
        on_path = GeminiAdapter({}, billing=StaticBilling(True), which=lambda name: "/opt/x/" + name)
        self.assertTrue(on_path.capabilities()["can_dispatch"])
        self.assertEqual(GeminiAdapter({}, which=lambda name: None).capabilities()["can_dispatch"], False)

    def test_cursor_is_never_dispatched_whatever_its_binary_or_config(self):
        cursor = CursorAdapter({"bin": self.bin, "zero_spend": True, "billing": "verified"})
        caps = cursor.capabilities()
        self.assertTrue(caps["can_dispatch"])
        self.assertFalse(caps["can_enforce_zero_spend"])
        self.assertEqual(adapter_dispatch_problem(cursor), "billing_unverified")
        self.assertEqual(adapter_dispatch_problem(cursor, resuming=True), "billing_unverified")
        self.assertEqual(cursor.capability_details()["unsupported_reason"], CURSOR_BILLING_REASON)
        with self.assertRaises(TypeError):
            CursorAdapter({"bin": self.bin}, billing=StaticBilling(True))  # not even code can hand it a verdict
        from timetrace.agent import _capability_zero_spend
        self.assertFalse(_capability_zero_spend(cursor, {}))

    def test_build_adapters_registers_configured_acp_agents_with_their_real_verifiers(self):
        adapters = build_adapters({"claude": {}, "codex": {}, "gemini": {"bin": self.bin}, "cursor": {"bin": "x"}})
        self.assertIsInstance(adapters["gemini"].billing, billing.BillingVerifier)
        self.assertEqual(adapters["cursor"].binary(), "x")
        self.assertEqual(CursorAdapter({}).command(), ["cursor-agent", "acp"])
        self.assertEqual(GeminiAdapter({"extra_args": ["--debug"]}).command(), ["gemini", "--acp", "--skip-trust", "--debug"])
        for name in ("gemini", "cursor"):
            self.assertTrue(adapters[name].interactive_runs and adapters[name].supports_mcp)


class GeminiRunTest(AdapterCase):
    def run_gemini(self, scenario, resume=None, **kwargs):
        adapter = GeminiAdapter({"bin": self.bin}, billing=StaticBilling(True))
        with mock.patch.dict(os.environ, self.scenario(scenario)):
            if resume:
                return adapter.resume("next step", str(self.work), resume, self.log, **kwargs)
            return adapter.start("fix it", str(self.work), "timetrace-drawn-id", self.log, **kwargs)

    def test_start_runs_one_acp_turn_with_the_rules_first(self):
        res = self.run_gemini({"turn": [{"chunk": "Working.\n"},
                                        {"update": {"sessionUpdate": "tool_call", "toolCallId": "t", "kind": "edit",
                                                    "title": "Edit a.py"}},
                                        {"chunk": "Committed the fix."}]})
        self.assertTrue(res.ok)
        self.assertEqual((res.exit_code, res.session_id, res.output), (0, "fake-session-1", "Committed the fix."))
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["--acp", "--skip-trust"])
        text = self.received("session/prompt")[0]["params"]["prompt"][0]["text"]
        self.assertEqual(text, SAFETY_RULES + "\nfix it")
        self.assertNotIn("timetrace-drawn-id", self.transcript.read_text(), "the agent names its own sessions")

    def test_resume_sends_the_agents_session_back(self):
        res = self.run_gemini({"initialize": {"loadSession": True}, "turn": [{"chunk": "again"}]}, resume="g-7")
        self.assertTrue(res.ok)
        self.assertEqual(self.received("session/load")[0]["params"]["sessionId"], "g-7")
        self.assertEqual(res.session_id, "g-7")

    def test_billing_variables_are_stripped_from_the_child(self):
        leak = {name: "x" for name in billing.billing_env_keys("gemini")}
        with mock.patch.dict(os.environ, leak):
            self.run_gemini({"env_probe": sorted(leak), "turn": []})
        env = next(r["env"] for r in self.records() if "env" in r)
        self.assertEqual({k: v for k, v in env.items() if v is not None}, {})

    def test_a_usage_limit_on_the_prompt_is_blocked_and_keeps_the_session(self):
        res = self.run_gemini({"turn": [{"error": {"code": 429, "message": "Quota exceeded: RESOURCE_EXHAUSTED"}}]})
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.session_id, "fake-session-1")

    def test_the_mcp_token_is_masked_everywhere_the_run_writes(self):
        access = McpAccess(url="https://v.example.test/timetrace/mcp", token=TOKEN, expires_at=time.time() + 600)
        res = self.run_gemini({"initialize": {"mcpHttp": True}, "turn": [{"chunk": "env says " + TOKEN}]}, mcp=access)
        self.assertTrue(res.ok)
        self.assertEqual(self.received("session/new")[0]["params"]["mcpServers"][0]["headers"],
                         [{"name": "Authorization", "value": "Bearer " + TOKEN}])
        self.assertNotIn(TOKEN, Path(self.log).read_text())
        self.assertNotIn(TOKEN, res.output)
        self.assertNotIn(TOKEN, json.dumps(next(r["argv"] for r in self.records() if "argv" in r)))

    def test_a_worktree_that_would_load_an_api_key_never_starts_the_agent(self):
        (self.work / ".env").write_text("GEMINI_API_KEY=AIza-something-real\n")
        res = self.run_gemini({"turn": [{"chunk": "x"}]})
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "billing_unverified: api_key_fallback_in_env_file:GEMINI_API_KEY")
        self.assertEqual(self.records(), [], "nothing was spawned")
        self.assertIn("not started: billing_unverified", Path(self.log).read_text())

    def test_run_result_only_counts_an_end_turn_as_success(self):
        self.assertTrue(run_result(AcpResult(stop_reason="end_turn", exit_code=-15), None).ok)
        for stop in ("max_tokens", "max_turn_requests", "refusal", "cancelled", ""):
            res = run_result(AcpResult(stop_reason=stop, exit_code=0, session_id="s"), None)
            self.assertFalse(res.ok)
            self.assertEqual(res.exit_code, 1)
            self.assertIn("the agent ended the turn", res.error)
        early = run_result(AcpResult(error="session/new failed (429): quota", failed_method="session/new"), "s")
        self.assertFalse(early.blocked, "no session to resume yet: an ordinary failure")


class InventoryAndDoctorTest(AdapterCase):
    def test_inventory_reports_each_agent_by_its_own_binary(self):
        from timetrace.cli import _runner_tools
        adapters = {"cursor": CursorAdapter({}, which=lambda name: None),
                    "gemini": GeminiAdapter({"bin": self.bin}, billing=StaticBilling(True))}
        with mock.patch("timetrace.cli.shutil.which", side_effect=lambda b: b if b == self.bin else None) as which:
            tools = {t["provider"]: t for t in _runner_tools({}, adapters)}
        self.assertIn(mock.call("cursor-agent"), which.call_args_list, "not `cursor`, which is the editor")
        self.assertEqual((tools["gemini"]["status"], tools["gemini"]["can_enforce_zero_spend"]), ("available", True))
        self.assertEqual((tools["cursor"]["status"], tools["cursor"]["can_enforce_zero_spend"]), ("unavailable", False))
        self.assertFalse(tools["gemini"]["can_interact"])

    def test_doctor_explains_why_an_agent_is_not_dispatched(self):
        from timetrace.cli import _acp_tool_lines, _tool_billing_details
        cursor = CursorAdapter({"bin": self.bin})
        lines = _acp_tool_lines("cursor", cursor, _tool_billing_details("cursor", {"cursor": cursor}))
        self.assertIn("ACP：%s acp" % self.bin, lines[0])
        self.assertTrue(any("零付费核验: 未通过" in line and CURSOR_BILLING_REASON in line for line in lines))
        gemini = GeminiAdapter({"bin": str(self.d / "nope")}, billing=StaticBilling(False, "settings_missing"))
        lines = _acp_tool_lines("gemini", gemini, _tool_billing_details("gemini", {"gemini": gemini}))
        self.assertIn("未找到", lines[0])
        self.assertTrue(any("未登录或登录已失效" in line for line in lines))
        self.assertTrue(any("Login with Google" in line for line in lines))


class CursorRunTest(AdapterCase):
    def test_the_run_path_works_but_the_gate_never_opens(self):
        adapter = CursorAdapter({"bin": self.bin})
        with mock.patch.dict(os.environ, self.scenario({"turn": [{"chunk": "hi"}]})):
            res = adapter.start("p", str(self.work), "s", self.log)
        self.assertTrue(res.ok)
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["acp"])
        self.assertEqual(adapter_dispatch_problem(adapter), "billing_unverified")


class GeminiVerifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        (self.home / ".gemini").mkdir()
        self.no_system = str(self.home / "no-system-settings.json")

    def settings(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        (self.home / ".gemini" / "settings.json").write_text(text)

    def verdict(self, env=None, system=None):
        return verify_gemini({}, env=env or {}, home=str(self.home), system_settings_path=system or self.no_system, now=1.0)

    def assertReason(self, reason, **kwargs):
        verdict = self.verdict(**kwargs)
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.reason, reason)

    def test_personal_google_login_in_either_layout_is_verified(self):
        for layout in (PERSONAL, {"selectedAuthType": "oauth-personal"},
                       '{\n  // written by hand\n  "security": {"auth": {/* login */ "selectedType": "oauth-personal"}},\n'
                       '  "ui": {"theme": "http://not-a-comment"}\n}'):
            with self.subTest(layout=layout):
                self.settings(layout)
                verdict = self.verdict()
                self.assertTrue(verdict.verified, verdict.reason)
                self.assertEqual((verdict.provider, verdict.auth_method), ("gemini", "oauth-personal"))

    def test_any_billing_variable_in_the_environment_is_unverified(self):
        self.settings(PERSONAL)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
                     "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
            with self.subTest(name=name):
                self.assertReason("api_key_fallback_in_env:" + name, env={name: "1"})

    def test_a_key_in_the_home_env_file_is_unverified(self):
        self.settings(PERSONAL)
        env_file = self.home / ".gemini" / ".env"
        for text, reason in (("GEMINI_API_KEY=abc\n", "api_key_fallback_in_env_file:GEMINI_API_KEY"),
                             ('export GOOGLE_API_KEY="abc" # mine\n', "api_key_fallback_in_env_file:GOOGLE_API_KEY"),
                             ("GOOGLE_CLOUD_PROJECT: my-project\n", "api_key_fallback_in_env_file:GOOGLE_CLOUD_PROJECT")):
            with self.subTest(text=text):
                env_file.write_text(text)
                self.assertReason(reason)
        env_file.write_text("# GEMINI_API_KEY=commented out\nGEMINI_API_KEY=\nDEBUG=1\n")
        self.assertTrue(self.verdict().verified, "an empty or commented-out key is not a billing path")

    def test_home_dotenv_is_the_fallback_only_without_the_gemini_one(self):
        self.settings(PERSONAL)
        (self.home / ".env").write_text("GEMINI_API_KEY=abc\n")
        self.assertReason("api_key_fallback_in_env_file:GEMINI_API_KEY")
        (self.home / ".gemini" / ".env").write_text("DEBUG=1\n")
        self.assertTrue(self.verdict().verified, "Gemini CLI loads ~/.gemini/.env and stops there")

    def test_every_other_login_type_is_unverified(self):
        for selected in ("gemini-api-key", "vertex-ai", "cloud-shell", "compute-default-credentials"):
            with self.subTest(selected=selected):
                self.settings({"security": {"auth": {"selectedType": selected}}})
                self.assertReason("auth_method_not_subscription:" + selected)
        self.settings({"selectedAuthType": "vertex-ai"})
        self.assertReason("auth_method_not_subscription:vertex-ai")
        self.settings({"security": {"auth": {"selectedType": "oauth-personal", "enforcedType": "vertex-ai"}}})
        self.assertReason("auth_type_enforced:vertex-ai")
        self.settings({"security": {"auth": {"selectedType": "oauth-personal", "useExternal": True}}})
        self.assertReason("external_auth_configured")

    def test_missing_unreadable_or_unset_settings_are_unverified(self):
        self.assertReason("settings_missing")
        self.settings("{not json")
        self.assertReason("settings_unreadable")
        self.settings(["not", "an", "object"])
        self.assertReason("settings_unreadable")
        self.settings({"ui": {"theme": "dark"}})
        self.assertReason("auth_type_unset")

    def test_system_settings_override_the_users(self):
        self.settings(PERSONAL)
        system = self.home / "system.json"
        system.write_text(json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}))
        self.assertReason("system_settings_override:gemini-api-key", system=str(system))
        system.write_text(json.dumps({"security": {"auth": {"enforcedType": "oauth-personal"}}}))
        self.assertTrue(self.verdict(system=str(system)).verified)
        admin = self.home / "admin.json"   # the path Gemini CLI takes from this variable instead of its default
        admin.write_text(json.dumps({"security": {"auth": {"enforcedType": "vertex-ai"}}}))
        verdict = verify_gemini({}, env={"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(admin)}, home=str(self.home), now=1.0)
        self.assertEqual(verdict.reason, "system_settings_override:vertex-ai")

    def test_the_verifier_is_cached_and_reads_only_files(self):
        with mock.patch.object(billing, "_run_status", side_effect=AssertionError("no command is ever run")):
            verifier = billing.gemini_verifier({})
            verifier.verdict()
            self.assertIsInstance(verifier, billing.BillingVerifier)


class GeminiWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.parent = root / "repo"
        self.work = self.parent / "worktree"
        self.work.mkdir(parents=True)
        self.home.mkdir()

    def test_the_env_file_gemini_would_load_is_the_nearest_one(self):
        (self.parent / ".env").write_text("A=1\n")
        self.assertEqual(gemini_env_file(str(self.work), str(self.home)), self.parent / ".env")
        (self.work / ".gemini").mkdir()
        (self.work / ".gemini" / ".env").write_text("A=1\n")
        (self.work / ".env").write_text("A=1\n")
        self.assertEqual(gemini_env_file(str(self.work), str(self.home)), self.work / ".gemini" / ".env")

    def test_a_project_env_file_or_workspace_settings_block_the_run(self):
        self.assertEqual(gemini_workspace_problem(str(self.work), str(self.home)), "")
        (self.parent / ".env").write_text("GOOGLE_CLOUD_PROJECT=corp-project\n")
        self.assertEqual(gemini_workspace_problem(str(self.work), str(self.home)),
                         "api_key_fallback_in_env_file:GOOGLE_CLOUD_PROJECT")
        (self.parent / ".env").write_text("PORT=8080\n")
        (self.work / ".gemini").mkdir()
        settings = self.work / ".gemini" / "settings.json"
        settings.write_text(json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}))
        self.assertEqual(gemini_workspace_problem(str(self.work), str(self.home)),
                         "workspace_settings_override:gemini-api-key")
        settings.write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}, "ui": {}}))
        self.assertEqual(gemini_workspace_problem(str(self.work), str(self.home)), "")
        settings.write_text("{broken")
        self.assertEqual(gemini_workspace_problem(str(self.work), str(self.home)), "workspace_settings_unreadable")

    def test_gemini_billing_keys_are_stripped_and_listed(self):
        keys = billing.billing_env_keys("gemini")
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
                     "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
            self.assertIn(name, keys)
        env = {"PATH": "/bin", "GEMINI_API_KEY": "k", "GOOGLE_CLOUD_PROJECT": "p", "OPENAI_API_KEY": "o"}
        self.assertEqual(set(billing.sanitized_env("gemini", env)), {"PATH", "OPENAI_API_KEY"})
        self.assertIn("GEMINI_API_KEY", billing.billing_env_keys(""), "every known key, for check commands")


if __name__ == "__main__":
    unittest.main()
