"""~/.timetrace/audit.log: an append-only record of remote-control decisions.

One JSON object per line: {"at": RFC3339 UTC, "event": …, …fields}. Events:
pause / resume (local), interrupt, append, approval (every decision that was
not a local auto-allow), version_rejected, and the manual set-aside of a refused
upload batch: discard_requested (written and forced to disk BEFORE anything is
changed), then outbox_discard (what was set aside) or discard_unchanged /
discard_failed, all under one discard_id; and the manual retry of a batch whose
automatic sending had stopped after repeated refusals: retry_requested (forced
to disk BEFORE the change), then outbox_retry or retry_unchanged, under one
retry_id. The file is created 0600 and only ever
opened with O_APPEND; it stays on this computer.

`record` is best effort and never raises. `record_durable` is for a decision that
must not happen without its entry: it raises OSError unless the whole line is on
disk."""
import errno
import fcntl
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


def _line(event: str, fields: dict) -> bytes:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
             "event": str(event)}
    entry.update({str(k): _bounded(v) for k, v in fields.items()})
    return (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _flush(fd: int) -> None:
    """Force the entry to the disk itself (on macOS plain fsync only reaches the drive's cache)."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def _append(home: Path, line: bytes, durable: bool) -> None:
    """Append one complete line, or raise OSError. A write that takes only part of the line is continued; if an
    earlier process died mid-line the broken fragment is closed off first, so entries never merge into it. The
    lock keeps two processes' entries from interleaving."""
    with _lock:
        Path(home).mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path(home)), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            pending = memoryview(line)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError(errno.EIO, "the audit log accepted no more bytes")
                pending = pending[written:]
            if durable:
                _flush(fd)
        finally:
            os.close(fd)


def record(home: Path, event: str, **fields: Any) -> None:
    try:
        _append(home, _line(event, fields), durable=False)
    except OSError:
        pass


def record_durable(home: Path, event: str, **fields: Any) -> None:
    """Store the entry completely and force it to disk, or raise OSError (and say so to the caller)."""
    _append(home, _line(event, fields), durable=True)


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
