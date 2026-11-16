"""Gemini CLI and Cursor through the generic ACP client: never dispatched (fixed unverified billing), but their runs
work and are hardened (one environment snapshot, allowlisted child environment, absolute-only PATH, the resolved
binary started by its real path). The agent is always tests/fake_acp_agent.py behind a wrapper script; no real
Gemini CLI or Cursor is started and no model is called.

Only names that also exist at commit 0d26a55 are imported here, so the review regression tests below can be run
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
from timetrace.billing import StaticBilling
from timetrace.dispatch import adapter_dispatch_problem
from timetrace.mcp_access import McpAccess

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
TOKEN = "ttm_" + "Qa1Ws2Ed3Rf4Tg5Yh6Uj7Ik8"
GEMINI_BILLING_REASON = "gemini_billing_unverifiable"
# The setup the last Gemini verifier (0d26a55) accepted: personal login, credits off, nothing else.
PERFECT_SETTINGS = {"security": {"auth": {"selectedType": "oauth-personal"}}, "billing": {"overageStrategy": "never"}}
PERFECT_CREDS = {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "token_type": "Bearer",
                 "expiry_date": 1}


def wrapper(path: Path, directory: Path) -> str:
    """An executable standing in for the CLI: it runs the fake agent with whatever arguments the adapter passes. The
    fake's scenario and transcript paths are part of the script, because the child gets no variable of the test's
    environment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec /usr/bin/env TT_ACP_SCENARIO="%s" TT_ACP_TRANSCRIPT="%s" "%s" "%s" "$@"\n'
                    % (directory / "scenario.json", directory / "transcript.jsonl", sys.executable, FAKE))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def npm_install(root: Path, directory: Path, name: str) -> str:
    """An npm-style install of the fake as @google/gemini-cli 0.60.0: root/bin/<name> -> the package's script."""
    package = root / "lib" / "node_modules" / "@google" / "gemini-cli"
    script = wrapper(package / "dist" / "index.js", directory)
    (package / "package.json").write_text(json.dumps({"name": "@google/gemini-cli", "version": "0.60.0"}))
    link = root / "bin" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(script)
    return str(link)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()
        self.work = self.d / "worktree"
        (self.work / ".git").mkdir(parents=True)
        self.bin = npm_install(self.d / "npm", self.d, "gemini")
        self.script = os.path.realpath(self.bin)
        self.transcript = self.d / "transcript.jsonl"
        self.log = str(self.d / "run.log")
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        self.home = self.d / "home"
        (self.home / ".gemini").mkdir(parents=True)
        (self.home / ".gemini" / "settings.json").write_text(json.dumps(PERFECT_SETTINGS))
        (self.home / ".gemini" / "oauth_creds.json").write_text(json.dumps(PERFECT_CREDS))
        os.environ["HOME"] = str(self.home)

    def scenario(self, scenario):
        (self.d / "scenario.json").write_text(json.dumps(scenario))

    def records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def received(self, method):
        return [r["received"] for r in self.records() if r.get("received", {}).get("method") == method]

    def child_env(self):
        return next(r["env"] for r in self.records() if "env" in r)

    def run_agent(self, adapter, scenario, resume=None, **kwargs):
        self.scenario(scenario)
        if resume:
            return adapter.resume("next step", str(self.work), resume, self.log, **kwargs)
        return adapter.start("fix it", str(self.work), "timetrace-drawn-id", self.log, **kwargs)

    def recorded_spawn(self, adapter):
        """The command and base environment a run would spawn with (the session itself is not started)."""
        seen = {}

        class Recording:
            def __init__(self, cmd, *args, **kwargs):
                seen["cmd"], seen["base_env"] = cmd, kwargs.get("base_env")

            def run(self):
                return AcpResult(stop_reason="end_turn", session_id="s")
        with mock.patch("timetrace.adapters.acp.AcpSession", Recording):
            result = adapter.start("p", str(self.work), "s", self.log)
        return result, seen


