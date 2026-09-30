"""Parallel sub-task jobs: N coding slots, per-plan locks, branch names, subtasks."""
import hashlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from timetrace import results, worktree
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.dispatch import LockBusy, UnclearedOwner, coding_slot_lock, plan_lock
from timetrace.models import RunResult


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=path, check=True)


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def lease():
    return datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()


class QueueCloud:
    """Hands out the queued jobs one per claim, then nothing."""

    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.events = []
        self.lock = threading.Lock()

    def claim(self, token):
        with self.lock:
            if not self.jobs:
                return None
            job = self.jobs.pop(0)
        base = {"workspace_id": "ws1", "tool_profile_id": "codex-default", "provider": "codex", "prompt": "p"}
        base.update(job)
        return {"job": base, "attempt_id": "a-" + base["id"], "lease_epoch": 1, "lease_expires_at": lease()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        with self.lock:
            self.events.extend((job_id, e) for e in events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": lease()}

    def post_quota_samples(self, token, samples):
        return {}


class BarrierAdapter:
    """start() only returns once `parties` runs are inside it at the same time."""

    def __init__(self, parties=2, timeout=10):
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.cwds = []

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.cwds.append(cwd)
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            return RunResult(exit_code=1, ok=False, error="ran alone")
        return RunResult(exit_code=0, ok=True, output="ok", session_id=session_id)

    start_folder = None

    def chat(self, *args):
        raise AssertionError("no chat here")


class SlotTest(unittest.TestCase):
    def test_n_slots(self):
        with tempfile.TemporaryDirectory() as home:
            first = coding_slot_lock(home).acquire()
            second = coding_slot_lock(home, 1).acquire()
            with self.assertRaises(LockBusy):
                coding_slot_lock(home, 1).acquire()
            self.assertEqual(Path(first.path).name, "coding-slot.lock")
            self.assertNotEqual(first.path, second.path)
            first.release(); second.release()

    def test_plan_lock_is_per_plan(self):
        with tempfile.TemporaryDirectory() as home:
            a = plan_lock(home, "plan-a").acquire()
            b = plan_lock(home, "plan-b").acquire()
            with self.assertRaises(LockBusy):
                plan_lock(home, "plan-a").acquire()
            a.release(); b.release()


class ParallelAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, cloud, adapter, **kwargs):
        return Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "token", **kwargs)

    def run_concurrently(self, agent, n=2):
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(agent.run_once())) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        return sorted(outcomes)

    def test_two_plans_in_one_repository_run_at_the_same_time(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        adapter = BarrierAdapter()
        outcomes = self.run_concurrently(self.agent(cloud, adapter, max_parallel=2, workspace_wait=10))
        self.assertEqual(outcomes, ["job j1 → awaiting_review", "job j2 → awaiting_review"])
        self.assertEqual(len(set(adapter.cwds)), 2, "each job has its own worktree")

    def test_one_slot_keeps_the_runner_sequential(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        adapter = BarrierAdapter(timeout=1)
        outcomes = self.run_concurrently(self.agent(cloud, adapter, max_parallel=1))
        self.assertEqual(len(adapter.cwds), 1)
        self.assertTrue(any("deferred (runner busy)" in o for o in outcomes), outcomes)

    def test_same_plan_is_never_run_twice_at_once(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        adapter = BarrierAdapter(parties=1)
        agent = self.agent(cloud, adapter, max_parallel=2)
        held = plan_lock(self.d / "home", "p1").acquire()
        try:
            self.assertEqual(agent.run_once(), "job j1 → deferred (plan busy)")
        finally:
            held.release()
        self.assertEqual(adapter.cwds, [])

    def test_folder_workspace_stays_serialized(self):
        footage = self.d / "footage"; footage.mkdir()
        self.db.upsert_workspace("ws1", "footage", str(footage), "", kind="folder")
        runs = []
        release = threading.Event()

        class FolderAdapter(BarrierAdapter):
            def start_folder(self, prompt, cwd, workspace, session_id, log_file, cancel_event=None):
                runs.append(cwd)
                release.wait(5)
                return RunResult(exit_code=0, ok=True, session_id=session_id)
            resume_folder = start_folder

        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        agent = self.agent(cloud, FolderAdapter(), max_parallel=2)
        outcomes = []
        first = threading.Thread(target=lambda: outcomes.append(agent.run_once()))
        first.start()
        for _ in range(100):
            if runs:
                break
            time.sleep(.05)
        outcomes.append(agent.run_once())
        release.set()
        first.join(10)
        self.assertIn("job j2 → deferred (workspace busy)", outcomes)
        self.assertIn("job j1 → awaiting_review", outcomes)

    def test_crash_fence_on_every_slot_is_reported(self):
        for index in (0, 1):
            path = Path(coding_slot_lock(self.d / "home", index).path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1234")
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        outcome = self.agent(cloud, BarrierAdapter(parties=1), max_parallel=2).run_once()
        self.assertIn("manual recovery required", outcome)

    def test_fenced_slot_is_skipped_when_another_is_free(self):
        path = Path(coding_slot_lock(self.d / "home").path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        outcome = self.agent(cloud, BarrierAdapter(parties=1), max_parallel=2).run_once()
        self.assertEqual(outcome, "job j1 → awaiting_review")
        self.assertEqual(path.read_text(), "1234", "the fence stays for manual recovery")

    def test_fenced_and_busy_slots_report_the_fence(self):
        path = Path(coding_slot_lock(self.d / "home").path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        busy = coding_slot_lock(self.d / "home", 1).acquire()
        try:
            cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
            outcome = self.agent(cloud, BarrierAdapter(parties=1), max_parallel=2).run_once()
        finally:
            busy.release()
        self.assertIn("deferred (runner busy)", outcome)
        self.assertIn("manual recovery required", outcome)

    def test_worker_exception_is_logged_with_job_id(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, BarrierAdapter(parties=1), max_parallel=2)
        lines = []

        def boom(claim):
            raise RuntimeError("disk full")
        agent.handle = boom
        original = cloud.claim
        def claim(token):
            answer = original(token)
            if answer is None:
                raise KeyboardInterrupt()
            return answer
        cloud.claim = claim
        with patch("timetrace.agent.time.sleep"), self.assertRaises(KeyboardInterrupt):
            agent.run_forever(interval=0, log=lines.append)
        self.assertTrue(any("j1" in l and "RuntimeError" in l and "disk full" in l for l in lines), lines)

    def test_daemon_runs_up_to_max_parallel_jobs(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        adapter = BarrierAdapter()
        agent = self.agent(cloud, adapter, max_parallel=2, workspace_wait=10)
        claims = []
        original = cloud.claim

        def claim(token):
            answer = original(token)
            claims.append(answer)
            if answer is None and len([e for _, e in cloud.events if e["type"] == "completed"]) == 2:
                raise KeyboardInterrupt()
            return answer
        cloud.claim = claim
        with patch("timetrace.agent.time.sleep"), self.assertRaises(KeyboardInterrupt):
            agent.run_forever(interval=0, log=lambda message: None)
        completed = sorted(job for job, e in cloud.events if e["type"] == "completed")
        self.assertEqual(completed, ["j1", "j2"])


class BranchNameTest(unittest.TestCase):
    def test_validation(self):
        for good in ("timetrace/breakdown/ui", "timetrace/stage-1/sub_2", "timetrace/a.b/c"):
            self.assertTrue(worktree.valid_task_branch(good), good)
        for bad in ("main", "timetrace/", "timetrace/../main", "timetrace/A", "timetrace/a//b", "timetrace/a/", "timetrace/.a",
                    "timetrace/a.lock", "timetrace/a b", "timetrace/" + "a" * 101, None, 3, "timetrace/a.", "timetrace/a@{1}"):
            self.assertFalse(worktree.valid_task_branch(bad), bad)

    def test_ensure_uses_the_requested_branch(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"; init_repo(repo)
            path, branch = worktree.ensure(str(repo), 5, Path(d) / "home", "main", branch="timetrace/bd/ui")
            self.assertEqual(branch, "timetrace/bd/ui")
            self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=path), "timetrace/bd/ui")
            # Idempotent for the same branch.
            self.assertEqual(worktree.ensure(str(repo), 5, Path(d) / "home", "main", branch="timetrace/bd/ui")[1],
                             "timetrace/bd/ui")
            with self.assertRaises(ValueError):
                worktree.ensure(str(repo), 6, Path(d) / "home", "main", branch="main")

    def test_agent_passes_only_valid_branch_names(self):
        suffix = hashlib.sha256(b"j1").hexdigest()[:8]
        for requested, expected in (("timetrace/bd/ui", "timetrace/bd/ui-" + suffix), ("timetrace/../x", None), ("main", None),
                                    (None, None), ("timetrace/" + "a" * 100, None)):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as d:
                repo = Path(d) / "repo"; init_repo(repo)
                db = Database(Path(d) / "timetrace.db")
                db.upsert_workspace("ws1", "repo", str(repo.resolve()), "main")
                job = {"id": "j1", "plan_id": "p1"}
                if requested:
                    job["branch_name"] = requested
                seen = []

                def prepare(repo_path, task_id, home, base, branch=None):
                    seen.append(branch)
                    return repo_path, branch or "timetrace/x"
                agent = Agent(db, QueueCloud([job]), {"codex": BarrierAdapter(parties=1)}, Path(d), lambda: "t",
                              prepare_workspace=prepare)
                agent.run_once()
                self.assertEqual(seen, [expected])


class BranchUniquenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_jobs_with_the_same_branch_name_get_their_own_branches(self):
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/bd/ui"},
                            {"id": "j2", "plan_id": "p2", "branch_name": "timetrace/bd/ui"}])
        agent = Agent(self.db, cloud, {"codex": BarrierAdapter(parties=1)}, self.d / "home", lambda: "t")
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(agent.run_once(), "job j2 → awaiting_review")
        branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads/timetrace/", cwd=self.repo).split()
        self.assertEqual(len(branches), 2, branches)
        self.assertTrue(all(b.startswith("timetrace/bd/ui-") for b in branches), branches)

    def test_resume_uses_the_checkpointed_branch(self):
        class Blocking(BarrierAdapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.cwds.append(cwd)
                return RunResult(exit_code=1, blocked=True, session_id="native", error="quota")

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.cwds.append(cwd)
                return RunResult(exit_code=0, ok=True, output="ok", session_id=session_id)
        adapter = Blocking(parties=1)
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/bd/ui"},
                            {"id": "j2", "plan_id": "p1", "branch_name": "timetrace/other/name"}])
        agent = Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "t")
        self.assertEqual(agent.run_once(), "job j1 → waiting_quota")
        branch = self.db.get_checkpoint("p1").branch
        self.assertEqual(branch, "timetrace/bd/ui-" + hashlib.sha256(b"j1").hexdigest()[:8])
        self.assertEqual(agent.run_once(), "job j2 → awaiting_review")
        self.assertEqual(adapter.cwds[0], adapter.cwds[1])
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=adapter.cwds[1]), branch)

    def test_legacy_checkpoint_resumes_on_the_unsuffixed_branch(self):
        class Blocking(BarrierAdapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=1, blocked=True, session_id="native", error="quota")
            resume = BarrierAdapter.start
        seen = []

        def prepare(repo, task_id, home, base, branch=None):
            seen.append(branch)
            return str(self.repo), branch or "timetrace/x"
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/bd/ui"},
                            {"id": "j2", "plan_id": "p1", "branch_name": "timetrace/bd/ui"}])
        agent = Agent(self.db, cloud, {"codex": Blocking(parties=1)}, self.d / "home", lambda: "t",
                      prepare_workspace=prepare)
        self.assertEqual(agent.run_once(), "job j1 → waiting_quota")
        cp = self.db.get_checkpoint("p1")
        cp.branch = None  # written before branches were made unique
        self.db.save_checkpoint(cp)
        agent.run_once()
        self.assertEqual(seen[1], "timetrace/bd/ui")

    def test_branch_prefix_conflict_fails_early_with_a_clear_message(self):
        git("branch", "timetrace/bd", cwd=self.repo)
        with self.assertRaises(worktree.BranchConflict) as caught:
            worktree.ensure(str(self.repo), 1, self.d / "home", "main", branch="timetrace/bd/ui-1")
        self.assertIn("timetrace/bd", str(caught.exception))
        git("branch", "timetrace/x/y", cwd=self.repo)
        with self.assertRaises(worktree.BranchConflict):
            worktree.ensure(str(self.repo), 2, self.d / "home", "main", branch="timetrace/x")
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/bd/ui"}])
        agent = Agent(self.db, cloud, {"codex": BarrierAdapter(parties=1)}, self.d / "home", lambda: "t")
        self.assertEqual(agent.run_once(), "job j1 → failed")
        message = cloud.events[-1][1]["message"]
        self.assertIn("冲突", message)
        self.assertNotIn("crashed", message)


