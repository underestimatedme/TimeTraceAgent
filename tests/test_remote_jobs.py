"""Protocol 2 on a whole task job: interrupt, resume, append, approvals."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from timetrace.agent import RESUME_PROMPT, Agent
from timetrace.db import Database
from timetrace.models import RunResult
from tests.test_parallel import QueueCloud, init_repo, lease


class ControlCloud(QueueCloud):
    """Renewals carry whatever `lease_extra()` returns (controls, approvals)."""

    def __init__(self, jobs, lease_extra=lambda: {}):
        super().__init__(jobs)
        self.lease_extra = lease_extra
        self.renewals = 0

    def renew(self, token, attempt_id, epoch):
        self.renewals += 1
        answer = {"lease_expires_at": lease()}
        answer.update(self.lease_extra())
        return answer


class Adapter:
    def __init__(self):
        self.entered = threading.Event()
        self.calls = []

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}


class WaitUntilCancelled(Adapter):
    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append(("start", prompt, session_id))
        self.entered.set()
        cancel_event.wait(20)
        return RunResult(exit_code=-15, ok=False, error="terminated", session_id="thread-7")

    def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append(("resume", prompt, session_id))
        return RunResult(exit_code=0, ok=True, output="resumed", session_id=session_id)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"
        init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.home = self.d / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, cloud, adapter, provider="codex", **kw):
        kw.setdefault("heartbeat_interval", .05)
        return Agent(self.db, cloud, {provider: adapter}, self.home, lambda: "t", **kw)

    @staticmethod
    def events(cloud, job="j1"):
        return [e for j, e in cloud.events if j == job]

    def assert_contiguous(self, events):
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)), events)


class InterruptTest(Base):
    def test_interrupt_keeps_the_worktree_writes_a_checkpoint_and_resume_continues(self):
        adapter = WaitUntilCancelled()
        extra = {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1", "branch_name": "timetrace/build"}], lambda: extra)
        agent = self.agent(cloud, adapter)
        worker = threading.Thread(target=lambda: self.outcome.append(agent.run_once()))
        self.outcome = []
        worker.start()
        self.assertTrue(adapter.entered.wait(10))
        extra["controls"] = [{"id": "c1", "action": "interrupt"}]
        worker.join(15)
        self.assertEqual(self.outcome, ["job j1 → interrupted"])
        events = self.events(cloud)
        self.assert_contiguous(events)
        self.assertEqual([e["type"] for e in events], ["running", "interrupted_by_user"])
        last = events[-1]
        self.assertEqual(last["provider_session_id"], "thread-7")
        self.assertTrue(last["branch"].startswith("timetrace/build-"))
        checkpoint = self.db.get_checkpoint("p1")
        self.assertEqual(checkpoint.reason, "interrupted_by_user")
        self.assertEqual(checkpoint.provider_session_id, "thread-7")
        self.assertTrue(Path(checkpoint.execution_path).is_dir())

        # The phone's "continue" queues a resume job on the same plan.
        cloud.jobs.append({"id": "j2", "plan_id": "p1", "resume_of_job_id": "j1", "resume_note": "先跑测试",
                           "provider_session_id": "thread-7", "branch_name": last["branch"]})
        extra.clear()
        self.assertEqual(agent.run_once(), "job j2 → awaiting_review")
        kind, prompt, session = adapter.calls[-1]
        self.assertEqual((kind, session), ("resume", "thread-7"))
        self.assertTrue(prompt.startswith(RESUME_PROMPT))
        self.assertIn("先跑测试", prompt)
        done = self.events(cloud, "j2")
        self.assertEqual(done[-1]["type"], "completed")
        self.assertEqual(done[-1]["branch"], last["branch"])

    def test_completion_that_reaches_the_computer_first_wins(self):
        class Quick(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.entered.set()  # the run is over before the interrupt arrives
                return RunResult(exit_code=0, ok=True, output="done", session_id="s")
        quick = Quick()
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}],
                             lambda: {"controls": [{"id": "c1", "action": "interrupt"}]} if quick.entered.is_set() else {})
        agent = self.agent(cloud, quick)
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(self.events(cloud)[-1]["type"], "completed")

    def test_cancel_still_dominates_an_interrupt(self):
        adapter = WaitUntilCancelled()
        extra = {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}], lambda: extra)
        agent = self.agent(cloud, adapter)
        out = []
        worker = threading.Thread(target=lambda: out.append(agent.run_once()))
        worker.start()
        self.assertTrue(adapter.entered.wait(10))
        extra.update({"controls": [{"id": "c1", "action": "interrupt"}], "desired_action": "cancel"})
        worker.join(15)
        self.assertEqual(out, ["job j1 → cancelled"])


class AppendTest(Base):
    def test_codex_append_runs_as_the_next_turn_in_the_same_job(self):
        class Turn(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.calls.append(("start", prompt))
                self.entered.set()
                time.sleep(.5)  # renewals (with the append) happen meanwhile
                return RunResult(exit_code=0, ok=True, output="first", session_id="thread-1")

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.calls.append(("resume", prompt, session_id))
                return RunResult(exit_code=0, ok=True, output="second", session_id=session_id)
        adapter = Turn()
        extra = {}
        def lease_extra():
            if adapter.entered.is_set():
                extra["controls"] = [{"id": "c9", "action": "append", "text": "顺便补一个单元测试"}]
            return extra
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}], lease_extra)
        agent = self.agent(cloud, adapter)
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(adapter.calls[1], ("resume", "顺便补一个单元测试", "thread-1"))
        events = self.events(cloud)
        self.assert_contiguous(events)
        self.assertEqual([(e["type"], e.get("status")) for e in events],
                         [("running", None), ("control_applied", "queued_next_turn"),
                          ("control_applied", "applied"), ("completed", None)])
        self.assertEqual(events[-1]["result_summary"], "second")

    def test_append_is_not_run_when_the_zero_spend_gate_closes(self):
        class Turn(Adapter):
            verified = True
            def capabilities(self):
                caps = super().capabilities()
                caps["can_enforce_zero_spend"] = self.verified
                return caps
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.entered.set()
                time.sleep(.4)
                self.verified = False  # e.g. an API key appeared meanwhile
                return RunResult(exit_code=0, ok=True, output="first", session_id="thread-1")
            def resume(self, *a, **k):
                raise AssertionError("must not resume")
        adapter = Turn()
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}],
                             lambda: {"controls": [{"id": "c9", "action": "append", "text": "more"}]}
                             if adapter.entered.is_set() else {})
        agent = self.agent(cloud, adapter)
        # A closed gate after the spawn fences the whole job (as before).
        self.assertEqual(agent.run_once(), "job j1 → fenced (billing_unverified)")

    def test_append_without_a_resumable_session_is_rejected(self):
        class Turn(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.entered.set()
                time.sleep(.4)
                return RunResult(exit_code=0, ok=True, output="first", session_id="-not-a-session")
            def resume(self, *a, **k):
                raise AssertionError("must not resume")
        adapter = Turn()
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}],
                             lambda: {"controls": [{"id": "c9", "action": "append", "text": "more"}]}
                             if adapter.entered.is_set() else {})
        self.assertEqual(self.agent(cloud, adapter).run_once(), "job j1 → awaiting_review")
        applied = [e for e in self.events(cloud) if e["type"] == "control_applied"]
        self.assertEqual([e["status"] for e in applied], ["queued_next_turn", "rejected"])
        self.assertIn("未执行", applied[-1]["message"])


class ApprovalJobTest(Base):
    def test_approval_request_rides_the_job_and_the_decision_comes_back_on_renewal(self):
        class Asking(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                self.entered.set()
                self.answer = control.permission({"tool_name": "Bash", "input": {"command": "npm install left-pad"}},
                                                 threading.Event())
                return RunResult(exit_code=0, ok=True, output="installed", session_id=session_id)
        adapter = Asking()
        def lease_extra():
            asked = [e for _, e in cloud.events if e["type"] == "approval_requested"]
            if asked:
                return {"approvals": [{"request_id": asked[0]["request_id"], "decision": "approve",
                                       "remember": False}], "renew_after_seconds": 10}
            return {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1", "provider": "claude",
                               "tool_profile_id": "claude-default"}], lease_extra)
        agent = self.agent(cloud, adapter, provider="claude")
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(adapter.answer, {"behavior": "allow"})
        events = self.events(cloud)
        self.assert_contiguous(events)
        self.assertEqual([e["type"] for e in events],
                         ["running", "approval_requested", "approval_resolved", "completed"])
        self.assertEqual(events[1]["summary"], "运行 npm install left-pad")
        self.assertEqual(events[1]["input"], {"command": "npm install left-pad"})
        self.assertEqual(events[2]["outcome"], "approved")

    def test_renewal_interval_follows_renew_after_seconds(self):
        agent = self.agent(ControlCloud([]), Adapter(), heartbeat_interval=30)
        class C:
            renew_after = 10.0
        self.assertEqual(agent._renew_wait("", time.time() + 90, C()), 10.0)
        self.assertEqual(agent._renew_wait("", time.time() + 90, None), 30)


if __name__ == "__main__":
    unittest.main()