class PermanentlyUnverifiedTest(AdapterCase):
    """Final review (0d26a55): Gemini is never dispatched, exactly like Cursor, even for the setup the last
    verifier accepted (exact settings, personal cached login, @google/gemini-cli 0.60.0, no .env or workspace
    config)."""

    def test_neither_agent_is_dispatched_in_any_configuration(self):
        for adapter in (GeminiAdapter({"bin": self.bin}), GeminiAdapter({}), CursorAdapter({"bin": self.bin})):
            with self.subTest(adapter=adapter.name, cfg=adapter.cfg):
                caps = adapter.capabilities()
                self.assertFalse(caps["can_enforce_zero_spend"])
                self.assertEqual(adapter_dispatch_problem(adapter), "billing_unverified")
                self.assertEqual(adapter_dispatch_problem(adapter, resuming=True), "billing_unverified")
                from timetrace.agent import _capability_zero_spend
                self.assertFalse(_capability_zero_spend(adapter, {}))
        details = GeminiAdapter({"bin": self.bin}).capability_details()
        self.assertEqual(details["unsupported_reason"], GEMINI_BILLING_REASON)
        self.assertTrue(GeminiAdapter({"bin": self.bin}).capabilities()["can_dispatch"], "the run path is declared")
        adapters = build_adapters({"claude": {}, "codex": {}, "gemini": {"bin": self.bin}})
        self.assertEqual(adapter_dispatch_problem(adapters["gemini"]), "billing_unverified")

    def test_no_constructor_argument_or_setting_can_verify_it(self):
        for cls in (GeminiAdapter, CursorAdapter):
            with self.subTest(adapter=cls.name), self.assertRaises(TypeError):
                cls({"bin": self.bin}, billing=StaticBilling(True))
        odd = GeminiAdapter({"bin": self.bin, "zero_spend": True, "billing": "verified", "verified": True})
        self.assertEqual(adapter_dispatch_problem(odd), "billing_unverified")
        self.assertFalse(hasattr(billing, "verify_gemini"), "no verifier left that could be wired back in")


class HardenedRunTest(AdapterCase):
    """Final review (0d26a55): the resolved binary is started by its absolute real path, and PATH keeps only its
    absolute entries; the child's environment is the allowlisted snapshot (both agents)."""

    def test_the_child_is_the_real_path_of_the_resolved_binary(self):
        for adapter in (GeminiAdapter({"bin": self.bin}), CursorAdapter({"bin": self.bin})):
            with self.subTest(adapter=adapter.name):
                result, seen = self.recorded_spawn(adapter)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(seen["cmd"][0], self.script)

    def test_a_relative_path_entry_never_supplies_the_binary_or_reaches_the_child(self):
        project = self.work / "bin"
        wrapper(project / "gemini", self.d)   # a project's own `gemini`, reachable only through a relative entry
        os.environ["PATH"] = os.pathsep.join(["bin", "", str(self.d / "npm" / "bin"), "/usr/bin", "/bin"])
        old_cwd = os.getcwd()
        os.chdir(str(self.work))
        try:
            result, seen = self.recorded_spawn(GeminiAdapter({}))
        finally:
            os.chdir(old_cwd)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen["cmd"][0], self.script)
        self.assertEqual(seen["base_env"]["PATH"], os.pathsep.join([str(self.d / "npm" / "bin"), "/usr/bin", "/bin"]))

    def test_only_allowlisted_variables_reach_the_child(self):
        os.environ.update({"CODE_ASSIST_ENDPOINT": "https://collector.example.test", "GEMINI_API_KEY": "k",
                           "CURSOR_API_KEY": "c", "SOME_TOOL_SETTING": "1", "LC_ALL": "en_US.UTF-8", "TZ": "UTC"})
        for adapter in (GeminiAdapter({"bin": self.bin}), CursorAdapter({"bin": self.bin})):
            with self.subTest(adapter=adapter.name):
                if self.transcript.exists():
                    self.transcript.unlink()
                res = self.run_agent(adapter, {"env_probe": ["CODE_ASSIST_ENDPOINT", "GEMINI_API_KEY", "CURSOR_API_KEY",
                                                             "SOME_TOOL_SETTING", "LC_ALL", "TZ", "HOME"], "turn": []})
                self.assertTrue(res.ok, res.error)
                env = self.child_env()
                for name in ("CODE_ASSIST_ENDPOINT", "GEMINI_API_KEY", "CURSOR_API_KEY", "SOME_TOOL_SETTING"):
                    self.assertIsNone(env[name], name)
                self.assertEqual((env["LC_ALL"], env["TZ"], env["HOME"]), ("en_US.UTF-8", "UTC", str(self.home)))

    def test_switching_HOME_between_the_snapshot_and_the_spawn_changes_nothing(self):
        real_prepare = CursorAdapter.prepare

        def prepare_then_switch(adapter, cwd):
            answer = real_prepare(adapter, cwd)
            os.environ["HOME"] = str(self.d / "elsewhere")   # after the snapshot, before the spawn
            return answer
        with mock.patch.object(CursorAdapter, "prepare", prepare_then_switch):
            res = self.run_agent(CursorAdapter({"bin": self.bin}), {"env_probe": ["HOME"], "turn": []})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.child_env()["HOME"], str(self.home))

    def test_a_binary_that_cannot_be_resolved_is_not_started(self):
        os.environ["PATH"] = "relative-only"
        res = self.run_agent(GeminiAdapter({}), {"turn": []})
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "could not start gemini: not found on an absolute PATH entry")
        self.assertEqual(self.records(), [])


class AcpRunTest(AdapterCase):
    """The ACP runs themselves still work (for a future audit), through either adapter."""

    def test_start_runs_one_acp_turn_with_the_rules_first(self):
        res = self.run_agent(GeminiAdapter({"bin": self.bin}), {"turn": [
            {"chunk": "Working.\n"},
            {"update": {"sessionUpdate": "tool_call", "toolCallId": "t", "kind": "edit", "title": "Edit a.py"}},
            {"chunk": "Committed the fix."}]})
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.exit_code, res.session_id, res.output), (0, "fake-session-1", "Committed the fix."))
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["--acp", "--skip-trust"])
        text = self.received("session/prompt")[0]["params"]["prompt"][0]["text"]
        self.assertEqual(text, SAFETY_RULES + "\nfix it")
        self.assertNotIn("timetrace-drawn-id", self.transcript.read_text(), "the agent names its own sessions")

    def test_cursor_runs_with_its_own_launch_command(self):
        res = self.run_agent(CursorAdapter({"bin": self.bin}), {"turn": [{"chunk": "hi"}]})
        self.assertTrue(res.ok, res.error)
        self.assertEqual(next(r["argv"] for r in self.records() if "argv" in r), ["acp"])

    def test_resume_sends_the_agents_session_back(self):
        res = self.run_agent(GeminiAdapter({"bin": self.bin}), {"initialize": {"loadSession": True},
                                                                "turn": [{"chunk": "again"}]}, resume="g-7")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.received("session/load")[0]["params"]["sessionId"], "g-7")
        self.assertEqual(res.session_id, "g-7")

    def test_a_usage_limit_on_the_prompt_is_blocked_and_keeps_the_session(self):
        res = self.run_agent(GeminiAdapter({"bin": self.bin}), {"turn": [
            {"error": {"code": 429, "message": "Quota exceeded: RESOURCE_EXHAUSTED"}}]})
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.session_id, "fake-session-1")

    def test_the_mcp_token_travels_only_in_the_session_new_header(self):
        access = McpAccess(url="https://v.example.test/timetrace/mcp", token=TOKEN, expires_at=time.time() + 600)
        res = self.run_agent(GeminiAdapter({"bin": self.bin}), {"initialize": {"mcpHttp": True},
                                                                "env_probe": ["TIMETRACE_MCP_TOKEN"],
                                                                "turn": [{"chunk": "env says " + TOKEN}]}, mcp=access)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.received("session/new")[0]["params"]["mcpServers"][0]["headers"],
                         [{"name": "Authorization", "value": "Bearer " + TOKEN}])
        self.assertIsNone(self.child_env()["TIMETRACE_MCP_TOKEN"])
        self.assertNotIn(TOKEN, Path(self.log).read_text())
        self.assertNotIn(TOKEN, res.output)
        self.assertNotIn(TOKEN, json.dumps(next(r["argv"] for r in self.records() if "argv" in r)))

    def test_capabilities_follow_the_binary(self):
        self.assertTrue(GeminiAdapter({"bin": self.bin}).capabilities()["can_dispatch"])
        self.assertFalse(GeminiAdapter({"bin": str(self.d / "missing")}).capabilities()["can_dispatch"])
        self.assertTrue(GeminiAdapter({}, which=lambda name: "/opt/x/" + name).capabilities()["can_dispatch"])
        self.assertFalse(CursorAdapter({}, which=lambda name: None).capabilities()["can_dispatch"])
        self.assertEqual(CursorAdapter({}).command(), ["cursor-agent", "acp"])
        self.assertEqual(GeminiAdapter({"extra_args": ["--debug"]}).command(), ["gemini", "--acp", "--skip-trust", "--debug"])
        for adapter in (GeminiAdapter({}), CursorAdapter({})):
            self.assertTrue(adapter.interactive_runs and adapter.supports_mcp)

    def test_run_result_only_counts_an_end_turn_as_success(self):
        self.assertTrue(run_result(AcpResult(stop_reason="end_turn", exit_code=-15), None).ok)
        for stop in ("max_tokens", "max_turn_requests", "refusal", "cancelled", ""):
            res = run_result(AcpResult(stop_reason=stop, exit_code=0, session_id="s"), None)
            self.assertFalse(res.ok)
            self.assertEqual(res.exit_code, 1)
            self.assertIn("the agent ended the turn", res.error)
        early = run_result(AcpResult(error="session/new failed (429): quota", failed_method="session/new"), "s")
        self.assertFalse(early.blocked, "no session to resume yet: an ordinary failure")

    def test_billing_keys_stay_listed_for_check_commands(self):
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "CURSOR_API_KEY"):
            self.assertIn(name, billing.billing_env_keys(""))


