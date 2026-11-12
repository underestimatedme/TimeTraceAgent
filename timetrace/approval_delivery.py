"""Prompt delivery of approval requests and control acknowledgements.

Why this exists. Approval events used to be queued by the job's supervising
thread and sent by the same thread inside the one, globally locked outbox
flush. A slow upload ahead of a new approval - the same attempt's previous
event, another attempt's stale batch, or whoever held the flush lock -
therefore spent the new approval's whole budget before it was even queued
(production 2026-11-12: the Runner asked at 15:46:16.06, the Agent queued it at
15:46:29.33, with the previous event's upload outstanding until 15:46:25).

What it does. One `ApprovalPump` per running job with a RunControl:

- a thread of its own wakes the moment the RunControl has an event, queues it
  in the durable outbox (same `_report` as before, under a lock that keeps
  sequence numbers allocated one at a time), and hands the attempt to a few
  daemon upload threads;
- an uploader sends that attempt's whole unacknowledged tail (a cumulative
  batch) with a short per-request timeout and one immediate retry. It takes
  neither the global flush lock nor anyone else's batch, so a slow or stuck
  upload elsewhere cannot hold it;
- at most `APPROVAL_UPLOAD_MAX_INFLIGHT` uploads of one attempt are in flight; a new
  event always goes out beside them, a quiet retry only when none is in flight, and
  more events than slots coalesce into the next upload. stop() waits a fixed, short
  time, never for an upload that does not return.

What it relies on, and what it does not change. Valley applies an attempt's
events strictly in sequence and skips every event at or below the sequence it
already holds before validating anything, so overlapping, replayed and
late-arriving batches are idempotent and an acknowledgement is only given for
committed events. A row is marked sent only after such an acknowledgement.
Conflicts (409: ended, cancelled, fenced or lease-expired attempt) are handled
exactly as the normal flush handles them: the batch stays queued and is retried
rarely, never dropped. A 401 is left to the normal flush, which refreshes the
credentials. Event contents, sequence numbers and the wire format are unchanged.

What it cannot do. If Valley itself is slow or down for the new request too
(a stall inside Valley, an outage), nothing on this computer can deliver
sooner; after the two bounded tries the rows simply wait for the normal
flush's backoff.
"""
import threading
import time
from typing import Any, Dict, List


# Budget (Runner asks -> Valley holds it): 10 s end to end, minus the page's 4 s
# poll and one normal read (about 1 s) = 5 s. A healthy POST took 1-2 s in
# production, so one lost request (2.5 s) plus a normal one fits; more than one
# lost request means Valley is the slow part.
APPROVAL_UPLOAD_TIMEOUT_SECONDS = 2.5
APPROVAL_UPLOAD_ATTEMPTS = 2
APPROVAL_UPLOAD_MAX_INFLIGHT = 3
_IDLE_WAKE_SECONDS = 0.5
_RETRY_TICK_SECONDS = 1.0
# A socket timeout is not a wall-clock deadline (a trickling response or a credential
# wait can outlast it). An upload older than this stops counting against the slots
# and against the normal flush's wait, and stop() never waits longer than STOP_WAIT.
APPROVAL_UPLOAD_STUCK_AFTER_SECONDS = APPROVAL_UPLOAD_TIMEOUT_SECONDS * APPROVAL_UPLOAD_ATTEMPTS + 3
APPROVAL_PUMP_STOP_WAIT_SECONDS = 1.0


class ApprovalPump:
    def __init__(self, agent: Any, claim: Dict[str, Any], control: Any):
        self.agent, self.claim, self.control = agent, claim, control
        self._lock = threading.Lock()
        self._uploads: List[Any] = []        # [thread, started (monotonic)]
        self._again = False
        self._thread = threading.Thread(target=self._run, name="approval-pump", daemon=True)

    def start(self) -> "ApprovalPump":
        self._thread.start()
        return self

    def stop(self) -> None:
        """The job is over (RunControl.close() was called). Waits a short, fixed time
        for the pump and for uploads in flight, never for a stuck one: the threads are
        daemons, an upload is idempotent, and whatever it did not finish stays queued
        for the normal flush."""
        self.control.pending.set()  # do not make the job wait for the next idle tick
        end = time.monotonic() + APPROVAL_PUMP_STOP_WAIT_SECONDS
        self._thread.join(max(0.0, end - time.monotonic()))
        with self._lock:
            uploads = list(self._uploads)
        for thread, _ in uploads:
            thread.join(max(0.0, end - time.monotonic()))

    # ---- the pump thread --------------------------------------------------
    def _run(self) -> None:
        control = self.control
        ticked = time.monotonic()
        while not control.closed:
            control.pending.wait(_IDLE_WAKE_SECONDS)
            if control.closed:
                break
            if control.pending.is_set() and not control.started:
                time.sleep(.05)
                continue
            if not control.pending.is_set():
                # Nothing new: events the fast tries could not send are retried
                # on the normal backoff, quietly (see _submit).
                if time.monotonic() - ticked >= _RETRY_TICK_SECONDS:
                    ticked = time.monotonic()
                    try:
                        if self.agent._upload_due(self.claim):
                            self._submit(force=False)
                    except Exception as exc:
                        self.agent.log("approval pump: retry check failed: %s" % type(exc).__name__)
                continue
            try:
                self.agent._report(self.claim, [], flush=False)
            except Exception as exc:  # queueing failed; the terminal report still carries the events
                self.agent.log("approval pump: queueing failed: %s" % type(exc).__name__)
                time.sleep(.2)
                continue
            self._submit(force=True)

    def _live(self) -> List[Any]:
        """Uploads still counted: running, and not past the stuck limit. Caller holds the lock."""
        now = time.monotonic()
        self._uploads = [u for u in self._uploads if u[0].is_alive()]
        return [u for u in self._uploads if now - u[1] < APPROVAL_UPLOAD_STUCK_AFTER_SECONDS]

    def _submit(self, force: bool = True) -> None:
        """force: a new event, which goes out at once beside whatever is in flight (its batch is
        the whole unacknowledged tail, so it carries older events too). A quiet retry (force=False)
        starts only when nothing is in flight: it must neither resend the tail an upload is already
        sending nor use up the slots a new approval needs."""
        with self._lock:
            live = self._live()
            if not force and live:
                return
            if len(live) >= APPROVAL_UPLOAD_MAX_INFLIGHT:
                self._again = True
                return
            thread = threading.Thread(target=self._upload, args=(force,), name="approval-upload", daemon=True)
            self._uploads.append([thread, time.monotonic()])
        self.agent.fast_begin(self.claim)  # before the thread starts: the normal flush must see it
        thread.start()

    def _upload(self, force: bool) -> None:
        try:
            self.agent._upload_attempt(self.claim, force)
        except Exception as exc:
            self.agent.log("approval upload: %s" % type(exc).__name__)
        finally:
            self.agent.fast_end(self.claim)
            with self._lock:
                again, self._again = self._again, False
            if again and not self.control.closed:
                self._submit()