class SubtasksTest(unittest.TestCase):
    def collect(self, payload):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".timetrace" / "out"; out.mkdir(parents=True)
            (out / "result.json").write_text(json.dumps(payload))
            return results.collect(d)

    def test_valid_subtasks_are_forwarded_and_redacted(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        got = self.collect({"subtasks": [
            {"key": "api", "title": "API", "brief": "use " + secret, "depends_on": [], "tool": "codex",
             "estimate_minutes": 30, "extra": "dropped"},
            {"key": "ui", "title": "UI", "brief": "screens", "depends_on": ["api"]},
        ]})
        self.assertTrue(got["valid"])
        self.assertEqual(got["result"], {"subtasks": [
            {"key": "api", "title": "API", "brief": "use [REDACTED:aws]", "depends_on": [], "tool": "codex",
             "estimate_minutes": 30},
            {"key": "ui", "title": "UI", "brief": "screens", "depends_on": ["api"], "tool": None,
             "estimate_minutes": None},
        ]})

    def test_subtasks_and_draft_travel_together(self):
        got = self.collect({"pipeline_draft": {"t": 1}, "subtasks": [{"key": "a", "title": "A"}]})
        self.assertEqual(set(got["result"]), {"pipeline_draft", "subtasks"})

    def test_keys_are_lowercased_to_match_branch_names(self):
        got = self.collect({"subtasks": [{"key": "API", "title": "A"},
                                         {"key": "Ui", "title": "U", "depends_on": ["API"]}]})
        self.assertTrue(got["valid"])
        self.assertEqual([(t["key"], t["depends_on"]) for t in got["result"]["subtasks"]],
                         [("api", []), ("ui", ["api"])])
        for bad in ([{"key": "Api", "title": "A"}, {"key": "api", "title": "B"}],
                    [{"key": "a.lock", "title": "A"}], [{"key": "a.", "title": "A"}],
                    [{"key": "a..b", "title": "A"}]):
            with self.subTest(bad=bad):
                self.assertFalse(self.collect({"subtasks": bad})["valid"])

    def test_keys_are_bounded_like_valleys(self):
        # Valley accepts [a-z0-9][a-z0-9_.-]{0,39}: at most 40 characters.
        self.assertTrue(self.collect({"subtasks": [{"key": "k" * 40, "title": "A"}]})["valid"])
        self.assertFalse(self.collect({"subtasks": [{"key": "k" * 41, "title": "A"}]})["valid"])
        self.assertFalse(self.collect({"subtasks": [{"key": "a", "title": "A", "depends_on": ["d" * 41]}]})["valid"])

    def test_invalid_subtasks_invalidate_the_result(self):
        one = {"key": "a", "title": "A"}
        for bad in ("x", [one] * 1 + [dict(one)], [dict(one, key="k%d" % i) for i in range(31)],
                    [{"title": "no key"}], [dict(one, key="")], [dict(one, key="a b")],
                    [dict(one, title="")], [dict(one, depends_on="a")], [dict(one, depends_on=[1])],
                    [dict(one, estimate_minutes=True)], [dict(one, estimate_minutes=-1)],
                    [dict(one, tool=5)], [dict(one, brief=["x"])], ["not an object"]):
            with self.subTest(bad=bad):
                got = self.collect({"subtasks": bad})
                self.assertFalse(got["valid"])
                self.assertIsNone(got["result"])


if __name__ == "__main__":
    unittest.main()