class InventoryAndDoctorTest(AdapterCase):
    def test_inventory_reports_each_agent_by_its_own_binary(self):
        from timetrace.cli import _runner_tools
        adapters = {"cursor": CursorAdapter({}, which=lambda name: None), "gemini": GeminiAdapter({"bin": self.bin})}
        with mock.patch("timetrace.cli.shutil.which", side_effect=lambda b: b if b == self.bin else None) as which:
            tools = {t["provider"]: t for t in _runner_tools({}, adapters)}
        self.assertIn(mock.call("cursor-agent"), which.call_args_list, "not `cursor`, which is the editor")
        self.assertEqual((tools["gemini"]["status"], tools["gemini"]["can_enforce_zero_spend"]), ("available", False))
        self.assertEqual((tools["cursor"]["status"], tools["cursor"]["can_enforce_zero_spend"]), ("unavailable", False))
        self.assertFalse(tools["gemini"]["can_interact"])

    def test_doctor_explains_why_an_agent_is_not_dispatched(self):
        from timetrace.cli import _acp_tool_lines, _tool_billing_details
        cursor = CursorAdapter({"bin": self.bin})
        lines = _acp_tool_lines("cursor", cursor, _tool_billing_details("cursor", {"cursor": cursor}))
        self.assertIn("ACP：%s acp" % self.bin, lines[0])
        self.assertTrue(any("零付费核验: 未通过" in line and CURSOR_BILLING_REASON in line for line in lines))
        gemini = GeminiAdapter({"bin": str(self.d / "nope")})
        lines = _acp_tool_lines("gemini", gemini, _tool_billing_details("gemini", {"gemini": gemini}))
        self.assertIn("未找到", lines[0])
        self.assertTrue(any(GEMINI_BILLING_REASON in line for line in lines))
        self.assertTrue(any("从不给 Gemini 派发任务" in line and "MCP OAuth" in line for line in lines),
                        "the hint says Gemini is never dispatched and what an audit must cover")


class RunResultRedactionTest(unittest.TestCase):
    """Review blocker 4, adapter side: RunResult.error (stored, shown on the phone, uploaded) is masked whole first."""

    def test_agent_text_in_the_run_error_is_masked_before_it_is_cut(self):
        for acp in (AcpResult(error="session/prompt failed (1): " + "e" * 460 + " " + TOKEN),
                    AcpResult(error="said " + TOKEN),
                    AcpResult(stop_reason="weird " + TOKEN, session_id="s")):
            error = run_result(acp, None).error
            for piece in (TOKEN, TOKEN[:10]):
                self.assertNotIn(piece, error)



# ---- review of b3237bf: fails on that commit ------------------------------------------------------------------------

LONG = "ttm_" + "Ab1-Cd2_" * 8
BAD_IDS = (LONG, "s-" + LONG, "has space", "a/b", "x" * 129)


class SessionIdEverywhereTest(AdapterCase):
    """One rule for every session id that enters or leaves an adapter run."""

    def test_resume_with_an_invalid_id_is_refused_before_anything_is_prepared(self):
        for adapter in (GeminiAdapter({"bin": self.bin}), CursorAdapter({"bin": self.bin})):
            for session in BAD_IDS:
                with self.subTest(adapter=adapter.name, session=session[:20]):
                    self.scenario({"initialize": {"loadSession": True}, "turn": []})
                    res = adapter.resume("p", str(self.work), session, self.log)
                    self.assertFalse(res.ok)
                    self.assertEqual((res.error, res.session_id), ("invalid session id", None))
        self.assertEqual(self.records(), [], "nothing was spawned")

    def test_fallbacks_never_return_an_invalid_id(self):
        # Preparation failure (no binary): a valid id is kept, an invalid one is not (whatever reaches _run).
        missing = GeminiAdapter({"bin": str(self.d / "missing")})
        self.assertEqual(missing.resume("p", str(self.work), "g-7", self.log).session_id, "g-7")
        for session in BAD_IDS:
            with self.subTest(session=session[:20]):
                res = missing._run("p", str(self.work), session, self.log, None, None, None)
                self.assertIsNone(res.session_id)
                self.assertIsNone(run_result(AcpResult(session_id=session, stop_reason="end_turn"), None).session_id)
                self.assertIsNone(run_result(AcpResult(error="boom"), session).session_id)
                self.assertEqual(run_result(AcpResult(error="boom"), "g-7").session_id, "g-7")

    def test_normal_ids_are_unaffected(self):
        res = self.run_agent(GeminiAdapter({"bin": self.bin}), {"initialize": {"loadSession": True},
                                                                "turn": [{"chunk": "again"}]}, resume="g-7")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.session_id, "g-7")


if __name__ == "__main__":
    unittest.main()
