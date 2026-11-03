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


if __name__ == "__main__":
    unittest.main()


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
import json, os, sys
with open(os.environ["TT_FAKE_TRANSCRIPT"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "token_in_env": os.environ.get("TIMETRACE_MCP_TOKEN")}) + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
print(json.dumps({"type": "turn.completed"}))
''')
        script.chmod(0o755)
        return str(script)

    def run_adapter(self, resume, mcp):
        import os, tempfile
        from unittest.mock import patch
        from timetrace.billing import StaticBilling
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "t.jsonl"
            adapter = codex.CodexAdapter({"bin": self.fake_codex(d), "sandbox": "workspace-write"}, billing=StaticBilling(True))
            before = dict(os.environ)
            with patch.dict(os.environ, {"TT_FAKE_TRANSCRIPT": str(transcript)}):
                with patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                    run = adapter.resume if resume else adapter.start
                    result = run("go", d, "tid", str(Path(d) / "run.log"), None, mcp=mcp)
            records = [json.loads(line) for line in transcript.read_text().splitlines()]
            log = (Path(d) / "run.log").read_text()
            after = dict(os.environ)
            before.pop("TT_FAKE_TRANSCRIPT", None); after.pop("TT_FAKE_TRANSCRIPT", None)
            self.assertEqual(before, after, "the process's own environment changed")
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

    def test_a_continuation_gets_the_new_token(self):
        _, first, _ = self.run_adapter(False, self.access())
        _, second, _ = self.run_adapter(True, self.access("ttm_SENTINEL_" + "Q1w2E3r4T5" * 4))
        self.assertNotEqual(first[0]["token_in_env"], second[0]["token_in_env"])
