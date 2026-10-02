"""`timetrace agent pause|resume`: the computer's own "stop taking work" switch.

Kept in its own file (~/.timetrace/local_pause.json, 0600) rather than in
runner_state.json, which the running agent rewrites on every upkeep round
and could overwrite a pause written in between. The agent reads it before
every claim and reports it as inventory `accepting_local`. A local pause
wins over the phone: Valley cannot lift it. Running jobs are not affected."""
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

FILE_NAME = "local_pause.json"


def state(home: Path) -> Dict[str, Any]:
    try:
        doc = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"paused": False}
    if not isinstance(doc, dict):
        return {"paused": False}
    return {"paused": doc.get("paused") is True, "since": doc.get("since")}


def paused(home: Path) -> bool:
    return state(home)["paused"]


def set_paused(home: Path, value: bool) -> Dict[str, Any]:
    doc = {"paused": bool(value), "since": int(time.time())}
    target = Path(home) / FILE_NAME
    Path(home).mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + FILE_NAME + ".%d.tmp" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(target))
    return doc
