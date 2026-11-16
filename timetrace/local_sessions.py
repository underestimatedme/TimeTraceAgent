"""本机 AI 会话记入 TimeTrace: metadata of the Claude Code / Codex sessions a person ran by hand on this computer.

Contract: docs/superpowers/specs/2026-11-16-local-agent-sessions-design.md (TimeTrace repository). Off by default; the
Runner scans only while the last inventory answer said `local_session_import: true`.

Only metadata leaves this computer, one record per session: provider, an opaque session key (sha256 of
"<provider>:<session id>"; the id itself is never sent), the registered workspace id, the most used model, the last
git branch, start / end time, the number of user prompts, four token totals and the NUMBER of distinct files edited.
No conversation text of any kind leaves it, not even a title (the record's `title` is always ""; free text cannot be
guaranteed free of paths, names or secrets), and neither does command output, a file path or file contents. Prompt
text is read only to tell a question the person asked from one the tool injected, and is never kept. A session whose
working directory is not inside a workspace registered with this Runner is never sent, and neither is a session
TimeTrace itself ran.

Read only: `$CLAUDE_CONFIG_DIR` (or ~/.claude) `projects/*/*.jsonl` and `$CODEX_HOME` (or ~/.codex)
`sessions/**/rollout-*.jsonl`. Symbolic links below those directories are not followed and credential files are never
opened.
"""
import hashlib
import json
import os
import re
import stat
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

CLAUDE = "claude"
CODEX = "codex"
# Valley's switch, and the refusal code of POST /runner/local-sessions while it is off.
PREFERENCE = "local_session_import"
OFF_CODE = "local_session_import_off"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES_PER_CYCLE = 200
BATCH_SIZE = 100
MODEL_CHARS = 64
BRANCH_CHARS = 120
# Never opened, whatever a directory listing says (they never match the session patterns either).
CREDENTIAL_NAMES = frozenset((".credentials.json", "auth.json"))
# A state row whose session was not sent (no record, outside every workspace, TimeTrace's own run) keeps an empty
# fingerprint; one Valley refused as out of bounds keeps this prefix before the record's fingerprint, so the same
# record is not offered again. Only a successful upload stores the bare fingerprint.
REJECTED = "rejected:"

# Claude Code edit tools; the edited path is input.file_path (input.notebook_path for notebooks).
CLAUDE_EDIT_TOOLS = frozenset(("Edit", "Write", "MultiEdit", "NotebookEdit"))
# turnOrigin / origin.kind values of user lines the tool injected itself (background task notices, hooks, peers,
# automatic continuations, schedules): not something the person asked.
_CLAUDE_NOT_ASKED = frozenset(("task_notification", "task-notification", "host_synthetic", "auto_continuation",
                               "auto-continuation", "peer", "scheduled", "system", "hook"))
# A user message that starts with a markup tag whose name has a "-" or "_" (<system-reminder>, <task-notification>,
# <command-name>, <local-command-stdout>, <bash-input>, <environment_context>, <user_instructions>,
# <send_user_message_question_reply>, ...) was written by the tool, not typed as a question.
_SYNTHETIC_TAG = re.compile(r"\s*<[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+[ >]")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:?\d{2})?$")


class ImportOff(Exception):
    """Valley refused an upload because the person's switch is off (HTTP 403, code local_session_import_off)."""


