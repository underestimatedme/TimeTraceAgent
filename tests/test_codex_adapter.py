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
