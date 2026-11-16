"""A generic Agent Client Protocol (ACP v1) client over stdio, standard library only.

ACP is how editors drive coding agents (Zed, and the Codeg project that runs twenty agents through it): JSON-RPC
2.0, one JSON object per line on the agent's stdin / stdout, logs on its stderr. Gemini CLI (`gemini --acp`) and
Cursor (`cursor-agent acp`) both speak it, so one client runs a dispatched task with either of them instead of one
output parser per tool. The wire facts below are from the official schema (agentclientprotocol/agent-client-protocol,
schema v1); they were not exercised against a real Gemini or Cursor process on this machine.

  client -> agent  initialize       {protocolVersion: 1, clientCapabilities: {fs: {readTextFile: false,
                                     writeTextFile: false}, terminal: false}, clientInfo}
                                     -> {protocolVersion, agentCapabilities, authMethods}
                   session/new      {cwd, mcpServers}                       -> {sessionId}
                   session/resume   {sessionId, cwd, mcpServers}            only with sessionCapabilities.resume
                   session/load     {sessionId, cwd, mcpServers}            only with loadSession; replays history
                   session/prompt   {sessionId, prompt: [{type: "text"}]}   -> {stopReason}
                   session/cancel   notification; the pending prompt then answers stopReason "cancelled"
  agent -> client  session/update   notification: message / thought chunks, tool calls, plan, usage, ...
                   session/request_permission   answered with a one-time option, never an *_always one
                   fs/*, terminal/* never expected (neither is advertised): -32601, as is any other request

Safety posture, the same as the other adapters':
- The agent is started as its own session (process group) right after enforce_spawn_authority, with child_env's
  environment minus the billing keys; a stop or a cancel ends the group (the queue's StopRequest semantics
  included), and whatever is left of the group is killed however the run ends.
- The client never calls `authenticate`: choosing an auth method on the user's behalf could select an API key.
- TimeTrace's MCP token reaches the agent inside the session/new, session/resume or session/load request (the
  header of an http MCP server entry), never in argv or the environment, so the model's shell commands cannot read
  it; the transcript names the servers and never a header value, and everything written to it is masked
  (timetrace.redact). A session id the agent answers with is kept only if it has a conservative shape and holds
  no token.
- Permission prompts go to the host (the job's RunControl: the worktree's local rules, then the phone) and are
  answered with allow_once / reject_once only. Nothing lasting is granted, or refused, on the user's behalf.
"""
import itertools
import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from timetrace import __version__, process
from timetrace.dispatch import enforce_spawn_authority
from timetrace.mcp_access import McpAccess, skip_note
from timetrace.process import MAX_LOG_BYTES, StopRequest, ToolExitUnconfirmed, _shell_quote, child_env
from timetrace.redact import _TIMETRACE_TOKEN, holds_timetrace_token, mask_structure, mask_timetrace_tokens, redact

PROTOCOL_VERSION = 1
METHOD_NOT_FOUND = -32601
MCP_SERVER = "timetrace"
# How long a setup request (initialize, session/new, session/resume) may go unanswered; session/load replays the
# whole history first, so it gets longer. The prompt itself is bounded by the run's own timeout only.
REQUEST_TIMEOUT_SECONDS = 60.0
LOAD_TIMEOUT_SECONDS = 300.0
# After session/cancel: how long the agent has to answer the prompt with "cancelled" before its group is stopped.
# Small, because a queue stop must stay inside launchd's 20 s (this + process.py's own stop bounds).
CANCEL_GRACE_SECONDS = 2.0
# After the turn: an ACP agent normally leaves when its stdin closes; one that does not is stopped as a group.
EXIT_GRACE_SECONDS = 2.0
MAX_TEXT_CHARS = 256 * 1024     # agent text kept for the result (the most recent part)
MAX_PLAN_ENTRIES = 50
SHOWN_CHARS = 300               # one transcript detail (a title, a garbage line, an error message)
_FLUSH_CHARS = 16 * 1024        # streamed text with no newline is written once this long
# How much streamed text a forced flush always keeps back: more than the longest TimeTrace token
# (mcp_access.MAX_TOKEN_CHARS), so one still arriving is masked once it is complete, never written in two pieces.
_HOLD_CHARS = 1024
# The session ids this client keeps: an agent's sessionId ends up in results and checkpoints, so one of any other
# shape (or one holding a TimeTrace token) is a protocol error and is never kept.
SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# The stop reasons ACP v1 defines; any other value an agent sends is reported as "other", never verbatim.
STOP_REASONS = ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")


class AcpError(RuntimeError):
    """The conversation with the agent failed: an error answer, no answer in time, or the agent went away."""

    def __init__(self, message: str, code: Optional[int] = None, method: str = ""):
        super().__init__(message)
        self.code, self.method = code, method


