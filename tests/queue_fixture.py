"""Shared fixture for the local-queue dependency tests: a real SQLite database in a temporary TIMETRACE_HOME,
the real CLI (`timetrace.cli.main`) and a scripted executor that does nothing but answer.

Nothing here talks to a model, a network or a real queue: the executor is a fixed script, the worktree factory
only makes a directory, and the home is a fresh temporary one."""
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, scheduler
from timetrace.db import Database
from timetrace.models import RunResult

CFG = {"interval_sec": 1, "jitter_sec": 0, "default_block_sleep_sec": 600, "circuit_breaker_failures": 3,
       "circuit_window_mins": 300, "hook_max_tasks": 5}


class Call:
    def __init__(self, kind, label, cwd, session_id):
        self.kind, self.label, self.cwd, self.session_id = kind, label, cwd, session_id

    def __repr__(self):
        return "%s(%s)" % (self.kind, self.label)


class FixedExecutor:
    """The 'tool': answers each task (found by its prompt, which is its label) from a script and records the
    call. Unscripted answers are success. It never touches the repository."""
    name = "claude"

    def __init__(self, script=None):
        self.script = {label: list(answers) for label, answers in (script or {}).items()}
        self.calls = []

    def read_limits(self):
        return None

    def capabilities(self):
        return {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": True}

    def start(self, prompt, cwd, session_id, log_file):
        return self._answer("start", prompt, cwd, session_id)

    def resume(self, prompt, cwd, session_id, log_file):
        return self._answer("resume", prompt, cwd, session_id)

    def _answer(self, kind, prompt, cwd, session_id):
        label = prompt.rsplit("\n", 1)[-1]  # a resume wraps the original prompt after "Original task:"
        self.calls.append(Call(kind, label, cwd, session_id))
        queue = self.script.get(label)
        res = queue.pop(0) if queue else RunResult(ok=True)
        res.session_id = res.session_id or session_id
        return res

    def labels(self):
        return [c.label for c in self.calls]


class Worktrees:
    """`ensure_worktree` stand-in: a directory per task, and a record of which branch each one was based on."""

    def __init__(self):
        self.bases = []

    def __call__(self, repo, task_id, home, base="HEAD"):
        self.bases.append((task_id, base))
        path = Path(home) / "worktrees" / str(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path), "timetrace/%d" % task_id


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.repo = root / "repo"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "a").write_text("a")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        user_home = root / "user"
        user_home.mkdir()
        patches = [patch.dict(os.environ, {"TIMETRACE_HOME": str(self.home)}),
                   patch("timetrace.cli.CredentialStore.load", lambda self, account="default": None),
                   patch("timetrace.cli._discover_tools", lambda home, user_home=None: (cli.config.load(home), {})),
                   patch("timetrace.cli.Path.home", lambda: user_home)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.db = Database(self.home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.worktrees = Worktrees()
        self.clock = 1000

    # ---- the real CLI -----------------------------------------------------------------------
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def add(self, label, *flags):
        code, out, err = self.cli("add", "--repo", str(self.repo), "--tool", "claude", *(list(flags) + [label]))
        self.assertEqual(code, 0, err)
        return int(re.search(r"task (\d+) added", out).group(1))

    def ls_json(self):
        code, out, err = self.cli("ls", "--all", "--json")
        self.assertEqual(code, 0, err)
        return {t["id"]: t for t in json.loads(out)}

    # ---- the scheduler ----------------------------------------------------------------------
    def tick(self, executor, adapters=None):
        """One scheduler iteration, one second after the previous one."""
        self.clock += 1
        now = self.clock
        return scheduler.run_once(
            self.db, adapters or {"claude": executor}, CFG, self.home, now=now, log=lambda s: None,
            ensure_worktree=self.worktrees, hook_runner=lambda *a: None, rng=lambda: 0.0, clock=lambda: now)

    def states(self, *ids):
        return [self.db.get_task(i)["state"] for i in ids]

    def events(self, type_):
        return self.db.list_events(limit=100, type_=type_)
