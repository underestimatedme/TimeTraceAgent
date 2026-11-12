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
  sequence numbers allocated one at a time), and hands the attempt to a small
  pool of uploaders;
- an uploader sends that attempt's whole unacknowledged tail (a cumulative
  batch) with a short per-request timeout and one immediate retry. It takes
  neither the global flush lock nor anyone else's batch, so a slow or stuck
  upload elsewhere cannot hold it;
- at most `APPROVAL_UPLOAD_MAX_INFLIGHT` uploads of one attempt are in flight;
  more events coalesce into the next one.

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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict


# Budget (Runner asks -> Valley holds it): 10 s end to end, minus the page's 4 s
# poll and one normal read (about 1 s) = 5 s. A healthy POST took 1-2 s in
# production, so one lost request (2.5 s) plus a normal one fits; more than one
# lost request means Valley is the slow part.
APPROVAL_UPLOAD_TIMEOUT_SECONDS = 2.5
APPROVAL_UPLOAD_ATTEMPTS = 2
APPROVAL_UPLOAD_MAX_INFLIGHT = 3
_IDLE_WAKE_SECONDS = 0.5
_RETRY_TICK_SECONDS = 1.0
_STOP_WAIT_SECONDS = APPROVAL_UPLOAD_TIMEOUT_SECONDS * APPROVAL_UPLOAD_ATTEMPTS + 2


class ApprovalPump:
    def __init__(self, agent: Any, claim: Dict[str, Any], control: Any):
        self.agent, self.claim, self.control = agent, claim, control
        self._lock = threading.Lock()
        self._inflight = 0
        self._again = False
        self._pool = ThreadPoolExecutor(max_workers=APPROVAL_UPLOAD_MAX_INFLIGHT,
                                        thread_name_prefix="approval-upload")
        self._thread = threading.Thread(target=self._run, name="approval-pump", daemon=True)

    def start(self) -> "ApprovalPump":
        self._thread.start()
        return self

    def stop(self) -> None:
        """The job is over (RunControl.close() was called): let the uploads in
        flight finish - they are bounded - and release the threads."""
        self._thread.join(_STOP_WAIT_SECONDS)
        self._pool.shutdown(wait=True)

    # ---- the pump thread --------------------------------------------------
    def _run(self) -> None:
        control = self.control
        control.delivering = True
        try:
            self._loop(control)
        finally:
            control.delivering = False

    def _loop(self, control: Any) -> None:
        ticked = time.monotonic()
        while not control.closed:
            control.wakeup.wait(_IDLE_WAKE_SECONDS)
            if control.closed:
                break
            if not control.started:
                continue
            if not control.wakeup.is_set():
                # Nothing new: events the fast tries could not send are retried
                # on the normal backoff (the supervising loop no longer flushes
                # control events itself while this pump delivers them).
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

    def _submit(self, force: bool = True) -> None:
        with self._lock:
            if self._inflight >= APPROVAL_UPLOAD_MAX_INFLIGHT:
                self._again = True
                return
            self._inflight += 1
        try:
            self._pool.submit(self._upload, force)
        except RuntimeError:  # pool already shut down: the job is over
            with self._lock:
                self._inflight -= 1

    def _upload(self, force: bool) -> None:
        try:
            self.agent._upload_attempt(self.claim, force)
        except Exception as exc:
            self.agent.log("approval upload: %s" % type(exc).__name__)
        finally:
            with self._lock:
                self._inflight -= 1
                again, self._again = self._again, False
            if again and not self.control.closed:
                self._submit()
