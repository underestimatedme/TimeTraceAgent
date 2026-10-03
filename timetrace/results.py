"""Structured task results: `.timetrace/out/result.json` in the execution worktree.

The model writes the file, so everything about it is untrusted: it may be a
symlink to a secret, a FIFO, huge, malformed, or carry secrets. Only a bounded
regular file is read, only three keys survive (`artifacts`, `pipeline_draft`,
`subtasks`), and every string is redacted
before it can be queued for Valley.
"""
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from timetrace.redact import redact

OUT_DIR = (".timetrace", "out")
RESULT_NAME = "result.json"
RESULT_BYTES = 64 * 1024       # result.json itself
CONTENT_BYTES = 64 * 1024      # one doc snapshot
ARTIFACTS_BYTES = 64 * 1024    # the whole `artifacts` list as Valley stores it
REF_CHARS = 512
ARTIFACT_KINDS = ("doc", "commit", "link", "note", "folder")
INVALID_NOTE = "结构化结果无效"
DROPPED_NOTE = "忽略 %d 项格式无效的产出物"
DROPPED_DRAFT_NOTE = "忽略格式无效的 pipeline_draft"
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
SUBTASKS_MAX = 30
# Keys end up in branch names (`timetrace/<stage>/<key>`), which are lowercase:
# keys are lowercased first, then must match this and form a valid ref part.
# Same pattern and 40-character bound as Valley's subtaskKeyPattern.
_SUBTASK_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")


def _subtask_key(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.lower()
    if not _SUBTASK_KEY.fullmatch(key) or ".." in key or key.endswith(".") or key.endswith(".lock"):
        return None
    return key
TITLE_CHARS = 200
BRIEF_CHARS = 8000
TOOL_CHARS = 32
ESTIMATE_MAX = 7 * 24 * 60


class Invalid(ValueError):
    pass


def truncate_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:max(0, limit)].decode("utf-8", errors="ignore")


PREVIOUS_PREFIX = "out.prev-"


def prepare_out_dir(root: str, fresh: bool = False) -> bool:
    """Create `.timetrace/out/` for the run; never through a symlink the model made.

    `fresh` (a new run, not a resume): whatever an earlier run left in
    `.timetrace/out` — a result.json above all — is renamed to
    `.timetrace/out.prev-<time>` first, so a reused output directory or worktree
    can never report the previous run's result as this one's. A symlink there
    is renamed itself, never followed."""
    base = Path(root) / OUT_DIR[0]
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        return False
    base.mkdir(exist_ok=True)
    out = base / OUT_DIR[1]
    if fresh and os.path.lexists(str(out)):
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-%06d" % (time.time_ns() // 1000 % 1_000_000)
        os.rename(str(out), str(base / (PREVIOUS_PREFIX + stamp)))
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        return False
    out.mkdir(exist_ok=True)
    return True


