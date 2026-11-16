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
                 log: Callable[[str], None] = None, *, turn_id: str = "", message_id: str = "",
                 reserve_input: Optional[Callable[[str], bool]] = None,
                 observe: Optional[Callable[[], str]] = None,
                 input_gate: Optional[Callable[[float], bool]] = None):
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
        # Set only by approval/control events (not by `running`): what the ApprovalPump waits for.
        self.pending = threading.Event()
        self._cond = threading.Condition()
        self._events: List[Dict[str, Any]] = []
        # request_id -> when the Runner asked (monotonic): the Agent logs how long a request waited to be queued.
        self.emitted_at: Dict[str, float] = {}
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
        self.turn_id, self.message_id = turn_id, message_id
        self._reserve_input = reserve_input
        self._observe = observe
        self._input_gate = input_gate
        self._input_deadline = None
        self.persist_provider_session = None
        self.provider_session_id = ""
        self._initial_status = ""
        self._interactive_send = None
        self._inflight = None
        self._last_reply = ""

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
        if self.turn_id:
            self._dispatch_append()

    def take_events(self) -> List[Dict[str, Any]]:
        with self._cond:
            events, self._events = self._events, []
            self.wakeup.clear()
            self.pending.clear()
        return events

    def _emit(self, event: Dict[str, Any]) -> None:
        """Caller holds self._cond."""
        if event.get("type") == "approval_requested":
            self.emitted_at[str(event.get("request_id"))] = time.monotonic()
        if self._observe is not None:
            event["observed_at"] = self._observe()
        self._events.append(event)
        self.wakeup.set()
        self.pending.set()

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
            if self.turn_id and action == "append" and control.get("work_session_turn_id") != self.turn_id:
                self._ack(control_id, "rejected", "追加指令所属轮次已经结束")
                return
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
            if self.turn_id:
                self._queued.append((control_id, text))
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

    def reserve_initial(self) -> bool:
        if self._reserve_input is None or self._reserve_input(self.message_id):
            return True
        self.initial_delivery("uncertain")
        return False

    def authorize_provider_input(self, deadline: float) -> None:
        self._input_deadline = deadline

    def provider_input_allowed(self, *, check_gate: bool = True) -> bool:
        deadline = self._input_deadline
        if self._closed or self.cancel_event.is_set() or (deadline is not None and time.time() >= deadline):
            return False
        if check_gate and self._input_gate is not None:
            try:
                if deadline is None or not self._input_gate(deadline):
                    return False
            except Exception:
                return False
            # The gate may have blocked in a local capability probe.
            return self.provider_input_allowed(check_gate=False)
        return True

    def provider_session(self, session_id: str) -> None:
        if self.persist_provider_session is not None:
            self.persist_provider_session(session_id)
        with self._cond:
            self.provider_session_id = session_id
            self._emit({"type": "provider_session", "provider_session_id": session_id})

    def initial_delivery(self, status: str) -> None:
        if status not in ("applied", "uncertain"):
            return
        with self._cond:
            if not self._initial_status:
                self._initial_status = status
                kind = "message_delivered" if status == "applied" else "message_uncertain"
                self._emit({"type": kind, "work_session_message_id": self.message_id})

    def assistant_message(self, reply: str) -> None:
        if isinstance(reply, str) and reply.strip():
            with self._cond:
                self._last_reply = reply
                self._emit({"type": "assistant_message", "reply": reply})

    def terminal_reply(self, reply: str) -> str:
        with self._cond:
            return "" if self._last_reply == reply else reply

    def attach_interactive(self, send) -> None:
        with self._cond:
            self._interactive_send = send
        self._dispatch_append()

    def _dispatch_append(self) -> None:
        with self._cond:
            send = self._interactive_send
            if not (send and self._queued and not self._pending and not self._inflight
                    and not self._finished and not self.cancel_event.is_set()):
                return
            control_id, text = self._queued.pop(0)
            self._inflight = control_id
        def finish(status):
            with self._cond:
                if self._inflight != control_id:
                    return
                self._inflight = None
                self._ack(control_id, status)
            self._dispatch_append()
        try:
            if not self.provider_input_allowed():
                finish("rejected")
            elif self._reserve_input is not None and not self._reserve_input(control_id):
                finish("uncertain")
            else:
                send(control_id, text, finish)
        except Exception:
            finish("uncertain")

    def detach(self) -> None:
        with self._cond:
            self._send = None
            self._interactive_send = None

    def next_followup(self, ok: bool) -> Optional[Tuple[str, str]]:
        """After a turn: the next queued instruction to run as a resumed turn
        in the same job, or None — then the run is finished and a later
        interrupt or append no longer applies."""
        with self._cond:
            if self.turn_id:
                if self._inflight:
                    self._ack(self._inflight, "uncertain")
                    self._inflight = None
                for control_id, _ in self._queued:
                    self._ack(control_id, "rejected", "追加指令所属轮次已经结束")
                self._queued.clear()
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

    @property
    def closed(self) -> bool:
        return self._closed

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

    def permission(self, request: Dict[str, Any], cancelled: threading.Event, *, respond=None) -> Dict[str, Any]:
        try:
            decision = self._permission(request, cancelled, respond)
            if respond is not None and decision is not None and not cancelled.is_set() and self.provider_input_allowed():
                respond(decision)
            return decision or {}
        finally:
            if self.turn_id:
                # The provider has its decision. Resume messages queued during
                # the permission wait without delaying for another renewal.
                self._dispatch_append()

    def _permission(self, request: Dict[str, Any], cancelled: threading.Event, respond) -> Optional[Dict[str, Any]]:
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
        if verdict.action == approvals.ALLOW and not request.get("require_phone"):
            return {"behavior": "allow"}
        key = None if self.turn_id else approvals.remember_key(tool, tool_input)
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
            if self._closed or cancelled.is_set() or self.cancel_event.is_set():
                return {"behavior": "deny", "message": NO_PHONE_DENY}
            self._pending[request_id] = {"tool": tool, "key": key}
            self._emit({"type": "approval_requested", "request_id": request_id, "tool": tool, "summary": summary,
                        "input": approvals.safe_input(tool_input), "message": summary})
            deadline = self.clock() + self.approval_timeout
            while True:
                if cancelled.is_set() or self._closed or self.cancel_event.is_set():
                    self._resolve(request_id, "expired")
                    return {"behavior": "deny", "message": TIMEOUT_DENY}
                left = deadline - self.clock()
                if left <= 0:
                    self._resolve(request_id, "expired")
                    audit.record(self.home, "approval", job_id=self.job_id, request_id=request_id, tool=tool,
                                 decision="deny", source="timeout", summary=summary)
                    return {"behavior": "deny", "message": TIMEOUT_DENY}
                decision = self._decisions.get(request_id)
                if decision is not None:
                    break
                self._cond.wait(min(left, .25))
            expired = decision.get("reason") == "expired"
            approved = decision.get("decision") == "approve" and not expired
            remember = approved and decision.get("remember") is True and key is not None
            if remember:
                self._remembered.add(key)
            outcome = "approved" if approved else ("expired" if expired else "denied")
            if respond is None:
                self._resolve(request_id, outcome)
        response = {"behavior": "allow"} if approved else {
            "behavior": "deny", "message": TIMEOUT_DENY if expired else str(decision.get("reason") or USER_DENY)[:2000],
        }
        if respond is not None:
            def approval_open():
                return self.clock() < deadline

            written = False
            try:
                if not cancelled.is_set() and self.provider_input_allowed():
                    if approved and approval_open():
                        written = respond(response, approval_open=approval_open)
                    else:
                        if approved:
                            approved, expired, remember = False, True, False
                            outcome = "expired"
                            response = {"behavior": "deny", "message": TIMEOUT_DENY}
                        written = respond(response)
            finally:
                if not written:
                    approved, expired, remember = False, True, False
                with self._cond:
                    self._resolve(request_id, outcome if written else "expired")
        audit.record(self.home, "approval", job_id=self.job_id, request_id=request_id, tool=tool,
                     decision="allow" if approved else "deny", source="timeout" if expired else "phone",
                     remember=remember, summary=summary)
        return response if respond is None else None
