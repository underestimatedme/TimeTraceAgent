"""A single Codex App Server turn over its private stdio transport."""
import json
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict

from timetrace import approvals, usage
from timetrace.billing import billing_env_keys
from timetrace.dispatch import enforce_spawn_authority
from timetrace.models import CODEX, RunResult
from timetrace.process import _kill_group, child_env
from timetrace.redact import mask_structure

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"


def _read_messages(stream, incoming):
    try:
        for line in stream:
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    incoming.put(data)
            except ValueError:
                pass
    finally:
        incoming.put(None)


def _input(text):
    return [{"type": "text", "text": text, "text_elements": []}]


class CodexTurn:
    def __init__(self, cmd, cwd, control, cancel_event, timeout, read_only, env=None):
        self.cmd, self.cwd, self.host, self.cancel = cmd, str(Path(cwd).resolve()), control, cancel_event
        self.timeout, self.read_only, self.env = timeout, read_only, env
        self.result = RunResult(exit_code=1)
        self.incoming = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.proc = None
        self.thread_id = ""
        self.turn_id = ""
        self.next_id = 0
        self.calls: Dict[int, Callable] = {}
        self.permission_requests: Dict[object, threading.Event] = {}
        self.approval_workers = []
        self.file_changes = {}
        self.replies = set()
        self.initial_sent = False
        self.initial_confirmed = False
        self.ended = False
        # Token usage (timetrace.usage totals): the thread's running totals before this turn as its rollout recorded
        # them (set by the adapter for a resumed thread; None when not known for certain), those the App Server
        # replayed for an earlier turn, and this turn's. Anything that does not fit together means no usage.
        self.saved_totals = usage.ZERO
        self.replayed = []
        self.turn_totals = []
        self.usage_conflict = False
        self.model = ""

    def _write_locked(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _write(self, msg):
        with self.lock:
            return self._write_locked(msg)

    def _call(self, method, params, callback, *, sends_input=False):
        with self.lock:
            if sends_input and not self.host.provider_input_allowed(check_gate=False):
                return False
            self.next_id += 1
            id_ = self.next_id
            self.calls[id_] = callback
            if self._write_locked({"id": id_, "method": method, "params": params}):
                return True
            del self.calls[id_]
            return False

    def _request(self, method, params):
        """Initialization requests run before the provider turn exists."""
        responses = []
        if not self._call(method, params, responses.append):
            return None
        deadline = time.monotonic() + min(self.timeout, 30)
        while not responses and not self.cancel.is_set() and time.monotonic() < deadline:
            if not self._receive():
                break
        return responses[0] if responses else None

    def _receive(self):
        try:
            msg = self.incoming.get(timeout=.05)
        except queue.Empty:
            return True
        if msg is None:
            return False
        method, id_ = msg.get("method"), msg.get("id")
        params = msg.get("params")
        if method and isinstance(params, dict):
            if isinstance(id_, (str, int)):
                self._approval(id_, method, params)
            else:
                self._notification(method, params)
        elif isinstance(id_, int):
            with self.lock:
                callback = self.calls.pop(id_, None)
            if callback is not None:
                result = msg.get("result")
                callback(result if isinstance(result, dict) and "error" not in msg else None)
        return True

    def send_user(self, control_id, text, finish):
        if self.ended or self.cancel.is_set() or not self.turn_id:
            finish("rejected")
            return
        params = {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                  "clientUserMessageId": control_id, "input": _input(text)}

        def answered(result):
            if result is None:
                # A server error may arrive after its turn ended.
                finish("rejected")
            else:
                finish("applied" if result.get("turnId") == self.turn_id else "uncertain")

        if not self._call("turn/steer", params, answered, sends_input=True):
            finish("uncertain")

    def _notification(self, method, params):
        if params.get("threadId") != self.thread_id:
            return
        if method == "thread/tokenUsage/updated":
            self._token_usage(params)
            return
        if method in ("turn/started", "turn/completed"):
            turn = params.get("turn")
            if method == "turn/started" and not self.turn_id and self.initial_sent and isinstance(turn, dict):
                self.turn_id = turn.get("id") or ""
            if method == "turn/completed" and isinstance(turn, dict) and turn.get("id") == self.turn_id:
                self._complete(turn)
            return
        if method == "serverRequest/resolved":
            with self.lock:
                cancelled = self.permission_requests.get(params.get("requestId"))
                if cancelled is not None:
                    cancelled.set()
            return
        if params.get("turnId") != self.turn_id or self.ended:
            return
        item = params.get("item")
        if method == "item/started" and isinstance(item, dict) and item.get("type") == "fileChange":
            paths = [change.get("path") for change in item.get("changes", []) if isinstance(change, dict)]
            self.file_changes[item.get("id")] = paths
        if method == "item/completed":
            self._reply(item)

    def _token_usage(self, params):
        """Each update carries the thread's running totals. One for an earlier turn, before this turn reported any, is
        the App Server replaying what a resumed thread had saved (the starting point); this turn's last is where it
        ended. A total that is not whole, an earlier turn's after this one's, or one that goes down is a conflict."""
        token_usage = params.get("tokenUsage")
        totals = usage.codex_totals(token_usage.get("total") if isinstance(token_usage, dict) else None,
                                    usage.CODEX_CAMEL)
        if totals is None:
            self.usage_conflict = True
        elif self.turn_id and params.get("turnId") == self.turn_id:
            if self.turn_totals and not usage.grows(self.turn_totals[-1], totals):
                self.usage_conflict = True
            self.turn_totals.append(totals)
        elif self.turn_totals:
            self.usage_conflict = True
        elif totals not in self.replayed:
            self.replayed.append(totals)

    def turn_usage(self):
        """What this turn used; None when it reported nothing, when where it began is not certain (no saved or replayed
        totals for a resumed thread, two different replays, a replay that disagrees with the rollout) or on a conflict."""
        if self.usage_conflict or not self.turn_totals or len(self.replayed) > 1:
            return None
        start = self.saved_totals
        if self.replayed:
            if start is not None and start != self.replayed[0]:
                return None
            start = self.replayed[0]
        return usage.codex_used(self.turn_totals[-1], start, self.model)

    def _reply(self, item):
        if (not isinstance(item, dict) or item.get("type") != "agentMessage"
                or not isinstance(item.get("text"), str) or item.get("id") in self.replies):
            return
        self.replies.add(item.get("id"))
        reply = mask_structure(item["text"])
        if reply.strip():
            self.result.output = reply
            self.host.assistant_message(reply)

    def _complete(self, turn):
        self.ended = True
        for item in turn.get("items") or []:
            self._reply(item)
        self.result.ok = turn.get("status") == "completed"
        self.result.exit_code = 0 if self.result.ok else 1
        if not self.result.ok:
            error = turn.get("error") or {}
            info = error.get("codexErrorInfo") if isinstance(error, dict) else None
            self.result.blocked = info in ("usageLimitExceeded", "rateLimitExceeded")
            self.result.error = "Codex usage limit reached" if self.result.blocked else "Codex turn did not complete"

    def _permission_input(self, method, params):
        if any(params.get(key) for key in ("additionalPermissions", "requestedPermissions", "permissions",
                                           "networkApprovalContext", "proposedNetworkPolicyAmendments")):
            return None
        if method == COMMAND_APPROVAL:
            command, cwd = params.get("command"), params.get("cwd")
            if (params.get("kind") not in (None, "command") or not isinstance(command, str)
                    or not command.strip() or not isinstance(cwd, str)):
                return None
            if not Path(cwd).is_absolute() or not Path(cwd).resolve().is_relative_to(self.cwd):
                return None
            # The server may report an argv rendered as a shell command.
            # Apply TimeTrace's local policy to the contained command too.
            try:
                words = shlex.split(command)
            except ValueError:
                return None
            if words and Path(words[0]).name in {"sh", "bash", "zsh", "fish", "dash", "ksh"}:
                if len(words) != 3 or words[1] not in ("-c", "-lc"):
                    return None
                command = words[2]
            return {"tool_name": "Bash", "input": {"command": command, "cwd": cwd}, "require_phone": True}
        if method == FILE_APPROVAL:
            paths = self.file_changes.get(params.get("itemId")) or []
            if (not paths or params.get("grantRoot") or
                    any(not isinstance(p, str) or not (Path(self.cwd) / p).resolve().is_relative_to(self.cwd)
                        for p in paths)):
                return None
            if any(approvals.evaluate("Edit", {"file_path": p}, self.cwd).action == approvals.DENY for p in paths):
                return None
            return {"tool_name": "Edit", "input": {"file_path": paths[0], "paths": paths}, "require_phone": True}
        return None

    def _approval(self, id_, method, params):
        if method not in (COMMAND_APPROVAL, FILE_APPROVAL):
            self._write({"id": id_, "error": {"code": -32601, "message": "Unsupported approval request"}})
            return
        matches = params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id
        request = self._permission_input(method, params) if matches and not self.read_only and not self.ended else None
        if request is None or self.cancel.is_set():
            self._write({"id": id_, "result": {"decision": "decline"}})
            return
        cancelled = threading.Event()
        with self.lock:
            if id_ in self.permission_requests:
                return
            self.permission_requests[id_] = cancelled

        def answer(decision, *, approval_open=None):
            with self.lock:
                if self.ended or self.cancel.is_set() or cancelled.is_set() or not self.host.provider_input_allowed(check_gate=False):
                    return False
                expired = approval_open is not None and not approval_open()
                choice = "accept" if not expired and decision.get("behavior") == "allow" else "decline"
                written = self._write_locked({"id": id_, "result": {"decision": choice}})
                return written and not expired

        def permission():
            try:
                self.host.permission(request, cancelled, respond=answer)
            except Exception:
                answer({"behavior": "deny"})
            finally:
                with self.lock:
                    self.permission_requests.pop(id_, None)

        worker = threading.Thread(target=permission, daemon=True)
        self.approval_workers.append(worker)
        worker.start()

    def _start(self, prompt, session_id, params):
        if self._request("initialize", {"clientInfo": {"name": "timetrace", "version": "1.0"},
                                        "capabilities": {"experimentalApi": True}}) is None:
            self.result.error = "Codex App Server initialize failed"
            return
        self._write({"method": "initialized"})
        method = "thread/resume" if session_id else "thread/start"
        response = self._request(method, dict(params, threadId=session_id, excludeTurns=True) if session_id else params)
        thread = (response or {}).get("thread")
        model = (response or {}).get("model")
        self.model = model if isinstance(model, str) and model.strip() else str(params.get("model") or "")
        actual = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(actual, str) or not actual or (session_id and actual != session_id):
            self.result.error = "Codex session unavailable" if session_id else "Codex could not start a session"
            return
        self.host.provider_session(actual)
        self.thread_id = self.result.session_id = actual
        if self.cancel.is_set() or not self.host.provider_input_allowed():
            self.result.error = "Codex did not start a turn without a valid lease"
            return

        def started(result):
            turn = (result or {}).get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                self.result.error = "Codex refused to start the turn"
                self.initial_sent = False
                self.ended = True
                return
            self.turn_id = turn["id"]
            self.initial_confirmed = True
            self.host.initial_delivery("applied")
            if not self.ended:
                self.host.attach_interactive(self.send_user)

        self.initial_sent = self._call("turn/start", {
            "threadId": actual, "input": _input(prompt), "clientUserMessageId": self.host.message_id,
        }, started, sends_input=True)

    def run(self, prompt, session_id, params, log_file):
        env = child_env(billing_env_keys(CODEX), self.env)
        env.pop("RUST_LOG", None)
        enforce_spawn_authority(self.cancel)
        self.proc = proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                            encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        reader = threading.Thread(target=_read_messages, args=(proc.stdout, self.incoming), daemon=True)
        reader.start()
        try:
            self._start(prompt, session_id, params)
            remaining = self.timeout
            while self.initial_sent and (not self.ended or not self.initial_confirmed) and not self.cancel.is_set():
                before = time.monotonic()
                with self.lock:
                    waiting = bool(self.permission_requests)
                if not self._receive():
                    break
                if self.result.error and self.ended:
                    break
                if not waiting:
                    remaining -= time.monotonic() - before
                    if remaining <= 0:
                        self.result.error = "Codex turn timed out"
                        break
            if self.cancel.is_set() and self.turn_id and not self.ended:
                self._call("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, lambda _: None)
                deadline = time.monotonic() + 2
                while not self.ended and time.monotonic() < deadline and self._receive():
                    pass
            if not self.result.ok and not self.result.error:
                self.result.error = "Codex App Server disconnected"
            try:
                self.result.usage = self.turn_usage()
            except Exception:
                self.result.usage = None
        finally:
            self.ended = True
            self.host.detach()
            if self.initial_sent and not self.initial_confirmed:
                self.host.initial_delivery("uncertain")
            with self.lock:
                for cancelled in self.permission_requests.values():
                    cancelled.set()
            _kill_group(proc)
            proc.wait(timeout=5)
            reader.join(timeout=2)
            for worker in self.approval_workers:
                worker.join(timeout=1)
            proc.stdin.close()
            proc.stdout.close()
        return self.result
