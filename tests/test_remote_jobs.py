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


class RunningEventTest(Base):
    def test_running_reaches_valley_while_the_tool_still_runs(self):
        """A sequential agent (run_once, max_parallel 1) has no other loop to
        flush the outbox: `running` must go out while the job runs, not with
        its terminal event, so the phone sees the job running."""
        seen = threading.Event()
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}])

        class Waits(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                deadline = time.time() + 5
                while time.time() < deadline and not seen.is_set():
                    if any(e["type"] == "running" for e in Base.events(cloud)):
                        seen.set()
                    time.sleep(.02)
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)
        self.assertEqual(self.agent(cloud, Waits()).run_once(), "job j1 → awaiting_review")
        self.assertTrue(seen.is_set(), "running was only sent after the run ended")
        self.assertEqual([e["type"] for e in self.events(cloud)], ["running", "completed"])


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


class ApprovalDeliveryTimingTest(Base):
    """A new approval must not wait behind a lease renewal.

    Production 2026-11-12 (job 22383aff…): approval 2 expired while approval 3 was
    being requested. The expiry flush took ~19 s; approval 3 was asked 10 s into
    it, but the supervising loop renewed the lease (~4 s) before reporting it,
    and the phone saw it 16.6 s after the Runner asked."""

    SLOW_FLUSH = .6
    SLOW_RENEW = .4

    def run_scenario(self):
        calls, asked_at, arrived_at = [], {}, {}

        class Slow(ControlCloud):
            def append_events(inner, token, job_id, attempt_id, epoch, events):
                types = [e["type"] for e in events]
                calls.append(("append", types, time.monotonic()))
                for e in events:
                    if e["type"] == "approval_requested":
                        arrived_at.setdefault(e["request_id"], time.monotonic())
                if "approval_resolved" in types:
                    time.sleep(self.SLOW_FLUSH)      # Valley answers the expiry flush slowly
                super().append_events(token, job_id, attempt_id, epoch, events)

            def renew(inner, token, attempt_id, epoch):
                calls.append(("renew", [], time.monotonic()))
                time.sleep(self.SLOW_RENEW)
                return super().renew(token, attempt_id, epoch)

        class Asking(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                self.entered.set()
                cancelled = threading.Event()
                control.permission({"tool_name": "Bash", "input": {"command": "echo first"}}, cancelled)
                # The first decision's flush is slow; ask again while it is in flight.
                second = threading.Thread(target=lambda: control.permission(
                    {"tool_name": "Bash", "input": {"command": "echo second"}}, cancelled))
                second.start()
                time.sleep(.1)
                asked_at["second"] = time.monotonic() - .1
                second.join(20)
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)

        def lease_extra():
            asked = [e for _, e in cloud.events if e["type"] == "approval_requested"]
            return {"approvals": [{"request_id": e["request_id"], "decision": "approve", "remember": False}
                                  for e in asked[:1] + asked[1:]], "renew_after_seconds": 1}
        cloud = Slow([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default"}],
                     lease_extra)
        agent = self.agent(cloud, Asking(), provider="claude")
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        return calls, asked_at, arrived_at, cloud

    def test_new_approval_is_reported_before_the_next_lease_renewal(self):
        calls, asked_at, arrived_at, cloud = self.run_scenario()
        slow = next(i for i, c in enumerate(calls) if "approval_resolved" in c[1])
        requested = next(i for i, c in enumerate(calls) if i > slow and "approval_requested" in c[1])
        between = [c[0] for c in calls[slow + 1:requested]]
        self.assertNotIn("renew", between,
                         "a lease renewal ran between the slow flush and the report of the new approval: %s" % calls)

    def test_new_approval_reaches_valley_within_the_slow_flush_not_flush_plus_renewal(self):
        calls, asked_at, arrived_at, cloud = self.run_scenario()
        second_id = [e["request_id"] for _, e in cloud.events if e["type"] == "approval_requested"][1]
        delay = arrived_at[second_id] - asked_at["second"]
        # The flush in flight may take up to SLOW_FLUSH; nothing else may add to it.
        self.assertLess(delay, self.SLOW_FLUSH + self.SLOW_RENEW / 2,
                        "approval waited %.2fs: slow flush %.2fs plus a lease renewal %.2fs" % (
                            delay, self.SLOW_FLUSH, self.SLOW_RENEW))


class RenewalNotStarvedTest(Base):
    def test_a_steady_stream_of_events_cannot_hold_back_lease_renewal_beyond_the_bound(self):
        import timetrace.agent as module
        old = module.REPORT_BEFORE_RENEW_SECONDS
        module.REPORT_BEFORE_RENEW_SECONDS = .3
        stamps = []
        class Cloud(ControlCloud):
            def renew(inner, token, attempt_id, epoch):
                stamps.append(time.monotonic())
                return super().renew(token, attempt_id, epoch)
        cloud = Cloud([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default"}])
        class Chatty(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    with control._cond:
                        control._ack("c-%f" % time.monotonic(), "applied")
                    time.sleep(.005)
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)
        try:
            self.agent(cloud, Chatty(), provider="claude").run_once()
        finally:
            module.REPORT_BEFORE_RENEW_SECONDS = old
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(len(stamps), 3, stamps)
        self.assertLess(max(gaps), .3 + .4, gaps)   # heartbeat .05 + bounded hold-back + one report


class ApprovalDeliveryDiagnosticsTest(Base):
    """The missing link in the 2026-11-12 trace: the Agent said nothing about
    whether a slow upload waited on its own flush lock or on Valley."""

    def test_slow_upload_is_logged_with_lock_wait_and_post_time_but_no_payload(self):
        lines = []
        class SlowCloud(ControlCloud):
            def append_events(inner, token, job_id, attempt_id, epoch, events):
                time.sleep(.25)
                super().append_events(token, job_id, attempt_id, epoch, events)
        cloud = SlowCloud([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default"}])
        class Asking(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)
        agent = self.agent(cloud, Asking(), provider="claude", log=lines.append)
        import timetrace.agent as module
        old = module.SLOW_UPLOAD_LOG_SECONDS
        module.SLOW_UPLOAD_LOG_SECONDS = .2
        try:
            agent.run_once()
        finally:
            module.SLOW_UPLOAD_LOG_SECONDS = old
        slow = [l for l in lines if "slow upload" in l]
        self.assertTrue(slow, lines)
        self.assertRegex(slow[0], r"lock_wait=\d+\.\d+s post=\d+\.\d+s")
        self.assertNotIn("SECRET-ARG", " ".join(lines))

    def test_approval_request_that_waited_before_it_was_queued_is_logged(self):
        lines = []
        import timetrace.agent as module
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default"}])
        def lease_extra():
            asked = [e for _, e in cloud.events if e["type"] == "approval_requested"]
            return {"approvals": [{"request_id": e["request_id"], "decision": "approve", "remember": False}
                                  for e in asked]}
        cloud.lease_extra = lease_extra
        class Asking(Adapter):
            interactive_runs = True
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                control.permission({"tool_name": "Bash", "input": {"command": "echo SECRET-ARG"}}, threading.Event())
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)
        agent = self.agent(cloud, Asking(), provider="claude", log=lines.append)
        real_report, delayed = agent._report, []
        def late_report(claim, events, flush=True):
            if not delayed:
                delayed.append(1)
                time.sleep(.3)      # the supervising thread is busy elsewhere when the Runner asks
            return real_report(claim, events, flush)
        agent._report = late_report
        old = module.SLOW_UPLOAD_LOG_SECONDS
        module.SLOW_UPLOAD_LOG_SECONDS = .2
        try:
            agent.run_once()
        finally:
            module.SLOW_UPLOAD_LOG_SECONDS = old
        waited = [l for l in lines if "approval request waited" in l]
        self.assertTrue(waited, lines)
        self.assertRegex(waited[0], r"waited \d+\.\d+s before it was queued")
        self.assertNotIn("SECRET-ARG", " ".join(lines))


if __name__ == "__main__":
    unittest.main()