class _Interrupted(Exception):
    """A stop / cancel (reason "stop") or the run's timeout (reason "timeout") while waiting for the agent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class AcpResult:
    stop_reason: str = ""               # the prompt's stopReason ("" when it never answered)
    text: str = ""                      # every agent_message_chunk of the turn, the most recent MAX_TEXT_CHARS
    last_message: str = ""              # the agent's text since its last tool call (what a final answer is)
    session_id: Optional[str] = None    # set once the agent created / resumed the session
    usage: Optional[Dict[str, Any]] = None   # the last usage_update
    tool_calls: int = 0
    plan: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    failed_method: str = ""             # which request failed, when one did
    exit_code: Optional[int] = None
    cancelled: bool = False
    mcp_attached: bool = False


# ---- permission prompts ------------------------------------------------------------------------------------------

# The worktree's rule table (timetrace.approvals, behind the host) speaks Claude Code's tool names. An ACP tool call
# is described by its `kind`; the kinds whose effect that table can judge are translated to the matching name with
# the paths / command the agent declared, everything else reaches the host as "acp:<kind>" (the table then refuses
# only credential access and leaves the decision to the phone). `require_phone` keeps the table's refusals but turns
# its local "allow" into a question, for calls the table cannot fully see.
_PATH_KEYS = ("file_path", "path", "absolute_path", "notebook_path", "filename", "source", "destination", "target",
              "old_path", "new_path")
_LIST_PATH_KEYS = ("paths", "file_paths")
_DIR_KEYS = ("dir_path", "directory", "cwd", "working_directory", "workdir")
_SLOTS = ("file_path", "path", "notebook_path")   # the keys approvals._paths reads, all of them
_FILE_KINDS = ("edit", "delete", "move", "read", "search")


def _declared_paths(tool_call: Dict[str, Any], raw: Dict[str, Any]) -> List[str]:
    found = [loc["path"] for loc in tool_call.get("locations") or []
             if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"]]
    found += [raw[key] for key in _PATH_KEYS + _DIR_KEYS if isinstance(raw.get(key), str) and raw[key]]
    for key in _LIST_PATH_KEYS:
        if isinstance(raw.get(key), list):
            found += [value for value in raw[key] if isinstance(value, str) and value]
    return list(dict.fromkeys(found))


def _runs_elsewhere(raw: Dict[str, Any], root: str) -> bool:
    """A command whose declared working directory is not the worktree root itself: the rule table resolves a
    command's relative paths against the root, so it cannot judge one run anywhere else (a subdirectory included)."""
    real_root = os.path.realpath(root)
    for key in _DIR_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value and os.path.realpath(os.path.join(real_root, value)) != real_root:
            return True
    return False


def host_request(tool_call: Any, root: str) -> Optional[Dict[str, Any]]:
    """What the host is asked about one session/request_permission, or None when it is refused without asking
    anyone: a mode switch, which could widen what the agent may do without asking for the rest of the session."""
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    raw = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), dict) else {}
    kind = str(tool_call.get("kind") or "other")[:32]
    title = _short(tool_call.get("title") or "")
    base = {"acp_kind": kind, "title": title, "tool_use_id": str(tool_call.get("toolCallId") or "")[:128]}
    if kind == "switch_mode":
        return None
    command = raw.get("command")
    if kind == "execute" and isinstance(command, str) and command.strip():
        return dict(base, tool_name="Bash", input={"command": command}, require_phone=_runs_elsewhere(raw, root))
    if kind in _FILE_KINDS:
        paths = _declared_paths(tool_call, raw)
        # Only an edit of declared paths may be allowed by the table alone. A read or search can name its target in
        # ways not listed here (a glob pattern, an include filter), and a delete / move is not an edit a task run is
        # allowed without asking, so those always reach the phone unless the table refuses them first.
        unseen = kind != "edit" or not paths or len(paths) > len(_SLOTS)
        return dict(base, tool_name="Read" if kind in ("read", "search") else "Edit",
                    input=dict(zip(_SLOTS, paths)), require_phone=unseen)
    if kind == "fetch":
        url = raw.get("url") if isinstance(raw.get("url"), str) else ""
        return dict(base, tool_name="WebFetch", input={"url": url, "title": title})
    tool_input = dict(raw)
    tool_input.setdefault("title", title)
    return dict(base, tool_name="acp:" + kind, input=tool_input)


def choose_option(options: Any, allow: bool) -> Dict[str, Any]:
    """The answer to a permission prompt: the agent's own allow_once / reject_once option, never an *_always one.
    When no one-time allow is on offer the prompt is refused (a lasting grant is never made on the user's behalf);
    when there is no one-time reject either, it is answered "cancelled"."""
    wanted = "allow_once" if allow else "reject_once"
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and option.get("kind") == wanted and isinstance(option.get("optionId"), str):
            return {"outcome": "selected", "optionId": option["optionId"]}
    if allow:
        return choose_option(options, False)
    return {"outcome": "cancelled"}


