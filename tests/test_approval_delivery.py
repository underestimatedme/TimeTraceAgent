"""A new approval reaches Valley inside its budget, whatever else is slow.

Production 2026-11-12 (job 22383aff…), in seconds:
    0.0   A2's expiry event is queued; its upload is outstanding until 19.0 (15:46:06 -> 15:46:25)
   10.06  the Runner asks for A3 (15:46:16.060)
   1.7    a healthy upload of the new request (queued 29.33, acknowledged 31)
Budget: the page must show A3 within 10 s of the Runner's request. The page needs its 4 s
poll plus one normal read (1 s), so Valley must hold the request 5 s after the Runner asks.

Real threads and real time, every duration of the evidence multiplied by SCALE so a scenario
takes seconds. `ValleyModel` follows Valley's AppendRemoteEvents rules (remote.go): events are
applied in strict sequence under the attempt's lock, an event at or below the held sequence is
skipped before anything is validated, an attempt that ended or lost its lease answers 409, and
an acknowledgement is only given after the commit. Latency is scripted per call.
"""
import threading
import time
import unittest

from timetrace import agent as agent_module, approval_delivery
from timetrace.cloud import CloudError
from timetrace.models import RunResult
from tests.test_parallel import QueueCloud, lease
from tests.test_remote_jobs import Adapter, Base

SCALE = .25
S = lambda seconds: seconds * SCALE
OLD_UPLOAD = S(19.0)            # the outstanding upload ahead of the new request
ASK_AFTER = S(10.06)            # the Runner asks this long after the old event was queued
NORMAL = S(1.7)                 # a healthy upload
BUDGET = S(5.0)                 # Runner asks -> Valley holds it (10 s - 4 s poll - 1 s read)
REQUESTS = [{"tool_name": "Bash", "input": {"command": "echo first"}},
            {"tool_name": "Bash", "input": {"command": "echo second"}}]


class ValleyModel(QueueCloud):
    def __init__(self, plan=None):
        super().__init__([{"id": "j1", "plan_id": "p1", "provider": "claude", "tool_profile_id": "claude-default"}])
        self.plan = plan or (lambda call, seqs, types: {})
        self.calls, self.persisted, self.last = [], {}, 0
        self.fenced = False
        self.stale = {}                    # attempt id -> seconds a request for it takes before Valley answers 409
        self.attempt_lock = threading.Lock()
        self.stall_until = 0.0             # Valley itself is stuck for this attempt until this monotonic time
        self.down_until = 0.0              # connection refused until this monotonic time
        self.inflight = self.max_inflight = 0
        self.fast_active = []              # approval-path requests (they carry a timeout) in flight: [first, last] seq
        self.fast_calls, self.fast_overlaps = [], []

    def append_events(self, token, job_id, attempt_id, epoch, events, timeout=None):
        seqs, types = [e["seq"] for e in events], [e["type"] for e in events]
        with self.lock:
            call = len(self.calls)
            self.calls.append((time.monotonic(), attempt_id, seqs, types))
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            mine = [seqs[0], seqs[-1]]
            if timeout is not None:
                self.fast_calls.append((time.monotonic(), seqs[0], seqs[-1]))
                for other in self.fast_active:
                    self.fast_overlaps.append((tuple(mine), tuple(other)))
                self.fast_active.append(mine)
        try:
            return self._serve(job_id, attempt_id, events, seqs, types, call, timeout)
        finally:
            with self.lock:
                self.inflight -= 1
                if timeout is not None:
                    self.fast_active.remove(mine)

    def _serve(self, job_id, attempt_id, events, seqs, types, call, timeout):
        deadline = time.monotonic() + (timeout or S(30))

        def wait(seconds):
            if time.monotonic() + seconds > deadline:
                time.sleep(max(0, deadline - time.monotonic()))
                raise TimeoutError("client timeout")
            time.sleep(seconds)
        if time.monotonic() < self.down_until:
            wait(S(0.05))
            raise ConnectionError("connection refused")
        if attempt_id in self.stale:
            wait(self.stale[attempt_id])
            raise CloudError("stale attempt", 409)
        plan = self.plan(call, seqs, types) or {}
        try:
            wait(plan.get("before", 0))              # on the way to Valley: a timeout here means it never arrived
        except TimeoutError:
            if plan.get("arrives_late"):
                threading.Timer(plan["before"] - max(0, deadline - time.monotonic()),
                                lambda: self._commit(job_id, attempt_id, events, late=True)).start()
            raise
        wait(NORMAL / 2)
        if not self.attempt_lock.acquire(timeout=max(0, deadline - time.monotonic())):   # Valley's FOR UPDATE on the attempt
            raise TimeoutError("client timeout waiting for the attempt lock")
        try:
            wait(max(plan.get("hold", 0), self.stall_until - time.monotonic()))
            self._commit(job_id, attempt_id, events)
        finally:
            self.attempt_lock.release()
        wait(plan.get("after", 0))                   # the acknowledgement is slow or lost, the commit is not
        wait(NORMAL / 2)
        return {}

    def _commit(self, job_id, attempt_id, events, late=False):
        with self.lock:
            if self.fenced:
                if late:
                    return
                raise CloudError("attempt ended or lease lost", 409)
            for e in events:
                if e["seq"] <= self.last:
                    continue                          # held already: skipped before anything is validated
                if e["seq"] != self.last + 1:
                    if late:
                        return
                    raise CloudError("sequence gap", 409)
                self.last = e["seq"]
                self.persisted[e["seq"]] = time.monotonic()
                self.events.append((job_id, e))

    def renew(self, token, attempt_id, epoch):
        asked = [e for _, e in list(self.events) if e["type"] == "approval_requested"]
        return {"lease_expires_at": lease(), "renew_after_seconds": 1,
                "approvals": [{"request_id": e["request_id"], "decision": "approve", "remember": False} for e in asked]}

    def seq_of_second_request(self):
        return [e["seq"] for _, e in self.events if e["type"] == "approval_requested"][1]


