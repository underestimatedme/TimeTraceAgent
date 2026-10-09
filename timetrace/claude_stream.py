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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from timetrace.dispatch import enforce_spawn_authority
from timetrace.process import MAX_CAPTURED_LINES, MAX_LOG_BYTES, _kill_group, _shell_quote, child_env

INIT_REQUEST_ID = "timetrace-init"
NO_HOST_DENY = "This run cannot ask anyone for permission; use a different approach or stop and explain."
DEFAULT_IDLE_GRACE = 5.0


def user_message(text: str) -> Dict[str, Any]:
    return {"type": "user", "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


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
                 idle_grace: float = DEFAULT_IDLE_GRACE):
        self.cmd, self.cwd, self.log_file, self.prompt = list(cmd), cwd, log_file, prompt
        self.host, self.timeout, self.cancel_event = host, timeout, cancel_event
        self.drop_env, self.env, self.idle_grace = drop_env, env, idle_grace
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
            raw = line.rstrip("\n")
            self._log(log, line)
            msg = None
            if raw.startswith("{"):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
            kind = msg.get("type") if isinstance(msg, dict) else None
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
            self.lines.append(raw)
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
                elif kind is not None:
                    self._last_was_result = False

    def _read_stderr(self, stream, log) -> None:
        for line in iter(stream.readline, ""):
            self._log(log, "[stderr] " + line)

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
                    decision = self.host.permission(request, cancelled) or {}
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
                if self._write(user_message(self.prompt)):
                    self._sent += 1
            out.start()
            err.start()
            if self.host is not None:
                try:
                    self.host.attach(self.send_user)
                except Exception:
                    pass
            started = time.monotonic()
            try:
                while proc.poll() is None:
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
                proc.stdout.close()
                proc.stderr.close()
                log.write("--- exit %s ---\n" % proc.returncode)
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
               env: Optional[dict] = None) -> Tuple[int, List[str]]:
    return StreamSession(cmd, cwd, log_file, prompt, host=host, timeout=timeout, cancel_event=cancel_event,
                         drop_env=drop_env, idle_grace=idle_grace, env=env).run()
