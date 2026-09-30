"""Mask secrets before any text leaves this computer.

Applied to every event message, result summary and output tail the runner
queues for Valley. The rules are deliberately shape-based (no entropy
guessing), so ordinary git output — commit hashes, diffs, file names such as
token_store.py, `max_tokens = 1024` — passes through unchanged.

Best effort: a secret in an unknown format can still get through. Setting
`upload_output_tail` to false keeps run logs on this computer entirely.
"""
import re
from typing import Match, Optional

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


def redact(text: Optional[str]) -> str:
    """`text` with every recognised secret replaced by a [REDACTED…] marker."""
    if not text:
        return ""
    out = str(text)
    for kind, pattern in _TOKEN_RULES:
        out = pattern.sub("[REDACTED:%s]" % kind, out)
    out = _BEARER.sub(lambda m: m.group(1) + MASK, out)
    out = _BASIC.sub(lambda m: m.group(1) + MASK, out)
    out = _URL_CREDENTIALS.sub(lambda m: m.group(1) + MASK + m.group(2), out)
    out = _ASSIGNMENT.sub(_assignment, out)
    return out
