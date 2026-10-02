"""~/.timetrace/runner_state.json: when the Runner last reached Valley.

Written by the running agent (inventory push, quota report) so that
`timetrace agent doctor`, a separate process, can say whether the phone is
seeing fresh data. Holds timestamps and counts only."""
import json
import os
from pathlib import Path
from typing import Any, Dict

FILE_NAME = "runner_state.json"


def load(home: Path) -> Dict[str, Any]:
    try:
        doc = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def record(home: Path, **fields: Any) -> None:
    """Merge `fields` into the state file; never raises."""
    try:
        doc = load(home)
        doc.update(fields)
        path = Path(home) / FILE_NAME
        tmp = path.with_name("." + FILE_NAME + ".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass
