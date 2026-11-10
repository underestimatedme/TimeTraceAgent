import json
import unittest
from pathlib import Path

from timetrace.adapters import codex
from timetrace.adapters.base import SAFETY_RULES
from timetrace.models import Sample

FIXTURES = Path(__file__).parent / "fixtures"


def load_lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class ParseRateLimitsTest(unittest.TestCase):
    def test_ordinary_permission_preserved_only_for_known_reserve_and_codex(self):
        for permission in (True, False, None, "true"):
            resp = {"ordinaryUsageAllowed": permission, "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 33, "windowDurationMins": 300}},
                "base_model_inference": {"limitName": "gpt-reserve", "primary": {"usedPercent": 100, "windowDurationMins": 10080}},
                "other": {"primary": {"usedPercent": 100, "windowDurationMins": 300}}}}
            samples = codex.parse_rate_limits(resp)
            values = {s.bucket_key: getattr(s, "codex_ordinary_usage_allowed", None) for s in samples}
            expected = permission if type(permission) is bool else None
            self.assertEqual(values["codex:codex:primary"], expected)
            self.assertEqual(values["codex:base_model_inference:primary"], expected)
            self.assertIsNone(values["codex:other:primary"])
            self.assertEqual(samples[1].used_pct, 100)
        resp["rateLimitsByLimitId"]["base_model_inference"]["limitName"] = "unknown"
        resp["ordinaryUsageAllowed"] = True
        self.assertIsNone(getattr(codex.parse_rate_limits(resp)[1], "codex_ordinary_usage_allowed", None))

    def test_multi_bucket_response(self):
        resp = json.loads((FIXTURES / "codex_ratelimits.json").read_text())
        samples = codex.parse_rate_limits(resp)
        by_key = {s.bucket_key: s for s in samples}
        self.assertEqual(set(by_key), {
            "codex:codex:primary", "codex:codex:secondary",
            "codex:base_model_inference:primary",
        })
        p = by_key["codex:codex:primary"]
        self.assertEqual(p.used_pct, 100.0)
        self.assertEqual(p.reset_at, 1788361284)
        self.assertEqual(p.window_mins, 300)
        self.assertTrue(p.is_representative)
        self.assertEqual(p.source, "live")
        self.assertEqual(by_key["codex:codex:secondary"].used_pct, 47.0)
        self.assertEqual(by_key["codex:codex:secondary"].window_mins, 10080)
        self.assertFalse(by_key["codex:base_model_inference:primary"].is_representative)

    def test_falls_back_to_single_bucket_view(self):
        resp = {"rateLimits": {"limitId": "codex",
                               "primary": {"usedPercent": 10, "resetsAt": 5, "windowDurationMins": 300},
                               "secondary": None}}
        samples = codex.parse_rate_limits(resp)
        self.assertEqual([s.bucket_key for s in samples], ["codex:codex:primary"])
        self.assertEqual(samples[0].used_pct, 10.0)


class ParseExecTest(unittest.TestCase):
    def test_blocked_run(self):
        res = codex.parse_exec(load_lines("codex_exec_blocked.jsonl"))
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.session_id, "01a06248-458d-7432-91f9-4406d4d3a97e")
        self.assertIn("usage limit", res.error)
        self.assertIsNone(res.reset_at)  # text is not parsed; adapter asks app-server

    def test_success_run(self):
        res = codex.parse_exec(load_lines("codex_exec_success.jsonl"))
        self.assertTrue(res.ok)
        self.assertFalse(res.blocked)
        self.assertEqual(res.output, "PROBE_OK")
        self.assertEqual(res.session_id, "0199d2d9-1111-7aaa-bbbb-000000000001")

    def test_generic_failure_is_not_block(self):
        lines = ['{"type":"thread.started","thread_id":"t"}',
                 '{"type":"turn.failed","error":{"message":"model refused"}}']
        res = codex.parse_exec(lines)
        self.assertFalse(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "model refused")


class BuildCmdTest(unittest.TestCase):
    cfg = {"bin": "codex", "sandbox": "workspace-write", "model": None, "extra_args": []}

    def test_start(self):
        cmd = codex.build_cmd(self.cfg, "do it", "/wt", last_msg_file="/log.last.md")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/log.last.md")
        self.assertTrue(cmd[-1].startswith(SAFETY_RULES))
        self.assertTrue(cmd[-1].endswith("do it"))

    def test_resume(self):
        cmd = codex.build_cmd(dict(self.cfg, model="gpt-5.6"), "go on", "/wt", resume="tid")
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "tid"])
        self.assertIn("--json", cmd)
        self.assertNotIn("-C", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.6")


class SafetyRulesTest(unittest.TestCase):
    def test_rules_allow_committing_in_a_linked_worktree(self):
        # The model refused to commit because rule 3 read as "never touch
        # anything outside cwd" while the worktree's git metadata lives in the
        # main repository; the rule must carve that out explicitly.
        self.assertIn("git add", SAFETY_RULES)
        self.assertIn("commit", SAFETY_RULES)
        self.assertIn(".git", SAFETY_RULES)


def config_overrides(cmd):
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == "-c"]


class NetworkDomainsTest(unittest.TestCase):
    """`codex.network_domains`: the only way a task run's commands reach the network, and only those domains."""
    allowed = dict(BuildCmdTest.cfg, network_domains=["api.appstoreconnect.apple.com", "*.apple.com"])

    def test_no_domains_is_the_sandbox_as_it_was(self):
        for cfg in (BuildCmdTest.cfg, dict(BuildCmdTest.cfg, network_domains=[])):
            cmd = codex.build_cmd(cfg, "go", "/wt")
            self.assertIn("-s", cmd)
            self.assertIn("sandbox_workspace_write.network_access=false", config_overrides(cmd))
            self.assertFalse([o for o in config_overrides(cmd) if "default_permissions" in o or "network_proxy" in o])

    def test_defaults_leave_the_network_off(self):
        from timetrace import config
        self.assertEqual(config.DEFAULTS["codex"]["network_domains"], [])

    def test_domains_run_under_a_profile_that_allows_only_them(self):
        overrides = config_overrides(codex.build_cmd(self.allowed, "go", "/wt"))
        self.assertIn("features.network_proxy=true", overrides)
        self.assertIn('default_permissions="timetrace_task"', overrides)
        self.assertIn("permissions.timetrace_task.network.enabled=true", overrides)
        self.assertIn('permissions.timetrace_task.network.domains={"api.appstoreconnect.apple.com"="allow","*.apple.com"="allow"}', overrides)

    def test_a_profile_replaces_the_legacy_sandbox_settings_rather_than_mixing_with_them(self):
        cmd = codex.build_cmd(self.allowed, "go", "/wt")
        self.assertNotIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertFalse([o for o in config_overrides(cmd) if o.startswith(("sandbox_mode", "sandbox_workspace_write"))])

    def test_the_profile_writes_only_the_worktree_temp_and_the_narrow_git_paths(self):
        overrides = config_overrides(codex.build_cmd(self.allowed, "go", "/wt", add_dirs=["/repo/.git/objects", '/q"x']))
        self.assertIn('permissions.timetrace_task.filesystem={":root"="read",":project_roots"={"."="write"},":tmpdir"="write",":slash_tmp"="write","/repo/.git/objects"="write","/q\\"x"="write"}', overrides)

    def test_a_resumed_run_keeps_the_profile(self):
        cmd = codex.build_cmd(self.allowed, "go on", "/wt", resume="tid")
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "tid"])
        self.assertNotIn("-s", cmd)
        self.assertNotIn("-C", cmd)
        self.assertIn('default_permissions="timetrace_task"', config_overrides(cmd))

    def test_domains_are_lowercased_and_deduplicated(self):
        cfg = dict(BuildCmdTest.cfg, network_domains=["Foo.Example.com", "foo.example.com"])
        self.assertIn('permissions.timetrace_task.network.domains={"foo.example.com"="allow"}', config_overrides(codex.build_cmd(cfg, "go", "/wt")))

    def test_one_bad_entry_leaves_the_network_off(self):
        # A list that cannot be read as written must not half apply, and `*` or a URL is not a narrow allowance.
        for bad in (["*"], ["*.com"], ["github.com/x"], ["https://apple.com"], [""], ['a"b.com'], ["localhost"], ["ok.apple.com", "*"], [1], "apple.com", {"apple.com": True}):
            with self.subTest(bad=bad):
                cmd = codex.build_cmd(dict(BuildCmdTest.cfg, network_domains=bad), "go", "/wt")
                self.assertIn("sandbox_workspace_write.network_access=false", config_overrides(cmd))
                self.assertFalse([o for o in config_overrides(cmd) if "network_proxy" in o])

    def test_only_a_workspace_write_run_can_have_the_network(self):
        cmd = codex.build_cmd(dict(self.allowed, sandbox="read-only"), "go", "/wt")
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertIn("sandbox_workspace_write.network_access=false", config_overrides(cmd))
        self.assertFalse([o for o in config_overrides(cmd) if "network_proxy" in o])

    def test_folder_and_chat_runs_never_have_the_network(self):
        for cmd in (codex.build_folder_cmd(self.allowed, "go", "/out"), codex.build_chat_cmd(self.allowed, "hi", "/repo")):
            self.assertFalse([o for o in config_overrides(cmd) if "network_proxy" in o or "default_permissions" in o])
        self.assertIn("sandbox_workspace_write.network_access=false", config_overrides(codex.build_folder_cmd(self.allowed, "go", "/out")))


class WorktreeWritableRootTest(unittest.TestCase):
    def test_build_cmd_passes_extra_writable_dirs_as_config_for_start_and_resume(self):
        # `codex exec resume` rejects --add-dir; the config override works for both.
        for resume in (None, "tid"):
            with self.subTest(resume=resume):
                cmd = codex.build_cmd(BuildCmdTest.cfg, "go", "/wt", resume=resume, add_dirs=["/repo/.git/objects", '/q"x'])
                self.assertNotIn("--add-dir", cmd)
                self.assertIn('sandbox_workspace_write.writable_roots=["/repo/.git/objects", "/q\\"x"]',
                              config_overrides(cmd))

    def test_resume_forces_the_sandbox_and_both_disable_network(self):
        # Without an explicit sandbox a resumed run would fall back to whatever
        # ~/.codex/config.toml says, possibly danger-full-access.
        resumed = codex.build_cmd(BuildCmdTest.cfg, "go on", "/wt", resume="tid")
        self.assertEqual(resumed[:4], ["codex", "exec", "resume", "tid"])
        self.assertIn('sandbox_mode="workspace-write"', config_overrides(resumed))
        for cmd in (resumed, codex.build_cmd(BuildCmdTest.cfg, "go", "/wt")):
            self.assertIn("sandbox_workspace_write.network_access=false", config_overrides(cmd))

    def test_adapter_grants_only_the_git_paths_a_commit_needs(self):
        # A linked worktree keeps its metadata under the main repo's .git; the
        # sandbox may write the worktree's admin dir, objects and its branch
        # ref, but never the shared config or hooks (unsandboxed git runs them).
        import subprocess
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-qm", "init"], cwd=repo, check=True)
            wt = Path(d) / "wt"
            subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "timetrace/4"], cwd=repo, check=True)
            seen = {}

            def fake_run(cmd, cwd, log, **kwargs):
                seen["cmd"] = cmd
                return 0, []

            ad = codex.CodexAdapter({"bin": "codex"})
            original = codex.run_streaming
            codex.run_streaming = fake_run
            try:
                ad.start("do it", str(wt), "s", str(Path(d) / "log"))
            finally:
                codex.run_streaming = original
            roots_arg = [c for c in config_overrides(seen["cmd"]) if c.startswith("sandbox_workspace_write.writable_roots=")]
            self.assertEqual(len(roots_arg), 1)
            roots = [Path(r) for r in json.loads(roots_arg[0].split("=", 1)[1])]
            common = (repo / ".git").resolve()
            self.assertIn(common / "objects", roots)
            self.assertIn(common / "refs" / "heads" / "timetrace", roots)
            self.assertNotIn(common, roots)


class AdapterBlockedResetTest(unittest.TestCase):
    def test_blocked_run_pulls_reset_from_live_limits(self):
        ad = codex.CodexAdapter({"bin": "codex"})
        ad.read_limits = lambda: [Sample("codex:codex:primary", "codex", 100, reset_at=999)]
        codex.run_streaming = lambda cmd, cwd, log, **kwargs: (1, load_lines("codex_exec_blocked.jsonl"))
        try:
            res = ad.start("x", "/wt", "sid", "/dev/null")
        finally:
            from timetrace.adapters import base
            codex.run_streaming = base.run_streaming
        self.assertTrue(res.blocked)
        self.assertEqual(res.reset_at, 999)
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(len(res.samples), 1)




class McpInjectionTest(unittest.TestCase):
    """A dispatched execution run is given TimeTrace's MCP server by two config options that name the address and the
    environment variable the token is read from; the token itself is in the one process's environment only."""

    TOKEN = "ttm_SENTINEL_" + "Z9y8X7w6V5" * 4
    URL = "https://apis.atlaspaces.com/timetrace/api/v1/mcp"
    cfg = {"bin": "codex", "sandbox": "workspace-write", "model": None, "extra_args": []}

    def access(self, token=None):
        from timetrace.mcp_access import McpAccess
        return McpAccess(self.URL, token or self.TOKEN, 4102444800.0)

    def test_the_command_names_the_address_and_the_variable_and_holds_no_token(self):
        for resume in (None, "tid"):
            cmd = codex.build_cmd(self.cfg, "do it", "/wt", resume=resume, mcp=self.access())
            overrides = config_overrides(cmd)
            self.assertIn('mcp_servers.timetrace.url="%s"' % self.URL, overrides)
            self.assertIn('mcp_servers.timetrace.bearer_token_env_var="TIMETRACE_MCP_TOKEN"', overrides)
            self.assertFalse([part for part in cmd if "ttm_" in part or self.TOKEN in part])
            self.assertTrue(cmd[-1].endswith("do it"))
            # before the configured extra arguments and the prompt
            self.assertLess(cmd.index('mcp_servers.timetrace.url="%s"' % self.URL), len(cmd) - 1)

    def test_codex_is_told_not_to_stop_at_a_confirmation_for_timetrace_tools(self):
        # `codex exec` runs with approval policy "never": without this a call of any MCP tool fails with "MCP tool call
        # requires approval, but approval policy is never" (found with the real codex 0.165; the fake cannot show it). It is
        # the one TimeTrace server only, and what its tools may do is decided by Valley's fixed policy of the run.
        for resume in (None, "tid"):
            overrides = config_overrides(codex.build_cmd(self.cfg, "do it", "/wt", resume=resume, mcp=self.access()))
            self.assertIn('mcp_servers.timetrace.default_tools_approval_mode="approve"', overrides)
            self.assertEqual([o for o in overrides if "approval_mode" in o], ['mcp_servers.timetrace.default_tools_approval_mode="approve"'])
        self.assertFalse([o for o in config_overrides(codex.build_cmd(self.cfg, "do it", "/wt")) if "approval" in o])

    def test_codex_is_not_left_to_write_its_environment_to_disk_while_it_holds_the_token(self):
        # Found with the real codex 0.165: its shell snapshot (a .sh under CODEX_HOME/shell_snapshots that is written for the
        # model's commands) holds `export TIMETRACE_MCP_TOKEN=...`, a file that outlives the run. A run with the access
        # does not write one.
        for resume in (None, "tid"):
            overrides = config_overrides(codex.build_cmd(self.cfg, "do it", "/wt", resume=resume, mcp=self.access()))
            self.assertIn("features.shell_snapshot=false", overrides)
        self.assertFalse([o for o in config_overrides(codex.build_cmd(self.cfg, "do it", "/wt")) if "shell_snapshot" in o])

    def test_without_it_the_command_is_what_it_was(self):
        for resume in (None, "tid"):
            plain = codex.build_cmd(self.cfg, "do it", "/wt", resume=resume)
            self.assertEqual(plain, codex.build_cmd(self.cfg, "do it", "/wt", resume=resume, mcp=None))
            self.assertFalse([o for o in config_overrides(plain) if "mcp_servers" in o])

    def test_the_other_kinds_of_run_are_untouched(self):
        for cmd in (codex.build_chat_cmd(self.cfg, "x", "/wt"), codex.build_folder_cmd(self.cfg, "x", "/wt")):
            self.assertFalse([o for o in config_overrides(cmd) if "mcp_servers" in o])

    def fake_codex(self, d):
        script = Path(d) / "codex"
        script.write_text("#!%s\n" % __import__("sys").executable + '''
import json, os, sys, time
token = os.environ.get("TIMETRACE_MCP_TOKEN")
old = os.environ.get("TT_FAKE_PREV", "")
with open(os.environ["TT_FAKE_TRANSCRIPT"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "token_in_env": token}) + "\\n")
# The final answer goes to the -o file, written by the tool itself (the real Codex does exactly this).
if "-o" in sys.argv:
    with open(sys.argv[sys.argv.index("-o") + 1], "w") as fh:
        fh.write("the token is " + str(token) + " and the last one was " + old)
print("diagnosticprefix" + str(token) + "suffix")
print("lastrun" + old)
time.sleep(float(os.environ.get("TT_FAKE_SLEEP", "0")))
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
print(json.dumps({"type": "turn.completed"}))
sys.exit(int(os.environ.get("TT_FAKE_EXIT", "0")))
''')
        script.chmod(0o755)
        return str(script)

    def run_adapter(self, resume, mcp, extra_env=None, cancel=None, keep=None):
        import os, tempfile
        from unittest.mock import patch
        from timetrace.billing import StaticBilling
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "t.jsonl"
            adapter = codex.CodexAdapter({"bin": self.fake_codex(d), "sandbox": "workspace-write"}, billing=StaticBilling(True))
            before = dict(os.environ)
            patched = dict({"TT_FAKE_TRANSCRIPT": str(transcript)}, **(extra_env or {}))
            with patch.dict(os.environ, patched):
                with patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                    run = adapter.resume if resume else adapter.start
                    try:
                        result = run("go", d, "tid", str(Path(d) / "run.log"), cancel, mcp=mcp)
                    except Exception:
                        result = None
                        self.raised = True
                during = dict(os.environ)  # (read while the patch is still in: after it, the environment is always restored)
            records = [json.loads(line) for line in transcript.read_text().splitlines()]
            log = (Path(d) / "run.log").read_text()
            last = Path(d) / "run.log.last.md"
            if keep is not None:
                keep.append(last.read_text() if last.exists() else None)
            self.assertEqual(dict(before, **patched), during, "the process's own environment changed")
        return result, records, log

    def test_a_run_has_the_token_only_in_its_environment(self):
        for resume in (False, True):
            result, records, log = self.run_adapter(resume, self.access())
            self.assertTrue(result.ok, result.error)
            self.assertEqual(records[0]["token_in_env"], self.TOKEN)
            self.assertNotIn(self.TOKEN, json.dumps(records[0]["argv"]))
            self.assertNotIn(self.TOKEN, log)
            self.assertIn('mcp_servers.timetrace.bearer_token_env_var="TIMETRACE_MCP_TOKEN"', records[0]["argv"])
            self.assertEqual(("resume" in records[0]["argv"]), resume)

    def test_a_run_without_it_has_no_token(self):
        result, records, log = self.run_adapter(False, None)
        self.assertIsNone(records[0]["token_in_env"])
        self.assertFalse([a for a in records[0]["argv"] if "mcp_servers" in a])

    OLD = "ttm_SENTINEL_" + "Q1w2E3r4T5" * 4

    def assertNoTokens(self, text, *tokens):
        for token in tokens or (self.TOKEN, self.OLD):
            self.assertNotIn(token, text)
            self.assertNotIn(token[4:], text)

    def test_the_final_answer_file_the_tool_writes_never_keeps_the_token_however_the_run_ends(self):
        import threading
        cases = {
            "finished": dict(),
            "non-zero exit": dict(extra_env={"TT_FAKE_EXIT": "3"}),
            "cancelled while running": None,
            "the adapter raising": None,
        }
        for name, case in cases.items():
            with self.subTest(name):
                kept = []
                if name == "cancelled while running":
                    cancel = threading.Event()
                    timer = threading.Timer(1.0, cancel.set)
                    timer.start()
                    self.addCleanup(timer.cancel)
                    self.run_adapter(False, self.access(), extra_env={"TT_FAKE_SLEEP": "30"}, cancel=cancel, keep=kept)
                elif name == "the adapter raising":
                    from unittest.mock import patch
                    with patch.object(codex.CodexAdapter, "_run", side_effect=self._raise_after_the_tool_wrote_its_answer):
                        self.run_adapter(False, self.access(), keep=kept)
                else:
                    self.run_adapter(False, self.access(), keep=kept, **case)
                self.assertTrue(kept and kept[0] is not None, "the tool's own final-answer file was not there to be checked")
                self.assertNoTokens(kept[0])
                self.assertIn("REDACTED", kept[0])

    def _raise_after_the_tool_wrote_its_answer(self, cmd, cwd, log_file, cancel_event=None, env=None):
        # the real tool ran and wrote its answer (with the token), then the adapter failed
        import subprocess
        Path(log_file).touch()
        subprocess.run(cmd, cwd=cwd, env=dict(__import__("os").environ, **(env or {})), capture_output=True)
        raise RuntimeError("the adapter broke after the run")

    def test_a_continuation_with_a_new_token_leaves_neither_the_new_nor_the_old_one(self):
        kept = []
        result, records, log = self.run_adapter(True, self.access("ttm_SENTINEL_" + "Qq1Ww2Ee3R" * 4), extra_env={"TT_FAKE_PREV": self.TOKEN}, keep=kept)
        self.assertTrue(result.ok, result.error)
        new = "ttm_SENTINEL_" + "Qq1Ww2Ee3R" * 4
        self.assertEqual(records[0]["token_in_env"], new)
        for where in (log, kept[0], json.dumps(result.output), json.dumps(result.error)):
            self.assertNoTokens(where or "", new, self.TOKEN)

    def test_a_token_run_into_other_words_is_masked_in_the_log_and_the_result(self):
        kept = []
        result, records, log = self.run_adapter(False, self.access(), extra_env={"TT_FAKE_PREV": self.OLD}, keep=kept)
        self.assertNoTokens(log)
        self.assertIn("diagnosticprefix[REDACTED", log)
        self.assertIn("lastrun[REDACTED", log)

    def test_a_continuation_gets_the_new_token(self):
        _, first, _ = self.run_adapter(False, self.access())
        _, second, _ = self.run_adapter(True, self.access("ttm_SENTINEL_" + "Q1w2E3r4T5" * 4))
        self.assertNotEqual(first[0]["token_in_env"], second[0]["token_in_env"])


if __name__ == "__main__":
    unittest.main()
