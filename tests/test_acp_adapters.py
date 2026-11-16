"""Gemini CLI and Cursor through the generic ACP client: capabilities, the zero-spend gate, the hermetic Gemini
billing check and what a run sends. The agent is always tests/fake_acp_agent.py behind a wrapper script; no real
Gemini CLI or Cursor is started and no model is called.

Only names that also exist at commit ec54723 are imported here, so the review regression tests below can be run
against that commit to show they fail there."""
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
from timetrace.billing import StaticBilling, gemini_workspace_problem, verify_gemini
from timetrace.dispatch import adapter_dispatch_problem
from timetrace.mcp_access import McpAccess

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
TOKEN = "ttm_" + "Qa1Ws2Ed3Rf4Tg5Yh6Uj7Ik8"
# The only user settings the gate accepts: personal Google login (current layout), paid AI credits explicitly off.
PERSONAL = {"security": {"auth": {"selectedType": "oauth-personal"}}, "billing": {"overageStrategy": "never"}}
API_KEY_LOGIN = {"security": {"auth": {"selectedType": "gemini-api-key"}}, "billing": {"overageStrategy": "never"}}
ALLOWED = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")


def wrapper(directory: Path) -> str:
    """An executable standing in for `gemini` / `cursor-agent`: it runs the fake agent with whatever arguments the
    adapter passes. The fake's scenario and transcript paths are part of the script, because the Gemini child gets no
    variable of the test's environment."""
    path = directory / "agent-bin"
    path.write_text('#!/bin/sh\nexec /usr/bin/env TT_ACP_SCENARIO="%s" TT_ACP_TRANSCRIPT="%s" "%s" "%s" "$@"\n'
                    % (directory / "scenario.json", directory / "transcript.jsonl", sys.executable, FAKE))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def gemini_home(directory: Path, settings) -> Path:
    """A home directory with Gemini user settings (a dict, or raw text)."""
    (directory / ".gemini").mkdir(parents=True, exist_ok=True)
    (directory / ".gemini" / "settings.json").write_text(settings if isinstance(settings, str) else json.dumps(settings))
    return directory


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()
        self.work = self.d / "worktree"
        (self.work / ".git").mkdir(parents=True)   # a worktree root, as the runner's are
        self.bin = wrapper(self.d)
        self.transcript = self.d / "transcript.jsonl"
        self.log = str(self.d / "run.log")
        self.no_system = str(self.d / "no-system" / "settings.json")
        # The machine's own Gemini system files and variables never reach a test.
        patcher = mock.patch.dict(billing.GEMINI_SYSTEM_SETTINGS, {
            "darwin": str(self.d / "sys-darwin" / "settings.json"), "linux": str(self.d / "sys-linux" / "settings.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        # The test home is set up the zero-spend way: every Gemini run repeats the whole check before its spawn.
        self.home = gemini_home(self.d / "home", PERSONAL)
        os.environ["HOME"] = str(self.home)

    def scenario(self, scenario):
        (self.d / "scenario.json").write_text(json.dumps(scenario))
        return {"TT_ACP_SCENARIO": str(self.d / "scenario.json"), "TT_ACP_TRANSCRIPT": str(self.transcript)}

    def records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def version_calls(self):
        path = Path(str(self.transcript) + ".version")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def received(self, method):
        return [r["received"] for r in self.records() if r.get("received", {}).get("method") == method]

    def child_env(self):
        return next(r["env"] for r in self.records() if "env" in r)

    def snapshot(self, **extra):
        return dict({"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, **extra)

    def verdict(self, env=None, cfg=None, system=None, version=None):
        if version is not None:
            self.scenario({"version": version})
        return verify_gemini(cfg or {"bin": self.bin}, env=self.snapshot() if env is None else env,
                             system_settings_path=system or self.no_system, now=1.0)

    def assertReason(self, reason, **kwargs):
        verdict = self.verdict(**kwargs)
        self.assertFalse(verdict.verified, "verified, expected %s" % reason)
        self.assertEqual(verdict.reason, reason)

    def start(self, scenario, cfg=None, **kwargs):
        adapter = GeminiAdapter(dict({"bin": self.bin}, **(cfg or {})), billing=StaticBilling(True))
        self.scenario(scenario)
        return adapter.start("fix it", str(self.work), "timetrace-drawn-id", self.log, **kwargs)


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
    def test_start_runs_one_acp_turn_with_the_rules_first(self):
        res = self.start({"turn": [{"chunk": "Working.\n"},
                                   {"update": {"sessionUpdate": "tool_call", "toolCallId": "t", "kind": "edit",
                                               "title": "Edit a.py"}},
                                   {"chunk": "Committed the fix."}]})
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.exit_code, res.session_id, res.output), (0, "fake-session-1", "Committed the fix."))
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["--acp", "--skip-trust"])
        text = self.received("session/prompt")[0]["params"]["prompt"][0]["text"]
        self.assertEqual(text, SAFETY_RULES + "\nfix it")
        self.assertNotIn("timetrace-drawn-id", self.transcript.read_text(), "the agent names its own sessions")
        self.assertEqual(len(self.version_calls()), 1, "the version is checked right before the spawn")

    def test_resume_sends_the_agents_session_back(self):
        adapter = GeminiAdapter({"bin": self.bin}, billing=StaticBilling(True))
        self.scenario({"initialize": {"loadSession": True}, "turn": [{"chunk": "again"}]})
        res = adapter.resume("next step", str(self.work), "g-7", self.log)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.received("session/load")[0]["params"]["sessionId"], "g-7")
        self.assertEqual(res.session_id, "g-7")

    def test_a_usage_limit_on_the_prompt_is_blocked_and_keeps_the_session(self):
        res = self.start({"turn": [{"error": {"code": 429, "message": "Quota exceeded: RESOURCE_EXHAUSTED"}}]})
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.session_id, "fake-session-1")

    def test_the_mcp_token_is_masked_everywhere_the_run_writes(self):
        access = McpAccess(url="https://v.example.test/timetrace/mcp", token=TOKEN, expires_at=time.time() + 600)
        res = self.start({"initialize": {"mcpHttp": True}, "turn": [{"chunk": "env says " + TOKEN}]}, mcp=access)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.received("session/new")[0]["params"]["mcpServers"][0]["headers"],
                         [{"name": "Authorization", "value": "Bearer " + TOKEN}])
        self.assertNotIn(TOKEN, Path(self.log).read_text())
        self.assertNotIn(TOKEN, res.output)
        self.assertNotIn(TOKEN, json.dumps(next(r["argv"] for r in self.records() if "argv" in r)))

    def test_a_worktree_with_a_dotenv_never_starts_the_agent(self):
        (self.work / ".env").write_text("PORT=8080\n")
        res = self.start({"turn": [{"chunk": "x"}]})
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "billing_unverified: gemini_dotenv_present:%s" % (self.work / ".env"))
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
        self.assertTrue(any("oauth-personal" in line and "overageStrategy" in line and ".env" in line and "0.60.x" in line
                            for line in lines), "the hint names the exact allow conditions")


