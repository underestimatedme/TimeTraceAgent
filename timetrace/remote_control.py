"""Remote control of one running task job (protocol 2).

A RunControl sits between three parties:

- the job's supervising thread, which renews the lease and hands every
  response to `apply_lease()` (controls[] and approvals[] repeat until the
  computer acknowledges them, so both are de-duplicated here), and which
  drains `take_events()` into the job's event stream;
- the tool run: a Claude stream session calls `attach()` / `detach()` and
  `permission()`; any adapter's run ends with `next_followup()`;
- the phone, through Valley: interrupt, append, approval decisions.

Interrupt and completion race: whichever reaches this computer first wins.
Once the run has finished (`next_followup()` returned None) an interrupt is
ignored (Valley then rejects it); once an interrupt took effect the run is
reported `interrupted_by_user` unless the tool had already succeeded.
"""
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from timetrace import approvals, audit

APPROVAL_TIMEOUT_SECONDS = 600
# What the AI is told (spec §4).
TIMEOUT_DENY = "用户未批准，请换一种不需要该操作的做法或结束并说明"
USER_DENY = "用户拒绝了这个操作，请换一种不需要该操作的做法或结束并说明"
NO_PHONE_DENY = "这个运行不能请求手机批准；请换一种不需要该操作的做法或结束并说明"