class DeliveryBase(Base):
    def setUp(self):
        super().setUp()
        self.lines, self.emitted = [], {}
        self.patches = [(approval_delivery, "APPROVAL_UPLOAD_TIMEOUT_SECONDS", S(2.5)),
                        (agent_module, "OUTBOX_ERROR_RETRY_FIRST_SECONDS", S(5)),
                        (agent_module, "OUTBOX_ERROR_RETRY_SECONDS", S(30)),
                        (agent_module, "OUTBOX_REFUSED_RETRY_SECONDS", S(60))]
        self.saved = [(m, n, getattr(m, n)) for m, n, _ in self.patches]
        for m, n, v in self.patches:
            setattr(m, n, v)

    def tearDown(self):
        for m, n, v in self.saved:
            setattr(m, n, v)
        super().tearDown()

    def run_job(self, cloud, after_resolved=None, ask_after=ASK_AFTER):
        emitted, test = self.emitted, self

        class Asking(Adapter):
            interactive_runs = True

            def start(self, prompt, cwd, session_id, log_file, cancel_event=None, control=None):
                emitted["first"] = time.monotonic()
                control.permission(REQUESTS[0], threading.Event())
                emitted["resolved"] = time.monotonic()
                if after_resolved:
                    after_resolved(test.agent_under_test)
                time.sleep(ask_after)
                emitted["second"] = time.monotonic()
                control.permission(REQUESTS[1], threading.Event())
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)
        self.agent_under_test = self.agent(cloud, Asking(), provider="claude", log=self.lines.append)
        runner = threading.Thread(target=self.agent_under_test.run_once, daemon=True)
        runner.start(); runner.join(S(80))
        if runner.is_alive():       # a hang is a failure with a message, not a stalled suite
            self.agent_under_test.stop(); runner.join(5)
            self.fail("the job was still waiting for an approval decision after %.0fs: the request never reached Valley "
                      "(Valley holds %s)" % (S(80), sorted(cloud.persisted)))
        return self.agent_under_test

    def first_resolved_is_slow(self, **slow):
        """Only the first upload that carries the expiry/decision event misbehaves."""
        seen = []

        def plan(call, seqs, types):
            if "approval_resolved" in types and not seen:
                seen.append(call)
                return slow
            return {}
        return plan

    def assert_in_budget(self, cloud):
        delay = cloud.persisted[cloud.seq_of_second_request()] - self.emitted["second"]
        visible = delay + S(4.0) + S(1.0)            # the page's poll and one normal read
        self.assertLessEqual(delay, BUDGET, "Valley held the new approval %.2fs after the Runner asked (budget %.2fs); "
                             "the page would show it after %.2fs of %.2fs" % (delay, BUDGET, visible, S(10)))
        return delay

    def assert_valley_state_is_exact(self, cloud):
        seqs = [e["seq"] for _, e in cloud.events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), "Valley must hold a contiguous sequence exactly once")
        self.assertEqual([e["type"] for _, e in cloud.events],
                         ["running", "approval_requested", "approval_resolved", "approval_requested",
                          "approval_resolved", "completed"])