def _read_bounded(path: Path, limit: int) -> Optional[bytes]:
    """Bytes of a regular, non-symlinked file, or None when absent. Raises
    Invalid when it is anything else or longer than `limit`."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise Invalid("unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Invalid("not a regular file")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise Invalid("too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def redact_all(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_all(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_all(v) for k, v in value.items()}
    return value


def _artifact(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        # A bare path is how models most often name a file they wrote.
        raw = {"kind": "doc", "ref": raw}
    if not isinstance(raw, dict):
        raise Invalid("artifact is not an object")
    kind, ref = raw.get("kind"), raw.get("ref")
    if kind not in ARTIFACT_KINDS:
        raise Invalid("unknown artifact kind")
    if not isinstance(ref, str) or not ref.strip() or len(ref) > REF_CHARS:
        raise Invalid("bad artifact ref")
    out = {"kind": kind, "ref": ref}
    if "content" in raw and raw["content"] is not None:
        if not isinstance(raw["content"], str):
            raise Invalid("artifact content is not text")
        out["content"] = raw["content"]
    if "commit_sha" in raw and raw["commit_sha"] is not None:
        if not isinstance(raw["commit_sha"], str) or not _SHA.fullmatch(raw["commit_sha"]):
            raise Invalid("bad commit sha")
        out["commit_sha"] = raw["commit_sha"]
    return out


def _text(raw: Dict[str, Any], key: str, limit: int, required: bool) -> Optional[str]:
    value = raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise Invalid("bad subtask %s" % key)
    return value


def _subtasks(raw: Any) -> List[Dict[str, Any]]:
    """`breakdown` output: at most 30 {key,title,brief,depends_on,tool,
    estimate_minutes}. Types and bounds only; Valley checks the graph."""
    if not isinstance(raw, list) or len(raw) > SUBTASKS_MAX:
        raise Invalid("subtasks is not a list of at most %d" % SUBTASKS_MAX)
    out, keys = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise Invalid("subtask is not an object")
        key = _subtask_key(item.get("key"))
        if key is None or key in keys:
            raise Invalid("bad or duplicate subtask key")
        keys.add(key)
        depends = item.get("depends_on")
        if depends is None:
            depends = []
        if not isinstance(depends, list) or len(depends) > SUBTASKS_MAX:
            raise Invalid("bad subtask depends_on")
        depends = [_subtask_key(d) for d in depends]
        if any(d is None for d in depends):
            raise Invalid("bad subtask depends_on")
        estimate = item.get("estimate_minutes")
        if estimate is not None and (type(estimate) is not int or not 0 <= estimate <= ESTIMATE_MAX):
            raise Invalid("bad subtask estimate")
        out.append({
            "key": key,
            "title": _text(item, "title", TITLE_CHARS, True),
            "brief": _text(item, "brief", BRIEF_CHARS, False) or "",
            "depends_on": list(depends),
            "tool": _text(item, "tool", TOOL_CHARS, False),
            "estimate_minutes": estimate,
        })
    return out


def _doc_file(root: Path, ref: str) -> Optional[Path]:
    """The regular file `ref` names inside the worktree (symlinks resolved and
    required to stay inside), never git metadata."""
    try:
        base = root.resolve()
        target = (base / ref).resolve()
        rel = target.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    if not rel.parts or ".git" in rel.parts or not target.is_file():
        return None
    return target


def _attach_docs(root: Path, artifacts: List[Dict[str, Any]], head: Callable[[], Optional[str]]) -> None:
    sha = None
    for art in artifacts:
        if art["kind"] != "doc":
            continue
        target = _doc_file(root, art["ref"])
        if target is None:
            continue
        try:
            fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                data = os.read(fd, CONTENT_BYTES)
            finally:
                os.close(fd)
        except OSError:
            continue
        art["content"] = data.decode("utf-8", errors="ignore")
        if sha is None:
            try:
                sha = head() or ""
            except Exception:
                sha = ""
        if sha:
            art["commit_sha"] = sha
        else:
            art.pop("commit_sha", None)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _fit(artifacts: List[Dict[str, Any]]) -> None:
    """Bound each content, then the whole list, trimming later contents first."""
    for art in artifacts:
        if "content" in art:
            art["content"] = truncate_utf8(art["content"], CONTENT_BYTES)
    bare = [{k: v for k, v in a.items() if k != "content"} for a in artifacts]
    if _json_bytes(bare) > ARTIFACTS_BYTES:
        raise Invalid("artifacts too large")
    room = ARTIFACTS_BYTES - _json_bytes(bare)
    for art in artifacts:
        if "content" not in art:
            continue
        # `"content": ` plus the comma; escaping can make JSON longer than UTF-8.
        overhead = len(', "content": '.encode("utf-8"))
        allowed = room - overhead
        text = truncate_utf8(art["content"], max(0, allowed))
        while text and _json_bytes(text) > allowed:
            text = truncate_utf8(text, len(text.encode("utf-8")) - max(64, _json_bytes(text) - allowed))
        if allowed <= 2 or (not text and art["content"]):
            del art["content"]
            continue
        art["content"] = text
        room -= overhead + _json_bytes(text)


def with_folder(folder_artifact: Dict[str, Any], artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The runner's own `folder` artifact first, then the ones result.json
    declared, the whole list redacted and bounded like any other."""
    merged = redact_all([dict(folder_artifact)] + [dict(a) for a in artifacts or []])
    try:
        _fit(merged)
    except Invalid:
        merged = redact_all([dict(folder_artifact)])
        _fit(merged)
    return merged


def _artifacts(raw: Any):
    """Declared artifacts are side information: each unusable entry (or a
    non-list) is dropped on its own and counted, never voiding the result."""
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    kept = []
    for item in raw:
        try:
            kept.append(_artifact(item))
        except Invalid:
            pass
    return kept, len(raw) - len(kept)