class CursorRunTest(AdapterCase):
    def test_the_run_path_works_but_the_gate_never_opens(self):
        adapter = CursorAdapter({"bin": self.bin})
        with mock.patch.dict(os.environ, self.scenario({"turn": [{"chunk": "hi"}]})):
            res = adapter.start("p", str(self.work), "s", self.log)
        self.assertTrue(res.ok)
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["acp"])
        self.assertEqual(adapter_dispatch_problem(adapter), "billing_unverified")


class RunResultRedactionTest(unittest.TestCase):
    """Review blocker 4, adapter side: RunResult.error (stored, shown on the phone, uploaded) is masked whole first."""

    def test_agent_text_in_the_run_error_is_masked_before_it_is_cut(self):
        for acp in (AcpResult(error="session/prompt failed (1): " + "e" * 460 + " " + TOKEN),
                    AcpResult(error="said " + TOKEN),
                    AcpResult(stop_reason="weird " + TOKEN, session_id="s")):
            error = run_result(acp, None).error
            for piece in (TOKEN, TOKEN[:10]):
                self.assertNotIn(piece, error)


class GeminiVerifierTest(AdapterCase):
    def test_the_exact_allow_conditions_verify(self):
        verdict = self.verdict()
        self.assertTrue(verdict.verified, verdict.reason)
        self.assertEqual((verdict.provider, verdict.auth_method), ("gemini", "oauth-personal"))
        calls = self.version_calls()
        self.assertEqual(len(calls), 1)
        # Besides the allowlist: the wrapper's own two variables, what /bin/sh and the OS add, and LC_* (allowed;
        # Python sets LC_CTYPE itself under a C locale).
        extra = {name for name in calls[0]["env"] if not name.startswith("LC_")} - set(ALLOWED) - {
            "TT_ACP_SCENARIO", "TT_ACP_TRANSCRIPT", "PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
        self.assertEqual(extra, set(), "the version check runs with the hermetic environment")

    def test_user_settings_must_say_exactly_personal_login_and_credits_never(self):
        cases = (
            ({"billing": {"overageStrategy": "never"}}, "auth_type_unset"),
            (API_KEY_LOGIN, "auth_method_not_subscription:gemini-api-key"),
            (dict(PERSONAL, security={"auth": {"selectedType": "oauth-personal", "enforcedType": "oauth-personal"}}),
             "auth_override_present:security.auth.enforcedType"),
            (dict(PERSONAL, security={"auth": {"selectedType": "oauth-personal", "useExternal": False}}),
             "auth_override_present:security.auth.useExternal"),
            (dict(PERSONAL, billing={}), "billing_overage_unset"),
            (dict(PERSONAL, billing={"overageStrategy": "ask"}), "billing_overage_enabled:ask"),
            (dict(PERSONAL, billing={"overageStrategy": "always"}), "billing_overage_enabled:always"),
            ('{"security": {"auth": {"selectedType": "oauth-personal"}}, // a comment\n "billing": {}}',
             "settings_unreadable"),
            (["not", "an", "object"], "settings_unreadable"),
        )
        for settings, reason in cases:
            with self.subTest(reason=reason):
                gemini_home(self.home, settings)
                self.assertReason(reason)
        (self.home / ".gemini" / "settings.json").unlink()
        self.assertReason("settings_missing")

    def test_home_rules(self):
        self.assertReason("home_unset", env={"PATH": "/usr/bin"})
        self.assertReason("home_not_absolute", env={"HOME": "relative/home"})

    def test_the_verifier_is_cached(self):
        verifier = GeminiAdapter({"bin": self.bin}).billing
        self.assertIsInstance(verifier, billing.BillingVerifier)
        self.assertTrue(verifier.verdict().verified, verifier.verdict().reason)
        verifier.verdict()
        self.assertEqual(len(self.version_calls()), 1, "cached between calls; the gate forces a fresh one")
        verifier.verdict(force=True)
        self.assertEqual(len(self.version_calls()), 2)


class WorkspaceSettingsTest(AdapterCase):
    def problem(self, **extra):
        return gemini_workspace_problem(str(self.work), env=self.snapshot(**extra), binary=self.bin,
                                        system_settings_path=self.no_system)

    def test_workspace_settings_may_not_touch_security_billing_or_advanced(self):
        self.assertEqual(self.problem(), "")
        (self.work / ".gemini").mkdir()
        settings = self.work / ".gemini" / "settings.json"
        settings.write_text(json.dumps({"ui": {"theme": "dark"}}))
        self.assertEqual(self.problem(), "")
        for key in ("security", "billing", "advanced"):
            with self.subTest(key=key):
                settings.write_text(json.dumps({key: {}}))
                self.assertEqual(self.problem(), "workspace_settings_override:" + key)
        settings.write_text("{broken")
        self.assertEqual(self.problem(), "workspace_settings_unreadable")

    def test_a_subdirectory_run_checks_up_to_the_repository_root(self):
        sub = self.work / "pkg" / "app"
        sub.mkdir(parents=True)
        (self.work / ".gemini").mkdir()
        (self.work / ".gemini" / "settings.json").write_text(json.dumps({"billing": {"overageStrategy": "always"}}))
        problem = gemini_workspace_problem(str(sub), env=self.snapshot(), binary=self.bin,
                                           system_settings_path=self.no_system)
        self.assertEqual(problem, "workspace_settings_override:billing")

    def test_gemini_billing_keys_are_listed_for_check_commands(self):
        keys = billing.billing_env_keys("gemini")
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
                     "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
            self.assertIn(name, keys)
        self.assertIn("GEMINI_API_KEY", billing.billing_env_keys(""), "every known key, for check commands")


class SnapshotTest(AdapterCase):
    """One environment snapshot per run: what is verified is what the child gets."""

    def test_the_verifier_reads_HOME_from_the_environment_it_is_given(self):
        bad = gemini_home(self.d / "bad-home", API_KEY_LOGIN)
        with mock.patch.dict(os.environ, {"HOME": str(bad)}):
            verdict = self.verdict()
        self.assertTrue(verdict.verified, verdict.reason)

    def test_switching_HOME_between_the_check_and_the_spawn_changes_nothing(self):
        bad = gemini_home(self.d / "bad-home", API_KEY_LOGIN)
        real_prepare = GeminiAdapter.prepare

        def prepare_then_switch(adapter, cwd):
            answer = real_prepare(adapter, cwd)
            os.environ["HOME"] = str(bad)   # after the check, before the spawn
            return answer
        with mock.patch.object(GeminiAdapter, "prepare", prepare_then_switch):
            res = self.start({"env_probe": ["HOME"], "turn": []})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.child_env()["HOME"], str(self.home), "the child runs with the HOME that was verified")


