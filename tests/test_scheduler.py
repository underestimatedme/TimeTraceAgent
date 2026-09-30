import tempfile
import unittest
import sys
from pathlib import Path

from timetrace import limits, scheduler
from timetrace.db import Database
from timetrace.models import BLOCKED, DONE, FAILED, PENDING, RUNNABLE, RunResult, Sample


class FakeAdapter:
    def __init__(self, name, results=None, live=None):
        self.name = name
        self.results = list(results or [])
        self.live = live
        self.calls = []  # (kind, prompt, cwd, session_id)

    def read_limits(self):
        return self.live

    def capabilities(self):
        return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

    def start(self, prompt, cwd, session_id, log_file):
        self.calls.append(("start", prompt, cwd, session_id))
        return self._next(session_id)

    def resume(self, prompt, cwd, session_id, log_file):
        self.calls.append(("resume", prompt, cwd, session_id))
        return self._next(session_id)

    def _next(self, session_id):
        res = self.results.pop(0) if self.results else RunResult(ok=True)
        res.session_id = res.session_id or session_id
        return res


WORKTREE_CALLS = []


def fake_worktree(repo, task_id, home, base="HEAD"):
    WORKTREE_CALLS.append((task_id, base))
    p = Path(home) / "worktrees" / str(task_id)
    p.mkdir(parents=True, exist_ok=True)
    return str(p), "timetrace/%d" % task_id


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.cfg = {"interval_sec": 1, "jitter_sec": 0, "default_block_sleep_sec": 600,
                    "circuit_breaker_failures": 3, "circuit_window_mins": 300, "hook_max_tasks": 5}
        self.hooks = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_once(self, adapters, now):
        return scheduler.run_once(
            self.db, adapters, self.cfg, self.home, now=now, log=lambda s: None,
            ensure_worktree=fake_worktree,
            hook_runner=lambda *a: self.hooks.append(a), rng=lambda: 0.0, clock=lambda: now,
        )

    def test_defers_when_coding_slot_held_by_agent(self):
        # The cloud agent holds the runner-wide coding slot; the local scheduler
        # must defer rather than start a second concurrent process.
        from timetrace.dispatch import coding_slot_lock

        self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude", [RunResult(ok=True)])
        held = coding_slot_lock(self.home).acquire()
        try:
            out = self.run_once({"claude": ad}, now=100)
        finally:
            held.release()
        self.assertEqual(out, "task 1 → deferred (runner busy)")
        self.assertEqual(ad.calls, [])  # adapter never invoked

    def test_preparation_revoking_billing_capability_blocks_spawn(self):
        task_id = self.db.add_task("do", "/repo", tool="claude")
        adapter = FakeAdapter("claude")
        def prepare(*args):
            adapter.capabilities = lambda: {"can_enforce_zero_spend": False}
            return fake_worktree(*args)
        outcome = scheduler.run_once(self.db, {"claude": adapter}, self.cfg, self.home,
                                     ensure_worktree=prepare, log=lambda *args: None)
        self.assertEqual(outcome, "task %d → blocked (billing_unverified)" % task_id)
        self.assertEqual(adapter.calls, [])

    def test_missing_dispatch_or_resume_capability_blocks(self):
        for missing in ("can_dispatch", "can_resume"):
            with self.subTest(missing=missing):
                task_id = self.db.add_task("do", "/repo", tool="claude")
                if missing == "can_resume":
                    self.db.update_task(task_id, session_id="native")
                adapter = FakeAdapter("claude")
                adapter.capabilities = lambda: {"can_enforce_zero_spend": True, "can_dispatch": True,
                                                 "can_resume": True, missing: False}
                outcome = self.run_once({"claude": adapter}, now=100)
                self.assertIn("blocked", outcome)
                self.assertEqual(adapter.calls, [])
                self.db.update_task(task_id, state=DONE)

    def test_billing_revoked_during_logging_cannot_spawn(self):
        self.db.add_task("do", "/repo", tool="claude")
        adapter = FakeAdapter("claude")
        def log(*args):
            adapter.capabilities = lambda: {"can_enforce_zero_spend": False}
        outcome = scheduler.run_once(self.db, {"claude": adapter}, self.cfg, self.home,
                                     ensure_worktree=fake_worktree, log=log)
        self.assertIn("billing_unverified", outcome)
        self.assertEqual(adapter.calls, [])

    def test_billing_revoked_inside_adapter_cannot_create_process(self):
        from timetrace.process import run_streaming
        self.db.add_task("do", "/repo", tool="claude")
        adapter = FakeAdapter("claude")
        marker = self.home / "spawned"
        def start(prompt, cwd, session_id, log_file):
            adapter.capabilities = lambda: {"can_enforce_zero_spend": False}
            code, lines = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)], cwd, log_file)
            return RunResult(ok=code == 0, exit_code=code)
        adapter.start = start
        outcome = self.run_once({"claude": adapter}, now=100)
        self.assertIn("billing_unverified", outcome)
        self.assertFalse(marker.exists())

    def test_resume_revoked_inside_adapter_cannot_create_process(self):
        from timetrace.process import run_streaming
        task_id = self.db.add_task("do", "/repo", tool="claude")
        self.db.update_task(task_id, session_id="native")
        adapter = FakeAdapter("claude")
        marker = self.home / "spawned"
        def resume(prompt, cwd, session_id, log_file):
            adapter.capabilities = lambda: {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": False}
            code, lines = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)], cwd, log_file)
            return RunResult(ok=code == 0, exit_code=code)
        adapter.resume = resume
        outcome = self.run_once({"claude": adapter}, now=100)
        self.assertIn("resume_unavailable", outcome)
        self.assertFalse(marker.exists())
        self.assertEqual(self.db.get_task(task_id)["session_id"], "native")

    def test_success_hook_resume_keeps_process_authority(self):
        from timetrace.process import run_streaming
        self.db.add_task("do", "/repo", tool="claude", on_success="suggest follow-up")
        adapter = FakeAdapter("claude")
        marker = self.home / "hook-spawned"
        def resume(prompt, cwd, session_id, log_file):
            adapter.capabilities = lambda: {"can_enforce_zero_spend": False}
            code, lines = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)], cwd, log_file)
            return RunResult(ok=code == 0, exit_code=code, output="[]")
        adapter.resume = resume
        outcome = scheduler.run_once(self.db, {"claude": adapter}, self.cfg, self.home,
                                     ensure_worktree=fake_worktree, log=lambda *args: None)
        self.assertIn("done", outcome)
        self.assertFalse(marker.exists())
        self.assertIn("billing_unverified", self.db.list_events(type_="hook_rejected")[0]["payload"]["reason"])

    def test_crash_fence_surfaces_manual_clearance_in_scheduler(self):
        from timetrace.dispatch import coding_slot_lock
        self.db.add_task("do", "/repo", tool="claude")
        path = Path(coding_slot_lock(self.home).path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        outcome = self.run_once({"claude": FakeAdapter("claude")}, now=100)
        self.assertIn("manual", outcome)
        self.assertIn(str(path), outcome)
        self.assertEqual(path.read_text(), "1234")

    def test_missing_or_false_zero_spend_capability_blocks_start_and_resume(self):
        """The scheduler must never bypass the shared billing gate."""
        for capabilities in ({}, {"can_enforce_zero_spend": False}):
            for session_id in (None, "prior-session"):
                with self.subTest(capabilities=capabilities, session_id=session_id):
                    task_id = self.db.add_task("do", "/repo", tool="claude")
                    if session_id is not None:
                        self.db.update_task(task_id, session_id=session_id, now=100)

                    class CountingAdapter(FakeAdapter):
                        def capabilities(self):
                            return capabilities

                    adapter = CountingAdapter("claude", [RunResult(ok=True)])
                    out = self.run_once({"claude": adapter}, now=100)

                    self.assertEqual(out, "task %d → blocked (billing_unverified)" % task_id)
                    self.assertEqual(adapter.calls, [])
                    task = self.db.get_task(task_id)
                    self.assertEqual(task["state"], RUNNABLE)
                    self.assertEqual(task["last_error"], "billing_unverified")
                    self.db.update_task(task_id, state=DONE, now=100)

    def test_success_path_records_run_and_event(self):
        t = self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude", [RunResult(ok=True, output="done!", samples=[
            Sample("claude:five_hour", "claude", 20.0, reset_at=500)])])
        out = self.run_once({"claude": ad}, now=100)
        self.assertEqual(out, "task 1 → done")
        task = self.db.get_task(t)
        self.assertEqual(task["state"], DONE)
        self.assertTrue(task["session_id"])
        self.assertEqual(task["worktree"], str(self.home / "worktrees" / "1"))
        run = self.db.latest_run(t)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["summary"], "done!")
        self.assertEqual(ad.calls[0][0], "start")
        self.assertEqual(ad.calls[0][2], task["worktree"])
        self.assertEqual([e["type"] for e in self.db.list_events()][0], "task_done")
        self.assertEqual(limits.snapshot(self.db)[0]["used_pct"], 20.0)
        self.assertEqual(self.hooks, [])

    def test_failure_path(self):
        t = self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude", [RunResult(exit_code=1, error="error_max_turns: stopped")])
        out = self.run_once({"claude": ad}, now=100)
        self.assertTrue(out.startswith("task 1 → failed"))
        task = self.db.get_task(t)
        self.assertEqual(task["state"], FAILED)
        self.assertIn("error_max_turns", task["last_error"])
        self.assertEqual(self.db.latest_run(t)["blocked"], 0)

    def test_blocked_then_wake_then_resume_same_session(self):
        t = self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude", [
            RunResult(exit_code=1, blocked=True, reset_at=1000, error="hit your limit"),
            RunResult(ok=True, output="finished"),
        ])
        out = self.run_once({"claude": ad}, now=100)
        self.assertEqual(out, "task 1 → blocked until 1000")
        task = self.db.get_task(t)
        self.assertEqual(task["state"], BLOCKED)
        self.assertEqual(task["blocked_until"], 1000)
        sid = task["session_id"]
        self.assertEqual(self.db.latest_run(t)["blocked"], 1)
        # too early: nothing happens
        self.assertEqual(self.run_once({"claude": ad}, now=900), "idle")
        # reset passed: wake and resume with same session id
        out = self.run_once({"claude": ad}, now=1001)
        self.assertEqual(out, "task 1 → done")
        self.assertEqual(ad.calls[1][0], "resume")
        self.assertEqual(ad.calls[1][3], sid)
        self.assertIn("Original task:\ndo", ad.calls[1][1])
        types = [e["type"] for e in self.db.list_events()]
        self.assertIn("window_reset", types)
        self.assertIn("task_resumed", types)
        self.assertIn("task_blocked", types)
        self.assertEqual(len(self.db.runs_for_task(t)), 2)

    def test_blocked_without_reset_uses_default_sleep(self):
        self.db.add_task("do", "/repo", tool="codex")
        ad = FakeAdapter("codex", [RunResult(exit_code=1, blocked=True, error="usage limit")])
        out = self.run_once({"codex": ad}, now=100)
        self.assertEqual(out, "task 1 → blocked until 700")

    def test_any_tool_switches_when_preferred_exhausted(self):
        limits.record_samples(self.db, [Sample("codex:codex:primary", "codex", 100, reset_at=10 ** 10)],
                              at=1)
        t = self.db.add_task("do", "/repo", tool="codex", any_tool=True)
        claude = FakeAdapter("claude", [RunResult(ok=True)])
        codex = FakeAdapter("codex")
        out = self.run_once({"claude": claude, "codex": codex}, now=100)
        self.assertEqual(out, "task 1 → done")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(codex.calls, [])
        self.assertEqual(self.db.get_task(t)["tool"], "claude")
        sw = self.db.list_events(type_="tool_switched")
        self.assertEqual(sw[0]["payload"], {"task_id": 1, "from": "codex", "to": "claude"})

    def test_no_any_tool_waits_when_exhausted(self):
        limits.record_samples(self.db, [Sample("codex:codex:primary", "codex", 100)], at=1)
        self.db.add_task("do", "/repo", tool="codex")
        codex = FakeAdapter("codex")
        out = self.run_once({"claude": FakeAdapter("claude"), "codex": codex}, now=100)
        self.assertEqual(out, "idle")
        self.assertEqual(codex.calls, [])

    def test_task_with_session_never_switches_tool(self):
        t = self.db.add_task("do", "/repo", tool="codex", any_tool=True)
        codex = FakeAdapter("codex", [RunResult(exit_code=1, blocked=True, reset_at=500,
                                                samples=[Sample("codex:codex:primary", "codex", 100)])])
        claude = FakeAdapter("claude")
        self.run_once({"claude": claude, "codex": codex}, now=100)
        self.assertEqual(self.db.get_task(t)["state"], BLOCKED)
        # woken but codex still reads 100% → must wait, not jump to claude
        out = self.run_once({"claude": claude, "codex": codex}, now=600)
        self.assertEqual(out, "idle")
        self.assertEqual(self.db.get_task(t)["state"], RUNNABLE)
        self.assertEqual(claude.calls, [])
        # a fresh live sample shows the window reset → resume with codex
        limits.record_samples(self.db, [Sample("codex:codex:primary", "codex", 5)], at=650)
        out = self.run_once({"claude": claude, "codex": codex}, now=700)
        self.assertEqual(out, "task 1 → done")
        self.assertEqual(codex.calls[1][0], "resume")

    def test_interactive_block_seen_via_statusline_blocks_dispatch_until_reset(self):
        # The user's own Claude Code session hit the limit (statusline sample says 100%).
        limits.record_samples(self.db, [
            Sample("claude:five_hour", "claude", 100, reset_at=5000, source="statusline"),
        ], at=100)
        self.db.add_task("do", "/repo", tool="claude")
        ad = FakeAdapter("claude", [RunResult(ok=True)])
        self.assertEqual(self.run_once({"claude": ad}, now=200), "idle")
        self.assertEqual(ad.calls, [])
        # window reset passed with no fresh sample: reading is stale, dispatch proceeds
        self.assertEqual(self.run_once({"claude": ad}, now=5001), "task 1 → done")

    def test_unspecified_tool_picks_most_remaining(self):
        limits.record_samples(self.db, [
            Sample("claude:five_hour", "claude", 80),
            Sample("codex:codex:primary", "codex", 20),
        ], at=1)
        self.db.add_task("do", "/repo")
        claude, codex = FakeAdapter("claude"), FakeAdapter("codex")
        self.run_once({"claude": claude, "codex": codex}, now=100)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(claude.calls, [])

    def test_circuit_breaker_stops_dispatch(self):
        for i in range(3):
            self.db.add_task("bad %d" % i, "/repo", tool="claude")
        ad = FakeAdapter("claude", [RunResult(exit_code=1, error="boom")] * 3)
        for now in (100, 200, 300):
            self.assertTrue(self.run_once({"claude": ad}, now).startswith("task"))
        self.db.add_task("next", "/repo", tool="claude")
        out = self.run_once({"claude": ad}, now=400)
        self.assertEqual(out, "circuit open")
        self.assertEqual(len(ad.calls), 3)
        self.assertEqual(len(self.db.list_events(type_="circuit_open")), 1)
        # same window, second loop: no duplicate event
        self.run_once({"claude": ad}, now=500)
        self.assertEqual(len(self.db.list_events(type_="circuit_open")), 1)
        # window expired → dispatch resumes
        out = self.run_once({"claude": ad}, now=300 + 300 * 60 + 1)
        self.assertEqual(out, "task 4 → done")

    def test_dependencies_advance_and_fail(self):
        a = self.db.add_task("parent", "/repo", tool="claude")
        b = self.db.add_task("child", "/repo", tool="claude", depends_on=a)
        c = self.db.add_task("orphan", "/repo", tool="claude", depends_on=999)
        ad = FakeAdapter("claude", [RunResult(ok=True), RunResult(ok=True)])
        self.assertEqual(self.db.get_task(b)["state"], PENDING)
        self.run_once({"claude": ad}, now=100)  # parent done
        self.assertEqual(self.db.get_task(a)["state"], DONE)
        self.assertEqual(self.db.get_task(c)["state"], FAILED)
        self.run_once({"claude": ad}, now=200)  # child promoted and run
        self.assertEqual(self.db.get_task(b)["state"], DONE)

    def test_dependent_task_branches_from_dependency(self):
        a = self.db.add_task("parent", "/repo", tool="claude")
        b = self.db.add_task("child", "/repo", tool="claude", depends_on=a)
        ad = FakeAdapter("claude", [RunResult(ok=True), RunResult(ok=True)])
        del WORKTREE_CALLS[:]
        self.run_once({"claude": ad}, now=100)
        self.run_once({"claude": ad}, now=200)
        self.assertEqual(WORKTREE_CALLS, [(a, "HEAD"), (b, "timetrace/%d" % a)])

    def test_child_fails_when_parent_fails(self):
        a = self.db.add_task("parent", "/repo", tool="claude")
        b = self.db.add_task("child", "/repo", tool="claude", depends_on=a)
        ad = FakeAdapter("claude", [RunResult(exit_code=1, error="x")])
        self.run_once({"claude": ad}, now=100)
        self.run_once({"claude": ad}, now=200)
        self.assertEqual(self.db.get_task(b)["state"], FAILED)
        self.assertIn("dependency 1 failed", self.db.get_task(b)["last_error"])

    def test_both_tools_exhausted_is_idle(self):
        limits.record_samples(self.db, [
            Sample("claude:five_hour", "claude", 100), Sample("codex:codex:primary", "codex", 100),
        ], at=1)
        self.db.add_task("do", "/repo", tool="claude", any_tool=True)
        out = self.run_once({"claude": FakeAdapter("claude"), "codex": FakeAdapter("codex")}, now=5)
        self.assertEqual(out, "idle")

    def test_on_success_triggers_hook_runner(self):
        self.db.add_task("do", "/repo", tool="claude", on_success="plan follow-ups")
        self.run_once({"claude": FakeAdapter("claude", [RunResult(ok=True)])}, now=100)
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.hooks[0][2]["id"], 1)

    def test_live_read_failure_is_recorded(self):
        class Broken(FakeAdapter):
            def read_limits(self):
                raise RuntimeError("app-server down")
        self.db.add_task("do", "/repo", tool="codex")
        self.run_once({"codex": Broken("codex", [RunResult(ok=True)])}, now=100)
        self.assertEqual(len(self.db.list_events(type_="sample_failure")), 1)


if __name__ == "__main__":
    unittest.main()