# ---- one session -----------------------------------------------------------------------------------------------

class _Pending:
    def __init__(self, method: str):
        self.method = method
        self.done = threading.Event()
        self.result: Dict[str, Any] = {}
        self.error: Optional[Dict[str, Any]] = None


class _PermissionWait:
    def __init__(self, request_id: Any, title: str):
        self.request_id, self.title = request_id, title
        self.cancelled = threading.Event()
        self.answered = False


def _id_key(value: Any) -> str:
    return json.dumps(value)


def _content_text(content: Any) -> str:
    if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def safe_text(value: Any, limit: int = SHOWN_CHARS) -> str:
    """Agent-supplied text (an error message, a title, a stop reason) as it may be logged, raised, stored in a result
    or uploaded: secrets masked in the WHOLE text first (TimeTrace tokens, then every other recognised secret), only
    then whitespace folded and cut. Cutting first could leave a token's prefix that no pattern recognises any more."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(redact(mask_timetrace_tokens(text)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_short = safe_text


def _safe_cut(text: str, cut: int) -> int:
    """`cut`, moved back to the start of a TimeTrace token it would split. Text kept or written in pieces is cut only
    there, so every token stays whole in one piece and is masked when that piece is (a token is at most
    _HOLD_CHARS long, so the move is bounded)."""
    for match in _TIMETRACE_TOKEN.finditer(text, max(0, cut - _HOLD_CHARS), min(len(text), cut + _HOLD_CHARS)):
        if match.start() < cut < match.end():
            return match.start()
    return cut


def safe_session_id(value: Any) -> Optional[str]:
    """The one rule for every session id that enters or leaves an ACP run (the caller's resume id, the agent's
    answer, every result and fallback): `value` when it has the conservative SESSION_ID shape and holds no TimeTrace
    token (by shape, or by the value of one this process handed out); None otherwise. Ids end up in results and
    checkpoints, so nothing else is ever kept."""
    if (isinstance(value, str) and SESSION_ID.fullmatch(value) is not None
            and not holds_timetrace_token(value.encode("utf-8")) and mask_timetrace_tokens(value) == value):
        return value
    return None


def stop_reason(value: Any) -> str:
    """An agent's stopReason as it may be stored and shown: one of ACP's own, "other", or "" when there is none."""
    if not isinstance(value, str) or not value:
        return ""
    return value if value in STOP_REASONS else "other"


class AcpSession:
    """One agent process and one prompt turn (a fresh session, or a resumed one when `session_id` is given).

    `host` (optional) answers permission prompts the way it does for a Claude stream session:
      host.permission(request, cancelled) -> {"behavior": "allow" | "deny", ...}
    with `request` as host_request builds it (masked), blocking; `cancelled` is set when the turn is cancelled or
    the run ends. Without a host, or when it fails, every prompt is refused. Time spent in host.permission does not
    count toward `timeout`."""

    def __init__(self, cmd: Sequence[str], cwd: str, log_file: str, prompt: str, *, session_id: Optional[str] = None,
                 host: Any = None, mcp: Optional[McpAccess] = None, timeout: Optional[float] = None,
                 cancel_event: Optional[threading.Event] = None, drop_env: Optional[Sequence[str]] = None,
                 env: Optional[dict] = None, base_env: Optional[Dict[str, str]] = None,
                 request_timeout: float = REQUEST_TIMEOUT_SECONDS,
                 load_timeout: float = LOAD_TIMEOUT_SECONDS, client_name: str = "timetrace"):
        self.cmd, self.cwd, self.log_file, self.prompt = list(cmd), os.path.abspath(cwd), log_file, prompt
        self.session_id, self.host, self.mcp, self.timeout = session_id, host, mcp, timeout
        self.cancel_event, self.drop_env, self.env, self.base_env = cancel_event, drop_env, env, base_env
        self.request_timeout, self.load_timeout, self.client_name = request_timeout, load_timeout, client_name
        self.garbage_lines = 0
        self._proc: Optional[subprocess.Popen] = None
        self._log = None
        self._log_lock = threading.Lock()
        # Every write to the agent's stdin happens on one writer thread, from this queue: an agent that stops reading
        # blocks only that thread, never the run (its timeout, a cancel and the cleanup all go on without it).
        self._outbox: "queue.Queue[Optional[str]]" = queue.Queue()
        self._writer: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()     # pending requests, permission waits, the pause clock
        self._ids = itertools.count(1)
        self._pending: Dict[str, _Pending] = {}
        self._permissions: Dict[str, _PermissionWait] = {}
        self._workers: List[threading.Thread] = []
        self._eof = threading.Event()           # the agent's stdout is closed: no answer can come any more
        self._ended = threading.Event()
        self._cancelling = False
        self._writing = False                   # the writer takes messages (guarded by _state_lock)
        self._session: Optional[str] = None
        self._prompt_pending: Optional[_Pending] = None
        self._replaying = False
        self._replayed = 0
        self._started = 0.0
        self._waiting = 0
        self._paused_total = 0.0
        self._paused_since: Optional[float] = None
        # The turn's aggregates; written by the stdout reader only. The agent's text is kept as one raw stream: the
        # final message (what it said after its last tool call) is a position in it, not a separate copy, so a token
        # that straddles a tool call is still masked as a whole.
        self._text: List[str] = []
        self._text_len = 0
        self._dropped = 0        # characters trimmed off the front of the kept text so far
        self._last_from = 0      # where the last message starts, counted over all text ever received
        self._tool_ids: set = set()
        self._titles: Dict[str, str] = {}
        self._plan: Optional[List[Dict[str, Any]]] = None
        self._usage: Optional[Dict[str, Any]] = None
        # The transcript's streams (agent message text, thoughts, stray stdout, stderr), each with its own pending
        # text. A stream's text is written only at its own newline, by a forced flush that keeps _HOLD_CHARS back, or
        # at the very end, and always masked as a whole: a line of another stream never ends or flushes it, so a
        # token arriving in pieces is masked once it is complete.
        self._pending_text: Dict[str, str] = {}

    # ---- transcript ----------------------------------------------------------------------------------------------
    def _write_locked(self, text: str) -> None:
        log = self._log
        if log is None:
            return
        try:
            if log.tell() < MAX_LOG_BYTES:
                log.write(text[:max(0, MAX_LOG_BYTES - log.tell())])
                log.flush()
        except (OSError, ValueError):   # closed under a late reader: the run is over
            pass

    _PREFIXES = {"message": "", "thought": "[thought] ", "stdout": "[stdout] ", "stderr": "[stderr] "}

    def _write_stream_locked(self, kind: str, text: str) -> None:
        self._write_locked(self._PREFIXES[kind] + mask_timetrace_tokens(text) + "\n")

    def _end_streams_locked(self) -> None:
        """The very end: whatever each stream still holds, masked whole."""
        for kind, text in list(self._pending_text.items()):
            if text:
                self._write_stream_locked(kind, text)
        self._pending_text.clear()

    def _line(self, text: str) -> None:
        """One complete line of the client's own (an event, a request, a decision). It is written at once and leaves
        every stream's pending text where it is."""
        with self._log_lock:
            self._write_locked(mask_timetrace_tokens(text) + "\n")

    def _stream(self, kind: str, chunk: str) -> None:
        with self._log_lock:
            text = self._pending_text.get(kind, "") + chunk
            while "\n" in text:
                line, text = text.split("\n", 1)
                self._write_stream_locked(kind, line)
            if len(text) > _FLUSH_CHARS:
                # A line this long is written in pieces, always keeping _HOLD_CHARS back and never cutting a token:
                # one that is still arriving is masked once it is complete. Whitespace is preferred (no token has any).
                cut = len(text) - _HOLD_CHARS
                space = text.rfind(" ", 0, cut)
                cut = space + 1 if space >= 0 else _safe_cut(text, cut)
                if cut > 0:
                    self._write_stream_locked(kind, text[:cut])
                    text = text[cut:]
            self._pending_text[kind] = text

    # ---- the wire ------------------------------------------------------------------------------------------------
    def _send(self, message: Dict[str, Any]) -> bool:
        """Queue one message for the agent; False once its input is closed. Never blocks."""
        line = json.dumps(message, ensure_ascii=False) + "\n"
        with self._state_lock:
            if not self._writing:
                return False
            self._outbox.put(line)
        return True

    def _close_stdin(self) -> None:
        """No more messages: the writer sends what is queued, then closes stdin (an ACP agent leaves on that EOF)."""
        with self._state_lock:
            if self._writing:
                self._writing = False
                self._outbox.put(None)

    def _write_stdin(self, stream) -> None:
        """The writer thread: the only code that touches the agent's stdin, so a blocked write (an agent that stopped
        reading, a prompt larger than the pipe) holds nothing anyone else needs. It ends on the close marker or when
        the pipe breaks, which stopping the agent's process group always does."""
        try:
            while True:
                line = self._outbox.get()
                if line is None:
                    break
                stream.write(line)
                stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            with self._state_lock:
                self._writing = False
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _read_stdout(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                raw = line.strip()
                if not raw:
                    continue
                message = None
                if raw.startswith("{"):
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        message = None
                if not isinstance(message, dict):
                    # ACP reserves stdout for JSON-RPC; a stray line is shown, never acted on.
                    self.garbage_lines += 1
                    self._stream("stdout", line if line.endswith("\n") else line + "\n")
                    continue
                try:
                    self._dispatch(message)
                except Exception as exc:
                    self._line("[client] a message from the agent could not be handled (%s)" % exc.__class__.__name__)
        except (OSError, ValueError):
            pass
        finally:
            self._eof.set()

    def _read_stderr(self, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._stream("stderr", line)
        except (OSError, ValueError):
            pass

    def _dispatch(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if message.get("id") is not None:
                self._on_agent_request(message["id"], method, params)
            elif method == "session/update":
                self._on_update(params)
            return  # any other notification changes nothing here
        with self._state_lock:
            pending = self._pending.pop(_id_key(message.get("id")), None)
        if pending is None:
            return
        if isinstance(message.get("error"), dict):
            pending.error = message["error"]
        elif "result" in message:
            pending.result = message["result"] if isinstance(message["result"], dict) else {}
        else:
            pending.error = {"message": "an answer with neither result nor error"}
        pending.done.set()

    def _on_agent_request(self, request_id: Any, method: str, params: Dict[str, Any]) -> None:
        if method == "session/request_permission":
            worker = threading.Thread(target=self._answer_permission, args=(request_id, params), daemon=True)
            with self._state_lock:
                self._workers.append(worker)
            worker.start()
            return
        if method.startswith(("fs/", "terminal/")):
            why = "this client advertises no %s capability" % method.split("/", 1)[0]
        else:
            why = "unknown method"
        self._line("[client] refused %s (%s)" % (_short(method), why))
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": METHOD_NOT_FOUND, "message": "Method not found: %s" % method[:200]}})

    # ---- permission prompts --------------------------------------------------------------------------------------
    def _answer_permission(self, request_id: Any, params: Dict[str, Any]) -> None:
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = params.get("options")
        wait = _PermissionWait(request_id, _short(tool_call.get("title") or tool_call.get("kind") or "tool call"))
        key = _id_key(request_id)
        with self._state_lock:
            over = self._ended.is_set() or self._cancelling
            if not over:
                self._permissions[key] = wait
                self._waiting += 1
                if self._waiting == 1:
                    self._paused_since = time.monotonic()
        if over:
            self._settle(wait, {"outcome": "cancelled"}, "the turn is being cancelled")
            return
        allow, who = False, "no host to ask"
        try:
            request = host_request(tool_call, self.cwd)
            if request is None:
                who = "a mode switch is never allowed"
            elif self.host is not None:
                answer = self.host.permission(mask_structure(request), wait.cancelled) or {}
                allow = isinstance(answer, dict) and answer.get("behavior") == "allow"
                who = "host"
        except Exception as exc:
            allow, who = False, "host failed (%s)" % exc.__class__.__name__
        finally:
            with self._state_lock:
                self._permissions.pop(key, None)
                self._waiting -= 1
                if self._waiting == 0 and self._paused_since is not None:
                    self._paused_total += time.monotonic() - self._paused_since
                    self._paused_since = None
        self._settle(wait, choose_option(options, allow), who)

    def _settle(self, wait: _PermissionWait, outcome: Dict[str, Any], who: str) -> None:
        """Answer one permission request, exactly once (the worker and a cancel may both try)."""
        with self._state_lock:
            if wait.answered:
                return
            wait.answered = True
        self._send({"jsonrpc": "2.0", "id": wait.request_id, "result": {"outcome": outcome}})
        self._line("[permission] %s -> %s (%s)" % (wait.title, outcome.get("optionId") or outcome.get("outcome"), who))

    def _paused_seconds(self) -> float:
        with self._state_lock:
            extra = time.monotonic() - self._paused_since if self._paused_since is not None else 0.0
            return self._paused_total + extra

    # ---- session/update --------------------------------------------------------------------------------------------
    def _on_update(self, params: Dict[str, Any]) -> None:
        if self._session is not None and params.get("sessionId") not in (None, self._session):
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if self._replaying:
            self._replayed += 1   # session/load replays history: shown as a count, never counted as this turn's work
            return
        if kind == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                self._add_text(text)
                self._stream("message", text)
        elif kind == "agent_thought_chunk":
            text = _content_text(update.get("content"))
            if text:
                self._stream("thought", text)
        elif kind == "tool_call":
            tool_id = str(update.get("toolCallId") or "")
            title = _short(update.get("title") or update.get("kind") or "tool call")
            self._tool_ids.add(tool_id or str(len(self._tool_ids)))
            if tool_id and len(self._titles) < 10_000:
                self._titles[tool_id] = title
            self._last_from = self._dropped + self._text_len   # what the agent says after a tool call is newer
            self._line("[tool] %s: %s (%s)" % (update.get("kind") or "other", title, update.get("status") or "pending"))
        elif kind == "tool_call_update":
            status = update.get("status")
            if status in ("completed", "failed"):
                tool_id = str(update.get("toolCallId") or "")
                self._line("[tool] %s: %s" % (self._titles.get(tool_id) or _short(tool_id), status))
        elif kind == "plan":
            self._plan = [{"content": _short(entry.get("content") or ""), "priority": str(entry.get("priority") or ""),
                           "status": str(entry.get("status") or "")}
                          for entry in (update.get("entries") or [])[:MAX_PLAN_ENTRIES] if isinstance(entry, dict)]
            marks = {"completed": "[x]", "in_progress": "[>]"}
            self._line("[plan] " + " | ".join("%s %s" % (marks.get(e["status"], "[ ]"), e["content"]) for e in self._plan))
        elif kind == "usage_update":
            self._usage = {key: update[key] for key in ("used", "size", "cost") if key in update}
        elif kind == "current_mode_update":
            self._line("[mode] %s" % _short(update.get("currentModeId") or ""))
        elif kind == "notice":
            self._line("[notice] %s" % _short({k: v for k, v in update.items() if k != "sessionUpdate"}))
        # Every other variant (user message chunks, commands, config options, session info, compaction, anything a
        # later schema adds) changes nothing for a dispatched run.

    def _add_text(self, text: str) -> None:
        self._text.append(text)
        self._text_len += len(text)
        # Kept raw (a token may still be arriving), so the front is cut only where no token is split.
        if self._text_len > 2 * MAX_TEXT_CHARS:
            joined = "".join(self._text)
            cut = _safe_cut(joined, len(joined) - MAX_TEXT_CHARS)
            self._text, self._text_len, self._dropped = [joined[cut:]], len(joined) - cut, self._dropped + cut

    def _final_texts(self) -> "tuple":
        """(all of the agent's text, its last message), each masked as a whole before it is cut. The last message
        starts where the last tool call came, moved back to the start of a token that straddles that point."""
        joined = "".join(self._text)
        start = min(max(0, self._last_from - self._dropped), len(joined))
        start = _safe_cut(joined, start) if 0 < start < len(joined) else start
        return (mask_timetrace_tokens(joined)[-MAX_TEXT_CHARS:],
                mask_timetrace_tokens(joined[start:])[-MAX_TEXT_CHARS:])

    # ---- requests --------------------------------------------------------------------------------------------------
    def _request(self, method: str, params: Dict[str, Any], shown: str = "") -> _Pending:
        pending = _Pending(method)
        with self._state_lock:
            request_id = next(self._ids)
            self._pending[_id_key(request_id)] = pending
        self._line("-> %s%s" % (method, shown))
        if not self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}):
            raise AcpError("%s: the agent no longer reads its input" % method, method=method)
        return pending

    def _call(self, method: str, params: Dict[str, Any], timeout: Optional[float], shown: str = "") -> Dict[str, Any]:
        return self._await(self._request(method, params, shown), timeout)

    def _elapsed(self) -> float:
        return time.monotonic() - self._started - self._paused_seconds()

    def _await(self, pending: _Pending, timeout: Optional[float]) -> Dict[str, Any]:
        started = time.monotonic()
        while not pending.done.is_set():
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise _Interrupted("stop")
            if self._eof.is_set():
                # The reader handles every line before it reports the end, so no answer is coming.
                if pending.done.is_set():
                    break
                code = self._proc.poll() if self._proc is not None else None
                raise AcpError("the agent closed its output before answering %s%s" % (
                    pending.method, "" if code is None else " (exit %s)" % code), method=pending.method)
            if timeout is not None and time.monotonic() - started >= timeout:
                raise AcpError("%s: no answer within %gs" % (pending.method, timeout), method=pending.method)
            if self.timeout is not None and self._elapsed() >= self.timeout:
                raise _Interrupted("timeout")
            pending.done.wait(.05)
        if pending.error is not None:
            code = pending.error.get("code")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            # A non-integer code is agent-supplied text like the message: masked whole, then cut.
            shown = code if code is not None else safe_text(pending.error.get("code"), 40)
            message = _short(pending.error.get("message") or "")
            self._line("<- %s error %s: %s" % (pending.method, shown, message))
            raise AcpError("%s failed (%s): %s" % (pending.method, shown, message), code=code, method=pending.method)
        return pending.result

    # ---- the conversation ----------------------------------------------------------------------------------------
    def _converse(self, result: AcpResult) -> None:
        init = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": self.client_name, "version": __version__},
        }, self.request_timeout)
        caps = init.get("agentCapabilities") if isinstance(init.get("agentCapabilities"), dict) else {}
        mcp_caps = caps.get("mcpCapabilities") if isinstance(caps.get("mcpCapabilities"), dict) else {}
        session_caps = caps.get("sessionCapabilities") if isinstance(caps.get("sessionCapabilities"), dict) else {}
        can_load = caps.get("loadSession") is True
        can_resume = isinstance(session_caps.get("resume"), dict)
        http_mcp = mcp_caps.get("http") is True
        methods = [m.get("id") for m in init.get("authMethods") or [] if isinstance(m, dict)]
        self._line("<- initialize: protocol %s, loadSession=%s, resume=%s, http MCP=%s, auth methods %s" % (
            _short(init.get("protocolVersion")), can_load, can_resume, http_mcp, _short(methods)))
        if init.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcpError("the agent speaks ACP protocol %s; this client speaks %d" % (
                _short(init.get("protocolVersion")), PROTOCOL_VERSION), method="initialize")
        servers = self._mcp_servers(http_mcp, result)
        shown = ", mcpServers: %s" % (", ".join("%s (%s)" % (s["name"], s["type"]) for s in servers) or "none")
        if self.session_id:
            self._session = self.session_id
            params = {"sessionId": self.session_id, "cwd": self.cwd, "mcpServers": servers}
            if can_resume:
                self._call("session/resume", params, self.request_timeout, " %s%s" % (self.session_id, shown))
            elif can_load:
                self._replaying = True
                try:
                    self._call("session/load", params, self.load_timeout, " %s%s" % (self.session_id, shown))
                finally:
                    self._replaying = False
                self._line("<- session/load: %d earlier updates replayed (not part of this turn)" % self._replayed)
            else:
                raise AcpError("resume_unsupported: the agent offers neither session/resume nor session/load",
                               method="session/resume")
        else:
            answer = self._call("session/new", {"cwd": self.cwd, "mcpServers": servers}, self.request_timeout,
                                " cwd=%s%s" % (self.cwd, shown))
            session = answer.get("sessionId")
            if safe_session_id(session) is None:
                raise AcpError("session/new answered without a session id this client accepts", method="session/new")
            self._session = session
            self._line("<- session/new: session %s" % session)
        result.session_id = safe_session_id(self._session)
        self._prompt_pending = self._request("session/prompt", {
            "sessionId": self._session, "prompt": [{"type": "text", "text": self.prompt}],
        }, " (%d characters)" % len(self.prompt))
        answer = self._await(self._prompt_pending, None)
        result.stop_reason = stop_reason(answer.get("stopReason"))
        self._line("<- session/prompt: stopReason %s" % (result.stop_reason or "missing"))

    def _mcp_servers(self, http_mcp: bool, result: AcpResult) -> List[Dict[str, Any]]:
        """TimeTrace's MCP server for this run, when there is an access and the agent takes http servers. The header
        carries the token: it goes to the agent in the request only, never to the transcript."""
        if self.mcp is None:
            return []
        if not http_mcp:
            self._line(skip_note("the agent does not advertise http MCP servers"))
            return []
        result.mcp_attached = True
        self._line("mcp: %s attached (http, %s)" % (MCP_SERVER, self.mcp.url))
        return [{"type": "http", "name": MCP_SERVER, "url": self.mcp.url,
                 "headers": [{"name": "Authorization", "value": "Bearer " + self.mcp.token}]}]

    def _cancel_turn(self) -> None:
        """session/cancel for the prompt in flight, then "cancelled" for every permission prompt still open (the
        protocol's order); the host stops waiting on each."""
        with self._state_lock:
            self._cancelling = True
            waits = list(self._permissions.values())
        if self._prompt_pending is not None and not self._prompt_pending.done.is_set() and self._session:
            self._line("-> session/cancel")
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self._session}})
        for wait in waits:
            wait.cancelled.set()
            self._settle(wait, {"outcome": "cancelled"}, "the turn was cancelled")

    def _interrupt(self, result: AcpResult, reason: str) -> None:
        """A stop / cancel or the timeout landed while the agent was working: cancel the turn, give the agent a short
        grace to answer it, then stop its process group (bounded, and confirmed, for the queue's StopRequest)."""
        proc = self._proc
        self._cancel_turn()
        pending = self._prompt_pending
        if pending is not None:
            deadline = time.monotonic() + CANCEL_GRACE_SECONDS
            while not pending.done.is_set() and not self._eof.is_set() and time.monotonic() < deadline:
                pending.done.wait(.05)
            if pending.done.is_set() and pending.error is None and stop_reason(pending.result.get("stopReason")):
                result.stop_reason = stop_reason(pending.result.get("stopReason"))
                self._line("<- session/prompt: stopReason %s" % result.stop_reason)
        result.cancelled = True
        result.error = "cancelled" if reason == "stop" else "timed out after %gs" % (self.timeout or 0)
        if proc is None or proc.poll() is not None:
            return
        stop = self.cancel_event
        if reason == "stop" and isinstance(stop, StopRequest):
            stop.tool_signalled = True
            self._line("--- stop requested (%s): signalling the agent's process group ---" % stop.reason)
            try:
                process._terminate_group(proc, confirm=process.STOP_CONFIRM_SECONDS)
            except ToolExitUnconfirmed:
                self._line("--- stop requested: the agent's exit is NOT confirmed ---")
                raise
            return
        self._line("--- %s: stopping the agent's process group ---" % ("cancelled" if reason == "stop" else "timeout"))
        process._terminate_group(proc)

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    def run(self) -> AcpResult:
        result = AcpResult()
        if self.session_id is not None and safe_session_id(self.session_id) is None:
            # Refused before anything is spawned or logged: the id is never sent, written or kept.
            result.error, result.failed_method, result.exit_code = "invalid session id", "session/resume", 1
            return result
        # With `base_env` the child gets that snapshot (the one a billing check read), not a fresh os.environ.
        run_env = child_env(self.drop_env, self.env, base=self.base_env)
        stopping = isinstance(self.cancel_event, StopRequest)
        with open(self.log_file, "a", encoding="utf-8") as log:
            self._log = log
            self._line("$ " + " ".join(_shell_quote(part) for part in self.cmd))
            # Right before the OS boundary, as run_streaming does: nothing is spawned once authority is gone.
            enforce_spawn_authority(self.cancel_event)
            try:
                proc = subprocess.Popen(
                    self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=run_env, text=True, bufsize=1, start_new_session=True,
                )
            except OSError as exc:
                self._line("--- the agent could not be started (%s) ---" % exc.__class__.__name__)
                result.error = "could not start %s: %s" % (os.path.basename(self.cmd[0]), exc.__class__.__name__)
                result.failed_method, result.exit_code = "spawn", 127
                self._log = None
                return result
            self._proc = proc
            self._writing = True
            self._writer = threading.Thread(target=self._write_stdin, args=(proc.stdin,), daemon=True)
            self._writer.start()
            self._started = time.monotonic()
            readers = [threading.Thread(target=self._read_stdout, args=(proc.stdout,), daemon=True),
                       threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True)]
            for reader in readers:
                reader.start()
            unconfirmed: Optional[ToolExitUnconfirmed] = None
            try:
                try:
                    self._converse(result)
                except _Interrupted as interrupted:
                    self._interrupt(result, interrupted.reason)
                except AcpError as exc:
                    result.error, result.failed_method = str(exc), exc.method
            except ToolExitUnconfirmed as exc:
                unconfirmed = exc
            finally:
                late = self._finish(proc, readers, stopping, unconfirmed is not None)
            unconfirmed = unconfirmed or late
            if unconfirmed is None and stopping and self.cancel_event.tool_signalled and not process._group_emptied(proc):
                unconfirmed = ToolExitUnconfirmed("the agent's process group (%d) is not confirmed gone after SIGKILL: "
                                                  "the kernel did not report it as non-existent" % proc.pid)
            if unconfirmed is not None:
                self._line("--- stop requested: the agent's process group is NOT confirmed gone ---")
                self._log = None
                raise unconfirmed
            self._line("--- exit %s ---" % proc.returncode)
            self._log = None
        result.exit_code = proc.returncode
        # The whole text is masked first, then cut (a cut inside a mask marker is harmless).
        result.text, result.last_message = self._final_texts()
        result.tool_calls, result.plan, result.usage = len(self._tool_ids), self._plan, self._usage
        return result

    def _finish(self, proc: subprocess.Popen, readers: List[threading.Thread], stopping: bool,
                unconfirmed: bool) -> Optional[ToolExitUnconfirmed]:
        """However the run ended: no permission wait is left open, stdin is closed (an ACP agent leaves by itself
        then), an agent that stays is stopped as a group, and whatever is left of the group is killed."""
        self._ended.set()
        with self._state_lock:
            waits = list(self._permissions.values())
        for wait in waits:
            wait.cancelled.set()
        self._close_stdin()
        problem = None
        if not unconfirmed and proc.poll() is None:
            try:
                proc.wait(timeout=EXIT_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                confirm = process.STOP_CONFIRM_SECONDS if stopping and self.cancel_event.is_set() else None
                try:
                    process._terminate_group(proc, confirm=confirm)
                except ToolExitUnconfirmed as exc:
                    problem = exc
        # However the agent ended, nothing it started may keep running after the runner's checks begin.
        process._kill_group(proc)
        for thread in readers + ([self._writer] if self._writer is not None else []):
            thread.join(timeout=2)
        with self._state_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        with self._log_lock:
            self._end_streams_locked()
        for line in self._summary_lines():
            self._line(line)
        return problem

    def _summary_lines(self) -> List[str]:
        lines = []
        if self._usage is not None:
            cost = self._usage.get("cost") if isinstance(self._usage.get("cost"), dict) else {}
            # A subscription login should never report money; when an agent does, the transcript says so plainly.
            lines.append("[usage] used %s of %s tokens%s" % (
                self._usage.get("used"), self._usage.get("size"),
                ", the agent reports a cost of %s %s" % (cost.get("amount"), cost.get("currency")) if cost else ""))
        if self._tool_ids or self.garbage_lines:
            lines.append("[summary] %d tool call(s)%s" % (len(self._tool_ids), ", %d non-JSON stdout line(s) ignored"
                                                          % self.garbage_lines if self.garbage_lines else ""))
        return lines


def run_acp(cmd: Sequence[str], cwd: str, log_file: str, prompt: str, **kwargs: Any) -> AcpResult:
    return AcpSession(cmd, cwd, log_file, prompt, **kwargs).run()