# ---- third review (ec54723): each class reproduces one finding and fails on that commit -----------------------------

class EnvironmentAllowlistTest(AdapterCase):
    """Finding 1: CODE_ASSIST_ENDPOINT (or any other variable) in the runner's environment reached the child."""

    def test_only_allowlisted_variables_reach_the_child(self):
        os.environ.update({"CODE_ASSIST_ENDPOINT": "https://collector.example.test", "SOME_TOOL_SETTING": "1",
                           "LC_ALL": "en_US.UTF-8", "TZ": "UTC"})
        res = self.start({"env_probe": ["CODE_ASSIST_ENDPOINT", "SOME_TOOL_SETTING", "LC_ALL", "TZ", "HOME"],
                          "turn": []})
        self.assertTrue(res.ok, res.error)
        env = self.child_env()
        self.assertIsNone(env["CODE_ASSIST_ENDPOINT"])
        self.assertIsNone(env["SOME_TOOL_SETTING"])
        self.assertEqual((env["LC_ALL"], env["TZ"], env["HOME"]), ("en_US.UTF-8", "UTC", str(self.home)))

    def test_an_endpoint_in_a_home_dotenv_refuses(self):
        (self.home / ".env").write_text("CODE_ASSIST_ENDPOINT=https://collector.example.test\n")
        self.assertReason("gemini_dotenv_present:~/.env")


