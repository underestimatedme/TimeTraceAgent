"""dev_status: read-only development facts (local git, documents, GitHub PRs)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from timetrace import dev_status
from timetrace.agent import Agent
from timetrace.db import Database
from tests.test_review_and_check import Cloud, git

SECRET = "ghp_" + "A" * 36


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "handoff.md").write_text("# 交接\n目标：提醒功能\n令牌 %s 不应外传\n" % SECRET)
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "docs", cwd=self.repo)
        (self.repo / "app.py").write_text("print(1)\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "app", cwd=self.repo)
        git("commit", "--allow-empty", "-qm", "more", cwd=self.repo)
        git("remote", "add", "origin", "https://someone:%s@github.com/Owner/sleep-app.git" % SECRET, cwd=self.repo)
        self.head = git("rev-parse", "HEAD", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()


class LocalGitTest(Repo):
    def test_reads_branch_head_dirty_files_and_a_credential_free_remote(self):
        (self.repo / "notes.txt").write_text("x")
        (self.repo / "app.py").write_text("print(2)\n")
        facts = dev_status.local_git(str(self.repo))
        self.assertEqual(facts["status"], "ok")
        self.assertEqual(facts["branch"], "main")
        self.assertEqual(facts["head"], self.head)
        self.assertEqual(facts["remote"], "github.com/Owner/sleep-app")
        self.assertTrue(facts["dirty"])
        self.assertEqual(facts["changed_count"], 2)
        self.assertEqual(sorted(f["path"] for f in facts["changed_files"]), ["app.py", "notes.txt"])
        self.assertNotIn(SECRET, json.dumps(facts))
        # Reading changed nothing.
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repo), self.head)

    def test_ssh_and_missing_remotes(self):
        self.assertEqual(dev_status.sanitize_remote("git@github.com:o/r.git"), "github.com/o/r")
        self.assertEqual(dev_status.sanitize_remote("ssh://git@github.com/o/r"), "github.com/o/r")
        self.assertEqual(dev_status.sanitize_remote("/local/path"), "")
        git("remote", "remove", "origin", cwd=self.repo)
        self.assertEqual(dev_status.local_git(str(self.repo))["remote"], "")

    def test_status_failure_never_reports_a_clean_repository(self):
        for exit_code in (1, 124):
            with self.subTest(exit_code=exit_code):
                def failing_status(argv, **kwargs):
                    if "status" in argv:
                        return exit_code, "", "private path or credential"
                    return dev_status.run(argv, **kwargs)
                facts = dev_status.local_git(str(self.repo), run=failing_status)
                self.assertEqual(facts["status"], "failed")
                self.assertNotIn("dirty", facts)
                self.assertNotIn("private path", json.dumps(facts))

    def test_not_a_repository_is_a_failure_not_empty_facts(self):
        facts = dev_status.local_git(str(self.d))
        self.assertEqual(facts["status"], "failed")


class DocTest(Repo):
    def test_document_digest_excerpt_and_staleness(self):
        facts = dev_status.doc_facts(str(self.repo), "docs/handoff.md")
        self.assertEqual(facts["status"], "ok")
        self.assertEqual(facts["path"], "docs/handoff.md")
        self.assertEqual(len(facts["sha256"]), 64)
        self.assertEqual(facts["commits_since"], 2)
        self.assertFalse(facts["dirty"])
        self.assertIn("目标：提醒功能", facts["excerpt"])
        self.assertNotIn(SECRET, facts["excerpt"])

    def test_git_failure_never_reports_fresh_or_clean_document(self):
        for command in ("log", "rev-list", "status"):
            with self.subTest(command=command):
                def failing_git(argv, **kwargs):
                    if command in argv:
                        return 124, "", "private path or credential"
                    return dev_status.run(argv, **kwargs)
                facts = dev_status.doc_facts(str(self.repo), "docs/handoff.md", run=failing_git)
                self.assertEqual(facts["status"], "failed")
                self.assertNotIn("dirty", facts)
                self.assertNotIn("commits_since", facts)
                self.assertNotIn("private path", json.dumps(facts))

    def test_paths_outside_or_sensitive_are_not_read(self):
        (self.d / "outside.md").write_text("outside")
        os.symlink(str(self.d / "outside.md"), str(self.repo / "docs" / "link.md"))
        (self.repo / ".env").write_text("TOKEN=x")
        for rel in ["../outside.md", "/etc/hosts", "docs/link.md", ".env", "docs/../../outside.md", "config/app.key"]:
            facts = dev_status.doc_facts(str(self.repo), rel)
            self.assertNotEqual(facts["status"], "ok", rel)
            self.assertNotIn("excerpt", facts, rel)
        self.assertEqual(dev_status.doc_facts(str(self.repo), "docs/missing.md")["status"], "unavailable")

    def test_uncommitted_document(self):
        (self.repo / "docs" / "new.md").write_text("草稿")
        facts = dev_status.doc_facts(str(self.repo), "docs/new.md")
        self.assertEqual(facts["status"], "ok")
        self.assertEqual(facts["last_commit"], "")
        self.assertTrue(facts["dirty"])


class FakeGH:
    """Answers the fixed gh invocations; records every argv."""

    def __init__(self, installed=True, authed=True, pr=None, runs=None, pr_error=None, run_list=None):
        self.installed, self.authed, self.pr, self.runs, self.pr_error = installed, authed, pr, runs or {}, pr_error
        self.run_list = run_list or []
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None, env=None):
        self.calls.append(list(argv))
        if argv[0] != "gh":
            raise AssertionError("unexpected command %r" % argv)
        if not self.installed:
            raise FileNotFoundError("gh")
        if argv[1] == "--version":
            return 0, "gh version 2.60.0", ""
        if argv[1:3] == ["auth", "status"]:
            return (0, "", "") if self.authed else (1, "", "You are not logged into any GitHub hosts")
        if argv[1:3] == ["pr", "view"]:
            if self.pr_error:
                return 1, "", self.pr_error
            return 0, json.dumps(self.pr), ""
        if argv[1:3] == ["run", "list"]:
            return 0, json.dumps([{"databaseId": i} for i in self.run_list]), ""
        if argv[1:3] == ["run", "view"]:
            run_id = int(argv[3])
            if "--log-failed" in argv:
                return 0, "test.go:12 FAIL\n" * 3 + "token " + SECRET, ""
            return 0, json.dumps(self.runs[run_id]), ""
        if argv[1] == "api":
            return 0, ".github/workflows/ci.yml\n", ""
        raise AssertionError("unexpected gh call %r" % argv)


PR_URL = "https://github.com/Owner/sleep-app/pull/7"
HEAD = "a" * 40


def gh_pr(rollup):
    return {"number": 7, "state": "OPEN", "isDraft": False, "title": "提醒", "url": PR_URL, "headRefName": "feat/reminder",
            "headRefOid": HEAD, "baseRefName": "main", "baseRefOid": "b" * 40, "updatedAt": "2026-10-15T10:00:00Z",
            "statusCheckRollup": rollup}


def gh_run(run_id, head, conclusion, steps):
    return {"databaseId": run_id, "headSha": head, "event": "pull_request", "status": "completed", "conclusion": conclusion,
            "workflowName": "CI", "url": "https://github.com/Owner/sleep-app/actions/runs/%d" % run_id,
            "jobs": [{"name": "build", "conclusion": conclusion,
                      "steps": [{"name": n, "conclusion": c, "status": "completed"} for n, c in steps]}]}


class PRTest(unittest.TestCase):
    def test_reads_pr_head_base_and_runs_with_steps(self):
        rollup = [{"__typename": "CheckRun", "name": "build", "detailsUrl": "https://github.com/Owner/sleep-app/actions/runs/11/job/99"}]
        gh = FakeGH(pr=gh_pr(rollup), run_list=[11, 10], runs={
            11: gh_run(11, HEAD, "failure", [("flutter test", "success"), ("Upload artifact", "failure")]),
            10: gh_run(10, "e" * 40, "success", [("flutter test", "success")]),
        })
        [entry] = dev_status.pr_facts([PR_URL], run=gh)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["url"], PR_URL)
        self.assertEqual(entry["pr"]["head_sha"], HEAD)
        self.assertEqual(entry["pr"]["base_ref"], "main")
        self.assertEqual([r["id"] for r in entry["runs"]], [11, 10])
        failed = entry["runs"][0]
        self.assertEqual(failed["workflow_path"], ".github/workflows/ci.yml")
        self.assertEqual(failed["jobs"][0]["steps"][1], {"name": "Upload artifact", "conclusion": "failure"})
        self.assertIn("FAIL", failed["failure_excerpt"])
        self.assertNotIn(SECRET, json.dumps(entry))
        self.assertNotIn("failure_excerpt", entry["runs"][1])
        # Only fixed read-only gh subcommands; the URL is an argument.
        for argv in gh.calls:
            self.assertIn(argv[1], ("--version", "auth", "pr", "run", "api"))
            self.assertNotIn("edit", argv)
            self.assertNotIn("merge", argv)
        self.assertIn(["gh", "pr", "view", PR_URL], [c[:4] for c in gh.calls])

    def test_missing_unauthorized_and_failures_are_explicit(self):
        [e] = dev_status.pr_facts([PR_URL], run=FakeGH(installed=False))
        self.assertEqual((e["status"], e["url"]), ("unavailable", PR_URL))
        self.assertIn("gh", e["message"])
        [e] = dev_status.pr_facts([PR_URL], run=FakeGH(authed=False))
        self.assertEqual(e["status"], "unauthorized")
        [e] = dev_status.pr_facts([PR_URL], run=FakeGH(pr_error="HTTP 403: API rate limit exceeded"))
        self.assertEqual(e["status"], "unavailable")
        self.assertIn("限流", e["message"])
        [e] = dev_status.pr_facts([PR_URL], run=FakeGH(pr_error="GraphQL: Could not resolve to a PullRequest"))
        self.assertEqual(e["status"], "failed")
        [e] = dev_status.pr_facts(["https://github.com/o/r/pull/1; rm -rf /"], run=FakeGH(pr=gh_pr([])))
        self.assertEqual(e["status"], "failed")

    def test_no_prs_needs_no_gh(self):
        gh = FakeGH(installed=False)
        self.assertEqual(dev_status.pr_facts([], run=gh), [])
        self.assertEqual(gh.calls, [])


class JobTest(Repo):
    def test_job_reports_facts_and_leaves_the_repository_alone(self):
        db = Database(self.d / "timetrace.db")
        db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        spec = {"prs": [], "docs": ["docs/handoff.md", "../escape.md"]}
        cloud = Cloud({"kind": "dev_status", "prompt": json.dumps(spec), "provider": "", "tool_profile_id": ""})
        agent = Agent(db, cloud, {}, self.d / "home", lambda: "token",
                      prepare_workspace=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no worktree")))
        outcome = agent.run_once()
        self.assertEqual(outcome, "job r1 → dev status collected")
        self.assertEqual([e["type"] for e in cloud.events], ["running", "completed"])
        facts = cloud.events[-1]["dev_status"]
        self.assertEqual(facts["local_git"]["head"], self.head)
        docs = {d["path"]: d for d in facts["docs"]}
        self.assertEqual(docs["docs/handoff.md"]["status"], "ok")
        self.assertNotEqual(docs["../escape.md"]["status"], "ok")
        self.assertEqual(facts["prs"], [])
        self.assertNotIn(SECRET, json.dumps(cloud.events))
        self.assertEqual(git("status", "--porcelain", cwd=self.repo), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repo), self.head)

    def test_malformed_spec_is_rejected(self):
        db = Database(self.d / "timetrace.db")
        db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        cloud = Cloud({"kind": "dev_status", "prompt": "not json", "provider": "", "tool_profile_id": ""})
        agent = Agent(db, cloud, {}, self.d / "home", lambda: "token")
        self.assertIn("rejected", agent.run_once())
        self.assertEqual(cloud.events[-1]["type"], "failed")


if __name__ == "__main__":
    unittest.main()
