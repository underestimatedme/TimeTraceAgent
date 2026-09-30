"""Real-process fixture for shared agent/scheduler crash fencing tests."""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.models import RunResult
from timetrace.scheduler import run_once


home, role, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
db = Database(home / (role + ".db"))
repo = str((home / "repo").resolve())


class Adapter:
    def capabilities(self):
        return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

    def read_limits(self):
        return []

    def start(self, *args):
        if mode == "hold":
            child = subprocess.Popen([sys.executable, "-c", """
import sys, time
from pathlib import Path
path = Path(sys.argv[1])
while True:
    with path.open('a') as out:
        out.write('writing\\n')
    time.sleep(.01)
""", str(home / "child-writes")])
            print("ready %d" % child.pid, flush=True)
            try:
                while not (home / "release").exists():
                    time.sleep(.01)
            finally:
                child.terminate()
                child.wait(timeout=5)
        else:
            print("spawned", flush=True)
        return RunResult(ok=True, exit_code=0)


class Cloud:
    def claim(self, token):
        return {"job": {"id": "job-" + str(os.getpid()), "workspace_id": "ws", "provider": "codex",
                        "tool_profile_id": "profile", "prompt": "work"},
                "attempt_id": "attempt", "lease_epoch": 1, **self.renew(token, "attempt", 1)}

    def renew(self, *args):
        return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def append_events(self, *args):
        pass


if role == "agent":
    db.upsert_workspace("ws", "repo", repo, "main")
    result = Agent(db, Cloud(), {"codex": Adapter()}, home, lambda: "token",
                   prepare_workspace=lambda *args: (repo, "main"), heartbeat_interval=.05).run_once()
else:
    db.add_task("work", repo, tool="codex")
    result = run_once(db, {"codex": Adapter()}, {}, home,
                      ensure_worktree=lambda *args: (repo, "main"), log=lambda *args: None)
print(result, flush=True)
