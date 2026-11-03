"""Mask secrets before any text leaves this computer.

Applied to every event message, result summary and output tail the runner
queues for Valley. The rules are deliberately shape-based (no entropy
guessing), so ordinary git output — commit hashes, diffs, file names such as
token_store.py, `max_tokens = 1024` — passes through unchanged.

Best effort: a secret in an unknown format can still get through. Setting
`upload_output_tail` to false keeps run logs on this computer entirely.
"""
import contextlib
import os
import re
import tempfile
import threading
from typing import Any, Iterable, Iterator, Match, Optional

MASK = "[REDACTED]"

_B = r"(?<![A-Za-z0-9_])"   # left boundary: not inside a longer token
_E = r"(?![A-Za-z0-9_])"    # right boundary

# (kind, pattern). Order matters: specific shapes before generic ones.
_TOKEN_RULES = [
    ("private-key", re.compile(
        r"-----BEGIN (?P<kind>[A-Z0-9 ]*)PRIVATE KEY-----"
        r"(?:[\s\S]*?-----END (?P=kind)PRIVATE KEY-----|[\s\S]*\Z)")),
    # A tail that starts inside a key block: base64 lines, then END.
    ("private-key", re.compile(
        r"(?m)(?:^[A-Za-z0-9+/=]{16,}\r?\n)+-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("anthropic", re.compile(_B + r"sk-ant-[a-z]{2,6}\d{0,3}-[A-Za-z0-9_-]{16,}")),
    ("openai", re.compile(_B + r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("stripe", re.compile(_B + r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}" + _E)),
    ("github", re.compile(_B + r"github_pat_[A-Za-z0-9_]{22,}")),
    ("github", re.compile(_B + r"gh[pousr]_[A-Za-z0-9]{36,}" + _E)),
    ("slack", re.compile(_B + r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("slack", re.compile(_B + r"xapp-\d-[A-Za-z0-9-]{10,}")),
    ("google", re.compile(_B + r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("aws", re.compile(_B + r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}" + _E)),
    ("aliyun", re.compile(_B + r"LTAI[0-9A-Za-z]{12,30}" + _E)),
    ("npm", re.compile(_B + r"npm_[A-Za-z0-9]{36}" + _E)),
    ("jwt", re.compile(_B + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")),
    # TimeTrace's own MCP tokens: the access token a dispatched run is given (ttm_) and the refresh token (ttr_).
    # No left boundary: what a tool prints is not always separated from a token ("diagnostic" + token, "x=token", a
    # path or a word run into it), and a token that is not masked because of what stands before it is a leak.
    ("timetrace", re.compile(r"tt[mr]_[A-Za-z0-9_-]{20,}")),
]

# "Authorization: Bearer <x>" / "Basic <x>" (Basic only after Authorization,
# because "basic" is an ordinary word).
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}")
_BASIC = re.compile(r"(?i)(\bauthorization\s*[:=]\s*[\"']?basic\s+)[A-Za-z0-9+/=]{8,}")

# scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@\"']+:)[^\s@/\"']+(@)")

# KEY=value, key: value, "key": "value" where the key name ends with a word
# that means secret. The key must END there: tokenizer, max_tokens and
# TokenStore are not secret names.
_SECRET_KEY = (r"[A-Za-z0-9_.-]*?(?:secret|token|password|passwd|api[_-]?key|access[_-]?key"
               r"|private[_-]?key|credentials?)")
_ASSIGNMENT = re.compile(
    r"(?i)(?P<key>(?<![A-Za-z0-9])" + _SECRET_KEY + r")"
    r"(?P<sep>\\?[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<quote>\\?[\"']?)"
    r"(?P<value>[^\s\"'\\,;=(){}\[\]<>$%*`][^\s\"'\\,;(){}\[\]<>`]*)")
_PLACEHOLDERS = {"true", "false", "null", "none", "nil", "undefined", "redacted", "changeme", "example"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _assignment(match: Match) -> str:
    value = match.group("value")
    after = match.string[match.end():match.end() + 1]
    quoted = bool(match.group("quote"))
    keep = (
        len(value) < 6
        or value.isdigit()
        or value.lower().strip(".") in _PLACEHOLDERS
        or set(value) <= set("xX*-._#")
        # Code, not a literal: `token = self._load_token()`, `password: Optional[str]`.
        or (not quoted and (after in ("(", "[", ".") or
                            (_IDENTIFIER.fullmatch(value) and not any(c.isdigit() for c in value))))
    )
    if keep:
        return match.group(0)
    return match.group("key") + match.group("sep") + match.group("quote") + MASK


_TIMETRACE_TOKEN = next(pattern for kind, pattern in _TOKEN_RULES if kind == "timetrace")

# The credentials this process has handed to a running tool (a dispatched run's MCP token), by value: whatever the
# tool prints that holds one of them, in whatever text it is run into, is masked. A token of an earlier attempt is
# not here (it is dead), but it has the shape the rule above masks.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict = {}


@contextlib.contextmanager
def masking(values: Iterable[str]) -> Iterator[None]:
    """Mask these exact values in everything this module masks, for as long as the block runs."""
    held = [v for v in values if isinstance(v, str) and len(v) >= 8]
    with _ACTIVE_LOCK:
        for value in held:
            _ACTIVE[value] = _ACTIVE.get(value, 0) + 1
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            for value in held:
                _ACTIVE[value] -= 1
                if _ACTIVE[value] <= 0:
                    del _ACTIVE[value]


def _mask_active(text: str) -> str:
    with _ACTIVE_LOCK:
        values = sorted(_ACTIVE, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, "[REDACTED:timetrace]")
    return text


def mask_timetrace_tokens(text: str) -> str:
    """`text` with TimeTrace MCP tokens masked and nothing else changed. A dispatched run's token is in the tool's
    environment, so whatever the tool prints (a model running `env`) is masked as it is written to the run's log, before
    anything reads that log."""
    if "tt" in text:
        text = _TIMETRACE_TOKEN.sub("[REDACTED:timetrace]", text)
    return _mask_active(text) if _ACTIVE else text


_TOKEN_BYTES = re.compile(rb"tt[mr]_[A-Za-z0-9_-]{20,}")


def holds_timetrace_token(data: bytes) -> bool:
    """Whether raw bytes hold a TimeTrace token in any of the ways a file may write one: as ASCII/UTF-8, or as UTF-16 of
    either byte order (at either alignment). For files that are not text we can safely rewrite."""
    if _TOKEN_BYTES.search(data):
        return True
    for view in (data, data[1:]):
        for encoding in ("utf-16-le", "utf-16-be"):
            if _TOKEN_BYTES.search(view.decode(encoding, errors="ignore").encode("ascii", errors="ignore")):
                return True
    return False


def mask_structure(value: Any) -> Any:
    """A decoded JSON value with every string in it (and every key) masked: a copy for the log, for the lines kept and for a
    person to read. What is sent back to the process that produced it is the original, never this."""
    if isinstance(value, str):
        return mask_timetrace_tokens(value)
    if isinstance(value, list):
        return [mask_structure(item) for item in value]
    if isinstance(value, dict):
        return {(mask_timetrace_tokens(key) if isinstance(key, str) else key): mask_structure(item) for key, item in value.items()}
    return value


def mask_file(path: Optional[str]) -> bool:
    """Mask TimeTrace tokens in a file a tool wrote (Codex's `-o` final-message file is one: it is written by the tool
    itself, outside the streams that are masked as they are read). Rewritten in place, atomically, keeping its mode;
    a path that is missing, not a regular file or a symbolic link is left alone. True when it was changed."""
    if not path or os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        text = data.decode("utf-8", errors="surrogateescape")
        masked = mask_timetrace_tokens(text)
        if masked == text:
            return False
        mode = os.stat(path).st_mode & 0o777
        fd, tmp = tempfile.mkstemp(prefix=".masked-", dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(masked.encode("utf-8", errors="surrogateescape"))
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        # The file could not be rewritten (a directory that cannot be written, a disk that is full): it is not left with
        # what it holds. It is removed, or, where that is not allowed either, emptied.
        return _drop_file(path)


def _drop_file(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)))  # (never creates a file)
        return True
    except OSError:
        return False


def redact(text: Optional[str]) -> str:
    """`text` with every recognised secret replaced by a [REDACTED…] marker."""
    if not text:
        return ""
    out = _mask_active(str(text)) if _ACTIVE else str(text)
    for kind, pattern in _TOKEN_RULES:
        out = pattern.sub("[REDACTED:%s]" % kind, out)
    out = _BEARER.sub(lambda m: m.group(1) + MASK, out)
    out = _BASIC.sub(lambda m: m.group(1) + MASK, out)
    out = _URL_CREDENTIALS.sub(lambda m: m.group(1) + MASK + m.group(2), out)
    out = _ASSIGNMENT.sub(_assignment, out)
    return out