class ProductionProportionsTest(DeliveryBase):
    """The new request's own path is healthy; something else is slow."""

    def test_previous_upload_of_the_same_attempt_acknowledged_late(self):
        cloud = ValleyModel(); cloud.plan = self.first_resolved_is_slow(after=OLD_UPLOAD)    # committed, ACK 19 s later
        self.run_job(cloud)
        self.assert_in_budget(cloud); self.assert_valley_state_is_exact(cloud)

    def test_previous_upload_of_the_same_attempt_still_on_the_way_then_arrives_late(self):
        cloud = ValleyModel(); cloud.plan = self.first_resolved_is_slow(before=OLD_UPLOAD, arrives_late=True)
        self.run_job(cloud)
        time.sleep(S(19))                                                                     # the old request lands: nothing changes
        self.assert_in_budget(cloud); self.assert_valley_state_is_exact(cloud)

    def test_global_flush_lock_held_by_another_upload(self):
        cloud = ValleyModel()

        def hold(agent):
            ready = threading.Event()

            def run():
                with agent._flush_lock:
                    ready.set(); time.sleep(OLD_UPLOAD)
            threading.Thread(target=run, daemon=True).start(); ready.wait(2)
        self.run_job(cloud, after_resolved=hold)
        self.assert_in_budget(cloud); self.assert_valley_state_is_exact(cloud)

    def test_a_stale_attempts_batch_ahead_in_the_queue_answers_409_slowly(self):
        cloud = ValleyModel(); cloud.stale["a-old"] = OLD_UPLOAD

        def stale_ahead(agent):
            agent.db.queue_remote_event("old", "a-old", 1, {"seq": 1, "type": "running", "observed_at": "2026-11-01T00:00:00Z"})
            def claim_loop_flush():
                try:
                    agent.flush_outbox()                       # the claim loop's flush takes the lock and waits for the 409
                except CloudError:
                    pass
            threading.Thread(target=claim_loop_flush, daemon=True).start()
        self.run_job(cloud, after_resolved=stale_ahead)
        self.assert_in_budget(cloud)
        self.assertEqual([e["seq"] for j, e in cloud.events if j == "j1"], [1, 2, 3, 4, 5, 6])


class ShortTimeoutRetryTest(DeliveryBase):
    def test_committed_but_unacknowledged_upload_is_retried_without_duplicating_or_gapping(self):
        """The upload carrying the new request commits and its ACK is lost: the short timeout fires,
        the retry replays the tail, Valley skips what it holds and answers."""
        cloud = ValleyModel(); lost = []

        def plan(call, seqs, types):
            if "approval_requested" in types and 4 in seqs and not lost:
                lost.append(call); return {"after": OLD_UPLOAD}
            return {}
        cloud.plan = plan
        self.run_job(cloud)
        self.assert_in_budget(cloud); self.assert_valley_state_is_exact(cloud)
        retried = [c for c in cloud.calls if 4 in c[2]]
        self.assertGreaterEqual(len(retried), 2, "the timed-out upload was never retried")
        self.assertEqual(self.agent_under_test.db.conn.execute(
            "SELECT COUNT(*) FROM remote_outbox WHERE sent_at IS NULL").fetchone()[0], 0)
        self.assertIsNone(self.agent_under_test.db.remote_event_retry("j1", "a-j1", 1))

    def test_upload_lost_on_the_way_is_retried_inside_the_budget(self):
        cloud = ValleyModel(); lost = []

        def plan(call, seqs, types):
            if 4 in seqs and not lost:
                lost.append(call); return {"before": OLD_UPLOAD, "arrives_late": True}
            return {}
        cloud.plan = plan
        self.run_job(cloud)
        self.assert_in_budget(cloud); self.assert_valley_state_is_exact(cloud)

    def test_a_slow_upload_that_fails_late_does_not_back_off_events_a_newer_one_delivered(self):
        cloud = ValleyModel(); cloud.plan = self.first_resolved_is_slow(before=OLD_UPLOAD)
        agent = self.run_job(cloud)
        time.sleep(S(32))
        self.assertIsNone(agent.db.remote_event_retry("j1", "a-j1", 1))