def claude_root(env: Optional[Dict[str, str]] = None, user_home: Optional[Path] = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("CLAUDE_CONFIG_DIR") or ""
    return Path(configured).expanduser() if configured else Path(user_home or Path.home()) / ".claude"


def codex_root(env: Optional[Dict[str, str]] = None, user_home: Optional[Path] = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("CODEX_HOME") or ""
    return Path(configured).expanduser() if configured else Path(user_home or Path.home()) / ".codex"


def session_key(provider: str, session_id: str) -> str:
    return hashlib.sha256(("%s:%s" % (provider, session_id)).encode("utf-8")).hexdigest()


def parse_time(value: Any) -> Optional[float]:
    """RFC 3339 / ISO 8601 text as epoch seconds (no zone: UTC). Anything else: None. Works on Python 3.9."""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError:
        return None
    if zone and zone not in ("Z", "z"):
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        moment -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return moment.timestamp() + (float("0." + fraction) if fraction else 0.0)


def format_time(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        value = int(value)
    return value if isinstance(value, int) and value > 0 else 0


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _synthetic(text: str) -> bool:
    return bool(_SYNTHETIC_TAG.match(text))


def _no_follow(path: str, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def _json_lines(path: str) -> Iterator[Dict[str, Any]]:
    """Every JSON object line of `path`; blank, broken (a line still being written) or non-object lines are skipped.
    O_NOFOLLOW: a file swapped for a symbolic link after it was listed is not opened."""
    with open(path, "rb", opener=_no_follow) as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if isinstance(value, dict):
                yield value


def _touched(paths: set, cwd: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    value = value.strip()
    if not os.path.isabs(value) and cwd:
        value = os.path.join(cwd, value)
    paths.add(os.path.normpath(value))


class Summary:
    """One session as read from its file (still holding the local session id and cwd, which never leave)."""

    __slots__ = ("provider", "session_id", "cwd", "model", "git_branch", "started_at", "ended_at", "turns",
                 "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "files_touched")

    def __init__(self, provider: str, session_id: str, **fields: Any):
        self.provider, self.session_id = provider, session_id
        for name in self.__slots__[2:]:
            setattr(self, name, fields.get(name, "" if name in ("cwd", "model", "git_branch") else 0))
        self.started_at = fields.get("started_at")
        self.ended_at = fields.get("ended_at")

    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


# ---- Claude Code ----------------------------------------------------------------------------------------------

class _ClaudeSession:
    def __init__(self):
        self.first = self.last = None
        self.cwd = self.branch = ""
        self.prompts = 0
        self.usage: Dict[Any, Dict[str, Any]] = {}
        self.models: Dict[Any, str] = {}
        self.files: set = set()
        self.unnamed = 0

    def add(self, line: Dict[str, Any]) -> None:
        moment = parse_time(line.get("timestamp"))
        if moment is not None:
            self.first = moment if self.first is None else min(self.first, moment)
            self.last = moment if self.last is None else max(self.last, moment)
        kind = line.get("type")
        if kind not in ("user", "assistant"):
            return
        if not self.cwd and _text(line.get("cwd")):
            self.cwd = line["cwd"]
        if _text(line.get("gitBranch")).strip():
            self.branch = line["gitBranch"].strip()
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        if kind == "assistant":
            self._assistant(message)
        else:
            self._user(line, message)

    def _assistant(self, message: Dict[str, Any]) -> None:
        # One reply is written as several lines (one per content block) carrying the same id and usage: count it once.
        key = message.get("id")
        if not (isinstance(key, str) and key):
            self.unnamed += 1
            key = ("line", self.unnamed)
        if isinstance(message.get("usage"), dict):
            self.usage[key] = message["usage"]
        model = message.get("model")
        if isinstance(model, str) and model.strip() and model != "<synthetic>":
            self.models[key] = model.strip()
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in CLAUDE_EDIT_TOOLS:
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                _touched(self.files, self.cwd, arguments.get("file_path") or arguments.get("notebook_path"))

    def _user(self, line: Dict[str, Any], message: Dict[str, Any]) -> None:
        if _claude_asked(line, message):
            self.prompts += 1

    def summary(self, session_id: str) -> Summary:
        totals = Counter()
        for usage in self.usage.values():
            totals["input"] += _count(usage.get("input_tokens"))
            totals["output"] += _count(usage.get("output_tokens"))
            totals["read"] += _count(usage.get("cache_read_input_tokens"))
            totals["write"] += _count(usage.get("cache_creation_input_tokens"))
        model = Counter(self.models.values()).most_common(1)
        return Summary(CLAUDE, session_id, cwd=self.cwd,
                       model=model[0][0] if model else "", git_branch=self.branch,
                       started_at=self.first, ended_at=self.last, turns=self.prompts,
                       input_tokens=totals["input"], output_tokens=totals["output"],
                       cache_read_tokens=totals["read"], cache_write_tokens=totals["write"],
                       files_touched=len(self.files))


def _claude_asked(line: Dict[str, Any], message: Dict[str, Any]) -> bool:
    """Whether a user line is a question the person asked (not a tool result, a meta or side-chain (sub-agent) line, a
    compaction summary, a slash-command echo or something the tool injected). The text is looked at, never kept."""
    if line.get("isSidechain") is True or line.get("isMeta") is True:
        return False
    if line.get("isCompactSummary") is True or line.get("isVisibleInTranscriptOnly") is True:
        return False
    if "toolUseResult" in line or line.get("promptSource") == "system":
        return False
    if line.get("turnOrigin") in _CLAUDE_NOT_ASKED:
        return False
    origin = line.get("origin")
    if isinstance(origin, dict) and origin.get("kind") is not None and origin.get("kind") != "human":
        return False
    if message.get("role") not in (None, "user"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        blocks = [block for block in content if isinstance(block, dict)]
        if not blocks or any(block.get("type") == "tool_result" for block in blocks):
            return False
        text = "\n".join(_text(block.get("text")) for block in blocks if block.get("type") == "text")
    else:
        return False
    return not (_synthetic(text) or (not text.strip() and isinstance(content, str)))


def parse_claude(path: str) -> Optional[Summary]:
    """One Claude Code transcript (`projects/<dir>/<session id>.jsonl`).

    The file is named after its session. A resumed or forked session's file can start with a copy of another
    session's lines (carrying that session's id, which has its own file): only the file's own session counts."""
    own = Path(path).stem
    sessions: Dict[str, _ClaudeSession] = {}
    named, last_id = set(), None
    for line in _json_lines(path):
        sid = line.get("sessionId")
        if isinstance(sid, str) and sid:
            named.add(sid)
            last_id = sid
        else:
            sid = own  # a line without a session id belongs to the file's own session
        sessions.setdefault(sid, _ClaudeSession()).add(line)
    if last_id is None:
        return None
    if own not in named:
        own = last_id  # a file not named after its session: the session it ends with
    return sessions[own].summary(own)


# ---- Codex ------------------------------------------------------------------------------------------------------

def _codex_patch_texts(payload: Dict[str, Any]) -> List[str]:
    """Texts of a tool call that may hold an apply_patch body (only read when the rollout has no file-change event)."""
    kind = payload.get("type")
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if "*** Begin Patch" in value:
                found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    if kind == "custom_tool_call":
        walk(payload.get("input"))
    elif kind == "function_call":
        arguments = payload.get("arguments")
        try:
            walk(json.loads(arguments) if isinstance(arguments, str) else arguments)
        except (ValueError, RecursionError):
            walk(arguments)
    elif kind == "local_shell_call":
        walk((payload.get("action") or {}).get("command") if isinstance(payload.get("action"), dict) else None)
    return found


def _codex_changes(paths: set, cwd: str, changes: Any) -> None:
    if isinstance(changes, dict):
        for name in changes:
            _touched(paths, cwd, name)
    elif isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict):
                _touched(paths, cwd, change.get("path"))


def _codex_user_text(item: Dict[str, Any]) -> Optional[str]:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    return "\n".join(_text(part.get("text")) for part in content if isinstance(part, dict))


def parse_codex(path: str) -> Optional[Summary]:
    """One Codex rollout (`sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`).

    Older Codex marks a question with an `event_msg` of type `user_message`; newer Codex with an `item_completed`
    event whose item is a `UserMessage`. Token totals are the last cumulative `token_count` (whose input includes the
    cached input, subtracted here). A rollout of a sub-agent thread is not a session the person started: skipped."""
    meta = None
    first = last = None
    models: Counter = Counter()
    total = recorded = None
    asked = {"event": 0, "item": 0}
    changed, patched = set(), set()
    structured = False
    for line in _json_lines(path):
        moment = parse_time(line.get("timestamp"))
        if moment is not None:
            first = moment if first is None else min(first, moment)
            last = moment if last is None else max(last, moment)
        kind = line.get("type")
        payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
        if kind == "session_meta":
            # The first one is the rollout's own thread; a later one describes the thread it was forked from.
            if meta is None:
                meta = payload
            continue
        cwd = _text((meta or {}).get("cwd"))
        if kind == "turn_context":
            if _text(payload.get("model")).strip():
                models[payload["model"].strip()] += 1
        elif kind == "token_usage_record":
            if isinstance(payload.get("thread_token_usage"), dict):
                recorded = payload["thread_token_usage"]
        elif kind == "event_msg":
            event = payload.get("type")
            if event == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    total = info["total_token_usage"]
            elif event == "user_message":
                if payload.get("kind") in (None, "plain") and _codex_asked(payload.get("message")):
                    asked["event"] += 1
            elif event == "item_completed":
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                if item.get("type") == "UserMessage":
                    if _codex_asked(_codex_user_text(item)):
                        asked["item"] += 1
                elif item.get("type") == "FileChange":
                    structured = True
                    _codex_changes(changed, cwd, item.get("changes"))
            elif event in ("patch_apply_begin", "patch_apply_end"):
                structured = True
                _codex_changes(changed, cwd, payload.get("changes"))
        elif kind == "response_item" and not structured:
            for text in _codex_patch_texts(payload):
                for name in _PATCH_FILE.findall(text):
                    _touched(patched, cwd, name)
    if not isinstance(meta, dict) or not _text(meta.get("id")).strip():
        return None
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return None
    # Count the questions from one marker only, so a rollout that writes both never counts one question twice.
    prompts = asked["event"] or asked["item"]
    usage = total if total is not None else (recorded or {})
    inputs, cached = _count(usage.get("input_tokens")), _count(usage.get("cached_input_tokens"))
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    model = models.most_common(1)
    return Summary(CODEX, meta["id"].strip(), cwd=_text(meta.get("cwd")),
                   model=model[0][0] if model else "", git_branch=_text(git.get("branch")).strip(),
                   started_at=first, ended_at=last, turns=prompts,
                   input_tokens=max(0, inputs - cached), output_tokens=_count(usage.get("output_tokens")),
                   cache_read_tokens=cached, cache_write_tokens=_count(usage.get("cache_write_input_tokens")),
                   files_touched=len(changed if structured else patched))


def _codex_asked(text: Any) -> bool:
    """Whether a user message is a question the person asked; the text is looked at, never kept."""
    return isinstance(text, str) and not _synthetic(text)


PARSERS: Dict[str, Callable[[str], Optional[Summary]]] = {CLAUDE: parse_claude, CODEX: parse_codex}


# ---- files ------------------------------------------------------------------------------------------------------

def _regular(entry: "os.DirEntry") -> Optional[os.stat_result]:
    if entry.name in CREDENTIAL_NAMES:
        return None
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _listing(path: str) -> List["os.DirEntry"]:
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except OSError:
        return []


def scan_top(root: Path, child: str) -> Optional[str]:
    """`<root>/<child>` (projects / sessions) when it is safe to read below it, else None: neither the tool's directory
    nor that entry may be a symbolic link, and the entry must really be inside the directory (a link higher up the
    path, such as macOS /var -> /private/var, is followed for both alike and does not count)."""
    base = os.path.normpath(os.path.abspath(str(root)))
    top = os.path.join(base, child)
    try:
        if os.path.islink(base) or os.path.islink(top) or not os.path.isdir(top):
            return None
        if os.path.realpath(top) != os.path.join(os.path.realpath(base), child):
            return None
    except (OSError, ValueError):
        return None
    return top


def claude_files(root: Path) -> Iterator[Tuple[str, os.stat_result]]:
    top = scan_top(root, "projects")
    if top is None:
        return
    for project in _listing(top):
        try:
            if not project.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        for entry in _listing(project.path):
            if entry.name.endswith(".jsonl"):
                info = _regular(entry)
                if info is not None:
                    yield entry.path, info


def codex_files(root: Path) -> Iterator[Tuple[str, os.stat_result]]:
    top = scan_top(root, "sessions")
    if top is None:
        return
    # followlinks=False: a linked sub-directory is listed but never entered; a linked file fails the lstat check.
    for folder, _, names in os.walk(top, followlinks=False):
        for name in names:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")) or name in CREDENTIAL_NAMES:
                continue
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield path, info


# ---- mapping and records ------------------------------------------------------------------------------------------

def real_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip() or not os.path.isabs(path.strip()):
        return ""
    return os.path.realpath(path.strip())


def inside(path: str, root: str) -> bool:
    """`path` is `root` or below it (both already real paths)."""
    if not path or not root:
        return False
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def workspace_for(cwd: str, workspaces: Sequence[Tuple[str, str]]) -> Optional[str]:
    """The id of the registered workspace that holds `cwd` (the innermost when they nest)."""
    best = None
    for workspace_id, root in workspaces:
        if inside(cwd, root) and (best is None or len(root) > len(best[1])):
            best = (workspace_id, root)
    return best[0] if best else None


def record(summary: Summary, workspace_id: str) -> Dict[str, Any]:
    """The upload record (contract table): nothing else of the session leaves this computer. `title` is always ""
    (the contract allows an empty one): no conversation text is uploaded."""
    started = summary.started_at
    ended = summary.ended_at if summary.ended_at is not None and summary.ended_at >= started else started
    return {
        "provider": summary.provider,
        "session_key": session_key(summary.provider, summary.session_id),
        "workspace_id": workspace_id,
        "title": "",
        "model": summary.model[:MODEL_CHARS],
        "git_branch": summary.git_branch[:BRANCH_CHARS],
        "started_at": format_time(started),
        "ended_at": format_time(ended),
        "turns": _count(summary.turns),
        "input_tokens": _count(summary.input_tokens),
        "output_tokens": _count(summary.output_tokens),
        "cache_read_tokens": _count(summary.cache_read_tokens),
        "cache_write_tokens": _count(summary.cache_write_tokens),
        "files_touched": _count(summary.files_touched),
    }


def fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def scope_of(workspaces: Sequence[Tuple[str, str]]) -> str:
    """What a "not sent" verdict depends on: registering or moving a workspace re-reads every file once."""
    return hashlib.sha256(json.dumps(sorted(workspaces)).encode("utf-8")).hexdigest()[:16]


class Importer:
    """One scan-and-upload round at a time; the caller supplies the database (on its own thread), the upload call and
    the consent switch, which is checked again before every file and every batch."""

    def __init__(self, home: Path, claude_dir: Optional[Path] = None, codex_dir: Optional[Path] = None,
                 max_files: int = MAX_FILES_PER_CYCLE, batch_size: int = BATCH_SIZE,
                 max_file_bytes: int = MAX_FILE_BYTES, log: Callable[[str], None] = None):
        self.home = Path(home)
        self.claude_dir, self.codex_dir = claude_dir, codex_dir
        self.max_files = max(1, int(max_files))
        self.batch_size = max(1, min(BATCH_SIZE, int(batch_size)))
        self.max_file_bytes = int(max_file_bytes)
        self.log = log or (lambda message: None)

    def files(self) -> Iterator[Tuple[str, str, os.stat_result]]:
        # Resolved at every round: $CLAUDE_CONFIG_DIR / $CODEX_HOME as this process sees them.
        for path, info in claude_files(Path(self.claude_dir) if self.claude_dir else claude_root()):
            yield CLAUDE, path, info
        for path, info in codex_files(Path(self.codex_dir) if self.codex_dir else codex_root()):
            yield CODEX, path, info

    def run(self, db: Any, upload: Callable[[List[Dict[str, Any]]], Any], enabled: Callable[[], bool] = lambda: True,
            busy_since: Callable[[], Optional[float]] = lambda: None,
            pending_own: Callable[[], set] = set) -> Dict[str, Any]:
        """Scan changed files (newest first, at most max_files) and upload what changed, in batches of at most
        batch_size. A file's state is written only once its record was accepted (or it has nothing to send), so an
        upload that fails is simply tried again next round. Returns counts only.

        `pending_own`: TimeTrace run session ids the Runner holds but has not written to the database yet."""

        def own_ids() -> set:
            # In memory FIRST, the database SECOND: an id leaves the pending set only after it was written, so one
            # that moves in between is seen by the second read.
            pending = set(pending_own())
            return pending | db.own_provider_session_ids()

        stats = {"read": 0, "uploaded": 0, "unchanged": 0, "not_sent": 0, "deferred": 0, "too_large": 0,
                 "rejected": 0, "off": False, "failed": ""}
        if not enabled():
            stats["off"] = True
            return stats
        workspaces = []
        for workspace in db.list_workspaces():
            root = real_path(workspace.get("path"))
            if root:
                workspaces.append((str(workspace["id"]), root))
        scope = scope_of(workspaces)
        home = real_path(str(self.home.expanduser().absolute()))
        known = db.local_session_files()
        seen, todo = set(), []
        for provider, path, info in self.files():
            seen.add(path)
            if info.st_size > self.max_file_bytes:
                stats["too_large"] += 1
                continue
            row = known.get(path)
            if row and row["mtime_ns"] == info.st_mtime_ns and row["size"] == info.st_size and row["scope"] == scope:
                stats["unchanged"] += 1
                continue
            todo.append((info.st_mtime_ns, path, provider, info))
        gone = [path for path in known if path not in seen]
        if gone:
            db.forget_local_session_files(gone)
        todo.sort(reverse=True)
        settled, pending = [], []
        for _, path, provider, info in todo[:self.max_files]:
            if not enabled():
                stats["off"] = True
                return stats
            busy = busy_since()
            if busy is not None and info.st_mtime >= busy - 1:
                # Written while a TimeTrace run was going: it may be that run's own session, whose id the Runner
                # learns when the run ends. Looked at again once nothing runs.
                stats["deferred"] += 1
                continue
            row = {"path": path, "provider": provider, "mtime_ns": info.st_mtime_ns, "size": info.st_size,
                   "scope": scope, "session_key": "", "fingerprint": ""}
            try:
                summary = PARSERS[provider](path)
            except OSError:
                continue  # vanished or unreadable now: nothing recorded, tried again next round
            except Exception as exc:
                # Not retried until the file changes: a format this version cannot read must not cost every round.
                self.log("local sessions: a %s file could not be read: %s" % (provider, exc.__class__.__name__))
                summary = None
            stats["read"] += 1
            # Decided on the run state as it is NOW: a TimeTrace run may have started, or ended and recorded its
            # provider session id, while this round was reading (a folder-workspace run's session lies inside the
            # workspace, so only its id tells it apart).
            # Order matters: the run state FIRST, the recorded ids SECOND. A run records its provider session id before
            # it stops counting as running, so whatever ends in between is caught by one read or the other.
            busy = busy_since()
            if busy is not None and info.st_mtime >= busy - 1:
                stats["deferred"] += 1
                continue
            own = own_ids()
            payload = self._payload(summary, workspaces, own, home)
            if payload is None:
                stats["not_sent"] += 1
                settled.append(row)
                continue
            row["session_key"], row["fingerprint"] = payload["session_key"], fingerprint(payload)
            previous = known.get(path)
            if (previous and previous["session_key"] == row["session_key"]
                    and previous["fingerprint"] in (row["fingerprint"], REJECTED + row["fingerprint"])):
                row["fingerprint"] = previous["fingerprint"]
                stats["unchanged"] += 1
                settled.append(row)
                continue
            pending.append((row, payload, summary.session_id, info.st_mtime))
        db.save_local_session_files(settled)
        pending = self._one_per_session(pending, db)

        def check(batch):
            return self._still_sendable(batch, db, stats, enabled, busy_since, own_ids)

        for start in range(0, len(pending), self.batch_size):
            try:
                self._send(pending[start:start + self.batch_size], upload, db, stats, check)
            except ImportOff:
                stats["off"] = True
                break
            except Exception as exc:
                stats["failed"] = exc.__class__.__name__
                break
        return stats

    @staticmethod
    def _payload(summary: Optional[Summary], workspaces, own, home) -> Optional[Dict[str, Any]]:
        if summary is None or summary.started_at is None:
            return None
        if summary.session_id in own:
            return None  # TimeTrace ran it: already counted as a run
        cwd = real_path(summary.cwd)
        if not cwd or inside(cwd, home):
            return None  # TimeTrace's own worktrees, review checkouts and import folders
        workspace_id = workspace_for(cwd, workspaces)
        if workspace_id is None:
            return None
        if summary.turns == 0 and summary.tokens() == 0:
            return None  # opened and closed without a question or a reply
        return record(summary, workspace_id)

    @staticmethod
    def _one_per_session(pending, db) -> list:
        """Two files of one session in one round (a copy): send the later one; the other is settled unsent."""
        chosen: Dict[str, tuple] = {}
        dropped = []
        for item in pending:
            key = item[1]["session_key"]
            other = chosen.get(key)
            if other is None:
                chosen[key] = item
                continue
            keep, drop = (item, other) if item[1]["ended_at"] > other[1]["ended_at"] else (other, item)
            chosen[key] = keep
            dropped.append(dict(drop[0], fingerprint=""))
        if dropped:
            db.save_local_session_files(dropped)
        return list(chosen.values())

    @staticmethod
    def _still_sendable(batch, db, stats, enabled, busy_since, own_ids) -> list:
        """Checked right before EVERY request (each batch and each half of a split one): the switch is still on (else
        ImportOff ends the round at once), and no record has meanwhile turned out to be a TimeTrace run (its provider
        session id was recorded: settled unsent) or been written while a run is going (left for a later round)."""
        if not enabled():
            raise ImportOff()
        # The run state FIRST, the recorded ids SECOND (see run()): a run that ends between the two reads recorded its
        # id before it stopped running, so it is either still running in the first read or recorded in the second.
        busy = busy_since()
        own = own_ids()
        sendable, settled = [], []
        for item in batch:
            row, _, session_id, mtime = item
            if session_id in own:
                stats["not_sent"] += 1
                settled.append(dict(row, session_key="", fingerprint=""))
            elif busy is not None and mtime >= busy - 1:
                stats["deferred"] += 1
            else:
                sendable.append(item)
        db.save_local_session_files(settled)
        return sendable

    def _send(self, batch, upload, db, stats, check) -> None:
        """Upload one batch; a 422 (a record Valley finds out of bounds) is split in halves until the refused record
        is found, which is settled as REJECTED (sent again only once its file changes). Any other error propagates.
        `check` filters the batch right before each request and raises ImportOff once the switch is off."""
        from timetrace.cloud import CloudError
        batch = check(batch)
        if not batch:
            return
        try:
            upload([item[1] for item in batch])
        except CloudError as exc:
            if exc.status != 422:
                raise
            if len(batch) == 1:
                stats["rejected"] += 1
                db.save_local_session_files([dict(batch[0][0], fingerprint=REJECTED + batch[0][0]["fingerprint"])])
                return
            half = len(batch) // 2
            self._send(batch[:half], upload, db, stats, check)
            self._send(batch[half:], upload, db, stats, check)
            return
        db.save_local_session_files([item[0] for item in batch])
        stats["uploaded"] += len(batch)
