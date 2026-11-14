"""AI 思路上报: what a running task step is doing, for the run canvas.

Two event types ride a task job's event stream (protocol 2, contiguous seq
through Agent._report) and only when the claim says the server accepts them:

- `progress`: one action-level line taken from a live tool call ("运行 npm
  test", "修改文件 web/src/Today.tsx"), built by approvals.summary() — never
  command output, file contents or the model's reasoning. At most one per job
  per PROGRESS_INTERVAL_SECONDS: within a window the latest line wins, an exact
  repeat of the line last sent is skipped, and a line still waiting when the run
  ends is dropped (the terminal event says how it ended).
- `intent`: the model's own one-sentence plan, only with the account's consent
  (`report_intent`). The run prompt asks for a line starting with `INTENT:`;
  such lines in assistant text are filtered (redact.intent_text) and sent when
  non-empty and different from the last one, at most MAX_INTENTS per attempt.

Gating (claim_flags): `job.report_progress is True` — an older Valley rejects a
whole batch that holds an unknown event type, so without it neither type is
ever sent and nothing else changes. `job.report_intent is True` additionally
for intents and for the prompt instruction.
"""
import json
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from timetrace import approvals
from timetrace.redact import INTENT_CHARS, intent_text

PROGRESS = "progress"
INTENT = "intent"
PROGRESS_CHARS = 200
PROGRESS_INTERVAL_SECONDS = 10.0
MAX_INTENTS = 5

# Appended to the run prompt (and the resume prompt) when the claim has report_intent.
INTENT_INSTRUCTION = (
    "\n\n在改动任何内容之前，先单独写一行以 INTENT: 开头的话，用不超过 120 个字说明你打算怎么完成这项工作"
    "（只写做法，不要包含代码、命令输出、密钥或个人信息）。如果之后做法有实质变化，再写一行新的 INTENT:。"
)
_INTENT_LINE = re.compile(r"^\s*(?:[*_]{1,2})?INTENT(?:[*_]{1,2})?\s*[:：]\s*(?:[*_]{1,2})?\s*(.*?)\s*(?:[*_]{1,2})?\s*$")
_SHELL_WRAPPER = re.compile(r"^\s*(?:\S*/)?(?:bash|zsh|sh)\s+-[A-Za-z]*c\s+(.+)$", re.S)

Observation = Tuple[str, str]  # (PROGRESS | INTENT, text)


def claim_flags(job: Any) -> Tuple[bool, bool]:
    """(report_progress, report_intent) from a claim's job, read strictly: only a JSON `true` counts, and only for a
    task job (Valley sets them for nothing else)."""
    if not isinstance(job, dict) or (job.get("kind") or "task") != "task":
        return False, False
    progress = job.get("report_progress") is True
    return progress, progress and job.get("report_intent") is True


def with_instruction(prompt: str) -> str:
    return prompt + INTENT_INSTRUCTION


def intent_lines(text: Any) -> List[str]:
    """The text after `INTENT:` of every line that starts with it (unfiltered)."""
    if not isinstance(text, str) or "INTENT" not in text:
        return []
    found = []
    for line in text.splitlines():
        match = _INTENT_LINE.match(line)
        if match:
            found.append(match.group(1))
    return found


