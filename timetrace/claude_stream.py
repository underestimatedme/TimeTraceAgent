"""A bidirectional Claude Code run: `claude -p --input-format stream-json
--output-format stream-json`.

The wire format (confirmed against the installed Claude Code 2.1.285: the
CLI's own zod schemas for `can_use_tool` and the permission result, the
routing in its print mode, and the Agent SDK 0.3.156 host code; a live probe
of the control channel with an `initialize` request only, no model call):

host → CLI (stdin), one JSON object per line
  user message      {"type":"user","session_id":"","message":{"role":"user","content":[{"type":"text","text":…}]},
                     "parent_tool_use_id":null}
  initialize        {"type":"control_request","request_id":…,"request":{"subtype":"initialize"}}
  permission answer {"type":"control_response","response":{"subtype":"success","request_id":…,
                     "response":{"behavior":"allow","updatedInput":{…},"toolUseID":…}}}
                    {… "response":{"behavior":"deny","message":…,"toolUseID":…}}
  unsupported       {"type":"control_response","response":{"subtype":"error","request_id":…,"error":…}}

CLI → host (stdout)
  permission prompt {"type":"control_request","request_id":…,"request":{"subtype":"can_use_tool",
                     "tool_name":…,"input":{…},"tool_use_id":…,"permission_suggestions":[…],
                     "blocked_path"?,"decision_reason"?,…}}
  withdrawn prompt  {"type":"control_cancel_request","request_id":…}
  everything else   the usual stream-json messages (system, assistant, user,
                    rate_limit_event, result).

Prompts reach the host only with `--permission-prompt-tool stdio` (what the
SDK passes); `--permission-prompts host` alone leaves an "ask" to the CLI,
which then denies it in print mode. `--permission-prompts none` denies
every prompt locally.

With stream-json input the CLI keeps running after a `result` until stdin
closes. The session closes stdin once every user message it wrote has been
answered by a `result`, or — when the CLI folded a message sent mid-turn
into the running turn and so answers with fewer results — once the output
has been quiet for `idle_grace` seconds after a result.
"""
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from timetrace.dispatch import enforce_spawn_authority
from timetrace.redact import mask_structure, mask_timetrace_tokens
from timetrace.remote_control import TIMEOUT_DENY
from timetrace.process import MAX_CAPTURED_LINES, MAX_LOG_BYTES, _kill_group, _shell_quote, child_env

INIT_REQUEST_ID = "timetrace-init"
NO_HOST_DENY = "This run cannot ask anyone for permission; use a different approach or stop and explain."
DEFAULT_IDLE_GRACE = 5.0


def user_message(text: str, message_uuid: Optional[str] = None) -> Dict[str, Any]:
    message = {"type": "user", "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    if message_uuid:
        message["uuid"] = message_uuid
    return message


def permission_response(request_id: str, tool_use_id: Optional[str], decision: Dict[str, Any],
                        original_input: Any) -> Dict[str, Any]:
    """The control_response for one can_use_tool request. `decision` is
    {"behavior": "allow"} or {"behavior": "deny", "message": …}."""
    if decision.get("behavior") == "allow":
        body = {"behavior": "allow",
                "updatedInput": original_input if isinstance(original_input, dict) else {}}
    else:
        body = {"behavior": "deny", "message": str(decision.get("message") or "Permission denied.")}
    if tool_use_id:
        body["toolUseID"] = tool_use_id
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                      "response": body}}


