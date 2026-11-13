"""All SQL lives here. Other modules call these methods and never write SQL."""
import json
import sqlite3
import time
from contextlib import contextmanager
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
    summary    TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0
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
    abandoned_at INTEGER,
    UNIQUE(job_id, attempt_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoint (
    plan_id    TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_outbox_retry (
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    status INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    last_failed_at REAL NOT NULL,
    retry_at REAL NOT NULL,
    PRIMARY KEY (job_id, attempt_id, lease_epoch)
);
-- What Valley's HTTP 409 answers have established about one batch, kept apart from remote_outbox_retry on purpose:
-- an older Runner writes that table by position, so it must keep exactly its seven columns. An older Runner
-- never looks at this one.
--   refused    consecutive 409 answers this version has seen for the batch (any other failure resets it to 0;
--              `failures` in remote_outbox_retry is the total of every kind and is a different number)
--   paused_at  NULL while the batch is still retried; set once automatic sending of it has stopped
--   basis      where the record comes from. NULL: an ordinary count of refusals. 'legacy_check': an older Runner
--              had failed the batch many times and its last answer was 409 -- a batch TO BE CHECKED, not stopped
--              (paused_at stays NULL): its next fresh request decides. Once stopped: 'confirmed' (refused
--              OUTBOX_REFUSED_PAUSE_AFTER times in a row) or 'legacy_refused' (the check was refused with 409)
--   legacy_failures  the older Runner's total of failures of every kind when the batch was marked 'legacy_check'
CREATE TABLE IF NOT EXISTS remote_outbox_hold (
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    refused INTEGER NOT NULL,
    last_refused_at REAL NOT NULL,
    paused_at REAL,
    basis TEXT,
    legacy_failures INTEGER,
    PRIMARY KEY (job_id, attempt_id, lease_epoch)
);
CREATE TABLE IF NOT EXISTS workspace_check (
    workspace_id TEXT NOT NULL,
    name         TEXT NOT NULL,
    argv         TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS objective_check (
    workspace_id TEXT NOT NULL, name TEXT NOT NULL, contract TEXT NOT NULL,
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


# Valley's generic 409 (40900) covers a lost lease, an attempt that already ended and a sequence gap alike, so one
# answer proves nothing. A batch is refused this many times in a row, one retry wait apart, before the Runner
# stops sending it by itself. Stopping never discards, never marks sent and never touches an event.
OUTBOX_REFUSED_PAUSE_AFTER = 5

# What an older Runner left in remote_outbox_retry is three facts: the status of the LAST failure, the TOTAL of failures
# of every kind and when to try again. It is not a run of refusals (4 server errors and one 409 are 5 failures whose last
# status is 409, and so are 5 refusals), so the upgrade stops nothing on it. A batch with at least this many failures
# (an hour of the old once-a-minute retries) whose last status is 409 is marked HOLD_LEGACY_CHECK instead: it gets one
# fresh request, which decides. Below it the old numbers say too little and the batch is counted from zero like a new one.
OUTBOX_LEGACY_CHECK_AFTER = 60

# remote_outbox_hold.basis (see the table)
HOLD_CONFIRMED = "confirmed"
HOLD_LEGACY_CHECK = "legacy_check"
HOLD_LEGACY_REFUSED = "legacy_refused"


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
        self._settle_refusal_holds()
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
        # Results a person set aside after the server refused them for good.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(remote_outbox)")}
        if "abandoned_at" not in columns:
            self.conn.execute("ALTER TABLE remote_outbox ADD COLUMN abandoned_at INTEGER")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(remote_outbox_hold)")}
        if "legacy_failures" not in columns:
            self.conn.execute("ALTER TABLE remote_outbox_hold ADD COLUMN legacy_failures INTEGER")
        # A run that ended because the scheduler was stopped (or was found unfinished after it died) is not a
        # failure of the tool: the circuit breaker does not count it.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(run)")}
        if "interrupted" not in columns:
            self.conn.execute("ALTER TABLE run ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0")

    def _settle_refusal_holds(self) -> None:
        """On every open: drop stops that no longer mean anything and mark what an older Runner kept resending.

        A stop whose events were acknowledged by a Runner that does not know about stops (a downgrade) or were set
        aside is stale and must not catch the attempt's later events. A batch an older Runner failed at least
        OUTBOX_LEGACY_CHECK_AFTER times whose last answer was 409 is only MARKED here (HOLD_LEGACY_CHECK, not stopped):
        the old `failures` counted every kind of failure and `status` is just the last one, so together they are
        evidence that the batch kept failing, never proof of a run of refusals, and the upgrade stops nothing on
        them. Its next fresh request decides (see refuse_remote_events). INSERT OR IGNORE leaves a batch that already
        has a record (one this version counted, one already checked, one a person resumed) alone, so reopening the
        database never undoes a decision or checks a batch twice."""
        # Every worker thread opens its own connection, so look before writing: the common case writes nothing.
        if self.conn.execute("SELECT 1 FROM remote_outbox_hold LIMIT 1").fetchone():
            self._drop_stale_holds()
        unmarked = (" FROM remote_outbox_retry WHERE status=409 AND failures>=? AND NOT EXISTS ("
                    "SELECT 1 FROM remote_outbox_hold h WHERE h.job_id=remote_outbox_retry.job_id"
                    " AND h.attempt_id=remote_outbox_retry.attempt_id AND h.lease_epoch=remote_outbox_retry.lease_epoch)"
                    " AND EXISTS (" + self._UNSENT_OF % {"t": "remote_outbox_retry"} + ")")
        if self.conn.execute("SELECT 1" + unmarked + " LIMIT 1", (OUTBOX_LEGACY_CHECK_AFTER,)).fetchone():
            self.conn.execute(
                "INSERT OR IGNORE INTO remote_outbox_hold"
                " (job_id,attempt_id,lease_epoch,refused,last_refused_at,paused_at,basis,legacy_failures)"
                " SELECT job_id,attempt_id,lease_epoch,0,last_failed_at,NULL,?,failures" + unmarked,
                (HOLD_LEGACY_CHECK, OUTBOX_LEGACY_CHECK_AFTER))

    _UNSENT_OF = ("SELECT 1 FROM remote_outbox o WHERE o.job_id=%(t)s.job_id AND o.attempt_id=%(t)s.attempt_id"
                  " AND o.lease_epoch=%(t)s.lease_epoch AND o.sent_at IS NULL AND o.abandoned_at IS NULL")

    def _drop_stale_holds(self) -> None:
        self.conn.execute("DELETE FROM remote_outbox_hold WHERE NOT EXISTS ("
                          + self._UNSENT_OF % {"t": "remote_outbox_hold"} + ")")

    def close(self) -> None:
        self.conn.close()

    def transaction(self):
        """One short write transaction (BEGIN IMMEDIATE ... COMMIT), for a decision that reads rows and writes
        others and must not interleave with another process's write: `timetrace rm`, `retry` and a second
        scheduler all serialize against it. Never hold it while a tool runs: it takes the database's write lock."""
        return self._transaction()

    @contextmanager
    def _transaction(self):
        """One write transaction (the connection otherwise commits every statement); joins one already open."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---- remote runner ---------------------------------------------------
    def upsert_workspace(self, workspace_id: str, name: str, path: str, default_branch: str,
                         now: Optional[int] = None, kind: str = "git") -> None:
        if kind not in ("git", "folder", "project_root"):
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
        self.conn.execute("DELETE FROM objective_check WHERE workspace_id=? AND name=?", (workspace_id, name))
        self.conn.execute(
            "INSERT INTO workspace_check (workspace_id,name,argv,updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(workspace_id,name) DO UPDATE SET argv=excluded.argv,updated_at=excluded.updated_at",
            (workspace_id, name, json.dumps(list(argv), ensure_ascii=False), now or _now()),
        )

    def save_objective_check(self, workspace_id, name, contract):
        self.conn.execute("INSERT OR REPLACE INTO objective_check VALUES (?,?,?)",
                          (workspace_id, name, json.dumps(contract, ensure_ascii=False)))

    def get_objective_check(self, workspace_id, name):
        row = self.conn.execute("SELECT contract FROM objective_check WHERE workspace_id=? AND name=?",
                                (workspace_id, name)).fetchone()
        return json.loads(row["contract"]) if row else None

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
        prompt = "" if job.get("kind") in ("import_parse", "planning") else job.get("prompt") or ""
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

    @staticmethod
    def _decoded(rows) -> List[Dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def pending_remote_events(self) -> List[Dict[str, Any]]:
        """Every event not acknowledged and not set aside, including those of a batch whose automatic sending stopped."""
        return self._decoded(self.conn.execute(
            "SELECT * FROM remote_outbox WHERE sent_at IS NULL AND abandoned_at IS NULL ORDER BY id").fetchall())

    def sendable_remote_events(self) -> List[Dict[str, Any]]:
        """The pending events the Runner may upload by itself: those of a batch it has not stopped sending."""
        return self._decoded(self.conn.execute(
            "SELECT * FROM remote_outbox WHERE sent_at IS NULL AND abandoned_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM remote_outbox_hold h WHERE h.job_id=remote_outbox.job_id"
            " AND h.attempt_id=remote_outbox.attempt_id AND h.lease_epoch=remote_outbox.lease_epoch"
            " AND h.paused_at IS NOT NULL) ORDER BY id").fetchall())

    def pending_remote_events_for(self, job: str, attempt: str, epoch: int) -> List[Dict[str, Any]]:
        """The unacknowledged events of one attempt, in sequence order: the batch
        that is safe to (re)send, because Valley skips what it already holds."""
        return self._decoded(self.conn.execute(
            "SELECT * FROM remote_outbox WHERE job_id=? AND attempt_id=? AND lease_epoch=?"
            " AND sent_at IS NULL AND abandoned_at IS NULL ORDER BY seq", (job, attempt, epoch)).fetchall())

    def mark_remote_event_sent(self, event_id: int, now: Optional[int] = None) -> None:
        self.conn.execute("UPDATE remote_outbox SET sent_at=? WHERE id=?", (now or _now(), event_id))
        self._release_failed_plans([event_id])

    def remote_event_retry(self, job: str, attempt: str, epoch: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM remote_outbox_retry WHERE job_id=? AND attempt_id=? AND lease_epoch=?",
            (job, attempt, epoch)).fetchone()
        return dict(row) if row else None

    def _defer(self, job: str, attempt: str, epoch: int, status: int, now: float, retry_at: float) -> None:
        self.conn.execute(
            "INSERT INTO remote_outbox_retry VALUES (?,?,?,?,1,?,?)"
            " ON CONFLICT(job_id,attempt_id,lease_epoch) DO UPDATE SET"
            " status=excluded.status, failures=failures+1,"
            " last_failed_at=excluded.last_failed_at, retry_at=excluded.retry_at",
            (job, attempt, epoch, status, now, retry_at))

    def defer_remote_events(self, job: str, attempt: str, epoch: int, status: int,
                            now: float, retry_at: float) -> bool:
        """Record that an upload of this batch failed without being refused, and when to try again.

        This is retry metadata only. Original payloads, sequences, occurrence times and sent_at remain untouched,
        including across process restarts. `failures` counts every failure of the batch whatever its kind; a
        failure that is not a 409 also ends the run of consecutive refusals (see refuse_remote_events); it is never
        recorded as a refusal and leaves a batch still to be checked (HOLD_LEGACY_CHECK) to be checked. A batch
        whose automatic sending has stopped records nothing more (an upload that was still in flight when it
        stopped must not rewrite what the stop is based on): returns False then."""
        with self._transaction():
            hold = self.remote_event_hold(job, attempt, epoch)
            if hold is not None and hold["paused_at"] is not None:
                return False
            self._defer(job, attempt, epoch, status, now, retry_at)
            if status != 409 and hold is not None:
                self.conn.execute(
                    "UPDATE remote_outbox_hold SET refused=0 WHERE job_id=? AND attempt_id=? AND lease_epoch=?",
                    (job, attempt, epoch))
        return True

    def remote_event_hold(self, job: str, attempt: str, epoch: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM remote_outbox_hold WHERE job_id=? AND attempt_id=? AND lease_epoch=?",
            (job, attempt, epoch)).fetchone()
        return dict(row) if row else None

    def remote_event_paused(self, job: str, attempt: str, epoch: int) -> bool:
        """Whether the Runner has stopped sending this batch by itself."""
        hold = self.remote_event_hold(job, attempt, epoch)
        return hold is not None and hold["paused_at"] is not None

    def paused_remote_batches(self) -> List[Dict[str, Any]]:
        """(job_id, attempt_id, lease_epoch, basis, refused, legacy_failures) of every batch whose automatic sending
        has stopped."""
        return [dict(row) for row in self.conn.execute(
            "SELECT job_id,attempt_id,lease_epoch,basis,refused,legacy_failures FROM remote_outbox_hold"
            " WHERE paused_at IS NOT NULL ORDER BY paused_at")]

    def unchecked_remote_batches(self) -> List[Dict[str, Any]]:
        """(job_id, attempt_id, lease_epoch, legacy_failures) of every batch an older Runner left with many failures
        and a last answer of 409 whose check (its next fresh request) has not been answered yet."""
        return [dict(row) for row in self.conn.execute(
            "SELECT job_id,attempt_id,lease_epoch,legacy_failures FROM remote_outbox_hold"
            " WHERE paused_at IS NULL AND basis=? ORDER BY rowid", (HOLD_LEGACY_CHECK,))]

    def refuse_remote_events(self, job: str, attempt: str, epoch: int, now: float, retry_at: float,
                             pause_after: int = OUTBOX_REFUSED_PAUSE_AFTER) -> Optional[Dict[str, Any]]:
        """Record one HTTP 409 answer for this batch; stop its automatic sending at the `pause_after`-th in a row.

        The count is of consecutive 409 answers: a failure of any other kind in between (defer_remote_events)
        starts it again, a success removes it with the batch. Answers inside the wait window of the previous one
        (two uploads of the same batch in flight at once) are one confirmation. Only the retry bookkeeping and this
        count change; no event is touched, marked sent or set aside.

        A batch marked HOLD_LEGACY_CHECK (an older Runner's many failures, the last one a 409) is decided by its first
        fresh answer: a 409 now stops it at once, with basis HOLD_LEGACY_REFUSED -- the old record (many failures, last
        answer 409) plus this refusal are the two facts the stop rests on, and `refused` is 1 because only one refusal
        was seen by this version. Any other kind of failure is not a refusal (defer_remote_events) and leaves the mark
        in place: a network error or a 5xx says nothing about whether Valley still refuses the batch.

        Returns None when nothing of the batch is pending any more (it was acknowledged or set aside meanwhile),
        else {"refused", "paused", "newly_paused", "pending_events", "basis", "legacy_failures"}."""
        with self._transaction():
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM remote_outbox WHERE job_id=? AND attempt_id=? AND lease_epoch=?"
                " AND sent_at IS NULL AND abandoned_at IS NULL", (job, attempt, epoch)).fetchone()[0]
            if not pending:
                return None
            hold = self.remote_event_hold(job, attempt, epoch)
            legacy_failures = hold["legacy_failures"] if hold is not None else None
            if hold is not None and hold["paused_at"] is not None:
                return {"refused": hold["refused"], "paused": True, "newly_paused": False, "pending_events": pending,
                        "basis": hold["basis"], "legacy_failures": legacy_failures}
            if hold is not None and hold["basis"] == HOLD_LEGACY_CHECK:
                refused, paused, basis = 1, True, HOLD_LEGACY_REFUSED
            else:
                retry = self.remote_event_retry(job, attempt, epoch)
                same_window = retry is not None and retry["status"] == 409 and now < retry["retry_at"]
                refused = (hold["refused"] if hold is not None else 0) + (0 if same_window else 1)
                paused = refused >= pause_after
                basis = HOLD_CONFIRMED if paused else None
            self._defer(job, attempt, epoch, 409, now, retry_at)
            self.conn.execute(
                "INSERT INTO remote_outbox_hold (job_id,attempt_id,lease_epoch,refused,last_refused_at,paused_at,basis)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id,attempt_id,lease_epoch) DO UPDATE SET"
                " refused=excluded.refused, last_refused_at=excluded.last_refused_at,"
                " paused_at=excluded.paused_at, basis=excluded.basis",
                (job, attempt, epoch, refused, now, now if paused else None, basis))
        return {"refused": refused, "paused": paused, "newly_paused": paused, "pending_events": pending,
                "basis": basis, "legacy_failures": legacy_failures}

    def resume_paused_remote_events(self, job: str, attempt: str, now: Optional[float] = None) -> int:
        """Put the stopped batches of this attempt back into service, by hand only (`agent retry-results`).

        The next flush sends each original batch again, as queued, and the usual rules apply from there: an ACK
        marks it sent, and another run of refusals stops it again. Nothing is rebuilt, no lease is renewed and
        nothing is run again. Returns how many events were put back (0: nothing of this attempt was stopped)."""
        resumed = 0
        with self._transaction():
            for row in self.conn.execute(
                    "SELECT lease_epoch FROM remote_outbox_hold WHERE job_id=? AND attempt_id=?"
                    " AND paused_at IS NOT NULL", (job, attempt)).fetchall():
                epoch = row["lease_epoch"]
                pending = self.conn.execute(
                    "SELECT COUNT(*) FROM remote_outbox WHERE job_id=? AND attempt_id=? AND lease_epoch=?"
                    " AND sent_at IS NULL AND abandoned_at IS NULL", (job, attempt, epoch)).fetchone()[0]
                if not pending:
                    continue
                self.conn.execute(
                    "UPDATE remote_outbox_hold SET paused_at=NULL, refused=0, basis=NULL"
                    " WHERE job_id=? AND attempt_id=? AND lease_epoch=?", (job, attempt, epoch))
                # Due at once, whatever the clock says: the wait it was under belongs to the failure before the stop.
                self.conn.execute(
                    "UPDATE remote_outbox_retry SET retry_at=last_failed_at"
                    " WHERE job_id=? AND attempt_id=? AND lease_epoch=?", (job, attempt, epoch))
                resumed += pending
        return resumed

    def clear_remote_event_retry(self, job: str, attempt: str, epoch: int) -> None:
        """The batch was acknowledged: forget everything recorded against it (retry wait, refusals, stop)."""
        with self._transaction():
            self.conn.execute(
                "DELETE FROM remote_outbox_retry WHERE job_id=? AND attempt_id=? AND lease_epoch=?",
                (job, attempt, epoch))
            self.conn.execute(
                "DELETE FROM remote_outbox_hold WHERE job_id=? AND attempt_id=? AND lease_epoch=?",
                (job, attempt, epoch))

    def remote_outbox_diagnostics(self) -> List[Dict[str, Any]]:
        """One row per pending batch that has failed: the retry state, the stop (if any) and what is kept.

        `paused_at` is set for a batch whose automatic sending has stopped; `refused` is its run of consecutive
        409 answers (see refuse_remote_events). `pause_basis` is the origin of the record: 'confirmed' or
        'legacy_refused' once stopped, 'legacy_check' for a batch still to be checked (not stopped; `legacy_failures`
        is the older Runner's total of failures of every kind)."""
        return [dict(row) for row in self.conn.execute(
            "SELECT r.*, h.paused_at AS paused_at, h.refused AS refused, h.basis AS pause_basis,"
            " h.legacy_failures AS legacy_failures,"
            " COUNT(o.id) AS pending_events, MIN(o.created_at) AS oldest_event_at,"
            " MIN(o.seq) AS first_seq, MAX(o.seq) AS last_seq"
            " FROM remote_outbox_retry r"
            " JOIN remote_outbox o ON o.job_id=r.job_id AND o.attempt_id=r.attempt_id"
            " AND o.lease_epoch=r.lease_epoch"
            " LEFT JOIN remote_outbox_hold h ON h.job_id=r.job_id AND h.attempt_id=r.attempt_id"
            " AND h.lease_epoch=r.lease_epoch WHERE o.sent_at IS NULL AND o.abandoned_at IS NULL"
            " GROUP BY r.job_id,r.attempt_id,r.lease_epoch ORDER BY r.last_failed_at")]

    def remote_event_observed_span(self, job: str, attempt: str, epoch: int):
        """(earliest, latest) `observed_at` stamped on the pending events of this batch, as the events carry it."""
        stamps = []
        for row in self.conn.execute(
                "SELECT payload FROM remote_outbox WHERE job_id=? AND attempt_id=? AND lease_epoch=?"
                " AND sent_at IS NULL AND abandoned_at IS NULL", (job, attempt, epoch)):
            try:
                stamp = json.loads(row["payload"]).get("observed_at")
            except (TypeError, ValueError, AttributeError):
                continue
            if isinstance(stamp, str):
                stamps.append(stamp)
        return (min(stamps), max(stamps)) if stamps else (None, None)

    def remote_outbox_health(self) -> Dict[str, Any]:
        """The anonymous summary of the result outbox, meant for a later status report to Valley.

        Counts, one reason code and epoch seconds only: no job, attempt or workspace id, no event content, no
        path. 'paused' is a batch the Runner stopped sending by itself after Valley kept refusing it (HTTP 409),
        'retrying' one that failed some other way and is still retried, 'set aside' what a person discarded."""
        batches = self.remote_outbox_diagnostics()
        paused = [b for b in batches if b["paused_at"] is not None]
        retrying = [b for b in batches if b["paused_at"] is None]
        return {
            "schema": 1,
            "paused_batches": len(paused),
            "paused_events": sum(b["pending_events"] for b in paused),
            "paused_reason": "http_409" if paused else None,
            "paused_since": int(min(b["paused_at"] for b in paused)) if paused else None,
            "oldest_paused_event_at": int(min(b["oldest_event_at"] for b in paused)) if paused else None,
            "retrying_batches": len(retrying),
            "retrying_events": sum(b["pending_events"] for b in retrying),
            "set_aside_events": self.abandoned_remote_event_count(),
        }

    def discard_conflicted_remote_events(self, job: str, attempt: str, now: Optional[int] = None) -> int:
        """Set aside the unsent results of an attempt the server refused (409), by hand only.

        They stay in the database, are never uploaded again and are never marked sent: setting
        a result aside is not delivering it. A batch that failed any other way may still
        succeed, so it is left alone. Returns how many results were set aside."""
        cursor = self.conn.execute(
            "UPDATE remote_outbox SET abandoned_at=? WHERE job_id=? AND attempt_id=?"
            " AND sent_at IS NULL AND abandoned_at IS NULL AND EXISTS ("
            "SELECT 1 FROM remote_outbox_retry r WHERE r.job_id=remote_outbox.job_id"
            " AND r.attempt_id=remote_outbox.attempt_id AND r.lease_epoch=remote_outbox.lease_epoch"
            " AND r.status=409)", (now or _now(), job, attempt))
        if cursor.rowcount:
            self.conn.execute("DELETE FROM remote_outbox_retry WHERE job_id=? AND attempt_id=? AND status=409",
                              (job, attempt))
            self._drop_stale_holds()
        return cursor.rowcount

    def abandoned_remote_event_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM remote_outbox WHERE abandoned_at IS NOT NULL AND sent_at IS NULL").fetchone()[0]

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

    def move_task(self, task_id: int, expected: str, now: Optional[int] = None, **fields: Any) -> bool:
        """`update_task`, but only while the task is still in state `expected`; True when it was changed.

        A scheduler decides a move from a row it read a moment ago. With this, a move cannot undo what happened to
        the row in between (it was started, finished, retried or removed by someone else)."""
        fields["updated_at"] = now or _now()
        cols = ", ".join("%s=?" % k for k in fields)
        cur = self.conn.execute("UPDATE task SET %s WHERE id=? AND state=?" % cols,
                                tuple(fields.values()) + (task_id, expected))
        return cur.rowcount > 0

    def delete_task(self, task_id: int) -> None:
        self.conn.execute("DELETE FROM task WHERE id=?", (task_id,))

    def task_has_run(self, task_id: int) -> bool:
        """Whether a tool process was ever started for the task (a run row exists), as opposed to a task that
        only ever sat in the queue."""
        return self.conn.execute("SELECT 1 FROM run WHERE task_id=? LIMIT 1", (task_id,)).fetchone() is not None

    def tasks_failed_only_by_dependency(self) -> List[Dict[str, Any]]:
        """Tasks an older scheduler marked FAILED with exactly "dependency N failed" (N being their parent) that
        never ran: no run, no session, no worktree. The failure belonged to the parent, not to the task."""
        rows = self.conn.execute(
            "SELECT * FROM task WHERE state=? AND depends_on IS NOT NULL"
            " AND last_error = 'dependency ' || depends_on || ' failed'"
            " AND session_id IS NULL AND worktree IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM run WHERE run.task_id = task.id)"
            " ORDER BY priority DESC, id ASC", (FAILED,)).fetchall()
        return [dict(r) for r in rows]

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
        self, run_id: int, exit_code: Optional[int], blocked: bool, summary: str = "",
        session_id: Optional[str] = None, now: Optional[int] = None, interrupted: bool = False,
    ) -> None:
        """`exit_code` None: how the tool ended is not known (a stop whose exit was not confirmed)."""
        self.conn.execute(
            "UPDATE run SET ended_at=?, exit_code=?, blocked=?, summary=?,"
            " session_id=COALESCE(?, session_id), interrupted=? WHERE id=?",
            (now or _now(), exit_code, int(bool(blocked)), summary, session_id, int(bool(interrupted)), run_id),
        )

    def end_unfinished_runs(self, task_id: int, summary: str, now: Optional[int] = None) -> int:
        """Close the runs of `task_id` that nothing ended (the scheduler died mid-run) with a note that says so.
        Their exit code stays NULL: nobody knows how the tool ended. Returns how many were closed."""
        cur = self.conn.execute(
            "UPDATE run SET ended_at=?, summary=?, interrupted=1 WHERE task_id=? AND ended_at IS NULL",
            (now or _now(), summary, task_id))
        return cur.rowcount

    def discard_unstarted_run(self, run_id: int) -> bool:
        """Delete a run row whose tool process was never created (the start was refused at the spawn gate), so the
        task is not recorded as one that ran. Only a row nothing has finished yet; True when it was deleted."""
        cur = self.conn.execute("DELETE FROM run WHERE id=? AND ended_at IS NULL", (run_id,))
        return cur.rowcount > 0

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
            "SELECT COUNT(*) AS n FROM run WHERE ended_at >= ? AND blocked = 0 AND interrupted = 0"
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
