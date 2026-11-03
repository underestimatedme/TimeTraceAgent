"""`timetrace agent sync`: ask the running Runner to re-check and report now.

The Runner re-verifies logins and re-pushes the inventory every few minutes;
right after `claude` / `codex` login that leaves the phone showing the old
state. The CLI drops a request in ~/.timetrace/sync_request.json (0600); the
Runner sees it on its next loop pass, drops its cached login verdicts, pushes
the inventory and self-check even when unchanged, and acknowledges with
`sync_id` / `sync_pushed` in runner_state.json. No restart, running jobs
continue."""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from timetrace import runner_state

FILE_NAME = "sync_request.json"


def request(home: Path) -> str:
    request_id = uuid.uuid4().hex
    target = Path(home) / FILE_NAME
    Path(home).mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + FILE_NAME + ".%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"id": request_id, "requested_at": int(time.time())}, fh)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(target))
    return request_id


def pending(home: Path) -> Optional[str]:
    """The id of a request the Runner has not acknowledged yet."""
    try:
        doc = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    request_id = doc.get("id") if isinstance(doc, dict) else None
    if not isinstance(request_id, str) or not request_id:
        return None
    return None if runner_state.load(home).get("sync_id") == request_id else request_id


def wait(home: Path, request_id: str, timeout: float, poll: float = 0.5) -> Optional[Dict[str, Any]]:
    """runner_state once the Runner acknowledged `request_id`, else None."""
    deadline = time.monotonic() + timeout
    while True:
        state = runner_state.load(home)
        if state.get("sync_id") == request_id:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