def _cap(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _shorten_paths(text: str, root: Optional[str], home: Optional[str]) -> str:
    """Paths inside the run's directory are shown relative to it; the user's home as ~."""
    if root:
        text = text.replace(root.rstrip("/") + "/", "")
    if home:
        text = text.replace(home.rstrip("/") + "/", "~/")
    return text


def progress_line(tool: Any, tool_input: Any, root: Optional[str] = None, home: Optional[str] = None) -> str:
    """One action-level line for a tool call, or "" for a tool with nothing to show. A shell command is shown up to
    its first line (a here-document's body is file contents)."""
    tool = approvals.tool_label(tool)
    if tool in approvals.INERT_TOOLS:
        return ""
    tool_input = dict(tool_input) if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return ""
        lines = command.strip().splitlines()
        tool_input = {"command": lines[0] + (" …" if len(lines) > 1 else "")}
    line = approvals.summary(tool, tool_input)
    return _cap(_shorten_paths(line, root, home), PROGRESS_CHARS)


def _unwrap_shell(command: Any) -> str:
    """`bash -lc 'npm test'` → `npm test` (how Codex reports a command)."""
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and re.fullmatch(r"(?:\S*/)?(?:bash|zsh|sh)", parts[0]) and re.fullmatch(r"-[A-Za-z]*c", parts[1]):
            return parts[-1]
        return " ".join(parts)
    if not isinstance(command, str):
        return ""
    match = _SHELL_WRAPPER.match(command)
    if match:
        try:
            parts = shlex.split(match.group(1))
        except ValueError:
            parts = []
        if len(parts) == 1:
            return parts[0]
    return command


def from_claude(msg: Any, root: Optional[str] = None, home: Optional[str] = None) -> List[Observation]:
    """What one Claude stream-json message shows: an `assistant` message's tool_use blocks (progress) and the
    INTENT lines of its text blocks, in order."""
    if not isinstance(msg, dict) or msg.get("type") != "assistant":
        return []
    message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    seen: List[Observation] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            seen.extend((INTENT, text) for text in intent_lines(block.get("text")))
        elif block.get("type") == "tool_use":
            line = progress_line(block.get("name"), block.get("input"), root, home)
            if line:
                seen.append((PROGRESS, line))
    return seen


def from_codex(raw: Any, root: Optional[str] = None, home: Optional[str] = None) -> List[Observation]:
    """What one `codex exec --json` line shows: a command as it starts, a file change, and the INTENT lines of an
    agent message. Reasoning items and command output are never read."""
    if not isinstance(raw, str):
        return []
    raw = raw.strip()
    if not raw.startswith("{"):
        return []
    try:
        msg = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(msg, dict):
        return []
    kind = msg.get("type")
    item = msg.get("item") if isinstance(msg.get("item"), dict) else {}
    item_type = item.get("type")
    if kind == "item.started" and item_type == "command_execution":
        line = progress_line("Bash", {"command": _unwrap_shell(item.get("command"))}, root, home)
        return [(PROGRESS, line)] if line else []
    if kind in ("item.started", "item.completed") and item_type == "file_change":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict) and isinstance(c.get("path"), str)]
        if not changes:
            return []
        line = progress_line("Edit", {"file_path": changes[0]["path"]}, root, home)
        if len(changes) > 1:
            line = _cap(line + " 等 %d 个文件" % len(changes), PROGRESS_CHARS)
        return [(PROGRESS, line)]
    if kind == "item.completed" and item_type == "agent_message":
        return [(INTENT, text) for text in intent_lines(item.get("text"))]
    return []


class Narrator:
    """Throttle, de-duplication and the intent cap of one attempt. Not thread-safe: the RunControl that owns it
    calls it under its own lock. Each method returns the events to send now (possibly none)."""

    def __init__(self, report_intent: bool, interval: float = PROGRESS_INTERVAL_SECONDS,
                 max_intents: int = MAX_INTENTS):
        self.report_intent = report_intent
        self.interval = interval
        self.max_intents = max_intents
        self.last_progress: Optional[str] = None
        self.last_progress_at: Optional[float] = None
        self.pending: Optional[str] = None
        self.last_intent: Optional[str] = None
        self.intents_sent = 0
        self.stopped = False

    def observe(self, seen: List[Observation], now: float) -> List[Dict[str, Any]]:
        events = self.tick(now)
        if self.stopped:
            return events
        for kind, text in seen:
            if kind == PROGRESS:
                event = self._progress(text, now)
            elif kind == INTENT:
                event = self._intent(text)
            else:
                event = None
            if event is not None:
                events.append(event)
        return events

    def _progress(self, line: str, now: float) -> Optional[Dict[str, Any]]:
        line = _cap(line or "", PROGRESS_CHARS)
        if not line:
            return None
        if line == self.last_progress:
            # The latest action is what is already shown: nothing waits any more.
            self.pending = None
            return None
        if self.last_progress_at is None or now - self.last_progress_at >= self.interval:
            self.pending = None
            return self._send(line, now)
        self.pending = line  # the latest line of this window wins
        return None

    def _send(self, line: str, now: float) -> Dict[str, Any]:
        self.last_progress, self.last_progress_at = line, now
        return {"type": PROGRESS, "message": line}

    def tick(self, now: float) -> List[Dict[str, Any]]:
        """The line that waited for its window, once the window has passed."""
        if self.stopped or self.pending is None:
            return []
        if self.last_progress_at is not None and now - self.last_progress_at < self.interval:
            return []
        line, self.pending = self.pending, None
        if line == self.last_progress:
            return []
        return [self._send(line, now)]

    def _intent(self, text: str) -> Optional[Dict[str, Any]]:
        if not self.report_intent or self.intents_sent >= self.max_intents:
            return None
        text = intent_text(text, INTENT_CHARS)
        if not text or text == self.last_intent:
            return None
        self.last_intent = text
        self.intents_sent += 1
        return {"type": INTENT, "message": text}

    def stop(self) -> None:
        """The run is over: a line still waiting is dropped and nothing more is sent."""
        self.pending = None
        self.stopped = True
