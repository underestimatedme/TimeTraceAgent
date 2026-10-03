"""Structured results: `.timetrace/out/result.json` uploaded with the completion event."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from timetrace import results, worktree
from timetrace.adapters.base import SAFETY_RULES
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.models import RunResult

SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@example.invalid", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "README.md").write_text("hi\n")
    git("add", "README.md", cwd=path)
    git("commit", "-qm", "init", cwd=path)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wt"
        self.root.mkdir()
        (self.root / ".timetrace" / "out").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, value):
        data = value if isinstance(value, (bytes, str)) else json.dumps(value)
        path = self.root / ".timetrace" / "out" / "result.json"
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")

    def collect(self):
        return results.collect(str(self.root), head=lambda: "abc1234")

    def test_absent_file_means_no_structured_result(self):
        self.assertIsNone(self.collect())

    def test_valid_result_keeps_only_known_keys_and_redacts_strings(self):
        self.write({"artifacts": [{"kind": "link", "ref": "https://example.com/pr/1", "extra": "dropped"},
                                  {"kind": "note", "ref": "note", "content": "token " + SECRET}],
                    "pipeline_draft": {"title": "t", "stages": [{"key": "a", "brief": "use " + SECRET}]},
                    "unknown": {"x": 1}})
        got = self.collect()
        self.assertTrue(got["valid"])
        self.assertEqual(got["artifacts"][0], {"kind": "link", "ref": "https://example.com/pr/1"})
        self.assertNotIn(SECRET, json.dumps(got))
        self.assertIn("[REDACTED:aws]", got["artifacts"][1]["content"])
        self.assertEqual(set(got["result"]), {"pipeline_draft"})
        self.assertIn("[REDACTED:aws]", got["result"]["pipeline_draft"]["stages"][0]["brief"])

    def test_result_without_pipeline_draft_has_no_result(self):
        self.write({"artifacts": []})
        got = self.collect()
        self.assertTrue(got["valid"])
        self.assertEqual(got["artifacts"], [])
        self.assertIsNone(got["result"])

    def test_invalid_inputs(self):
        cases = {
            "oversize": json.dumps({"artifacts": [], "pad": "x" * (64 * 1024)}),
            "not json": "{broken",
            "not utf-8": b"\xff\xfe{}",
            "top-level list": json.dumps([{"kind": "doc", "ref": "a"}]),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                self.write(data)
                got = self.collect()
                self.assertFalse(got["valid"])
                self.assertEqual(got["artifacts"], [])
                self.assertIsNone(got["result"])

    def test_each_bad_artifact_is_dropped_alone(self):
        cases = {
            "artifacts not a list": {"kind": "doc"},
            "unknown kind": [{"kind": "exe", "ref": "a"}],
            "missing ref": [{"kind": "doc"}],
            "ref too long": [{"kind": "link", "ref": "r" * 513}],
            "content not text": [{"kind": "note", "ref": "n", "content": 5}],
            "bad commit sha": [{"kind": "commit", "ref": "c", "commit_sha": "zz"}],
            "empty string": [" "],
        }
        for name, bad in cases.items():
            with self.subTest(name=name):
                self.write({"artifacts": bad, "pipeline_draft": {"title": "t"}})
                got = self.collect()
                self.assertTrue(got["valid"])
                self.assertEqual(got["artifacts"], [])
                self.assertEqual(got["dropped_artifacts"], 1)
                self.assertEqual(got["result"], {"pipeline_draft": {"title": "t"}})

    def test_a_draft_that_is_not_an_object_is_dropped_not_fatal(self):
        # Real breakdown output: a one-line "draft" string next to valid sub-tasks.
        self.write({"pipeline_draft": "a ∥ b → c", "subtasks": [{"key": "a", "title": "A"}]})
        got = self.collect()
        self.assertTrue(got["valid"])
        self.assertEqual(set(got["result"]), {"subtasks"})
        self.assertEqual(got["dropped_artifacts"], 0)
        self.assertEqual(got["dropped_draft"], True)

    def test_a_path_string_is_read_as_a_doc_artifact(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "req.md").write_text("# req\n")
        self.write({"artifacts": ["docs/req.md", {"kind": "link", "ref": "https://example.com"}]})
        got = self.collect()
        self.assertTrue(got["valid"])
        self.assertEqual(got["artifacts"], [{"kind": "doc", "ref": "docs/req.md", "content": "# req\n", "commit_sha": "abc1234"},
                                            {"kind": "link", "ref": "https://example.com"}])
        self.assertEqual(got["dropped_artifacts"], 0)

    def test_ref_at_the_limit_is_valid(self):
        self.write({"artifacts": [{"kind": "link", "ref": "r" * 512}]})
        self.assertTrue(self.collect()["valid"])

    def test_symlinked_result_is_never_followed(self):
        outside = Path(self.tmp.name) / "secret.json"
        outside.write_text(json.dumps({"artifacts": [{"kind": "note", "ref": SECRET}]}))
        os.symlink(outside, self.root / ".timetrace" / "out" / "result.json")
        got = self.collect()
        self.assertFalse(got["valid"])
        self.assertNotIn(SECRET, json.dumps(got))

    def test_doc_inside_the_repo_gets_content_and_head(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "req.md").write_text("# 需求\npassword = " + "hunter2hunter2\n" + "刻" * 30000)
        self.write({"artifacts": [{"kind": "doc", "ref": "docs/req.md", "content": "model's copy"}]})
        art = self.collect()["artifacts"][0]
        self.assertEqual(art["commit_sha"], "abc1234")
        self.assertTrue(art["content"].startswith("# 需求"))
        self.assertNotIn("hunter2hunter2", art["content"])
        self.assertLessEqual(len(art["content"].encode("utf-8")), 64 * 1024)
        art["content"].encode("utf-8").decode("utf-8")

    def test_doc_outside_the_repo_gets_no_content(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text(SECRET)
        os.symlink(outside, self.root / "link.md")
        (self.root / ".git").write_text("gitdir: /nowhere\n")
        for ref in ("../outside.md", str(outside), "link.md", ".git", "missing.md"):
            with self.subTest(ref=ref):
                self.write({"artifacts": [{"kind": "doc", "ref": ref}]})
                got = self.collect()
                self.assertTrue(got["valid"])
                self.assertNotIn("content", got["artifacts"][0])
                self.assertNotIn("commit_sha", got["artifacts"][0])
                self.assertNotIn(SECRET, json.dumps(got))

    def test_many_docs_stay_within_the_total_artifact_budget(self):
        (self.root / "docs").mkdir()
        refs = []
        for i in range(4):
            (self.root / "docs" / ("d%d.md" % i)).write_text("line\n" * 20000)
            refs.append({"kind": "doc", "ref": "docs/d%d.md" % i})
        self.write({"artifacts": refs})
        got = self.collect()
        self.assertTrue(got["valid"])
        self.assertEqual(len(got["artifacts"]), 4)
        self.assertLessEqual(len(json.dumps(got["artifacts"], ensure_ascii=False).encode("utf-8")),
                             results.ARTIFACTS_BYTES)


class FreshOutDirTest(unittest.TestCase):
    def test_fresh_run_moves_a_previous_out_dir_aside(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".timetrace" / "out"
            out.mkdir(parents=True)
            (out / "result.json").write_text('{"artifacts": [{"kind": "note", "ref": "stale"}]}')
            self.assertTrue(results.prepare_out_dir(d, fresh=True))
            self.assertTrue(out.is_dir())
            self.assertEqual(list(out.iterdir()), [])
            self.assertIsNone(results.collect(d))
            moved = [p for p in (Path(d) / ".timetrace").iterdir() if p.name.startswith("out.prev-")]
            self.assertEqual(len(moved), 1)
            self.assertTrue((moved[0] / "result.json").is_file())

    def test_resume_keeps_the_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".timetrace" / "out"
            out.mkdir(parents=True)
            (out / "result.json").write_text("{}")
            self.assertTrue(results.prepare_out_dir(d))
            self.assertTrue((out / "result.json").is_file())

    def test_a_symlinked_out_dir_is_moved_aside_not_followed(self):
        with tempfile.TemporaryDirectory() as d:
            elsewhere = Path(d) / "elsewhere"; elsewhere.mkdir()
            (elsewhere / "result.json").write_text("{}")
            (Path(d) / "wt" / ".timetrace").mkdir(parents=True)
            (Path(d) / "wt" / ".timetrace" / "out").symlink_to(elsewhere)
            self.assertTrue(results.prepare_out_dir(str(Path(d) / "wt"), fresh=True))
            self.assertFalse((Path(d) / "wt" / ".timetrace" / "out").is_symlink())
            self.assertTrue((elsewhere / "result.json").is_file(), "the target is untouched")


class ExcludeTest(unittest.TestCase):
    def test_out_dir_never_shows_in_git_status_of_a_task_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"; init_repo(repo)
            path, _ = worktree.ensure(str(repo), 7, Path(d) / "home")
            out = Path(path) / ".timetrace" / "out"
            out.mkdir(parents=True)
            (out / "result.json").write_text("{}")
            self.assertEqual(git("status", "--porcelain", cwd=path), "")
            git("add", "-A", cwd=path)
            self.assertEqual(git("diff", "--cached", "--name-only", cwd=path), "")
            # Idempotent: a second ensure does not duplicate the pattern.
            worktree.ensure(str(repo), 7, Path(d) / "home")
            exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=path))
            exclude = exclude if exclude.is_absolute() else Path(path) / exclude
            self.assertEqual(exclude.read_text().count(".timetrace/out/"), 1)

    def test_moved_aside_out_dirs_never_show_in_git_status(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"; init_repo(repo)
            path, _ = worktree.ensure(str(repo), 7, Path(d) / "home")
            results.prepare_out_dir(path)
            (Path(path) / ".timetrace" / "out" / "result.json").write_text("{}")
            results.prepare_out_dir(path, fresh=True)
            self.assertEqual(git("status", "--porcelain", "--ignored=no", cwd=path), "")

    def test_safety_rules_name_the_result_file(self):
        self.assertIn(".timetrace/out/result.json", SAFETY_RULES)


class TaskCloud:
    def __init__(self):
        self.events = []

    def claim(self, token):
        return {"job": {"id": "j1", "workspace_id": "ws1", "tool_profile_id": "codex-default",
                        "provider": "codex", "prompt": "write the doc"},
                "attempt_id": "a1", "lease_epoch": 1,
                "lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def post_quota_samples(self, token, samples):
        return {}


class WritingAdapter:
    def __init__(self, payload):
        self.payload = payload

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        out = Path(cwd) / ".timetrace" / "out"
        assert out.is_dir(), "the runner prepares .timetrace/out before the run"
        if self.payload is not None:
            (Path(cwd) / "docs").mkdir(exist_ok=True)
            (Path(cwd) / "docs" / "req.md").write_text("# req\n")
            git("add", "docs/req.md", cwd=cwd)
            git("commit", "-qm", "doc", cwd=cwd)
            (out / "result.json").write_text(self.payload)
        return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)


class AgentResultTest(unittest.TestCase):
    def run_job(self, payload):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo.resolve()), "main")
            cloud = TaskCloud()
            agent = Agent(db, cloud, {"codex": WritingAdapter(payload)}, Path(d), lambda: "token")
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            wt = next((Path(d) / "worktrees").iterdir())
            head = git("rev-parse", "HEAD", cwd=wt)
            self.assertEqual(git("status", "--porcelain", cwd=wt), "")
            return cloud.events[-1], head

    def test_completion_event_carries_artifacts_and_draft(self):
        event, head = self.run_job(json.dumps({
            "artifacts": [{"kind": "doc", "ref": "docs/req.md"}],
            "pipeline_draft": {"title": "p", "note": SECRET}}))
        self.assertEqual(event["type"], "completed")
        self.assertEqual(event["message"], "completed")
        self.assertEqual(event["artifacts"], [{"kind": "doc", "ref": "docs/req.md",
                                               "content": "# req\n", "commit_sha": head}])
        self.assertEqual(event["result"], {"pipeline_draft": {"title": "p", "note": "[REDACTED:aws]"}})

    def test_invalid_result_uploads_empty_artifacts_and_says_so(self):
        event, _ = self.run_job("{not json")
        self.assertEqual(event["type"], "completed")
        self.assertEqual(event["artifacts"], [])
        self.assertNotIn("result", event)
        self.assertIn("结构化结果无效", event["message"])
        self.assertIs(event["result_invalid"], True)

    def test_oversize_result_is_flagged_invalid(self):
        event, _ = self.run_job(json.dumps({"artifacts": [], "pad": "x" * (64 * 1024)}))
        self.assertIs(event["result_invalid"], True)
        self.assertIn("结构化结果无效", event["message"])

    def test_dropped_artifacts_are_noted_without_invalidating(self):
        event, _ = self.run_job(json.dumps({"artifacts": ["docs/req.md", {"kind": "exe", "ref": "a"}],
                                            "pipeline_draft": {"title": "p"}}))
        self.assertNotIn("result_invalid", event)
        self.assertNotIn("结构化结果无效", event["message"])
        self.assertIn("忽略 1 项格式无效的产出物", event["message"])
        self.assertEqual([a["ref"] for a in event["artifacts"]], ["docs/req.md"])
        self.assertEqual(event["result"], {"pipeline_draft": {"title": "p"}})

    def test_dropped_draft_is_noted_without_invalidating(self):
        event, _ = self.run_job(json.dumps({"pipeline_draft": "a → b"}))
        self.assertNotIn("result_invalid", event)
        self.assertIn("忽略格式无效的 pipeline_draft", event["message"])

    def test_valid_result_is_not_flagged(self):
        event, _ = self.run_job(json.dumps({"artifacts": []}))
        self.assertNotIn("result_invalid", event)

    def test_stale_result_in_a_reused_worktree_is_not_reported(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo.resolve()), "main")
            reused = Path(d) / "reused"; reused.mkdir()
            (reused / ".timetrace" / "out").mkdir(parents=True)
            (reused / ".timetrace" / "out" / "result.json").write_text(json.dumps({"pipeline_draft": {"old": 1}}))
            cloud = TaskCloud()
            agent = Agent(db, cloud, {"codex": WritingAdapter(None)}, Path(d), lambda: "token",
                          prepare_workspace=lambda *args, **kwargs: (str(reused), "timetrace/x"))
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            self.assertNotIn("result", cloud.events[-1])

    def test_no_result_file_adds_no_fields(self):
        event, _ = self.run_job(None)
        self.assertEqual(event["message"], "completed")
        self.assertNotIn("artifacts", event)
        self.assertNotIn("result", event)


if __name__ == "__main__":
    unittest.main()
