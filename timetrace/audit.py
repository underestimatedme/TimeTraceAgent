"""~/.timetrace/audit.log: an append-only record of remote-control decisions.

One JSON object per line: {"at": RFC3339 UTC, "event": …, …fields}. Events:
pause / resume (local), interrupt, append, approval (every decision that was
not a local auto-allow), version_rejected. The file is created 0600 and only
ever opened with O_APPEND; it stays on this computer. Nothing here raises."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILE_NAME = "audit.log"
FIELD_CHARS = 500
_lock = threading.Lock()


def path(home: Path) -> Path:
    return Path(home) / FILE_NAME


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value[:FIELD_CHARS]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:FIELD_CHARS]


def record(home: Path, event: str, **fields: Any) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
             "event": str(event)}
    entry.update({str(k): _bounded(v) for k, v in fields.items()})
    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    target = path(home)
    with _lock:
        try:
            Path(home).mkdir(parents=True, exist_ok=True)
            fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError:
            pass


def read(home: Path, limit: int = 50) -> list:
    """The last `limit` entries (for `agent doctor`)."""
    try:
        lines = path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for raw in lines[-limit:]:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out
