"""All SQL lives here. Other modules call these methods and never write SQL."""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from timetrace.models import BLOCKED, DONE, FAILED, PENDING, RUNNABLE, RUNNING, Sample

SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt        TEXT NOT NULL,
    repo          TEXT NOT NULL,
    tool          TEXT,
    any_tool      INTEGER NOT NULL DEFAULT 0,
    session_id    TEXT,
    state         TEXT NOT NULL,
    depends_on    INTEGER,
    on_success    TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    generation    INTEGER NOT NULL DEFAULT 0,
    parent_id     INTEGER,
    worktree      TEXT,
    branch        TEXT,
    blocked_until INTEGER,
    last_error    TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    exit_code  INTEGER,
    blocked    INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    log_path   TEXT,
    summary    TEXT
);
CREATE TABLE IF NOT EXISTS bucket (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tool              TEXT NOT NULL,
    bucket_key        TEXT NOT NULL UNIQUE,
    window_mins       INTEGER,
    is_representative INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sample (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_id INTEGER NOT NULL,
    at        INTEGER NOT NULL,
    used_pct  REAL NOT NULL,
    reset_at  INTEGER,
    source    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_bucket_at ON sample(bucket_id, at DESC);
CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,
    tool      TEXT,
    bucket_id INTEGER,
    at        INTEGER NOT NULL,
    payload   TEXT
);
CREATE TABLE IF NOT EXISTS remote_workspace (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    path           TEXT NOT NULL UNIQUE,
    default_branch TEXT NOT NULL,
    updated_at     INTEGER NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'git'
);
CREATE TABLE IF NOT EXISTS remote_claim (
    job_id       TEXT PRIMARY KEY,
    attempt_id   TEXT NOT NULL,
    lease_epoch  INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    tool_id      TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    state        TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    sent_at     INTEGER,
    created_at  INTEGER NOT NULL,
    UNIQUE(job_id, attempt_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoint (
    plan_id    TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_check (
    workspace_id TEXT NOT NULL,
    name         TEXT NOT NULL,
    argv         TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS remote_plan_started (
    plan_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        # Failures an older agent delivered still hold their Plan's tombstone.
        delivered = [row["id"] for row in self.conn.execute(
            "SELECT o.id FROM remote_outbox o JOIN remote_plan_started p"
            " ON p.job_id=o.job_id AND p.attempt_id=o.attempt_id WHERE o.sent_at IS NOT NULL")]
        if delivered:
            self._release_failed_plans(delivered)

    def _migrate(self) -> None:
        # Workspaces registered before folder workspaces existed are git repos.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(remote_workspace)")}
        if "kind" not in columns:
            self.conn.execute("ALTER TABLE remote_workspace ADD COLUMN kind TEXT NOT NULL DEFAULT 'git'")

    def close(self) -> None:
        self.conn.close()

    # ---- remote runner ---------------------------------------------------
    def upsert_workspace(self, workspace_id: str, name: str, path: str, default_branch: str,
                         now: Optional[int] = None, kind: str = "git") -> None:
        if kind not in ("git", "folder"):
            raise ValueError("unknown workspace kind: %s" % kind)
        self.conn.execute(
            "INSERT INTO remote_workspace (id,name,path,default_branch,updated_at,kind) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,path=excluded.path,"
            " default_branch=excluded.default_branch,updated_at=excluded.updated_at,kind=excluded.kind",
            (workspace_id, name, str(Path(path).resolve()), default_branch, now or _now(), kind),
        )

    def get_workspace(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM remote_workspace WHERE id=?", (workspace_id,)).fetchone()
        return dict(row) if row else None

    def list_workspaces(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM remote_workspace ORDER BY name").fetchall()]

    def remove_workspace(self, workspace_id: str) -> None:
        self.conn.execute("DELETE FROM remote_workspace WHERE id=?", (workspace_id,))
        self.conn.execute("DELETE FROM workspace_check WHERE workspace_id=?", (workspace_id,))

    # ---- local check commands (only ever registered on this computer) -----
    def save_check(self, workspace_id: str, name: str, argv: List[str], now: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT INTO workspace_check (workspace_id,name,argv,updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(workspace_id,name) DO UPDATE SET argv=excluded.argv,updated_at=excluded.updated_at",
            (workspace_id, name, json.dumps(list(argv), ensure_ascii=False), now or _now()),
        )

    def get_check(self, workspace_id: str, name: str) -> Optional[List[str]]:
        row = self.conn.execute("SELECT argv FROM workspace_check WHERE workspace_id=? AND name=?",
                                (workspace_id, name)).fetchone()
        if not row:
            return None
        argv = json.loads(row["argv"])
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return None
        return argv

    def list_checks(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if workspace_id is None:
            rows = self.conn.execute("SELECT * FROM workspace_check ORDER BY workspace_id, name").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM workspace_check WHERE workspace_id=? ORDER BY name",
                                     (workspace_id,)).fetchall()
        return [{"workspace_id": r["workspace_id"], "name": r["name"], "argv": json.loads(r["argv"])} for r in rows]

    def remove_check(self, workspace_id: str, name: str) -> bool:
        cursor = self.conn.execute("DELETE FROM workspace_check WHERE workspace_id=? AND name=?", (workspace_id, name))
        return cursor.rowcount > 0

    def save_remote_claim(self, claim: Dict[str, Any], state: str = "claimed", now: Optional[int] = None) -> None:
        job = claim["job"]
        # An import_parse prompt is a whole shared conversation: it is never
        # kept on disk here (it has no workspace either).
        prompt = "" if job.get("kind") == "import_parse" else job.get("prompt") or ""
        self.conn.execute(
            "INSERT INTO remote_claim (job_id,attempt_id,lease_epoch,workspace_id,tool_id,prompt,state,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "attempt_id=excluded.attempt_id,lease_epoch=excluded.lease_epoch,workspace_id=excluded.workspace_id,"
            "tool_id=excluded.tool_id,prompt=excluded.prompt,state=excluded.state,updated_at=excluded.updated_at",
            (job["id"], claim["attempt_id"], claim["lease_epoch"], job.get("workspace_id") or "",
             job.get("tool_profile_id") or "", prompt, state, now or _now()),
        )

    def update_remote_claim(self, job_id: str, state: str, now: Optional[int] = None) -> None:
        self.conn.execute("UPDATE remote_claim SET state=?,updated_at=? WHERE job_id=?", (state, now or _now(), job_id))

    def get_remote_claim(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM remote_claim WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def queue_remote_event(self, job_id: str, attempt_id: str, lease_epoch: int,
                           event: Dict[str, Any], now: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO remote_outbox (job_id,attempt_id,lease_epoch,seq,payload,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (job_id, attempt_id, lease_epoch, int(event["seq"]),
             json.dumps(event, ensure_ascii=False, sort_keys=True), now or _now()),
        )

    def last_remote_seq(self, job_id: str, attempt_id: str) -> int:
        """The highest event seq queued for this attempt (0 when none)."""
        row = self.conn.execute("SELECT MAX(seq) FROM remote_outbox WHERE job_id=? AND attempt_id=?",
                                (job_id, attempt_id)).fetchone()
        return int(row[0] or 0) if row else 0

    def pending_remote_events(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM remote_outbox WHERE sent_at IS NULL ORDER BY id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def mark_remote_event_sent(self, event_id: int, now: Optional[int] = None) -> None:
        self.conn.execute("UPDATE remote_outbox SET sent_at=? WHERE id=?", (now or _now(), event_id))
        self._release_failed_plans([event_id])

    def mark_remote_events_sent(self, event_ids: List[int], now: Optional[int] = None) -> None:
        if not event_ids:
            return
        # One SQLite statement is atomic even with our autocommit connection.
        # A crash/failed ack retains the batch for an idempotent whole-batch retry.
        placeholders = ",".join("?" for _ in event_ids)
        self.conn.execute("UPDATE remote_outbox SET sent_at=? WHERE id IN (" + placeholders + ")",
                          [now or _now()] + list(event_ids))
        self._release_failed_plans(event_ids)

    def _release_failed_plans(self, event_ids: List[int]) -> None:
        """Valley now holds this job's `failed` event, so the run's outcome is
        known: lift its Plan's started tombstone, letting a retry the user asks
        for start fresh. Runs after the ack is stored; a crash in between only
        keeps the (safe) block."""
        rows = []
        for start in range(0, len(event_ids), 500):  # under SQLite's variable limit
            chunk = list(event_ids[start:start + 500])
            rows += self.conn.execute(
                "SELECT job_id, attempt_id, payload FROM remote_outbox WHERE sent_at IS NOT NULL AND id IN ("
                + ",".join("?" for _ in chunk) + ")", chunk).fetchall()
        for row in rows:
            try:
                kind = json.loads(row["payload"]).get("type")
            except (TypeError, ValueError, AttributeError):
                continue
            if kind == "failed":
                self.conn.execute("DELETE FROM remote_plan_started WHERE job_id=? AND attempt_id=?",
                                  (row["job_id"], row["attempt_id"]))

    def prune_sent_remote_events(self, before: int) -> int:
        """Delete acknowledged events sent before `before` (epoch seconds).
        Unsent rows are kept whatever their age: they still need delivery."""
        cursor = self.conn.execute("DELETE FROM remote_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (int(before),))
        return cursor.rowcount

    # ---- checkpoints ------------------------------------------------------
    def mark_plan_started(self, plan_id: str, job_id: str, attempt_id: str) -> None:
        """A durable tombstone: deleting a checkpoint must never grant a fresh
        start to a new job for an already-started Plan."""
        self.conn.execute(
            "INSERT OR IGNORE INTO remote_plan_started (plan_id,job_id,attempt_id) VALUES (?,?,?)",
            (plan_id, job_id, attempt_id),
        )

    def plan_started(self, plan_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM remote_plan_started WHERE plan_id=?", (plan_id,)).fetchone() is not None

    def save_checkpoint(self, checkpoint: Any, now: Optional[int] = None) -> None:
        """Atomically persist one checkpoint per plan (INSERT OR REPLACE is a
        single autocommit statement). Stores no secrets or CLI environment."""
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoint (plan_id,data,updated_at) VALUES (?,?,?)",
            (checkpoint.plan_id, json.dumps(checkpoint.to_row(), ensure_ascii=False, sort_keys=True), now or _now()),
        )

    def get_checkpoint(self, plan_id: str) -> Optional[Any]:
        from timetrace.checkpoints import Checkpoint
        row = self.conn.execute("SELECT data FROM checkpoint WHERE plan_id=?", (plan_id,)).fetchone()
        return Checkpoint.from_row(json.loads(row["data"])) if row else None

    def list_checkpoints(self) -> List[Any]:
        from timetrace.checkpoints import Checkpoint
        rows = self.conn.execute("SELECT data FROM checkpoint ORDER BY plan_id").fetchall()
        return [Checkpoint.from_row(json.loads(row["data"])) for row in rows]

    def delete_checkpoint(self, plan_id: str) -> None:
        self.conn.execute("DELETE FROM checkpoint WHERE plan_id=?", (plan_id,))

    # ---- task -------------------------------------------------------------
    def add_task(
        self,
        prompt: str,
        repo: str,
        tool: Optional[str] = None,
        any_tool: bool = False,
        depends_on: Optional[int] = None,
        on_success: Optional[str] = None,
        priority: int = 0,
        generation: int = 0,
        parent_id: Optional[int] = None,
        now: Optional[int] = None,
    ) -> int:
        now = now or _now()
        state = PENDING if depends_on is not None else RUNNABLE
        cur = self.conn.execute(
            "INSERT INTO task (prompt, repo, tool, any_tool, state, depends_on, on_success,"
            " priority, generation, parent_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (prompt, repo, tool, int(bool(any_tool)), state, depends_on, on_success,
             priority, generation, parent_id, now, now),
        )
        return int(cur.lastrowid)

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, include_done: bool = False) -> List[Dict[str, Any]]:
        if include_done:
            rows = self.conn.execute("SELECT * FROM task ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM task WHERE state != ? ORDER BY id", (DONE,)
            ).fetchall()
        return [dict(r) for r in rows]

    def tasks_in_state(self, state: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM task WHERE state=? ORDER BY priority DESC, id ASC", (state,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id: int, now: Optional[int] = None, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now or _now()
        cols = ", ".join("%s=?" % k for k in fields)
        self.conn.execute(
            "UPDATE task SET %s WHERE id=?" % cols, tuple(fields.values()) + (task_id,)
        )

    def delete_task(self, task_id: int) -> None:
        self.conn.execute("DELETE FROM task WHERE id=?", (task_id,))

    # ---- run --------------------------------------------------------------
    def add_run(
        self, task_id: int, tool: str, session_id: Optional[str], log_path: str,
        now: Optional[int] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (task_id, tool, started_at, session_id, log_path)"
            " VALUES (?,?,?,?,?)",
            (task_id, tool, now or _now(), session_id, log_path),
        )
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, exit_code: int, blocked: bool, summary: str = "",
        session_id: Optional[str] = None, now: Optional[int] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE run SET ended_at=?, exit_code=?, blocked=?, summary=?,"
            " session_id=COALESCE(?, session_id) WHERE id=?",
            (now or _now(), exit_code, int(bool(blocked)), summary, session_id, run_id),
        )

    def latest_run(self, task_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM run WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def runs_for_task(self, task_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM run WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def failed_runs_since(self, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM run WHERE ended_at >= ? AND blocked = 0"
            " AND exit_code IS NOT NULL AND exit_code != 0",
            (since,),
        ).fetchone()
        return int(row["n"])

    # ---- bucket / sample --------------------------------------------------
    def upsert_bucket(
        self, tool: str, bucket_key: str, window_mins: Optional[int], is_representative: bool
    ) -> int:
        row = self.conn.execute(
            "SELECT id FROM bucket WHERE bucket_key=?", (bucket_key,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE bucket SET window_mins=COALESCE(?, window_mins),"
                " is_representative=? WHERE id=?",
                (window_mins, int(bool(is_representative)), row["id"]),
            )
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO bucket (tool, bucket_key, window_mins, is_representative)"
            " VALUES (?,?,?,?)",
            (tool, bucket_key, window_mins, int(bool(is_representative))),
        )
        return int(cur.lastrowid)

    def get_bucket(self, bucket_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM bucket WHERE bucket_key=?", (bucket_key,)
        ).fetchone()
        return dict(row) if row else None

    def add_sample(self, sample: Sample, at: Optional[int] = None) -> int:
        bucket_id = self.upsert_bucket(
            sample.tool, sample.bucket_key, sample.window_mins, sample.is_representative
        )
        cur = self.conn.execute(
            "INSERT INTO sample (bucket_id, at, used_pct, reset_at, source) VALUES (?,?,?,?,?)",
            (bucket_id, at or _now(), float(sample.used_pct), sample.reset_at, sample.source),
        )
        return int(cur.lastrowid)

    def latest_sample_at(self, source: str) -> Optional[int]:
        """Time of the newest sample from `source` (e.g. "statusline")."""
        row = self.conn.execute("SELECT MAX(at) FROM sample WHERE source=?", (source,)).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def latest_samples(self) -> List[Dict[str, Any]]:
        """One row per bucket: the newest sample joined with bucket metadata."""
        rows = self.conn.execute(
            "SELECT b.id AS bucket_id, b.tool, b.bucket_key, b.window_mins,"
            " b.is_representative, s.at, s.used_pct, s.reset_at, s.source"
            " FROM bucket b JOIN sample s ON s.id = ("
            "   SELECT id FROM sample WHERE bucket_id=b.id ORDER BY at DESC, id DESC LIMIT 1)"
            " ORDER BY b.tool, b.bucket_key"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- event ------------------------------------------------------------
    def add_event(
        self, type_: str, tool: Optional[str] = None, bucket_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None, at: Optional[int] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO event (type, tool, bucket_id, at, payload) VALUES (?,?,?,?,?)",
            (type_, tool, bucket_id, at or _now(),
             json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
        )
        return int(cur.lastrowid)

    def list_events(self, limit: int = 50, type_: Optional[str] = None) -> List[Dict[str, Any]]:
        if type_:
            rows = self.conn.execute(
                "SELECT * FROM event WHERE type=? ORDER BY id DESC LIMIT ?", (type_, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except ValueError:
                pass
            out.append(d)
        return out