class StreamSession:
    """One Claude process. `host` (optional) receives:
      host.attach(send_user)   the session accepts more user messages
      host.detach()            it no longer does (stdin closed or process gone)
      host.permission(request, cancelled) -> {"behavior": "allow"|"deny", "message"?}
         `request` is the can_use_tool body; blocking; `cancelled` is set
         when the CLI withdraws the prompt or the run ends.
    Time spent waiting in host.permission does not count toward `timeout`."""

    def __init__(self, cmd: Sequence[str], cwd: str, log_file: str, prompt: str, host: Any = None,
                 timeout: Optional[float] = None, cancel_event: Optional[threading.Event] = None,
                 drop_env: Optional[Sequence[str]] = None, env: Optional[dict] = None,
                 idle_grace: float = DEFAULT_IDLE_GRACE, confirm_session: str = "",
                 on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_unreadable: Optional[Callable[[str], None]] = None):
        self.cmd, self.cwd, self.log_file, self.prompt = list(cmd), cwd, log_file, prompt
        # Sees every decoded stdout message (masked) before `lines` keeps only the last MAX_CAPTURED_LINES: token usage.
        self.on_message = on_message
        # Sees every stdout line that looked like JSON but did not decode: a watcher that counts messages must know it
        # missed one (timetrace.run_facts).
        self.on_unreadable = on_unreadable
        self.host, self.timeout, self.cancel_event = host, timeout, cancel_event
        self.drop_env, self.env, self.idle_grace = drop_env, env, idle_grace
        self.confirm_session = confirm_session
        self._user_acks = {}
        self._session_confirmed = False
        self._session_error: Optional[Exception] = None
        self.lines: List[str] = []
        self._lock = threading.Lock()          # stdin writes, counters, close decision
        self._log_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._stdin_open = False
        self._sent = 0                          # user messages written
        self._results = 0                       # result messages seen
        self._last_was_result = False
        self._last_line_at = 0.0
        self._waiting = 0                       # permission prompts the host is answering
        self._paused_total = 0.0
        self._paused_since: Optional[float] = None
        self._pending: Dict[str, threading.Event] = {}  # request_id → cancelled
        self._ended = threading.Event()
        self._workers: List[threading.Thread] = []

    # ---- writing ---------------------------------------------------------
    def _write(self, obj: Dict[str, Any]) -> bool:
        """Caller holds self._lock."""
        if not self._stdin_open or self._proc is None or self._proc.stdin is None:
            return False
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self._close_stdin_locked()
            return False

    def send_user(self, text: str) -> bool:
        """Deliver one more user message (an appended instruction). False
        when the session no longer takes input."""
        with self._lock:
            if not self._write(user_message(text)):
                return False
            self._sent += 1
            self._last_was_result = False
            return True

    def _send_confirmed_locked(self, input_id, text, finish):
        """Caller holds self._lock. The CLI replays this UUID on acceptance."""
        if not self.host.provider_input_allowed(check_gate=False):
            return False
        message_uuid = str(uuid.uuid5(uuid.UUID(self.confirm_session), input_id))
        self._user_acks[message_uuid] = finish
        if self._write(user_message(text, message_uuid)):
            self._sent += 1
            self._last_was_result = False
        return True

    def send_confirmed(self, input_id, text, finish):
        with self._lock:
            if self._stdin_open and not self._last_was_result and self._send_confirmed_locked(input_id, text, finish):
                return
        finish("rejected")

    def _initial_echo(self, status):
        self.host.initial_delivery(status)
        if status == "applied":
            self.host.attach_interactive(self.send_confirmed)

    def _interactive_message(self, msg):
        if msg.get("session_id") != self.confirm_session:
            return
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init" and not self._session_confirmed:
            self.host.provider_session(self.confirm_session)
            self._session_confirmed = True
        if kind == "user" and msg.get("isReplay") is True:
            with self._lock:
                finish = self._user_acks.pop(msg.get("uuid"), None)
            if finish is not None:
                finish("applied")
        if kind == "assistant" and not msg.get("parent_tool_use_id"):
            message = msg.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            text = [block["text"] for block in content or []
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)]
            if text:
                self.host.assistant_message(mask_structure("\n".join(text)))

    def _close_stdin_locked(self) -> None:
        if not self._stdin_open:
            return
        self._stdin_open = False
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass
        if self.host is not None:
            try:
                self.host.detach()
            except Exception:
                pass

    # ---- reading ---------------------------------------------------------
    def _log(self, log, text: str) -> None:
        with self._log_lock:
            if log.tell() < MAX_LOG_BYTES:
                log.write(text[:max(0, MAX_LOG_BYTES - log.tell())])
                log.flush()

    def _read_stdout(self, stream, log) -> None:
        for line in iter(stream.readline, ""):
            # A run's token is in its environment, so what it prints is masked: the log, the lines kept and what the host is shown.
            # The line itself is read as it came (masking text inside raw JSON can break the JSON, or change a command that is
            # then echoed back to Claude as approved).
            raw = line.rstrip("\n")
            msg = None
            if raw.startswith("{"):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
            masked = mask_structure(msg) if isinstance(msg, dict) else None
            shown = json.dumps(masked, ensure_ascii=False) if masked is not None else mask_timetrace_tokens(raw)
            if raw.lstrip().startswith("{") and not isinstance(msg, dict) and self.on_unreadable is not None:
                try:
                    self.on_unreadable(raw)
                except Exception:
                    pass
            if masked is not None and self.on_message is not None:
                try:
                    self.on_message(masked)
                except Exception:
                    pass  # a watcher never stops the output from being read
            self._log(log, shown + "\n")
            kind = msg.get("type") if isinstance(msg, dict) else None
            if self.confirm_session and isinstance(msg, dict):
                try:
                    self._interactive_message(msg)
                except Exception as exc:
                    self._session_error = exc
                    self._ended.set()
                    return
            if kind == "control_request":
                self._on_control_request(msg)
                continue
            if kind == "control_cancel_request":
                cancelled = self._pending.get(str(msg.get("request_id")))
                if cancelled is not None:
                    cancelled.set()
                continue
            if kind in ("control_response", "keep_alive"):
                continue
            self.lines.append(shown)
            if len(self.lines) > MAX_CAPTURED_LINES:
                del self.lines[:len(self.lines) - MAX_CAPTURED_LINES]
            with self._lock:
                self._last_line_at = time.monotonic()
                if kind == "result":
                    self._results += 1
                    self._last_was_result = True
                    if self._results >= self._sent:
                        # Every message was answered: nothing more to do.
                        self._close_stdin_locked()
                elif kind is not None and (not self.confirm_session or kind in ("assistant", "stream_event")):
                    self._last_was_result = False

    def _read_stderr(self, stream, log) -> None:
        for line in iter(stream.readline, ""):
            self._log(log, "[stderr] " + mask_timetrace_tokens(line))

    def _on_control_request(self, msg: Dict[str, Any]) -> None:
        request_id = str(msg.get("request_id") or "")
        request = msg.get("request") if isinstance(msg.get("request"), dict) else {}
        if request.get("subtype") != "can_use_tool" or not request_id:
            with self._lock:
                self._write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": request_id,
                    "error": "unsupported control request: %s" % str(request.get("subtype"))[:80]}})
            return
        cancelled = threading.Event()
        self._pending[request_id] = cancelled
        worker = threading.Thread(target=self._answer, args=(request_id, request, cancelled), daemon=True)
        self._workers.append(worker)
        worker.start()

    def _answer(self, request_id: str, request: Dict[str, Any], cancelled: threading.Event) -> None:
        with self._lock:
            self._waiting += 1
            if self._waiting == 1:
                self._paused_since = time.monotonic()
        try:
            if self.host is None:
                decision = {"behavior": "deny", "message": NO_HOST_DENY}
            else:
                try:
                    if self.confirm_session:
                        def respond(answer, *, approval_open=None):
                            with self._lock:
                                if cancelled.is_set() or not self.host.provider_input_allowed(check_gate=False):
                                    return False
                                expired = approval_open is not None and not approval_open()
                                if expired:
                                    answer = {"behavior": "deny", "message": TIMEOUT_DENY}
                                written = self._write(permission_response(request_id, request.get("tool_use_id"),
                                                                          answer, request.get("input")))
                                return written and not expired
                        self.host.permission(request, cancelled, respond=respond)
                        return
                    decision = self.host.permission(mask_structure(request), cancelled) or {}
                except Exception:
                    decision = {"behavior": "deny", "message": NO_HOST_DENY}
        finally:
            with self._lock:
                self._waiting -= 1
                if self._waiting == 0 and self._paused_since is not None:
                    self._paused_total += time.monotonic() - self._paused_since
                    self._paused_since = None
            self._pending.pop(request_id, None)
        if cancelled.is_set():
            return  # withdrawn (or the run ended): nobody is listening
        with self._lock:
            self._write(permission_response(request_id, request.get("tool_use_id"), decision, request.get("input")))

    def _paused_seconds(self) -> float:
        with self._lock:
            extra = time.monotonic() - self._paused_since if self._paused_since is not None else 0.0
            return self._paused_total + extra

    # ---- lifecycle -------------------------------------------------------
    def run(self) -> Tuple[int, List[str]]:
        run_env = child_env(self.drop_env, self.env)
        with open(self.log_file, "a", encoding="utf-8") as log:
            log.write("$ " + " ".join(_shell_quote(part) for part in self.cmd) + "\n")
            log.flush()
            enforce_spawn_authority(self.cancel_event)
            self._proc = proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=run_env, text=True, bufsize=1, start_new_session=True,
            )
            out = threading.Thread(target=self._read_stdout, args=(proc.stdout, log), daemon=True)
            err = threading.Thread(target=self._read_stderr, args=(proc.stderr, log), daemon=True)
            with self._lock:
                self._stdin_open = True
                self._write({"type": "control_request", "request_id": INIT_REQUEST_ID,
                             "request": {"subtype": "initialize"}})
                if self.confirm_session:
                    if not self._send_confirmed_locked(self.host.message_id, self.prompt, self._initial_echo):
                        self._close_stdin_locked()
                elif self._write(user_message(self.prompt)):
                    self._sent += 1
            out.start()
            err.start()
            if self.host is not None and not self.confirm_session:
                try:
                    self.host.attach(self.send_user)
                except Exception:
                    pass
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    if self._ended.is_set():
                        self._terminate(proc)
                        break
                    if self.cancel_event is not None and self.cancel_event.wait(.1):
                        self._terminate(proc)
                        break
                    if self.cancel_event is None:
                        time.sleep(.1)
                    if self.timeout is not None and time.monotonic() - started - self._paused_seconds() >= self.timeout:
                        self._terminate(proc)
                        break
                    with self._lock:
                        quiet = (self._stdin_open and self._last_was_result and self._waiting == 0
                                 and time.monotonic() - self._last_line_at >= self.idle_grace)
                        if quiet:
                            # The CLI folded a mid-turn message into its turn.
                            self._close_stdin_locked()
            finally:
                self._ended.set()
                for cancelled in list(self._pending.values()):
                    cancelled.set()
                with self._lock:
                    self._close_stdin_locked()
                # However Claude ended, nothing it started may keep running.
                _kill_group(proc)
                out.join(timeout=2)
                err.join(timeout=2)
                for worker in list(self._workers):
                    worker.join(timeout=2)
                for finish in list(self._user_acks.values()):
                    finish("uncertain")
                self._user_acks.clear()
                proc.stdout.close()
                proc.stderr.close()
                log.write("--- exit %s ---\n" % proc.returncode)
        if self._session_error is not None:
            raise RuntimeError("Claude session confirmation failed") from self._session_error
        return int(proc.returncode), list(self.lines)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()


def run_stream(cmd: Sequence[str], cwd: str, log_file: str, prompt: str, host: Any = None,
               timeout: Optional[float] = None, cancel_event: Optional[threading.Event] = None,
               drop_env: Optional[Sequence[str]] = None, idle_grace: float = DEFAULT_IDLE_GRACE,
               env: Optional[dict] = None, confirm_session: str = "",
               on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
               on_unreadable: Optional[Callable[[str], None]] = None) -> Tuple[int, List[str]]:
    return StreamSession(cmd, cwd, log_file, prompt, host=host, timeout=timeout, cancel_event=cancel_event,
                         drop_env=drop_env, idle_grace=idle_grace, env=env, confirm_session=confirm_session,
                         on_message=on_message, on_unreadable=on_unreadable).run()