class WhenValleyItselfIsTheSlowPartTest(DeliveryBase):
    """Nothing on this computer can beat a stalled or unavailable Valley. The Agent must not make it
    worse: bounded concurrent requests, no gap or duplicate, and delivery as soon as Valley recovers."""

    def test_stall_inside_valley_is_not_amplified_and_recovers(self):
        cloud = ValleyModel()
        stall_end = []

        def stall(agent):
            cloud.stall_until = time.monotonic() + S(19.0); stall_end.append(cloud.stall_until)
        self.run_job(cloud, after_resolved=stall)
        delay = cloud.persisted[cloud.seq_of_second_request()] - self.emitted["second"]
        self.assertGreaterEqual(delay, stall_end[0] - self.emitted["second"] - .05, "cannot be faster than Valley")
        self.assertLessEqual(delay, stall_end[0] - self.emitted["second"] + S(30), "must recover on the backoff")
        self.assertLessEqual(cloud.max_inflight, approval_delivery.APPROVAL_UPLOAD_MAX_INFLIGHT + 1)
        self.assertLessEqual(len(cloud.calls), 40, "request storm during the stall: %d calls" % len(cloud.calls))
        self.assert_valley_state_is_exact(cloud)

    def test_conflict_is_not_hammered_and_events_survive_until_the_attempt_is_accepted_again(self):
        agent_module.OUTBOX_REFUSED_RETRY_SECONDS = S(8)
        cloud = ValleyModel(); marks = []

        def fence(agent):
            cloud.fenced = True; marks.append(len(cloud.calls))
            threading.Timer(S(8), lambda: setattr(cloud, "fenced", False)).start()
        self.run_job(cloud, after_resolved=fence, ask_after=S(1))
        during = [c for c in cloud.calls[marks[0]:] if c[0] < self.emitted["second"] + S(7)]
        self.assertLessEqual(len(during), 6, "409 retried in a loop: %d requests" % len(during))
        self.assert_valley_state_is_exact(cloud)
        self.assertTrue(any("HTTP 409" in line or "409" in line for line in self.lines), self.lines)


