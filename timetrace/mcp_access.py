"""TimeTrace MCP access for a dispatched run.

When Valley hands a computer an execution job it may add `mcp` to the claim: where the TimeTrace MCP server is,
and a token that works for this one execution attempt only. Claude Code and Codex use it to look things up in
TimeTrace and to report progress while they work.

The token is a credential, so it has one road only: from the claim into the environment of the AI tool's own process
(`TIMETRACE_MCP_TOKEN`). It is never written to an argument list, a log, an uploaded text or an exception, and it
does not leave a `McpAccess` except through `env()`.
"""
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

ENV_NAME = "TIMETRACE_MCP_TOKEN"
TOKEN_PREFIX = "ttm_"
MAX_TOKEN_CHARS = 512
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# The shape of the token Valley issues (ttm_ and base64url), and the shape redact.py masks: a value that is not this is not
# ours and is not put in an environment.
_TOKEN_SHAPE = re.compile(r"ttm_[A-Za-z0-9_-]{20,}")
MIN_SECONDS_LEFT = 30.0  # an access that runs out sooner than this is not started with


@dataclass(frozen=True)
class McpAccess:
    url: str
    token: str = field(repr=False)  # not in repr(): the token is in no text of ours
    expires_at: float = 0.0         # epoch seconds

    def env(self) -> Dict[str, str]:
        """What the AI tool's process is given: the one place the token goes."""
        return {ENV_NAME: self.token}

    def __str__(self) -> str:
        return "McpAccess(url=%r, token=[redacted])" % self.url

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __reduce__(self):
        raise TypeError("McpAccess holds a credential and cannot be pickled")

    # A copy is the same access (a resumed run is given a new one by the claim, but the same one may be handed on).
    def __copy__(self) -> "McpAccess":
        return McpAccess(self.url, self.token, self.expires_at)

    def __deepcopy__(self, memo) -> "McpAccess":
        return McpAccess(self.url, self.token, self.expires_at)

    @staticmethod
    def from_claim(claim: Dict[str, Any], now: Optional[float] = None,
                   cloud_base_url: Optional[str] = None) -> Optional["McpAccess"]:
        return parse_claim(claim, now=now, cloud_base_url=cloud_base_url)[0]


def _origin(parts: urllib.parse.SplitResult) -> Tuple[str, str, int]:
    port = parts.port  # may raise ValueError for a port that is not one
    if port == 0:
        raise ValueError("port 0")
    return parts.scheme, (parts.hostname or "").lower(), port or (443 if parts.scheme == "https" else 80)


_FRACTION = re.compile(r"\.(\d+)")


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    # Python 3.9 reads a fraction of 3 or 6 digits only; Go writes as many as are not zero (RFC 3339 with nanoseconds).
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value.replace("Z", "+00:00").replace("z", "+00:00"), count=1)
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def parse_claim(claim: Dict[str, Any], now: Optional[float] = None,
                cloud_base_url: Optional[str] = None) -> Tuple[Optional[McpAccess], str]:
    """The MCP access a claim carries, and why not when there is none (the reason names the rule, never a value:
    no token and no part of the URL)."""
    raw = claim.get("mcp") if isinstance(claim, dict) else None
    if raw is None:
        return None, "no mcp in the claim"
    if not isinstance(raw, dict):
        return None, "mcp is not an object"
    url, token, expires = raw.get("url"), raw.get("token"), raw.get("expires_at")
    if not isinstance(url, str) or not isinstance(token, str) or not url or not token:
        return None, "mcp url or token is missing or not text"
    if len(token) > MAX_TOKEN_CHARS or not _TOKEN_SHAPE.fullmatch(token):
        return None, "mcp token is not a TimeTrace token"
    if not url.isascii() or any(ch <= " " or ch == "\x7f" for ch in url):
        return None, "mcp url has a space, a control or a non-ASCII character"
    try:
        mcp = urllib.parse.urlsplit(url)
        cloud = urllib.parse.urlsplit(cloud_base_url or "")
        mcp_origin = _origin(mcp)
        cloud_origin = _origin(cloud) if cloud.hostname else None
    except ValueError:
        return None, "mcp url is malformed"
    if mcp.username is not None or mcp.password is not None:
        return None, "mcp url carries a user name or password"
    if mcp.query or mcp.fragment:
        return None, "mcp url carries a query or fragment"
    if not mcp.path.endswith("/mcp"):
        return None, "mcp url does not end in /mcp"
    if cloud_origin is None:
        return None, "the configured cloud address is unknown, so the mcp url cannot be checked against it"
    # Plain http is for a local Valley only, and only when the person configured the Agent to talk to one: the
    # configured address decides, never the one the claim names.
    local_cloud = cloud.scheme == "http" and (cloud.hostname or "").lower() in LOCAL_HOSTS
    if mcp.scheme != "https" and not (mcp.scheme == "http" and local_cloud):
        return None, "mcp url is not https"
    if mcp_origin != cloud_origin:
        return None, "mcp url is not on the same origin as the configured cloud address"
    at = _epoch(expires)
    current = time.time() if now is None else now
    if at is None:
        return None, "mcp expires_at is not a time"
    if at <= current:
        return None, "mcp access has expired"
    return McpAccess(url=url, token=token, expires_at=at), ""


def still_usable(access: McpAccess, now: Optional[float] = None) -> bool:
    """Whether this access has time left to be handed to a tool (a follow-up turn may come long after the claim)."""
    return access.expires_at - (time.time() if now is None else now) > MIN_SECONDS_LEFT


def skip_note(reason: str) -> str:
    """The one line the run's log gets when a claim's MCP access is not used."""
    return "mcp: skipped (%s)" % reason


__all__ = ["ENV_NAME", "McpAccess", "parse_claim", "skip_note", "still_usable"]