class RunControl:
    def __init__(self, job_id: str, home: Path, cancel_event: threading.Event, root: Optional[str] = None,
                 approvals_enabled: bool = True, approval_timeout: float = APPROVAL_TIMEOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic, user_home: Optional[str] = None,
                 log: Callable[[str], None] = None):
        self.job_id, self.home, self.cancel_event = job_id, Path(home), cancel_event
        self.root = root
        self.approvals_enabled = approvals_enabled
        self.approval_timeout = approval_timeout
        self.clock = clock
        self.user_home = user_home
        self.log = log or (lambda message: None)
        self.wakeup = threading.Event()        # set when events wait to be reported
        self.renew_after: Optional[float] = None
        # Set once the job reported `running`: events may be sent from then on.
        self.started = False
        self._cond = threading.Condition()
        self._events: List[Dict[str, Any]] = []
        self._seen_controls = set()
        self._queued: List[Tuple[str, str]] = []
        self._send: Optional[Callable[[str], bool]] = None
        self._finished = False
        self._closed = False
        self.interrupted = False
        self.interrupt_id = ""
        self._decisions: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._remembered = set()

    # ---- job thread: lease responses and events ---------------------------
    def apply_lease(self, lease: Any) -> None:
        if not isinstance(lease, dict):
            return
        after = lease.get("renew_after_seconds")
        if isinstance(after, (int, float)) and not isinstance(after, bool) and 1 <= after <= 300:
            self.renew_after = float(after)
        for control in lease.get("controls") or []:
            if isinstance(control, dict):
                self._control(control)
        for decision in lease.get("approvals") or []:
            if isinstance(decision, dict):
                self._decision(decision)

    def take_events(self) -> List[Dict[str, Any]]:
        with self._cond:
            events, self._events = self._events, []
            self.wakeup.clear()
        return events

    def _emit(self, event: Dict[str, Any]) -> None:
        """Caller holds self._cond."""
        self._events.append(event)
        self.wakeup.set()

    def _ack(self, control_id: str, status: str, message: str = "") -> None:
        self._emit({"type": "control_applied", "control_id": control_id, "status": status, "message": message})

    def _control(self, control: Dict[str, Any]) -> None:
        control_id = str(control.get("id") or "")
        action = control.get("action")
        with self._cond:
            if not control_id or control_id in self._seen_controls:
                return
            if self._finished:
                # The run already ended: Valley rejects the control itself.
                return
            self._seen_controls.add(control_id)
            if action == "interrupt":
                self.interrupted = True
                self.interrupt_id = control_id
                self.cancel_event.set()
                self._cond.notify_all()
                audit.record(self.home, "interrupt", job_id=self.job_id, control_id=control_id)
                return
            if action != "append":
                self._ack(control_id, "rejected", "不支持的指令")
                return
            text = control.get("text")
            if not isinstance(text, str) or not text.strip():
                self._ack(control_id, "rejected", "追加指令为空")
                return
            send = self._send
        # Writing to the tool happens outside the lock (the session takes its own).
        delivered = False
        if send is not None:
            try:
                delivered = bool(send(text))
            except Exception:
                delivered = False
        with self._cond:
            if delivered:
                self._ack(control_id, "applied")
                audit.record(self.home, "append", job_id=self.job_id, control_id=control_id, status="applied",
                             chars=len(text))
                return
            self._queued.append((control_id, text))
            self._ack(control_id, "queued_next_turn")
            audit.record(self.home, "append", job_id=self.job_id, control_id=control_id, status="queued_next_turn",
                         chars=len(text))

    # ---- tool run ---------------------------------------------------------
    def attach(self, send: Callable[[str], bool]) -> None:
        with self._cond:
            self._send = send

    def detach(self) -> None:
        with self._cond:
            self._send = None

    def next_followup(self, ok: bool) -> Optional[Tuple[str, str]]:
        """After a turn: the next queued instruction to run as a resumed turn
        in the same job, or None — then the run is finished and a later
        interrupt or append no longer applies."""
        with self._cond:
            if ok and not self.interrupted and not self.cancel_event.is_set() and self._queued:
                return self._queued.pop(0)
            self._finished = True
            self._send = None
            return None

    def followup_started(self, control_id: str) -> None:
        with self._cond:
            self._ack(control_id, "applied")

    def followup_refused(self, control_id: str, message: str) -> None:
        with self._cond:
            self._ack(control_id, "rejected", message)
            self._finished = True

    @property
    def finished(self) -> bool:
        return self._finished

    def close(self) -> None:
        """The job is over: stop every wait for a decision."""
        with self._cond:
            self._closed = True
            self._finished = True
            self._send = None
            self._cond.notify_all()

    # ---- permissions ------------------------------------------------------
    def _decision(self, decision: Dict[str, Any]) -> None:
        request_id = str(decision.get("request_id") or "")
        with self._cond:
            if request_id in self._pending and request_id not in self._decisions:
                self._decisions[request_id] = decision
                self._cond.notify_all()

    def _resolve(self, request_id: str, outcome: str) -> None:
        """Caller holds self._cond."""
        self._pending.pop(request_id, None)
        self._decisions.pop(request_id, None)
        self._emit({"type": "approval_resolved", "request_id": request_id, "outcome": outcome})

    def permission(self, request: Dict[str, Any], cancelled: threading.Event) -> Dict[str, Any]:
        """Answer one `can_use_tool` prompt: local rules, then what the job
        remembered, then the phone (waiting at most approval_timeout)."""
        tool = approvals.tool_label(request.get("tool_name"))
        tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        root = self.root
        if not root:
            return {"behavior": "deny", "message": NO_PHONE_DENY}
        verdict = approvals.evaluate(tool, tool_input, root, home=self.user_home)
        summary = approvals.summary(tool, tool_input)
        if verdict.action == approvals.DENY:
            audit.record(self.home, "approval", job_id=self.job_id, tool=tool, decision="deny", source="local",
                         reason=verdict.reason, summary=summary)
            return {"behavior": "deny", "message": approvals.DENY_PREFIX + verdict.reason}
        if verdict.action == approvals.ALLOW:
            return {"behavior": "allow"}
        key = approvals.remember_key(tool, tool_input)
        with self._cond:
            remembered = key is not None and key in self._remembered
        if remembered:
            audit.record(self.home, "approval", job_id=self.job_id, tool=tool, decision="allow", source="remembered",
                         summary=summary)
            return {"behavior": "allow"}
        if not self.approvals_enabled:
            return {"behavior": "deny", "message": NO_PHONE_DENY}
        request_id = "perm-" + uuid.uuid4().hex[:24]
        with self._cond:
            if self._closed:
                return {"behavior": "deny", "message": NO_PHONE_DENY}
            self._pending[request_id] = {"tool": tool, "key": key}
            self._emit({"type": "approval_requested", "request_id": request_id, "tool": tool, "summary": summary,
                        "input": approvals.safe_input(tool_input), "message": summary})
            deadline = self.clock() + self.approval_timeout
            while True:
                decision = self._decisions.get(request_id)
                if decision is not None:
                    break
                if cancelled.is_set() or self._closed or self.cancel_event.is_set():
                    self._resolve(request_id, "expired")
                    return {"behavior": "deny", "message": TIMEOUT_DENY}
                left = deadline - self.clock()
                if left <= 0:
                    self._resolve(request_id, "expired")
                    audit.record(self.home, "approval", job_id=self.job_id, request_id=request_id, tool=tool,
                                 decision="deny", source="timeout", summary=summary)
                    return {"behavior": "deny", "message": TIMEOUT_DENY}
                self._cond.wait(min(left, .25))
            approved = decision.get("decision") == "approve"
            expired = decision.get("reason") == "expired"
            remember = approved and decision.get("remember") is True and key is not None
            if remember:
                self._remembered.add(key)
            outcome = "approved" if approved else ("expired" if expired else "denied")
            self._resolve(request_id, outcome)
        audit.record(self.home, "approval", job_id=self.job_id, request_id=request_id, tool=tool,
                     decision="allow" if approved else "deny", source="timeout" if expired else "phone",
                     remember=remember, summary=summary)
        if approved:
            return {"behavior": "allow"}
        return {"behavior": "deny", "message": TIMEOUT_DENY if expired else USER_DENY}
