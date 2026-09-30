"""review_turn (read-only AI acceptance review) and check (local command) jobs."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from timetrace import checks, results, worktree
from timetrace.adapters import claude, codex
from timetrace.adapters.base import REVIEW_RULES
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.dispatch import coding_slot_lock, tool_slot_lock
from timetrace.models import RunResult

BRANCH = "timetrace/s1/sub-0123abcd"


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid"] + list(args),
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def lease():
    return datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()


class Cloud:
    def __init__(self, job):
        self.job = job
        self.events = []

    def claim(self, token):
        job = {"id": "r1", "workspace_id": "ws1", "tool_profile_id": "codex-default", "provider": "codex",
               "prompt": "验收标准：必须有 app.py"}
        job.update(self.job)
        return {"job": job, "attempt_id": "a1", "lease_epoch": 1, "lease_expires_at": lease()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": lease()}

    def post_quota_samples(self, token, samples):
        return {}


class Reviewer:
    def __init__(self, reply='ok\n```json\n{"verdict": "pass", "reasons": ["有 app.py"]}\n```', result=None,
                 zero_spend=True):
        self.reply, self.result, self.zero_spend = reply, result, zero_spend
        self.calls = []

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": self.zero_spend}

    def review(self, prompt, cwd, log_file, cancel_event=None):
        self.calls.append({"prompt": prompt, "cwd": cwd, "files": sorted(os.listdir(cwd))})
        if self.result is not None:
            return self.result
        return RunResult(exit_code=0, ok=True, output=self.reply, session_id="s")

    def start(self, *args, **kwargs):
        raise AssertionError("a review must not start a task run")

    resume = chat = start


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "README.md").write_text("hello\n")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-qm", "initial", cwd=self.repo)
        git("checkout", "-qb", BRANCH, cwd=self.repo)
        (self.repo / "app.py").write_text("print('hi')\n")
        git("add", "app.py", cwd=self.repo)
        git("commit", "-qm", "add app", cwd=self.repo)
        self.step_commit = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.home = self.d / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, adapters=None, **kwargs):
        # Each run in a test is a new claim of job r1, not a duplicate.
        self.db.conn.execute("DELETE FROM remote_claim")
        self.db.conn.execute("DELETE FROM remote_outbox")
        return Agent(self.db, self.cloud, adapters or {}, self.home, lambda: "token",
                     prepare_workspace=self.no_worktree, **kwargs)

    @staticmethod
    def no_worktree(*args, **kwargs):
        raise AssertionError("no task worktree for this job")

    def assert_repo_untouched(self):
        self.assertEqual(git("status", "--porcelain", cwd=self.repo), "")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "main")
        self.assertEqual(git("rev-parse", BRANCH, cwd=self.repo), self.step_commit)
        listed = git("worktree", "list", "--porcelain", cwd=self.repo)
        self.assertEqual(listed.count("worktree "), 1, listed)


class ReviewTurnTest(Base):
    def run_review(self, reviewer=None, **job):
        self.cloud = Cloud(dict({"kind": "review_turn", "branch_name": BRANCH, "base_ref": "main"}, **job))
        self.reviewer = reviewer or Reviewer()
        outcome = self.agent({"codex": self.reviewer}).run_once()
        return outcome, (self.cloud.events[-1] if self.cloud.events else None)

    def test_reviews_a_detached_copy_of_the_step_branch(self):
        outcome, done = self.run_review()
        self.assertEqual(outcome, "job r1 → reviewed (pass)")
        call = self.reviewer.calls[0]
        self.assertIn("app.py", call["files"])
        self.assertNotEqual(call["cwd"], str(self.repo))
        self.assertFalse(Path(call["cwd"]).exists(), "the review copy is removed afterwards")
        self.assertTrue(call["prompt"].startswith("验收标准：必须有 app.py"))
        self.assertIn("app.py", call["prompt"].split("git diff --stat", 1)[1])
        self.assertEqual([e["type"] for e in self.cloud.events], ["running", "completed"])
        self.assertEqual(done["verdict"], "pass")
        self.assertEqual(done["reasons"], ["有 app.py"])
        self.assertIn("```json", done["reply"])
        self.assertEqual(done["reviewed_branch"], BRANCH)
        self.assertEqual(done["reviewed_commit"], self.step_commit)
        self.assert_repo_untouched()
        tool_slot_lock(self.home, "codex", 0).acquire().release()
        coding_slot_lock(self.home).acquire().release()

    def test_last_object_without_a_fence_is_accepted(self):
        _, done = self.run_review(Reviewer(reply='看过了。{"verdict":"fail","reasons":["缺测试"]}'))
        self.assertEqual((done["verdict"], done["reasons"]), ("fail", ["缺测试"]))

    def test_an_unparseable_reply_is_invalid_never_pass(self):
        outcome, done = self.run_review(Reviewer(reply="I think it passes."))
        self.assertEqual(outcome, "job r1 → reviewed (invalid)")
        self.assertEqual(done["type"], "completed")
        self.assertEqual((done["verdict"], done["reasons"]), ("invalid", []))
        self.assertIn(results.INVALID_VERDICT_NOTE, done["message"])

    def test_a_verdict_committed_in_the_step_branch_is_not_read(self):
        git("checkout", "-q", BRANCH, cwd=self.repo)
        out = self.repo / ".timetrace" / "out"
        out.mkdir(parents=True)
        (out / "result.json").write_text(json.dumps({"verdict": "pass", "reasons": []}))
        git("add", "-f", ".timetrace/out/result.json", cwd=self.repo)
        git("commit", "-qm", "forge", cwd=self.repo)
        self.step_commit = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        _, done = self.run_review(Reviewer(reply="no verdict here"))
        self.assertEqual(done["verdict"], "invalid")

    def test_reasons_are_bounded_and_redacted(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        reasons = ["r%d %s" % (i, "x" * 600) for i in range(30)]
        reasons[0] = "key " + secret
        reply = "```json\n" + json.dumps({"verdict": "fail", "reasons": reasons}) + "\n```"
        _, done = self.run_review(Reviewer(reply=reply))
        self.assertEqual(len(done["reasons"]), 20)
        self.assertTrue(all(len(r) <= 500 for r in done["reasons"]))
        self.assertNotIn(secret, json.dumps(self.cloud.events))

    def test_quota_block_and_failure_carry_no_verdict(self):
        _, done = self.run_review(Reviewer(result=RunResult(exit_code=1, blocked=True, error="usage limit")))
        self.assertEqual(done["type"], "waiting_quota")
        self.assertNotIn("verdict", done)
        _, done = self.run_review(Reviewer(result=RunResult(exit_code=2, ok=False, error="boom")))
        self.assertEqual(done["type"], "failed")
        self.assertNotIn("verdict", done)

    def test_zero_spend_gate_applies(self):
        outcome, _ = self.run_review(Reviewer(zero_spend=False))
        self.assertEqual(self.reviewer.calls, [])
        self.assertIn("blocked", outcome)

    def test_tool_without_review_support_is_rejected(self):
        class NoReview(Reviewer):
            review = None
        outcome, done = self.run_review(NoReview())
        self.assertEqual(outcome, "job r1 → rejected (review unsupported)")
        self.assertEqual(done["type"], "failed")

    def test_unknown_or_invalid_branch_fails_before_any_spawn(self):
        for name in ("timetrace/nope", "main", "--output=/tmp/x", None):
            with self.subTest(name=name):
                outcome, done = self.run_review(branch_name=name)
                self.assertEqual(self.reviewer.calls, [])
                self.assertEqual(done["type"], "failed")
                self.assertIn("本机找不到要复核的分支", done["message"])

    def test_branch_found_from_the_step_job(self):
        # An older step's completion did not report its branch: the
        # requested name plus the step job's suffix finds it.
        unique = worktree.unique_branch("timetrace/s1/other", "job-7")
        git("branch", unique, BRANCH, cwd=self.repo)
        outcome, done = self.run_review(branch_name="timetrace/s1/other", source_job_id="job-7")
        self.assertEqual(outcome, "job r1 → reviewed (pass)")
        self.assertEqual(done["reviewed_branch"], unique)

    def test_option_like_base_falls_back_to_the_default_branch(self):
        _, done = self.run_review(base_ref="--output=/tmp/timetrace-x")
        self.assertIn("app.py", self.reviewer.calls[0]["prompt"])
        self.assertFalse(Path("/tmp/timetrace-x").exists())

    def test_diff_summary_is_bounded(self):
        git("checkout", "-q", BRANCH, cwd=self.repo)
        for i in range(400):
            (self.repo / ("file_with_a_rather_long_name_%04d.txt" % i)).write_text("x\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "many", cwd=self.repo)
        self.step_commit = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        self.run_review()
        summary = self.reviewer.calls[0]["prompt"].split("git diff --stat", 1)[1]
        self.assertLessEqual(len(summary.encode("utf-8")), worktree.DIFF_STAT_BYTES + 400)

    def test_folder_workspace_reviews_the_output_directory(self):
        footage = self.d / "footage"
        (footage / "timetrace-out" / "cut").mkdir(parents=True)
        (footage / "timetrace-out" / "cut" / "edit.txt").write_text("v1")
        self.db.upsert_workspace("ws1", "footage", str(footage), "", kind="folder")
        outcome, done = self.run_review(output_name="cut")
        self.assertEqual(outcome, "job r1 → reviewed (pass)")
        self.assertEqual(self.reviewer.calls[0]["cwd"], str((footage / "timetrace-out" / "cut").resolve()))
        self.assertIn("edit.txt", self.reviewer.calls[0]["prompt"])
        self.assertEqual(done["reviewed_output"], "timetrace-out/cut")
        outcome, done = self.run_review(output_name="missing")
        self.assertEqual(done["type"], "failed")

    def test_stop_during_review_is_reported_and_cleans_up(self):
        entered = threading.Event()

        class Slow(Reviewer):
            def review(self, prompt, cwd, log_file, cancel_event=None):
                self.calls.append({"prompt": prompt, "cwd": cwd, "files": []})
                entered.set()
                cancel_event.wait(10)
                return RunResult(exit_code=-15, ok=False, error="terminated")
        self.cloud = Cloud({"kind": "review_turn", "branch_name": BRANCH})
        reviewer = Slow()
        agent = self.agent({"codex": reviewer})
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(agent.run_once()))
        worker.start()
        self.assertTrue(entered.wait(10))
        agent.stop()
        worker.join(10)
        self.assertEqual(outcome, ["job r1 → blocked (runner_stopped)"])
        self.assertIn("runner_stopped", self.cloud.events[-1]["message"])
        self.assertFalse(Path(reviewer.calls[0]["cwd"]).exists())
        self.assert_repo_untouched()


class CheckJobTest(Base):
    def run_check(self, name="unit", agent_kwargs=None, **job):
        self.cloud = Cloud(dict({"kind": "check", "check_name": name, "branch_name": BRANCH,
                                 "provider": "", "tool_profile_id": ""}, **job))
        outcome = self.agent(**(agent_kwargs or {})).run_once()
        return outcome, (self.cloud.events[-1] if self.cloud.events else None)

    def test_runs_the_registered_argv_in_a_detached_copy_of_the_branch(self):
        marker = self.d / "cwd.txt"
        self.db.save_check("ws1", "unit", ["sh", "-c", 'test -f app.py && pwd > "$0" && echo checked', str(marker)])
        outcome, done = self.run_check()
        self.assertEqual(outcome, "job r1 → check passed")
        self.assertEqual([e["type"] for e in self.cloud.events], ["running", "completed"])
        result = done["check_result"]
        self.assertEqual(result["name"], "unit")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("checked", result["output_tail"])
        self.assertNotIn("$ sh", result["output_tail"], "the command line stays on this computer")
        self.assertIsInstance(result["duration_seconds"], float)
        self.assertFalse(result["timed_out"])
        self.assertEqual(done["verdict"], "pass")
        cwd = marker.read_text().strip()
        self.assertNotEqual(os.path.realpath(cwd), str(self.repo))
        self.assertFalse(Path(cwd).exists(), "the check copy is removed afterwards")
        self.assert_repo_untouched()
        coding_slot_lock(self.home).acquire().release()

    def test_claim_without_prompt_as_valley_sends_it(self):
        # Valley omits `prompt` from a check claim (nothing from the server is
        # a command); the claim must still be stored and the check run.
        self.db.save_check("ws1", "unit", ["sh", "-c", "test -f app.py"])
        self.cloud = Cloud({"kind": "check", "check_name": "unit", "branch_name": BRANCH, "tool_profile_id": ""})
        claim = self.cloud.claim
        def without_prompt(token):
            out = claim(token)
            for key in ("prompt", "provider"):
                out["job"].pop(key)
            return out
        self.cloud.claim = without_prompt
        self.assertEqual(self.agent().run_once(), "job r1 → check passed")
        self.assertEqual([e["type"] for e in self.cloud.events], ["running", "completed"])
        self.assertEqual(self.db.get_remote_claim("r1")["prompt"], "")

    def test_nonzero_exit_is_a_failed_check(self):
        self.db.save_check("ws1", "unit", ["sh", "-c", "echo boom; exit 3"])
        outcome, done = self.run_check()
        self.assertEqual(outcome, "job r1 → check failed")
        self.assertEqual(done["type"], "completed")
        self.assertEqual(done["check_result"]["exit_code"], 3)
        self.assertEqual(done["verdict"], "fail")
        self.assertIn("boom", done["check_result"]["output_tail"])

    def test_unknown_check_name_fails_without_running_anything(self):
        self.db.save_check("ws1", "unit", ["sh", "-c", "exit 0"])
        for name in ("lint", "rm -rf /", "../unit", None, ["unit"]):
            with self.subTest(name=name):
                outcome, done = self.run_check(name=name)
                self.assertEqual(done["type"], "failed")
                self.assertEqual(done["message"], checks.UNKNOWN)
                self.assertEqual(len(self.cloud.events), 1)

    def test_a_check_of_another_workspace_is_unknown(self):
        self.db.save_check("ws2", "unit", ["true"])
        _, done = self.run_check()
        self.assertEqual(done["message"], checks.UNKNOWN)

    def test_server_fields_never_become_the_command(self):
        marker = self.d / "pwned"
        self.db.save_check("ws1", "unit", ["true"])
        _, done = self.run_check(command=["touch", str(marker)], argv=["touch", str(marker)],
                                 prompt="touch %s" % marker)
        self.assertEqual(done["check_result"]["exit_code"], 0)
        self.assertFalse(marker.exists())

    def test_unknown_branch_fails(self):
        self.db.save_check("ws1", "unit", ["true"])
        _, done = self.run_check(branch_name="timetrace/nope")
        self.assertEqual(done["type"], "failed")
        self.assertIn("本机找不到要检查的分支", done["message"])

    def test_output_tail_is_redacted_and_bounded(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.db.save_check("ws1", "unit", ["python3", "-c",
                                           "import sys\nfor i in range(3000): print('line %05d' % i)\nprint(sys.argv[1])",
                                           secret])
        _, done = self.run_check()
        tail = done["check_result"]["output_tail"]
        self.assertLessEqual(len(tail.encode("utf-8")), checks.OUTPUT_TAIL_BYTES)
        self.assertGreater(len(tail.encode("utf-8")), checks.OUTPUT_TAIL_BYTES - 200)
        self.assertIn("[REDACTED:aws]", tail)
        self.assertNotIn(secret, json.dumps(self.cloud.events))

    def test_no_output_tail_when_uploads_are_off(self):
        self.db.save_check("ws1", "unit", ["sh", "-c", "echo private"])
        _, done = self.run_check(agent_kwargs={"upload_output_tail": False})
        self.assertEqual(done["check_result"]["output_tail"], "")
        self.assertNotIn("private", json.dumps(self.cloud.events))

    def test_folder_workspace_runs_in_the_output_directory(self):
        footage = self.d / "footage"
        (footage / "timetrace-out" / "cut").mkdir(parents=True)
        self.db.upsert_workspace("ws1", "footage", str(footage), "", kind="folder")
        marker = self.d / "cwd.txt"
        self.db.save_check("ws1", "unit", ["sh", "-c", 'pwd > "$0"', str(marker)])
        outcome, _ = self.run_check(output_name="cut", branch_name=None)
        self.assertEqual(outcome, "job r1 → check passed")
        self.assertEqual(os.path.realpath(marker.read_text().strip()),
                         str((footage / "timetrace-out" / "cut").resolve()))

    def test_stop_kills_the_check_and_reports_it(self):
        self.db.save_check("ws1", "unit", ["sleep", "30"])
        self.cloud = Cloud({"kind": "check", "check_name": "unit", "branch_name": BRANCH})
        agent = self.agent()
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(agent.run_once()))
        worker.start()
        for _ in range(100):
            if any(e["type"] == "running" for e in self.cloud.events):
                break
            time.sleep(.05)
        started = time.monotonic()
        agent.stop()
        worker.join(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(outcome, ["job r1 → blocked (runner_stopped)"])
        self.assert_repo_untouched()
        coding_slot_lock(self.home).acquire().release()

    def test_cancelled_or_expired_lease_never_runs(self):
        marker = self.d / "ran"
        self.db.save_check("ws1", "unit", ["touch", str(marker)])
        outcome, done = self.run_check(desired_action="cancel")
        self.assertEqual(done["type"], "cancelled")
        self.assertFalse(marker.exists())


class CheckRunTest(unittest.TestCase):
    def test_ai_credentials_and_secret_names_are_stripped(self):
        env = {"ANTHROPIC_API_KEY": "a", "ANTHROPIC_BASE_URL": "b", "OPENAI_API_KEY": "c",
               "OPENAI_ORG": "d", "CLAUDE_CODE_OAUTH": "e", "CODEX_HOME": "f", "MY_TOKEN": "g",
               "DB_PASSWORD": "h", "EXTRA_THING": "i", "PLAIN_VAR": "j", "PATH": os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, env):
            log = str(Path(d) / "log")
            result = checks.run("env", ["env"], d, log, extra_drop=["EXTRA_THING"], keep=["DB_PASSWORD", "OPENAI_ORG"])
            names = {line.split("=", 1)[0] for line in result.output_tail.splitlines() if "=" in line}
        self.assertIn("PLAIN_VAR", names)
        self.assertIn("DB_PASSWORD", names, "kept on request")
        for gone in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_ORG",
                     "CLAUDE_CODE_OAUTH", "CODEX_HOME", "MY_TOKEN", "EXTRA_THING"):
            self.assertNotIn(gone, names)

    def test_timeout_kills_the_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            started = time.monotonic()
            result = checks.run("slow", ["sleep", "30"], d, str(Path(d) / "log"), timeout=.5)
            self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)
        self.assertIn("超时", result.output_tail)

    def test_missing_program_is_exit_127_without_naming_it(self):
        with tempfile.TemporaryDirectory() as d:
            result = checks.run("x", ["/nonexistent/secret-tool-name"], d, str(Path(d) / "log"))
        self.assertEqual(result.exit_code, 127)
        self.assertNotIn("secret-tool-name", result.output_tail)

    def test_timeout_is_thirty_minutes(self):
        self.assertEqual(checks.TIMEOUT_SECONDS, 1800)


class VerdictParseTest(unittest.TestCase):
    def test_shapes(self):
        cases = [
            ('```json\n{"verdict":"PASS","reasons":[]}\n```', ("pass", [])),
            ('x ```json\n{"verdict":"fail"}\n``` y ```json\n{"verdict":"pass","reasons":["ok"]}\n```', ("pass", ["ok"])),
            ('{"verdict":"fail","reasons":["a"]} trailing', ("fail", ["a"])),
            ('{"a": {"verdict":"pass"}}', ("pass", [])),
            ('{"verdict":"maybe"}', ("invalid", [])),
            ('{"verdict":"pass","reasons":"all good"}', ("invalid", [])),
            ('{"verdict":"pass","reasons":[1]}', ("invalid", [])),
            ('', ("invalid", [])),
            (None, ("invalid", [])),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                found = results.parse_verdict(text)
                self.assertEqual((found["verdict"], found["reasons"]), expected)


class ReviewArgvTest(unittest.TestCase):
    def test_claude_review_is_a_plan_mode_chat_without_mcp(self):
        seen = {}

        def fake(cmd, cwd, log_file, **kwargs):
            seen["cmd"] = cmd
            return 0, ['{"type":"result","subtype":"success","result":"{\\"verdict\\":\\"pass\\"}","session_id":"s"}']
        cfg = {"bin": "claude", "allowed_tools": ["Bash(rm:*)"], "extra_args": ["--dangerously-skip-permissions"],
               "permission_mode": "bypassPermissions"}
        with tempfile.TemporaryDirectory() as d, patch.object(claude, "run_streaming", fake):
            res = claude.ClaudeAdapter(cfg, billing=object(), credentials=lambda: {}).review(
                "-p review", d, str(Path(d) / "log"))
        cmd = seen["cmd"]
        self.assertTrue(res.ok)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], REVIEW_RULES)
        for widening in ("--allowedTools", "--dangerously-skip-permissions", "bypassPermissions"):
            self.assertNotIn(widening, cmd)
        self.assertEqual(cmd[-1], " -p review")

    def test_codex_review_is_read_only(self):
        seen = {}

        def fake(cmd, cwd, log_file, **kwargs):
            seen["cmd"] = cmd
            return 0, ['{"type":"thread.started","thread_id":"t"}', '{"type":"turn.completed"}']
        cfg = {"bin": "codex", "sandbox": "danger-full-access", "extra_args": ["--yolo"]}
        with tempfile.TemporaryDirectory() as d, patch.object(codex, "run_streaming", fake):
            codex.CodexAdapter(cfg, billing=object()).review("review", d, str(Path(d) / "log"))
        cmd = seen["cmd"]
        self.assertNotIn("resume", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertNotIn("--yolo", cmd)
        self.assertNotIn("danger-full-access", " ".join(cmd))
        self.assertTrue(cmd[-1].startswith(REVIEW_RULES))


class TaskBranchReportTest(unittest.TestCase):
    def test_completed_task_reports_its_branch(self):
        from tests.test_parallel import BarrierAdapter, QueueCloud, init_repo
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            repo = d / "repo"; init_repo(repo)
            db = Database(d / "timetrace.db")
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/s1/ui"}])
            Agent(db, cloud, {"codex": BarrierAdapter(parties=1)}, d / "home", lambda: "t").run_once()
            done = [e for _, e in cloud.events if e["type"] == "completed"][0]
            self.assertEqual(done["branch"], worktree.unique_branch("timetrace/s1/ui", "j1"))


if __name__ == "__main__":
    unittest.main()