def collect(root: str, head: Callable[[], Optional[str]] = lambda: None) -> Optional[Dict[str, Any]]:
    """None when the run wrote no result.json; otherwise
    {"valid": bool, "artifacts": [...], "result": {"pipeline_draft"?: {...}, "subtasks"?: [...]} | None,
     "dropped_artifacts": int, "dropped_draft": bool (valid only)}.
    An invalid file yields valid=False with no artifacts and no result; bad
    artifact entries alone only raise dropped_artifacts."""
    base = Path(root)
    try:
        for i in range(1, len(OUT_DIR) + 1):
            part = base.joinpath(*OUT_DIR[:i])
            if part.is_symlink():
                raise Invalid("output directory is a symlink")
            if not part.exists():
                return None
        data = _read_bounded(base.joinpath(*OUT_DIR, RESULT_NAME), RESULT_BYTES)
        if data is None:
            return None
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Invalid("not JSON")
        if not isinstance(parsed, dict):
            raise Invalid("not an object")
        artifacts, dropped = _artifacts(parsed.get("artifacts"))
        draft = parsed.get("pipeline_draft")
        dropped_draft = draft is not None and not isinstance(draft, dict)
        if dropped_draft:
            # Only a generate step needs a draft, and Valley asks for a retry
            # when it is missing; a stray non-object never voids sub-tasks.
            draft = None
        subtasks = _subtasks(parsed["subtasks"]) if parsed.get("subtasks") is not None else None
        _attach_docs(base, artifacts, head)
        artifacts = redact_all(artifacts)
        _fit(artifacts)
        result = {}
        if draft is not None:
            result["pipeline_draft"] = redact_all(draft)
        if subtasks is not None:
            result["subtasks"] = redact_all(subtasks)
        result = result or None
        return {"valid": True, "artifacts": artifacts, "result": result, "dropped_artifacts": dropped,
                "dropped_draft": dropped_draft}
    except (Invalid, RecursionError):
        return {"valid": False, "artifacts": [], "result": None}


# ---- review_turn verdicts ------------------------------------------------------
VERDICTS = ("pass", "fail")
REASONS_MAX = 20
REASON_CHARS = 500
VERDICT_SCAN_BYTES = 64 * 1024
INVALID_VERDICT = "invalid"
INVALID_VERDICT_NOTE = "复核结论无效"
_FENCE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```", re.S | re.I)


def _verdict(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("verdict"), str):
        return None
    verdict = value["verdict"].strip().lower()
    reasons = value.get("reasons", [])
    if verdict not in VERDICTS or not isinstance(reasons, list):
        return None
    if not all(isinstance(r, str) for r in reasons):
        return None
    kept = [redact(r.strip())[:REASON_CHARS] for r in reasons if r.strip()][:REASONS_MAX]
    return {"verdict": verdict, "reasons": kept}


def parse_verdict(text: Optional[str]) -> Dict[str, Any]:
    """The reviewer's `{"verdict": "pass|fail", "reasons": [...]}` from its
    final message: the last fenced ```json block holding one, else the last
    `{...}` object in the text. Reasons: strings only, at most 20 kept, each
    cut to 500 characters and redacted. Anything else is
    `{"verdict": "invalid", "reasons": []}` — never a pass."""
    text = (text or "")[-VERDICT_SCAN_BYTES:]
    decoder = json.JSONDecoder()
    for block in reversed(_FENCE.findall(text)):
        try:
            found = _verdict(json.loads(block.strip()))
        except ValueError:
            continue
        if found:
            return found
    index = text.rfind("{")
    while index >= 0:
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            value = None
        found = _verdict(value)
        if found:
            return found
        index = text.rfind("{", 0, index)
    return {"verdict": INVALID_VERDICT, "reasons": []}


# ---- import_parse results -----------------------------------------------------
IMPORT_RESULT_BYTES = 64 * 1024   # the object as sent (compact JSON, UTF-8)
IMPORT_SCAN_CHARS = 256 * 1024    # tail of the reply that is searched
IMPORT_DECODE_TRIES = 4096        # `{` positions tried when no fence holds one
INVALID_IMPORT_NOTE = "解析结果无效"


def redact_deep(value: Any) -> Any:
    """Every string, keys included, masked; other scalars unchanged."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, dict):
        return {redact(str(k)): redact_deep(v) for k, v in value.items()}
    return value


def _decode(text: str, index: int, decoder: json.JSONDecoder):
    """(value, end) of the JSON value starting at `index`, or (None, index)."""
    try:
        return decoder.raw_decode(text, index)
    except (ValueError, RecursionError):
        return None, index


def extract_import(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The JSON object an import_parse run ends its reply with: the last
    fenced ```json block holding an object, else the last top-level `{...}`
    object in the text (a `{` inside an earlier object's strings or nesting
    is never a candidate). Every string is redacted; None when there is no
    object or it is larger than 64 KB as sent. The value is untrusted: Valley
    validates its shape."""
    text = (text or "")[-IMPORT_SCAN_CHARS:]
    decoder = json.JSONDecoder()
    found = None
    for block in reversed(_FENCE.findall(text)):
        block = block.strip()
        value, end = _decode(block, 0, decoder) if block.startswith("{") else (None, 0)
        if isinstance(value, dict) and not block[end:].strip():
            found = value
            break
    if found is None:
        index, tries = text.find("{"), 0
        while index >= 0 and tries < IMPORT_DECODE_TRIES:
            tries += 1
            value, end = _decode(text, index, decoder)
            if isinstance(value, dict):
                found, index = value, end
            else:
                index += 1
            index = text.find("{", index)
    if found is None:
        return None
    found = redact_deep(found)
    size = len(json.dumps(found, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return found if size <= IMPORT_RESULT_BYTES else None
