"""On-demand Claude Code quota via the OAuth usage endpoint that Claude Code's
own /usage command calls. The access token comes from the local credentials
(file, else the macOS Keychain item Claude Code uses), is sent once, and is
never stored, logged, or returned."""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from timetrace import tiers
from timetrace.cloud import no_redirect_opener, read_bounded
from timetrace.models import CLAUDE, Sample

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = (("five_hour", 300), ("seven_day", 7 * 24 * 60))


_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def _epoch(value: Any) -> Optional[int]:
    """RFC 3339 → epoch seconds, tolerating what Python 3.9's fromisoformat
    rejects: more than six fractional digits and compact offsets like +0000."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _ISO.match(str(value).strip())
    if not match:
        return None
    base, fraction, offset = match.groups()
    text = base
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    offset = offset or "+00:00"
    if offset == "Z":
        offset = "+00:00"
    elif ":" not in offset:
        offset = offset[:3] + ":" + offset[3:]
    try:
        return int(datetime.fromisoformat(text + offset).timestamp())
    except ValueError:
        return None


def _percent(value: Any) -> Optional[float]:
    """Utilization as reported: the endpoint speaks percent (41.0 = 41 %,
    observed live on 2026-09-25). No fraction heuristic — it would turn a
    0.5 % reading into 50 % and an exhausted 1.0 into 1 %."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def read_usage(credentials_path: Path, opener: Optional[Callable] = None,
               now: Optional[float] = None,
               keychain: Callable[[], Optional[str]] = tiers.keychain_secret) -> Optional[List[Sample]]:
    return usage_samples(tiers.claude_oauth(credentials_path, keychain), opener=opener, now=now)


def usage_samples(oauth: Dict[str, Any], opener: Optional[Callable] = None,
                  now: Optional[float] = None) -> Optional[List[Sample]]:
    """Samples for an already-loaded `claudeAiOauth` object (see tiers.claude_oauth).
    `opener` is resolved at call time so tests can patch urllib."""
    # No redirects: urllib would replay a normal Authorization header to
    # wherever a 3xx points.
    opener = opener or no_redirect_opener()
    token = str((oauth or {}).get("accessToken") or "")
    if not token:
        return None
    expires_ms = oauth.get("expiresAt")
    if expires_ms is not None and now is not None:
        try:
            if float(expires_ms) / 1000 <= now:
                return None  # let Claude Code refresh it; never send a stale token
        except (TypeError, ValueError):
            pass
    request = urllib.request.Request(USAGE_URL)
    request.add_unredirected_header("Authorization", "Bearer " + token)
    request.add_header("anthropic-beta", "oauth-2025-04-20")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "timetrace-runner")
    try:
        with opener(request, timeout=15) as response:
            doc = json.loads(read_bounded(response, 256 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None
    samples: List[Sample] = []
    for key, mins in WINDOWS:
        window = doc.get(key) if isinstance(doc, dict) else None
        if not isinstance(window, dict):
            continue
        used = _percent(window.get("utilization"))
        if used is None:
            continue
        samples.append(Sample(bucket_key="claude:" + key, tool=CLAUDE, used_pct=used,
                              reset_at=_epoch(window.get("resets_at")), window_mins=mins,
                              is_representative=(key == "five_hour"), source="live"))
    return samples or None