class SystemSettingsTest(AdapterCase):
    """Finding 2: a system settings file the CLI may skip (unsafe owner / permissions) could hide the user's own
    "always"; any system settings or defaults file now refuses, whatever it says."""

    def test_any_system_file_refuses(self):
        gemini_home(self.home, dict(PERSONAL, billing={"overageStrategy": "always"}))
        system = self.d / "admin" / "settings.json"
        system.parent.mkdir()
        system.write_text(json.dumps({"billing": {"overageStrategy": "never"}}))
        self.assertReason("gemini_system_settings_present:%s" % system, system=str(system))
        gemini_home(self.home, PERSONAL)
        for name in ("settings.json", "system-defaults.json"):
            with self.subTest(name=name):
                for existing in (self.d / "sys-darwin").glob("*"):
                    existing.unlink()
                path = self.d / "sys-darwin" / name
                path.parent.mkdir(exist_ok=True)
                path.write_text("{}")
                self.assertReason("gemini_system_settings_present:%s" % path)


class DotenvOverrideTest(AdapterCase):
    """Finding 3: GEMINI_CLI_HOME / system-path variables set from a .env file were not seen."""

    def test_a_dotenv_that_moves_the_settings_refuses(self):
        (self.home / ".gemini" / ".env").write_text("GEMINI_CLI_HOME=%s\n" % (self.d / "elsewhere"))
        self.assertReason("gemini_dotenv_present:~/.gemini/.env")

    def test_a_project_dotenv_with_a_settings_path_never_starts_the_agent(self):
        (self.d / ".env").write_text("GEMINI_CLI_SYSTEM_SETTINGS_PATH=/tmp/x.json\n")
        res = self.start({"turn": []})
        self.assertFalse(res.ok)
        self.assertEqual(self.records(), [], "nothing was spawned")


class DotenvParsingTest(AdapterCase):
    """Finding 4: a multiline-quoted key the old parser read as empty, and the old top-level login key."""

    def test_any_dotenv_refuses_without_being_parsed(self):
        (self.home / ".env").write_text('GEMINI_API_KEY="\nAIzaSyNotARealKey000000000000000000000"\n')
        self.assertReason("gemini_dotenv_present:~/.env")

    def test_the_old_top_level_login_key_does_not_count(self):
        gemini_home(self.home, {"selectedAuthType": "oauth-personal", "billing": {"overageStrategy": "never"}})
        self.assertReason("auth_type_unset")


class VersionPinTest(AdapterCase):
    """Only the audited Gemini CLI 0.60.x is accepted."""

    def test_only_a_plain_060_version_verifies(self):
        for printed, reason in (("0.61.0", "gemini_version_unaudited:0.61.0"),
                                ("0.59.9", "gemini_version_unaudited:0.59.9"),
                                ("0.60.0-preview.1", "gemini_version_unaudited:unparsable"),
                                ("Gemini CLI", "gemini_version_unaudited:unparsable")):
            with self.subTest(printed=printed):
                self.assertReason(reason, version=printed)
        self.assertTrue(self.verdict(version="0.60.7").verified)
        self.assertReason("gemini_version_unaudited:FileNotFoundError", cfg={"bin": str(self.d / "no-gemini")})



if __name__ == "__main__":
    unittest.main()