class QuietRetryAndShutdownTest(DeliveryBase):
    """The retry tick and stop() must not cost a new approval its slot, nor the job its exit."""

    def test_quiet_retry_never_resends_a_tail_that_is_in_flight_and_a_new_approval_still_gets_a_slot(self):
        cloud = ValleyModel()

        def stall(agent):
            cloud.stall_until = time.monotonic() + S(19.0)          # every request hangs until its timeout
        self.run_job(cloud, after_resolved=stall, ask_after=S(6.0))
        # An overlap is only allowed for a strictly newer tail (a new event); a quiet retry of the same tail never overlaps.
        for new, old in cloud.fast_overlaps:
            self.assertGreater(new[1], old[1], "a retry resent a tail already in flight: new=%s in flight=%s" % (new, old))
        self.assertLessEqual(len([c for c in cloud.fast_calls if c[2] == cloud.seq_of_second_request()]) , 6)
        # the new approval was sent within a moment of the Runner asking, though earlier uploads were hanging
        first_try = min(c[0] for c in cloud.fast_calls if c[2] >= 4)
        self.assertLessEqual(first_try - self.emitted["second"], S(1.0), "the new approval waited for a slot")
        self.assert_valley_state_is_exact(cloud)

    def test_temporary_outage_is_retried_without_a_new_event_on_the_existing_backoff(self):
        cloud = ValleyModel(); cloud.down_until = time.monotonic() + S(12.0)
        self.run_job(cloud, ask_after=S(0.5))
        first = cloud.persisted[2]                                   # the first approval request
        began = self.emitted["first"]
        self.assertGreaterEqual(first - began, S(12.0) - .05, "cannot be delivered while Valley refuses connections")
        self.assertLessEqual(first - began, S(12.0) + S(30.0) + S(2.0), "stranded: no new event, decision or TTL should be needed")
        during = [c for c in cloud.fast_calls if c[0] < cloud.persisted[2]]
        self.assertLessEqual(len(during), 12, "request storm during the outage: %d approval-path requests" % len(during))
        self.assertLessEqual(cloud.max_inflight, 2 + approval_delivery.APPROVAL_UPLOAD_MAX_INFLIGHT)
        self.assert_valley_state_is_exact(cloud)

    def test_quiet_ticks_do_not_stack_uploads_of_the_same_tail_and_leave_slots_for_a_new_event(self):
        """Pump level: an upload hangs, the tail is still 'due' at every tick."""
        release = threading.Event(); self.addCleanup(release.set)
        old_tick, approval_delivery._RETRY_TICK_SECONDS = approval_delivery._RETRY_TICK_SECONDS, .1
        self.addCleanup(setattr, approval_delivery, "_RETRY_TICK_SECONDS", old_tick)
        old_wake, approval_delivery._IDLE_WAKE_SECONDS = approval_delivery._IDLE_WAKE_SECONDS, .05
        self.addCleanup(setattr, approval_delivery, "_IDLE_WAKE_SECONDS", old_wake)

        class Control:
            closed = False; started = True
            pending = threading.Event(); wakeup = threading.Event()

        class StubAgent:
            def __init__(self): self.forced, self.quiet = 0, 0
            def log(self, line): pass
            def _report(self, claim, events, flush=True): Control.pending.clear()
            def _upload_due(self, claim): return True
            def fast_begin(self, claim): pass
            def fast_end(self, claim): pass
            def _upload_attempt(self, claim, force=True):
                if force: self.forced += 1
                else: self.quiet += 1
                release.wait(30)
        control, agent = Control(), StubAgent()
        pump = approval_delivery.ApprovalPump(agent, {"job": {"id": "j"}, "attempt_id": "a", "lease_epoch": 1}, control).start()
        self.addCleanup(lambda: (setattr(control, "closed", True), release.set(), pump.stop()))
        time.sleep(1.0)                                                  # ten ticks while the first quiet upload hangs
        self.assertEqual(agent.quiet, 1, "quiet retries stacked %d identical uploads (3 slots in all)" % agent.quiet)
        control.pending.set(); time.sleep(.3)                            # a new event beside it
        control.pending.set(); time.sleep(.3)
        self.assertEqual(agent.forced, 2, "new events must still get slots beside the quiet retry")

    def test_stop_returns_in_a_bounded_time_even_if_an_upload_never_returns(self):
        released = threading.Event(); self.addCleanup(released.set)

        class Control:
            closed = False; started = True
            pending = threading.Event(); wakeup = threading.Event()

        class StubAgent:
            def __init__(self): self.begun = self.ended = 0
            def log(self, line): pass
            def _report(self, claim, events, flush=True): Control.pending.clear()      # like take_events
            def _upload_due(self, claim): return False
            def fast_begin(self, claim): self.begun += 1
            def fast_end(self, claim): self.ended += 1
            def _upload_attempt(self, claim, force=True): released.wait(60)      # a stuck credential or socket call
        control, agent = Control(), StubAgent()
        pump = approval_delivery.ApprovalPump(agent, {"job": {"id": "j"}, "attempt_id": "a", "lease_epoch": 1}, control).start()
        control.pending.set(); time.sleep(.2)                        # an upload is now hung
        self.assertEqual(agent.begun, 1)
        began = time.monotonic(); control.closed = True; pump.stop()
        self.assertLess(time.monotonic() - began, approval_delivery.APPROVAL_PUMP_STOP_WAIT_SECONDS + .5)

    def test_a_stuck_upload_stops_counting_against_the_slots_and_the_normal_flush(self):
        agent = self.agent(ValleyModel(), Adapter(), provider="claude")
        claim = {"job": {"id": "j1"}, "attempt_id": "a-j1", "lease_epoch": 1}
        old = approval_delivery.APPROVAL_UPLOAD_STUCK_AFTER_SECONDS
        approval_delivery.APPROVAL_UPLOAD_STUCK_AFTER_SECONDS = .3
        try:
            agent.fast_begin(claim)                                  # an upload that never ends
            self.assertTrue(agent._fast_live(("j1", "a-j1", 1)))
            began = time.monotonic(); agent._wait_fast(("j1", "a-j1", 1))
            self.assertLess(time.monotonic() - began, .8)
            self.assertFalse(agent._fast_live(("j1", "a-j1", 1)))
        finally:
            approval_delivery.APPROVAL_UPLOAD_STUCK_AFTER_SECONDS = old

    def test_a_conflict_that_races_a_success_does_not_back_off_acknowledged_events(self):
        cloud = ValleyModel()
        agent = self.agent(cloud, Adapter(), provider="claude")
        claim = {"job": {"id": "j1"}, "attempt_id": "a-j1", "lease_epoch": 1}
        agent.db.queue_remote_event("j1", "a-j1", 1, {"seq": 1, "type": "running", "observed_at": "2026-11-12T00:00:00Z"})
        rows = agent.db.pending_remote_events_for("j1", "a-j1", 1)
        calls = []

        def conflict(*args, **kwargs):
            agent.db.mark_remote_events_sent([r["id"] for r in rows])      # a newer upload got them acknowledged first
            calls.append(1); raise CloudError("late conflict", 409)
        cloud.append_events = conflict
        agent._fast_rows = lambda key: rows
        agent._upload_attempt(claim)
        self.assertEqual(calls, [1])
        self.assertIsNone(agent.db.remote_event_retry("j1", "a-j1", 1), "a false 60 s backoff on acknowledged events")


if __name__ == "__main__":
    unittest.main()
