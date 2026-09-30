"""Outbound-only Valley runner loop."""
import time
import uuid
import hashlib
import os
import re
import shutil
import signal
import threading
from datetime import datetime, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError, wait
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict

from timetrace import __version__
from timetrace.cloud import CloudError
from timetrace.db import Database
from timetrace import approvals, audit, checks, folder, pause, quota, results, runner_state, worktree
from timetrace.remote_control import RunControl
from timetrace.process import tail_text
from timetrace.redact import redact
from timetrace.checkpoints import Checkpoint, checkpoint_problem
from timetrace.dispatch import (DispatchDenied, DispatchGate, LockBusy, UnclearedOwner, adapter_capabilities,
                           adapter_zero_spend_verified, coding_slot_lock, deny_reason, tool_slot_lock,
                           enforce_spawn_authority, plan_lock, spawn_authority, workspace_lock)


OUTPUT_TAIL_BYTES = 8000  # Valley accepts up to 8192
OUTBOX_RETENTION_SECONDS = 7 * 24 * 3600  # acknowledged events kept for diagnosis
REPLY_BYTES = 32 * 1024  # chat_turn reply bound, UTF-8 bytes (Valley rejects more)
JOB_KINDS = ("task", "chat_turn", "review_turn", "check", "import_parse")
# The import record an import_parse job belongs to (Valley's id alphabet).
IMPORT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A provider session id goes into argv after --resume / `exec resume`: it must
# never be able to look like an option or carry whitespace.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Checkpoint `git_head` of a folder-workspace run (there is no git state).
FOLDER_HEAD = "folder"
# The block reason of a job the daemon stopped (SIGTERM / SIGINT / stop()).
STOPPED = "runner_stopped"
# How long shutdown waits for workers after cancelling them. launchd sends
# SIGKILL ExitTimeOut (20 s) after SIGTERM; a killed tool group takes ≤ 2 s.
SHUTDOWN_GRACE_SECONDS = 15
# Protocol 2 (Valley docs/remote-control-protocol.md): controls, approvals,
# self-check, local pause, reset credits.
PROTOCOL_VERSION = 2
# Self-check and reset-credit reads: every 10 minutes (and at start; the
# self-check also after a failed job).
HEALTH_INTERVAL_SECONDS = 600
RESET_CREDITS_INTERVAL_SECONDS = 600
# Below this much free disk on the workspace volume nothing new is claimed.
DISK_ERROR_GB = 1.0
# Valley's "computer version not supported" error code.
VERSION_REJECTED_CODE = 42210
# Claim outcomes after which the loop only does upkeep and waits.
HOLD_OUTCOMES = ("idle", "paused", "disk_full")
# The message of a run the user interrupted from the phone.
INTERRUPTED_MESSAGE = "已按你的要求中断，改动保留在工作副本里"
# The instruction a resumed (previously interrupted) job continues with.
RESUME_PROMPT = ("The user interrupted this task and now asks you to continue it from where it stopped. "
                 "First look at the changes already in the working directory, then carry on.")
RESUME_NOTE = "\n\nAdditional instruction from the user:\n"


def _safe_id(value) -> str:
    """A server-chosen id reduced to a file-name alphabet."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:120] or "_"


def local_task_id(job_key) -> int:
    """The number a task job's worktree and fallback branch (`timetrace/<n>`) use."""
    return int(hashlib.sha256(str(job_key).encode("utf-8")).hexdigest()[:12], 16)


def truncate_utf8(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, never cutting a character in half."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def safe_reply(text) -> str:
    """The assistant's final message as it may leave this computer."""
    return truncate_utf8(redact(text or ""), REPLY_BYTES)


def install_stop_handlers(agent: "Agent") -> Dict[int, Any]:
    """SIGTERM (launchd stop) and SIGINT (Ctrl-C) call agent.stop(): no new
    claims, running jobs cancelled (process groups killed) and reported, locks
    released. Returns the previous handlers for restore_handlers()."""
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: agent.stop())
    return previous


def restore_handlers(previous: Dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@contextmanager
def _job_pool(cancel_event: threading.Event):
    """The one-worker pool a job runs its tool on. An interrupt (not an
    ordinary exception) cancels the tool first, so leaving the block never
    waits for a run that nobody will stop."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        yield pool
    except BaseException as exc:
        if not isinstance(exc, Exception):
            cancel_event.set()
        raise
    finally:
        pool.shutdown(wait=True)


class ToolBusy(LockBusy):
    """Every slot of one tool is taken (or fenced: `cause`)."""

    def __init__(self, tool, cause):
        super().__init__(str(cause))
        self.tool, self.cause = tool, cause


class _Slots:
    """The slot locks one job holds, released together."""

    def __init__(self, locks):
        self.locks = list(locks)

    def release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()


def _default_pool_binding(provider: str):
    """The pool a registered tool draws from. timetrace reads quota through the same
    login the tool executes with, so the reading identifies that pool: the
    profile id must equal the inventory tool id (`<provider>-default`)."""
    return "pool-" + provider, provider + "-default", True


def _capability_zero_spend(adapter: Any, job: Dict[str, Any]) -> bool:
    """Safe default: only a verified adapter capability authorises spend-free
    execution. A manual/source claim can never grant it. The billing verdict is
    re-checked (cache bypassed) right here, at the gate before any spawn, so a
    logout or a newly exported API key since the last poll closes the gate."""
    verdict = getattr(getattr(adapter, "billing", None), "verdict", None)
    if callable(verdict):
        try:
            verdict(force=True)
        except Exception:
            return False
    return adapter_zero_spend_verified(adapter)


class Agent:
    def __init__(self, db: Database, cloud: Any, adapters: Dict[str, Any], home: Path,
                 access_token: Callable[[], str], prepare_workspace: Callable = worktree.ensure,
                 heartbeat_interval: float = 30,
                 zero_spend_verified: Callable[[Any, Dict[str, Any]], bool] = _capability_zero_spend,
                 pool_binding: Callable[[str], Any] = _default_pool_binding,
                 inventory: Callable[[], Any] = None, quota_interval: float = 300,
                 log: Callable[[str], None] = None, on_revoked: Callable[[], None] = None,
                 upload_output_tail: bool = True, max_parallel: int = 1, workspace_wait: float = 0,
                 max_parallel_per_tool: Dict[str, int] = None, check_env_drop=(), check_env_keep=(),
                 report_protocol: bool = False, health_check: Callable[[], Dict[str, Any]] = None,
                 health_interval: float = HEALTH_INTERVAL_SECONDS,
                 reset_credits: Callable[[], list] = None,
                 reset_credits_interval: float = RESET_CREDITS_INTERVAL_SECONDS,
                 sleep_guard: Any = None, user_home: str = None):
        self.db, self.cloud, self.adapters = db, cloud, adapters
        # Protocol 2: the inventory carries protocol/agent version, the local
        # pause, the self-check and the reset credits.
        self.report_protocol = report_protocol
        self._health_check = health_check
        self.health_interval = health_interval
        self._health: Dict[str, Any] = None
        self._last_health = None
        self._health_due = True
        self._reset_credits_reader = reset_credits
        self.reset_credits_interval = reset_credits_interval
        self._reset_credits = None
        self._last_reset_credits = None
        self._last_inventory_base = None
        # Keeps the Mac awake (caffeinate) while any job runs; None: never.
        self._sleep = sleep_guard
        # The user's home for the local permission rules (credential dirs).
        self.user_home = user_home
        self._controls_lock = threading.Lock()
        self._controls: Dict[str, RunControl] = {}
        # Jobs run at once (config `max_parallel`); each holds one of that
        # many coding slots. 1 keeps the loop strictly sequential.
        self.max_parallel = max(1, int(max_parallel or 1))
        # AI jobs additionally hold one of their tool's own slots (config
        # `max_parallel_per_tool`; a tool not named gets 1). None: no
        # per-tool limit, only the overall one.
        self.max_parallel_per_tool = (None if max_parallel_per_tool is None else
                                      {str(k): max(1, int(v)) for k, v in max_parallel_per_tool.items()})
        # config `check_env_drop` / `check_env_keep`: extra variables removed
        # from, or secret-named ones kept in, a check command's environment.
        self.check_env_drop, self.check_env_keep = list(check_env_drop or ()), list(check_env_keep or ())
        # Seconds a job waits for another job's worktree preparation in the
        # same repository before it is deferred.
        self.workspace_wait = max(0.0, float(workspace_wait or 0))
        # Parallel jobs share the refresh-token rotation and the outbox.
        token_lock = threading.Lock()
        def locked_token():
            with token_lock:
                return access_token()
        self._flush_lock = threading.Lock()
        self._maintain_due = False
        # config `upload_output_tail`: false keeps run logs on this computer.
        self.upload_output_tail = upload_output_tail
        self.home, self.access_token = Path(home), locked_token
        self.prepare_workspace = prepare_workspace
        self.heartbeat_interval = heartbeat_interval
        self._zero_spend = zero_spend_verified
        self._pool_binding = pool_binding
        # Periodic upkeep: quota refresh at most every quota_interval seconds
        # (forced after a run), and a re-push of the tool inventory when it
        # changed (a tool logged out, a workspace was added).
        self._inventory = inventory
        self.quota_interval = quota_interval
        self._last_quota = 0.0
        self._last_inventory = None
        # One line per upkeep round so a silent None from an adapter is visible
        # in daemon.log; never includes tokens or exception text with paths.
        self.log = log or (lambda message: None)
        # Called once when Valley says this computer's binding was revoked
        # (unbound from the phone): the caller forgets the local credentials.
        self._on_revoked = on_revoked or (lambda: None)
        # Shutdown: once set, nothing new is claimed or spawned, and the
        # cancel event of every running job is set (its process group killed).
        self._stop = threading.Event()
        self._jobs_lock = threading.Lock()
        self._running = {}  # id(cancel event) → cancel event

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Stop claiming and cancel every running job. Safe from a signal
        handler: it only sets events."""
        self._stop.set()
        with self._jobs_lock:
            events = list(self._running.values())
        for event in events:
            event.set()

    def _track(self, cancel_event: threading.Event) -> None:
        with self._jobs_lock:
            self._running[id(cancel_event)] = cancel_event
            first = len(self._running) == 1
        if first and self._sleep is not None:
            try:
                self._sleep.start()
            except Exception as exc:
                self.log("sleep prevention failed: %s" % exc.__class__.__name__)
            self._maintain_due = True
        if self._stop.is_set():
            cancel_event.set()

    def _untrack(self, cancel_event: threading.Event) -> None:
        with self._jobs_lock:
            self._running.pop(id(cancel_event), None)
            idle = not self._running
        if idle and self._sleep is not None:
            try:
                self._sleep.stop()
            except Exception:
                pass
            self._maintain_due = True

    def _stop_reason(self) -> str:
        return STOPPED if self._stop.is_set() else ""

    def _cancel_reason(self, cancel_event) -> str:
        """Why a set cancel event stopped the run: shutdown or a cancel."""
        if not cancel_event.is_set():
            return ""
        return STOPPED if self._stop.is_set() else "cancelled"

    @property
    def db(self) -> Database:
        """The caller's database handle. SQLite connections stay on their
        thread: a parallel job's worker opens its own on the same file."""
        if threading.get_ident() == self._db_owner:
            return self._db
        local = getattr(self._db_local, "db", None)
        if local is None:
            path = getattr(self._db, "path", None)
            if path is None:
                return self._db
            local = self._db_local.db = Database(path)
        return local

    @db.setter
    def db(self, value: Database) -> None:
        self._db = value
        self._db_owner = threading.get_ident()
        self._db_local = threading.local()

    def maintain(self, now: float = None, force: bool = False) -> None:
        if threading.get_ident() != self._db_owner:
            # Upkeep (and the inventory callable's own db handle) belongs to
            # the loop's thread; a worker only asks for it.
            self._maintain_due = True
            return
        now = now if now is not None else time.time()
        self._refresh_health(now)
        self._refresh_reset_credits(now)
        due = force or now - self._last_quota >= self.quota_interval
        if due:
            self._last_quota = now
            try:
                counts = self.report_quota_counts(now)
                self.log("quota: " + ", ".join("%s %d" % (p, n) for p, n in counts.items()))
                runner_state.record(self.home, quota_reported_at=int(now), quota_samples=sum(counts.values()))
            except Exception as exc:
                self.log("quota report failed: %s" % exc.__class__.__name__)
        if self._inventory is None:
            return
        if due or self._last_inventory_base is None:
            try:
                self._last_inventory_base = self._inventory()
            except Exception as exc:
                self.log("inventory build failed: %s" % exc.__class__.__name__)
                return
        elif not self.report_protocol:
            return
        # Between full rounds only the protocol-2 extras (pause, self-check,
        # sleep prevention) are compared, so a pause reaches Valley at once.
        current = self._last_inventory_base
        extras = self.inventory_extras() if self.report_protocol else None
        if (current, extras) != self._last_inventory:
            try:
                if extras is None:
                    self.cloud.update_inventory(self.access_token(), *current)
                else:
                    self.cloud.update_inventory(self.access_token(), *current, extras=extras)
                self._last_inventory = (current, extras)
                runner_state.record(self.home, inventory_pushed_at=int(now), inventory_workspaces=len(current[0]),
                                    inventory_tools=len(current[1]))
                self.log("inventory pushed: %d workspaces, %d tools" % (len(current[0]), len(current[1])))
            except Exception as exc:
                self._note_cloud_error(exc, "inventory")
                self.log("inventory push failed: %s" % exc.__class__.__name__)

    def inventory_extras(self) -> Dict[str, Any]:
        """The protocol-2 inventory fields (docs/remote-control-protocol.md §3/§4)."""
        extras = {"protocol_version": PROTOCOL_VERSION, "agent_version": __version__,
                  "accepting_local": not self.locally_paused()}
        if self._health is not None:
            health = dict(self._health)
            health["sleep_prevention"] = self.sleep_state()
            extras["health"] = health
        if self._reset_credits:
            extras["reset_credits"] = self._reset_credits
        return extras

    def sleep_state(self) -> str:
        active = self._sleep is not None and getattr(self._sleep, "active", lambda: False)()
        return "active" if active else "inactive"

    def locally_paused(self) -> bool:
        return pause.paused(self.home)

    def disk_blocked(self) -> bool:
        free = (self._health or {}).get("disk_free_gb")
        return isinstance(free, (int, float)) and free < DISK_ERROR_GB

    def _refresh_health(self, now: float) -> None:
        if self._health_check is None:
            return
        if not self._health_due and self._last_health is not None and now - self._last_health < self.health_interval:
            return
        self._health_due = False
        self._last_health = now
        try:
            health = self._health_check()
        except Exception as exc:
            self.log("self-check failed: %s" % exc.__class__.__name__)
            return
        if isinstance(health, dict):
            self._health = health
            runner_state.record(self.home, health=health, health_checked_at=int(now))

    def _refresh_reset_credits(self, now: float) -> None:
        if self._reset_credits_reader is None:
            return
        if self._last_reset_credits is not None and now - self._last_reset_credits < self.reset_credits_interval:
            return
        self._last_reset_credits = now
        try:
            entries = self._reset_credits_reader()
        except Exception as exc:
            self.log("reset credits read failed: %s" % exc.__class__.__name__)
            return
        if isinstance(entries, list):
            self._reset_credits = entries
            runner_state.record(self.home, reset_credits=entries)

    def _note_cloud_error(self, exc: Exception, where: str) -> None:
        if isinstance(exc, CloudError) and exc.code == VERSION_REJECTED_CODE:
            audit.record(self.home, "version_rejected", where=where, message=str(exc)[:200])

    def run_once(self) -> str:
        claim = self._next_claim()
        if isinstance(claim, str):
            return claim
        return self.handle(claim)

    def _next_claim(self):
        """A claim, or "revoked" / "unpaired" / "idle" / "paused" (local
        pause, running jobs continue) / "disk_full" (self-check error)."""
        try:
            self.flush_outbox()
            if self.locally_paused():
                return "paused"
            if self.disk_blocked():
                return "disk_full"
            token = self.access_token()
            claim = self.cloud.claim(token)
        except CloudError as exc:
            if exc.status == 401:
                self._on_revoked()
                self.log("此电脑已在手机上解绑（或授权失效）；本机凭据已清除。重新运行 `timetrace cloud login` 即可再次绑定。")
                return "revoked"
            raise
        except RuntimeError as exc:
            if "not paired" in str(exc):
                return "unpaired"
            raise
        return claim or "idle"

    @staticmethod
    def _first_free(locks):
        """The first lock of `locks` that can be taken. A lock with a crash
        fence is skipped (it stays fenced for manual recovery); only when none
        is free does a fence surface, as UnclearedOwner, so the daemon log
        names it."""
        busy = fenced = None
        for lock in locks:
            try:
                return lock.acquire()
            except UnclearedOwner as exc:
                fenced = fenced or exc
            except LockBusy as exc:
                busy = exc
        raise fenced or busy

    def _acquire_slot(self, tool: str = None):
        """One of the tool's own slots (when `tool` is given and per-tool
        limits are configured), then the first free coding slot of
        max_parallel. Both are held until release(); a failure never leaves
        the first one taken. Raises ToolBusy / LockBusy / UnclearedOwner."""
        held = []
        if tool is not None and self.max_parallel_per_tool is not None:
            limit = self.max_parallel_per_tool.get(tool, 1)
            try:
                held.append(self._first_free([tool_slot_lock(self.home, tool, i) for i in range(limit)]))
            except LockBusy as exc:
                raise ToolBusy(tool, exc) from exc
        try:
            held.append(self._first_free([coding_slot_lock(self.home) if i == 0 else coding_slot_lock(self.home, i)
                                          for i in range(self.max_parallel)]))
        except BaseException:
            for lock in held:
                lock.release()
            raise
        return _Slots(held)

    @staticmethod
    def _busy(job_id, exc) -> str:
        """The outcome of a job deferred because no slot was free."""
        what = "tool busy: %s" % exc.tool if isinstance(exc, ToolBusy) else "runner busy"
        cause = exc.cause if isinstance(exc, ToolBusy) else exc
        return "job %s → deferred (%s)" % (job_id, what) + ("; " + str(cause) if isinstance(cause, UnclearedOwner) else "")

    def _acquire_workspace(self, path, deadline):
        """The workspace lock, waiting up to workspace_wait seconds (never past
        the lease) while another job prepares its worktree."""
        until = min(time.time() + self.workspace_wait, deadline)
        while True:
            try:
                return workspace_lock(self.home, path).acquire()
            except UnclearedOwner:
                raise
            except LockBusy:
                if time.time() + .2 >= until:
                    raise
                time.sleep(.2)

    def handle(self, claim) -> str:
        job = claim["job"]
        job_id = job["id"]
        existing = self.db.get_remote_claim(job_id)
        if job.get("status") == "cancelled" or job.get("desired_action") == "cancel":
            self.db.save_remote_claim(claim)
            return self._blocked(claim, job.get("plan_id") or job_id, "cancelled", seq=2 if existing else 1)
        if (existing and existing["attempt_id"] == claim["attempt_id"]
                and existing["state"] in ("launching", "running", "reported")):
            return "job %s → duplicate ignored" % job_id
        self.db.save_remote_claim(claim)  # durable before any local process starts
        if job.get("kind") == "import_parse":
            # No workspace: the run reads nothing on this computer.
            return self._import_parse(claim)
        workspace = self.db.get_workspace(job.get("workspace_id"))
        registered = Path(workspace["path"]) if workspace else None
        ws_kind = (workspace or {}).get("kind") or folder.GIT
        # A git workspace must still be a repository; a folder workspace only
        # a directory (and only when it was registered as one).
        if (not registered or not registered.is_dir()
                or str(registered.resolve()) != workspace["path"]
                or ws_kind not in folder.KINDS
                or (ws_kind == folder.GIT and not worktree.is_git_repo(workspace["path"]))):
            self._report(claim, [{"seq": 1, "type": "failed", "message": "unknown workspace"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (unknown workspace)" % job_id
        kind = job.get("kind") or "task"
        if kind not in JOB_KINDS:
            self._report(claim, [{"seq": 1, "type": "failed", "message": "unsupported job kind"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (unsupported job kind)" % job_id
        self.home.joinpath("logs").mkdir(parents=True, exist_ok=True)
        # The job id comes from the server: keep it from naming a path.
        log_file = str(self.home / "logs" / ("remote-%s.log" % _safe_id(job_id)))
        if kind == "check":
            # A locally registered command; no AI tool is involved.
            return self._check_job(claim, workspace, log_file)
        provider = job.get("provider")
        adapter = self.adapters.get(provider)
        if adapter is None:
            self._report(claim, [{"seq": 1, "type": "failed", "message": "tool unavailable"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (tool unavailable)" % job_id
        if kind == "chat_turn":
            return self._chat_turn(claim, workspace, adapter, log_file)
        if kind == "review_turn":
            return self._review_turn(claim, workspace, adapter, log_file)
        if ws_kind == folder.FOLDER and not (callable(getattr(adapter, "start_folder", None))
                                             and callable(getattr(adapter, "resume_folder", None))):
            self._report(claim, [{"seq": 1, "type": "failed", "message": "folder workspace unsupported"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (folder workspace unsupported)" % job_id

        plan_key = job.get("plan_id") or job_id
        deadline = self._deadline(claim)
        reason = self._gate(adapter, job, deadline)
        if reason:
            return self._blocked(claim, plan_key, reason)

        # Hold the slot and plan locks through preparation, execution and
        # durable checkpoint/terminal event persistence. The workspace lock
        # covers a git worktree's preparation (shared .git writes) and a
        # folder workspace's whole run. A crashed owner leaves a persistent fence.
        try:
            slot = self._acquire_slot(provider)
        except LockBusy as exc:
            return self._busy(job_id, exc)
        try:
            plan = plan_lock(self.home, plan_key).acquire()
        except LockBusy as exc:
            slot.release()
            return "job %s → deferred (plan busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
        try:
            ws_lock = self._acquire_workspace(workspace["path"], deadline)
        except LockBusy as exc:
            plan.release()
            slot.release()
            return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
        try:
            # Another job for this Plan may have finished between claim and
            # lock acquisition. Recovery evidence is authoritative only here.
            try:
                checkpoint = self.db.get_checkpoint(plan_key)
            except (TypeError, ValueError, KeyError):
                return self._blocked(claim, plan_key, "checkpoint unreadable; manual recovery required")
            latest = self.db.get_remote_claim(job_id)
            if checkpoint:
                if checkpoint.plan_id != plan_key:
                    reason = "checkpoint plan identity differs from stored key"
                else:
                    reason = checkpoint_problem(checkpoint, provider, job["tool_profile_id"],
                                                workspace["path"], adapter_capabilities(adapter).get("can_resume") is True)
                if reason:
                    return self._blocked(claim, plan_key, reason, checkpoint)
            elif (self.db.plan_started(plan_key) or (existing and existing["state"] != "claimed")
                  or (latest and latest["state"] != "claimed")):
                return self._blocked(claim, plan_key, "checkpoint missing for previously started job; manual recovery required")
            return self._execute(claim, workspace, adapter, checkpoint, plan_key, log_file, deadline, ws_lock)
        finally:
            ws_lock.release()
            plan.release()
            slot.release()

    @staticmethod
    def _deadline(lease):
        try:
            # Go RFC3339Nano emits 1–9 fractional digits. Python 3.9 accepts
            # only 3 or 6; truncate nanoseconds (never extend authority) and
            # pad to microseconds before parsing the unchanged timezone.
            value = re.sub(r"\.(\d{1,9})(?=Z$|[+-]\d{2}:\d{2}$)",
                           lambda match: "." + match.group(1)[:6].ljust(6, "0"),
                           lease["lease_expires_at"])
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError, AttributeError):
            return 0.0

    def _gate(self, adapter, job, deadline):
        reason = deny_reason(DispatchGate(
            cancelled=job.get("status") == "cancelled" or job.get("desired_action") == "cancel",
            lease_valid=deadline > time.time(), runner_online=True, dependencies_ready=True,
            zero_spend_verified=self._zero_spend(adapter, job),
        ))
        if reason:
            return reason
        if adapter_capabilities(adapter).get("can_dispatch") is not True:
            return "dispatch_unavailable"
        return ""

    def _renew(self, claim, adapter, deadline, gate=None, control=None):
        """`gate(deadline)` → block reason; default: the AI job gate. A
        protocol-2 response's controls[] / approvals[] go to `control`."""
        gate = gate or (lambda at: self._gate(adapter, claim["job"], at))
        # Once a lease expires the old owner cannot regain execution authority.
        reason = gate(deadline)
        if reason:
            return deadline, reason
        done = threading.Event()
        answer = []
        def request():
            try:
                answer.append(self.cloud.renew(self.access_token(), claim["attempt_id"], claim["lease_epoch"]))
            except Exception as exc:
                self._note_cloud_error(exc, "renew")
            finally:
                done.set()
        # A blocked HTTP/credential call must not hold the writer past expiry.
        # The abandoned daemon can finish its request but cannot grant a lease
        # or mutate the claim after this wait has failed.
        threading.Thread(target=request, daemon=True).start()
        if not done.wait(max(0, deadline - time.time())):
            return deadline, "lease_expired"
        lease = answer[0] if answer else None
        if not isinstance(lease, dict):
            return deadline, "lease renewal failed"
        if lease.get("desired_action") == "cancel":
            claim["job"]["desired_action"] = "cancel"
        if control is not None:
            control.apply_lease(lease)
        renewed = self._deadline(lease)
        # Include time spent in the renewal request: a late response cannot
        # bridge an interval in which this owner no longer held a lease.
        if time.time() >= deadline and claim["job"].get("desired_action") != "cancel":
            return deadline, "lease_expired"
        return renewed, gate(renewed)

    def _blocked(self, claim, plan_key, reason, checkpoint=None, seq=1, observed_at=None, note=""):
        """`note`: a folder run changed files outside its output directory
        before it was stopped; named in the event and never resumable."""
        cancelled = reason == "cancelled"
        if cancelled or note:
            self.db.delete_checkpoint(plan_key)
        elif checkpoint and checkpoint.plan_id == plan_key:
            # The database lookup key is trusted; malformed embedded identity
            # must not redirect a write or prevent the waiting_input event.
            checkpoint.reason = reason
            self.db.save_checkpoint(checkpoint)
        event_type = "cancelled" if cancelled else "waiting_input"
        message = "dispatch blocked: %s" % reason + ("; " + note if note else "")
        self._report(claim, [{"seq": seq, "type": event_type, "message": message,
                              "observed_at": observed_at or self._observed_now()}])
        self.db.update_remote_claim(claim["job"]["id"], "reported")
        if cancelled:
            return "job %s → cancelled" % claim["job"]["id"]
        if reason.startswith("checkpoint") and not note:
            return "job %s → waiting_input (%s)" % (claim["job"]["id"], reason)
        return "job %s → blocked (%s)" % (claim["job"]["id"], reason)

    def _fenced(self, job_id, plan_key, reason, note=""):
        """The lease was lost after the spawn: no event can be sent under it.
        A folder run's changes outside its output directory are logged."""
        self.db.delete_checkpoint(plan_key)
        self.db.update_remote_claim(job_id, "fenced")
        outcome = "job %s → fenced (%s)" % (job_id, reason)
        if note:
            outcome += "; " + note
            self.log(outcome)
        return outcome

    def _execute(self, claim, workspace, adapter, checkpoint, plan_key, log_file, deadline, ws_lock=None):
        job = claim["job"]
        job_id = job["id"]
        resuming = checkpoint is not None
        session_id = checkpoint.provider_session_id if resuming else str(uuid.uuid4())
        # Reuse the original execution worktree even if recovery has a new job id.
        execution_job = checkpoint.job_id if resuming else job_id
        local_id = local_task_id(execution_job)
        reason = self._stop_reason() or self._gate(adapter, job, deadline)
        if reason:
            return self._blocked(claim, plan_key, reason, checkpoint)
        cancel_event = threading.Event()
        # Protocol 2: interrupt / append / approvals for this task run.
        control = RunControl(job_id, self.home, cancel_event, user_home=self.user_home, log=self.log)
        control.started = False
        self._track(cancel_event)
        with self._controls_lock:
            self._controls[claim["attempt_id"]] = control
        try:
            return self._execute_tracked(claim, workspace, adapter, checkpoint, plan_key, log_file,
                                         deadline, ws_lock, cancel_event, resuming, session_id,
                                         execution_job, local_id, control)
        finally:
            control.close()
            with self._controls_lock:
                self._controls.pop(claim["attempt_id"], None)
            self._untrack(cancel_event)

    def _renew_wait(self, reason, deadline, control=None):
        """Seconds until the next lease renewal (Valley's renew_after_seconds
        while a protocol-2 job runs)."""
        if reason:
            return self.heartbeat_interval
        wait = min(self.heartbeat_interval, max(.001, (deadline - time.time()) / 2))
        if control is not None and control.renew_after:
            wait = min(wait, control.renew_after)
        return wait

    @staticmethod
    def _run_prompt(job, resuming):
        """A job that continues an interrupted one (`resume_of_job_id`)
        resumes the session with the user's note instead of the task again."""
        if resuming and job.get("resume_of_job_id"):
            note = job.get("resume_note")
            note = note.strip() if isinstance(note, str) else ""
            return RESUME_PROMPT + (RESUME_NOTE + note if note else "")
        return job["prompt"]

    def _execute_tracked(self, claim, workspace, adapter, checkpoint, plan_key, log_file, deadline,
                         ws_lock, cancel_event, resuming, session_id, execution_job, local_id, control=None):
        job = claim["job"]
        job_id = job["id"]
        running_announced = False
        observed_end = None
        spawned = False
        reason = ""
        in_folder = workspace.get("kind") == folder.FOLDER
        out_name = folder.output_name(job, plan_key) if in_folder else ""
        before = None
        checked = {}
        execution_path = None
        prepared_branch = ""
        branch = None
        interactive = control is not None and getattr(adapter, "interactive_runs", False) is True

        def abort_note():
            """Once the tool was spawned in a folder workspace, the change
            check runs on every exit path (stop, cancel, fence, crash)."""
            if not (in_folder and spawned and before is not None):
                return ""
            if "note" not in checked:
                try:
                    checked["note"] = self._folder_problem(workspace["path"], out_name, before)[0]
                except Exception as exc:
                    checked["note"] = "文件夹改动检查失败：%s" % exc.__class__.__name__
            return checked["note"]

        def user_interrupted():
            """The phone's interrupt took effect (not a cancel or a stop)."""
            return (control is not None and control.interrupted and not reason and not self._stop.is_set()
                    and job.get("desired_action") != "cancel" and job.get("status") != "cancelled")

        def interrupted(result):
            """Report `interrupted_by_user`: the process group is gone, the
            worktree / branch stay, and a checkpoint lets a resume job
            continue the same session."""
            terminal_seq = 2 if running_announced else 1
            note = abort_note()
            if note:
                # A folder run that wrote outside its output directory is a
                # failure whatever stopped it, and never resumable.
                self.db.delete_checkpoint(plan_key)
                self._report(claim, [{"seq": terminal_seq, "type": "failed", "message": note,
                                      "observed_at": observed_end or self._observed_now()}])
                self.db.update_remote_claim(job_id, "reported")
                return "job %s → failed" % job_id
            sid = getattr(result, "session_id", None) or ""
            if not (isinstance(sid, str) and SESSION_ID.fullmatch(sid)):
                sid = ""
            if spawned and execution_path:
                try:
                    if in_folder:
                        head, dirty_digest = FOLDER_HEAD, folder.digest(execution_path)
                    else:
                        # Git runs here unsandboxed in a directory the model could write.
                        worktree.verify_metadata(execution_path, workspace["path"])
                        head, dirty_digest = worktree.snapshot(execution_path)
                    self.db.save_checkpoint(Checkpoint(
                        plan_id=plan_key, job_id=execution_job, attempt_id=claim["attempt_id"],
                        tool_profile_id=job["tool_profile_id"], provider=job["provider"],
                        provider_session_id=sid, canonical_workspace=workspace["path"],
                        execution_path=execution_path, git_head=head, dirty_paths_digest=dirty_digest,
                        output_path=str(Path(log_file).resolve()), last_output_offset=Path(log_file).stat().st_size,
                        completed_criteria=[], side_effect_summary="", reason="interrupted_by_user",
                        branch="" if in_folder else (branch or ""),
                    ))
                except Exception as exc:
                    # Still an interrupt; a resume then stops for manual recovery.
                    self.db.delete_checkpoint(plan_key)
                    self.log("job %s: checkpoint after interrupt failed: %s" % (job_id, exc.__class__.__name__))
            elif checkpoint is not None and checkpoint.plan_id == plan_key:
                checkpoint.reason = "interrupted_by_user"
                self.db.save_checkpoint(checkpoint)
            event = {"seq": terminal_seq, "type": "interrupted_by_user", "message": INTERRUPTED_MESSAGE,
                     "observed_at": observed_end or self._observed_now()}
            if not in_folder and prepared_branch:
                event["branch"] = prepared_branch
            if sid:
                event["provider_session_id"] = sid
            self._report(claim, [event])
            self.db.update_remote_claim(job_id, "reported")
            if getattr(result, "samples", None):
                self._post_samples(job["provider"], adapter, result.samples)
            self.maintain(force=True)
            return "job %s → interrupted" % job_id

        try:
            with _job_pool(cancel_event) as pool:
                if in_folder:
                    # No worktree, branch or commit: the task's own directory.
                    preparation = pool.submit(lambda: (folder.prepare_output(workspace["path"], out_name), ""))
                else:
                    # Sub-task jobs name their branch (timetrace/<stage>/<sub>), made
                    # unique per job. A resume stays on the checkpoint's branch
                    # (older checkpoints: the job's name as it was then used).
                    if resuming and checkpoint.branch is not None:
                        branch = checkpoint.branch or None
                    elif resuming:
                        branch = job.get("branch_name") if worktree.valid_task_branch(job.get("branch_name")) else None
                    else:
                        branch = worktree.unique_branch(job.get("branch_name"), execution_job)
                    extra = {"branch": branch} if branch else {}
                    preparation = pool.submit(self.prepare_workspace, workspace["path"], local_id,
                                              self.home, workspace["default_branch"], **extra)
                reason = ""
                while True:
                    try:
                        wait = self.heartbeat_interval if reason else min(self.heartbeat_interval, max(.001, (deadline - time.time()) / 2))
                        execution_path, prepared_branch = preparation.result(timeout=wait)
                        break
                    except TimeoutError:
                        if not reason:
                            deadline, reason = self._renew(claim, adapter, deadline, None, control)
                        # Preparation may still be using git. Keep locks until it
                        # stops; a failed renewal never authorises a later spawn.
                if reason:
                    return self._blocked(claim, plan_key, reason, checkpoint)
                execution_path = str(Path(execution_path).resolve())
                if control is not None:
                    # Permission prompts are judged against this directory.
                    control.root = execution_path
                if resuming:
                    reason = checkpoint_problem(checkpoint, job["provider"], job["tool_profile_id"],
                                                workspace["path"], adapter_capabilities(adapter).get("can_resume") is True)
                    if not reason and checkpoint.execution_path != execution_path:
                        reason = "checkpoint execution directory changed"
                    if in_folder:
                        if not reason and (checkpoint.git_head != FOLDER_HEAD
                                           or folder.digest(execution_path) != checkpoint.dirty_paths_digest):
                            reason = "checkpoint output directory changed"
                    else:
                        if not reason:
                            try:
                                worktree.verify_metadata(execution_path, workspace["path"])
                            except worktree.MetadataTampered as exc:
                                reason = "checkpoint worktree metadata changed: %s" % exc
                        if not reason and not worktree.same_repository(execution_path, workspace["path"]):
                            reason = "checkpoint repository changed"
                        if not reason and worktree.snapshot(execution_path) != (checkpoint.git_head, checkpoint.dirty_paths_digest):
                            reason = "checkpoint git state changed"
                    if reason:
                        return self._blocked(claim, plan_key, reason, checkpoint)

                # Check authority again after potentially slow git operations.
                reason = self._stop_reason() or self._gate(adapter, job, deadline)
                if not reason and resuming and adapter_capabilities(adapter).get("can_resume") is not True:
                    reason = "checkpoint resume capability changed"
                if reason:
                    return self._blocked(claim, plan_key, reason, checkpoint)
                if in_folder:
                    # Everything but this run's own timetrace-out/<name>/ must look the same afterwards.
                    before = folder.snapshot(workspace["path"], own=out_name)
                elif ws_lock is not None:
                    # The worktree is ready: other jobs in this repository may
                    # prepare theirs while this one runs.
                    ws_lock.release()
                self.db.update_remote_claim(job_id, "launching")
                # Renew before execution; transport wait is not AI activity.
                deadline, reason = self._renew(claim, adapter, deadline, None, control)
                if not reason and resuming and adapter_capabilities(adapter).get("can_resume") is not True:
                    reason = "checkpoint resume capability changed"
                if reason:
                    return self._blocked(claim, plan_key, reason, checkpoint)
                self.db.mark_plan_started(plan_key, job_id, claim["attempt_id"])
                Path(log_file).touch(exist_ok=True)
                # A fresh run never inherits an earlier run's result.json.
                results.prepare_out_dir(execution_path, fresh=not resuming)
                extra_kw = {"control": control} if interactive else {}
                if in_folder:
                    folder_run = adapter.resume_folder if resuming else adapter.start_folder
                    def run(prompt, cwd, session, log, cancel):
                        return folder_run(prompt, cwd, workspace["path"], session, log, cancel, **extra_kw)
                    def followup_run(prompt, cwd, session, log, cancel):
                        return adapter.resume_folder(prompt, cwd, workspace["path"], session, log, cancel, **extra_kw)
                else:
                    first = adapter.resume if resuming else adapter.start
                    def run(prompt, cwd, session, log, cancel):
                        return first(prompt, cwd, session, log, cancel, **extra_kw)
                    def followup_run(prompt, cwd, session, log, cancel):
                        return adapter.resume(prompt, cwd, session, log, cancel, **extra_kw)
                prompt = self._run_prompt(job, resuming)
                observed_start = None
                adapter_entered = threading.Event()
                def authority():
                    if reason:
                        return reason
                    boundary_reason = self._stop_reason() or self._gate(adapter, job, deadline)
                    if not boundary_reason and resuming and adapter_capabilities(adapter).get("can_resume") is not True:
                        boundary_reason = "checkpoint resume capability changed"
                    return boundary_reason
                def with_followups(result):
                    """Instructions appended while the tool could not take
                    them run as resumed turns in this job, with the same
                    sandbox / read-only parameters and the same gates."""
                    while control is not None:
                        followup = control.next_followup(ok=bool(getattr(result, "ok", False)))
                        if followup is None:
                            return result
                        control_id, text = followup
                        refusal = self._stop_reason() or self._gate(adapter, job, deadline)
                        if not refusal and adapter_capabilities(adapter).get("can_resume") is not True:
                            refusal = "resume unsupported"
                        session = getattr(result, "session_id", None) or session_id
                        if not refusal and not (isinstance(session, str) and SESSION_ID.fullmatch(session)):
                            refusal = "no session to resume"
                        if refusal:
                            control.followup_refused(control_id, "未执行：%s" % refusal)
                            return result
                        control.followup_started(control_id)
                        following = followup_run(text, execution_path, session, log_file, cancel_event)
                        following.samples = list(getattr(result, "samples", None) or []) + list(following.samples or [])
                        result = following
                    return result
                def invoke():
                    nonlocal reason, spawned, observed_start, observed_end
                    # Executor scheduling is also a delay: fence inside the
                    # worker, at the call that can actually create a process.
                    if reason or cancel_event.is_set():
                        return None
                    reason = self._stop_reason() or self._gate(adapter, job, deadline)
                    if not reason and resuming and adapter_capabilities(adapter).get("can_resume") is not True:
                        reason = "checkpoint resume capability changed"
                    if reason:
                        return None
                    with spawn_authority(authority):
                        enforce_spawn_authority(cancel_event)
                        spawned = True
                        observed_start = self._observed_now()
                        adapter_entered.set()
                        try:
                            return with_followups(run(prompt, execution_path, session_id, log_file, cancel_event))
                        finally:
                            if control is not None:
                                control.next_followup(ok=False)  # the run is over either way
                            observed_end = self._observed_now()
                future = pool.submit(invoke)
                future.add_done_callback(lambda _: adapter_entered.set())
                adapter_entered.wait()
                if observed_start is not None:
                    # SQLite and outbox writes stay on their owning thread.
                    # Execution timestamps are captured only by the worker at
                    # the call boundaries, independently of this durable IO.
                    self.db.update_remote_claim(job_id, "running")
                    self._report(claim, [{"seq": 1, "type": "running", "message": "started",
                                          "observed_at": observed_start}], flush=False)
                    running_announced = True
                    if control is not None:
                        control.started = True
                if control is not None:
                    future.add_done_callback(lambda _: control.wakeup.set())
                next_renew = time.time() + self._renew_wait(reason, deadline, control)
                while True:
                    try:
                        if control is None:
                            result = future.result(timeout=max(.001, next_renew - time.time()))
                        else:
                            # Wake for approval requests / acknowledgements too.
                            control.wakeup.wait(max(.001, min(next_renew - time.time(), 1.0)))
                            if control.started and control.wakeup.is_set() and not future.done():
                                self._report(claim, [])
                            result = future.result(timeout=0)
                        break
                    except TimeoutError:
                        if time.time() >= next_renew:
                            if not reason:
                                deadline, reason = self._renew(claim, adapter, deadline, None, control)
                                if reason:
                                    cancel_event.set()
                            next_renew = time.time() + self._renew_wait(reason, deadline, control)
                if user_interrupted() and not getattr(result, "ok", False):
                    return interrupted(result)
                # An interrupt that lost the race to completion is not a cancel.
                cancelled_by = "" if user_interrupted() else self._cancel_reason(cancel_event)
                reason = reason or cancelled_by or self._gate(adapter, job, deadline)
                if reason:
                    if not spawned or reason in ("cancelled", STOPPED):
                        return self._blocked(claim, plan_key, reason, checkpoint,
                                             seq=2 if running_announced else 1, observed_at=observed_end,
                                             note=abort_note())
                    return self._fenced(job_id, plan_key, reason, abort_note())
        except Exception as exc:
            terminal_seq = 2 if running_announced else 1
            if user_interrupted():
                return interrupted(None)
            note = abort_note()
            # Cancellation dominates an adapter's shutdown exception. Lease
            # fencing also must not turn into a resumable recovery failure.
            if (job.get("desired_action") == "cancel" or job.get("status") == "cancelled"
                    or reason == "cancelled"
                    or (cancel_event.is_set() and not reason and not self._stop.is_set())
                    or (not reason and isinstance(exc, DispatchDenied) and str(exc) == "cancelled"
                        and not self._stop.is_set())):
                return self._blocked(claim, plan_key, "cancelled", checkpoint, seq=terminal_seq,
                                     observed_at=observed_end, note=note)
            if reason == STOPPED or (not reason and self._stop.is_set()
                                     and (cancel_event.is_set() or isinstance(exc, DispatchDenied))):
                return self._blocked(claim, plan_key, STOPPED, checkpoint, seq=terminal_seq,
                                     observed_at=observed_end, note=note)
            if reason and cancel_event.is_set():
                return self._fenced(job_id, plan_key, reason, note)
            if isinstance(exc, DispatchDenied):
                return self._blocked(claim, plan_key, str(exc), checkpoint, seq=terminal_seq,
                                     observed_at=observed_end, note=note)
            # Invalid recovery evidence must survive for manual diagnosis.
            if resuming and not note:
                return self._blocked(claim, plan_key, "checkpoint recovery failed: %s" % exc, checkpoint, seq=terminal_seq, observed_at=observed_end)
            if note:
                self.db.delete_checkpoint(plan_key)
            if isinstance(exc, worktree.BranchConflict):
                message = str(exc)
            else:
                message = "adapter/preparation crashed: %s" % exc + ("; " + note if note else "")
            self._report(claim, [{"seq": terminal_seq, "type": "failed", "message": message,
                                  "observed_at": observed_end or self._observed_now()}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → failed" % job_id

        # Every run may carry rate-limit readings; a successful run is the
        # freshest evidence of remaining quota, so upload them all.
        if result.samples:
            self._post_samples(job["provider"], adapter, result.samples)
        folder_note = ""
        if in_folder:
            problem, folder_note = self._folder_problem(workspace["path"], out_name, before)
            if problem:
                # Whatever the tool reported, a run that wrote outside its
                # output directory is a failure and never resumable.
                self.db.delete_checkpoint(plan_key)
                self._report(claim, [{"seq": 2, "type": "failed", "message": problem,
                                      "output_tail": tail_text(log_file), "observed_at": observed_end}])
                self.db.update_remote_claim(job_id, "reported")
                self.maintain(force=True)
                return "job %s → failed" % job_id
        if result.ok:
            self.db.delete_checkpoint(plan_key)
            event = {"seq": 2, "type": "completed", "message": "completed",
                     "result_summary": redact(result.output or "completed")[:1000],
                     "output_tail": tail_text(log_file)}
            head = (lambda: None) if in_folder else (lambda: worktree.head(execution_path, workspace["path"]))
            try:
                structured = results.collect(execution_path, head=head)
            except Exception:
                # Never lose the completion over a malformed result file.
                structured = {"valid": False, "artifacts": [], "result": None}
            if structured is not None:
                event["artifacts"] = structured["artifacts"]
                if structured["result"] is not None:
                    event["result"] = structured["result"]
                if not structured["valid"]:
                    event["message"] += "; " + results.INVALID_NOTE
                    event["result_invalid"] = True
            if not in_folder and prepared_branch:
                # The branch review and check jobs name (`branch_name`).
                event["branch"] = prepared_branch
            if in_folder:
                own = {"kind": "folder", "ref": "%s/%s" % (folder.OUTPUT_ROOT, out_name),
                       "content": folder.listing(execution_path)}
                event["artifacts"] = results.with_folder(own, event.get("artifacts") or [])
                if folder_note:
                    event["message"] += "; " + folder_note
            outcome = "awaiting_review"
        elif result.blocked:
            try:
                if in_folder:
                    head, dirty_digest = FOLDER_HEAD, folder.digest(execution_path)
                else:
                    # Git runs here unsandboxed in a directory the model could write.
                    worktree.verify_metadata(execution_path, workspace["path"])
                    head, dirty_digest = worktree.snapshot(execution_path)
                cp = Checkpoint(
                    plan_id=plan_key, job_id=execution_job, attempt_id=claim["attempt_id"],
                    tool_profile_id=job["tool_profile_id"], provider=job["provider"],
                    # Only an adapter-confirmed native session is resumable.
                    provider_session_id=result.session_id or "",
                    canonical_workspace=workspace["path"], execution_path=execution_path,
                    git_head=head, dirty_paths_digest=dirty_digest, output_path=str(Path(log_file).resolve()),
                    last_output_offset=Path(log_file).stat().st_size, completed_criteria=[],
                    side_effect_summary=(result.output or "")[:200], reason="waiting_quota",
                    branch="" if in_folder else (branch or ""),
                )
                self.db.save_checkpoint(cp)
            except Exception as exc:
                return self._blocked(claim, plan_key, "checkpoint capture failed: %s" % exc, checkpoint, seq=2, observed_at=observed_end)
            outcome = "waiting_quota"
            event = {"seq": 2, "type": outcome, "message": redact(result.error or "quota blocked")[:1000]}
        else:
            self.db.delete_checkpoint(plan_key)
            outcome = "failed"
            event = {"seq": 2, "type": outcome, "message": redact(result.error or "exit %s" % result.exit_code)[:1000],
                     "output_tail": tail_text(log_file)}
        event["observed_at"] = observed_end
        self._report(claim, [event])
        self.db.update_remote_claim(job_id, "reported")
        # A finished run changed the quota picture: refresh it right away.
        self.maintain(force=True)
        return "job %s → %s" % (job_id, outcome)

    @staticmethod
    def _folder_problem(workspace_path, out_name, before):
        """(failure message or "", note) for a finished folder run."""
        if not folder.output_intact(workspace_path, out_name):
            return "输出目录 %s/%s 已被替换或删除" % (folder.OUTPUT_ROOT, out_name), ""
        if before is None:
            return "", ""
        after = folder.snapshot(workspace_path, own=out_name)
        note = folder.CAPPED_NOTE if (before.capped or after.capped) else ""
        changed = folder.changes(before, after)
        if changed:
            message = folder.change_message(changed)
            return (message + "（" + note + "）" if note else message), note
        return "", note

    def _chat_turn(self, claim, workspace, adapter, log_file):
        """A phone conversation turn: the local AI answers read-only in the
        workspace's main directory. No worktree, no commit, no checkpoint;
        the same lease, zero-spend gate and spawn fencing as a task run."""
        job = claim["job"]
        job_id = job["id"]
        chat = getattr(adapter, "chat", None)
        if not callable(chat):
            self._report(claim, [{"seq": 1, "type": "failed", "message": "chat unsupported"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (chat unsupported)" % job_id
        session = job.get("provider_session_id") or ""
        if not isinstance(session, str) or (session and not SESSION_ID.fullmatch(session)):
            self._report(claim, [{"seq": 1, "type": "failed", "message": "invalid session"}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → rejected (invalid session)" % job_id
        deadline = self._deadline(claim)
        reason = self._stop_reason() or self._gate(adapter, job, deadline)
        if reason:
            return self._blocked(claim, job_id, reason)
        try:
            slot = self._acquire_slot(job.get("provider"))
        except LockBusy as exc:
            return self._busy(job_id, exc)
        ws_lock = None
        if workspace.get("kind") == folder.FOLDER:
            # A read-only turn never conflicts with worktrees of a git
            # workspace; a folder workspace stays serialized (its task's
            # change check wants a quiet folder).
            try:
                ws_lock = workspace_lock(self.home, workspace["path"]).acquire()
            except LockBusy as exc:
                slot.release()
                return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
        try:
            return self._run_chat(claim, workspace, adapter, chat, session, log_file, deadline)
        finally:
            if ws_lock is not None:
                ws_lock.release()
            slot.release()

    def _run_chat(self, claim, workspace, adapter, chat, session, log_file, deadline):
        job = claim["job"]
        cwd = workspace["path"]  # registered, resolved main checkout

        def finish(result):
            if result.ok:
                reply = safe_reply(result.output)
                return "replied", {"seq": 2, "type": "completed", "message": "completed",
                                   "reply": reply, "provider_session_id": result.session_id or session,
                                   "result_summary": reply[:1000], "output_tail": tail_text(log_file)}
            return self._ai_unfinished(result, log_file)

        return self._run_leased(claim, adapter, None, lambda cancel: chat(job["prompt"], cwd, session, log_file, cancel),
                                deadline, finish)

    @staticmethod
    def _ai_unfinished(result, log_file):
        if result.blocked:
            return "waiting_quota", {"seq": 2, "type": "waiting_quota",
                                     "message": redact(result.error or "quota blocked")[:1000]}
        return "failed", {"seq": 2, "type": "failed", "message": redact(result.error or "exit %s" % result.exit_code)[:1000],
                          "output_tail": tail_text(log_file)}

    def _run_leased(self, claim, adapter, gate, work, deadline, finish, crash="adapter crashed"):
        """Run `work(cancel_event)` once under the claim's lease: the same
        stop / cancel / lease fencing as a task run, without a worktree or
        checkpoint. `gate(deadline)` → block reason (default: the AI job
        gate of `adapter`); `finish(result)` → (outcome, terminal event)."""
        cancel_event = threading.Event()
        self._track(cancel_event)
        try:
            return self._run_leased_tracked(claim, adapter, gate, work, deadline, finish, crash, cancel_event)
        finally:
            self._untrack(cancel_event)

    def _run_leased_tracked(self, claim, adapter, gate, work, deadline, finish, crash, cancel_event):
        job = claim["job"]
        job_id = job["id"]
        gate = gate or (lambda at: self._gate(adapter, job, at))
        running_announced = False
        observed_end = None
        reason = ""
        spawned = False
        try:
            self.db.update_remote_claim(job_id, "launching")
            deadline, reason = self._renew(claim, adapter, deadline, gate)
            if reason:
                return self._blocked(claim, job_id, reason)
            observed_start = None
            adapter_entered = threading.Event()

            def authority():
                return reason or self._stop_reason() or gate(deadline)

            def invoke():
                nonlocal reason, spawned, observed_start, observed_end
                if reason or cancel_event.is_set():
                    return None
                reason = self._stop_reason() or gate(deadline)
                if reason:
                    return None
                with spawn_authority(authority):
                    enforce_spawn_authority(cancel_event)
                    spawned = True
                    observed_start = self._observed_now()
                    adapter_entered.set()
                    try:
                        return work(cancel_event)
                    finally:
                        observed_end = self._observed_now()

            with _job_pool(cancel_event) as pool:
                future = pool.submit(invoke)
                future.add_done_callback(lambda _: adapter_entered.set())
                adapter_entered.wait()
                if observed_start is not None:
                    self.db.update_remote_claim(job_id, "running")
                    self._report(claim, [{"seq": 1, "type": "running", "message": "started",
                                          "observed_at": observed_start}], flush=False)
                    running_announced = True
                while True:
                    try:
                        wait = self.heartbeat_interval if reason else min(self.heartbeat_interval, max(.001, (deadline - time.time()) / 2))
                        result = future.result(timeout=wait)
                        break
                    except TimeoutError:
                        if not reason:
                            deadline, reason = self._renew(claim, adapter, deadline, gate)
                            if reason:
                                cancel_event.set()
            reason = reason or self._cancel_reason(cancel_event) or gate(deadline)
            if reason:
                if not spawned or reason in ("cancelled", STOPPED):
                    return self._blocked(claim, job_id, reason, seq=2 if running_announced else 1,
                                         observed_at=observed_end)
                self.db.update_remote_claim(job_id, "fenced")
                return "job %s → fenced (%s)" % (job_id, reason)
        except Exception as exc:
            terminal_seq = 2 if running_announced else 1
            if (job.get("desired_action") == "cancel" or reason == "cancelled"
                    or (cancel_event.is_set() and not reason and not self._stop.is_set())
                    or (not reason and isinstance(exc, DispatchDenied) and str(exc) == "cancelled"
                        and not self._stop.is_set())):
                return self._blocked(claim, job_id, "cancelled", seq=terminal_seq, observed_at=observed_end)
            if reason == STOPPED or (not reason and self._stop.is_set()
                                     and (cancel_event.is_set() or isinstance(exc, DispatchDenied))):
                return self._blocked(claim, job_id, STOPPED, seq=terminal_seq, observed_at=observed_end)
            if reason and cancel_event.is_set():
                self.db.update_remote_claim(job_id, "fenced")
                return "job %s → fenced (%s)" % (job_id, reason)
            if isinstance(exc, DispatchDenied):
                return self._blocked(claim, job_id, str(exc), seq=terminal_seq, observed_at=observed_end)
            self._report(claim, [{"seq": terminal_seq, "type": "failed", "message": "%s: %s" % (crash, exc),
                                  "observed_at": observed_end or self._observed_now()}])
            self.db.update_remote_claim(job_id, "reported")
            return "job %s → failed" % job_id

        if getattr(result, "samples", None):
            self._post_samples(job["provider"], adapter, result.samples)
        outcome, event = finish(result)
        event["observed_at"] = observed_end
        self._report(claim, [event])
        self.db.update_remote_claim(job_id, "reported")
        self.maintain(force=True)
        return "job %s → %s" % (job_id, outcome)

    # ---- acceptance: review_turn and check jobs ---------------------------
    def _fail(self, claim, message, outcome):
        self._report(claim, [{"seq": 1, "type": "failed", "message": message}])
        self.db.update_remote_claim(claim["job"]["id"], "reported")
        return "job %s → %s" % (claim["job"]["id"], outcome)

    @staticmethod
    def _step_branch(repo, job):
        """(branch, commit) of the step a review / check job is about: the
        job's `branch_name` (a timetrace/* branch, as the step's completion
        reported it), else the branch the step job `source_job_id` made from
        that name (`<name>-<8 hex>`, or `timetrace/<n>` without a name). None when
        no such local branch exists."""
        name, source = job.get("branch_name"), job.get("source_job_id")
        candidates = [name] if worktree.valid_task_branch(name) else []
        if isinstance(source, str) and source:
            unique = worktree.unique_branch(name, source) if name else None
            candidates += [unique] if unique else []
            candidates.append(worktree.branch_name(local_task_id(source)))
        for branch in candidates:
            commit = worktree.branch_commit(repo, branch)
            if commit:
                return branch, commit
        return None

    @staticmethod
    def _step_output(workspace, job):
        """The resolved `timetrace-out/<output_name>/` of a folder step, or None."""
        name = job.get("output_name")
        if not folder._valid_name(name) or not folder.output_intact(workspace["path"], name):
            return None
        return name, str((Path(workspace["path"]) / folder.OUTPUT_ROOT / name).resolve())

    def _acceptance_target(self, claim, workspace, what):
        """(cwd-to-be, detail) for a review / check job, or an outcome string
        when the job was failed. Git: (None, (branch, commit)); folder:
        (output dir, name)."""
        job = claim["job"]
        if workspace.get("kind") == folder.FOLDER:
            found = self._step_output(workspace, job)
            if not found:
                return self._fail(claim, "找不到要%s的产出目录" % what, "failed (output missing)")
            return found[1], found[0]
        found = self._step_branch(workspace["path"], job)
        if not found:
            return self._fail(claim, "本机找不到要%s的分支" % what, "failed (branch missing)")
        return None, found

    @contextmanager
    def _acceptance_checkout(self, workspace, kind, job_id, commit, deadline):
        """A fresh detached checkout of `commit` under ~/.timetrace/<kind>/, made
        while holding the workspace lock (shared .git writes) and removed
        (with its registration) on every exit path."""
        path = self.home / kind / _safe_id(job_id)
        ws_lock = self._acquire_workspace(workspace["path"], deadline)
        try:
            cwd = worktree.add_detached(workspace["path"], path, commit)
        except BaseException:
            ws_lock.release()
            worktree.remove_detached(workspace["path"], path)
            raise
        ws_lock.release()
        try:
            yield cwd
        finally:
            worktree.remove_detached(workspace["path"], path)

    def _check_gate(self, job, deadline) -> str:
        """A check runs no AI tool: only cancellation and the lease gate it."""
        return deny_reason(DispatchGate(
            cancelled=job.get("status") == "cancelled" or job.get("desired_action") == "cancel",
            lease_valid=deadline > time.time(), runner_online=True, dependencies_ready=True,
            zero_spend_verified=True,
        )) or ""

    def _review_turn(self, claim, workspace, adapter, log_file):
        """An acceptance review: the local AI reads the step's result
        read-only (chat_turn argv) in a fresh detached checkout of the step
        branch (folder: its output directory) and answers with a verdict."""
        job = claim["job"]
        job_id = job["id"]
        review = getattr(adapter, "review", None)
        if not callable(review):
            return self._fail(claim, "review unsupported", "rejected (review unsupported)")
        deadline = self._deadline(claim)
        reason = self._stop_reason() or self._gate(adapter, job, deadline)
        if reason:
            return self._blocked(claim, job_id, reason)
        target = self._acceptance_target(claim, workspace, "复核")
        if isinstance(target, str):
            return target
        out_dir, detail = target
        in_folder = out_dir is not None
        if in_folder:
            summary = "## 产出文件（路径\t字节）\n" + (folder.listing(out_dir, worktree.DIFF_STAT_BYTES) or "（空）")
        else:
            branch, commit = detail
            base_name = job.get("base_ref")
            base = worktree.resolve_commit(workspace["path"], base_name)
            if not base:
                base_name = workspace.get("default_branch") or "HEAD"
                base = worktree.resolve_commit(workspace["path"], base_name)
            stat = worktree.diff_stat(workspace["path"], base, commit) if base else ""
            # Only names that resolved (so passed the ref checks) are shown.
            summary = ("## 差异摘要（git diff --stat %s...%s）\n%s"
                       % (base_name if base else "base", branch, stat or "（无法计算差异摘要）"))
        prompt = "%s\n\n%s\n" % (job.get("prompt") or "", summary)
        try:
            slot = self._acquire_slot(job.get("provider"))
        except LockBusy as exc:
            return self._busy(job_id, exc)
        try:
            def finish(result):
                if not result.ok:
                    return self._ai_unfinished(result, log_file)
                found = results.parse_verdict(result.output)
                reply = safe_reply(result.output)
                event = {"seq": 2, "type": "completed", "message": "completed",
                         "verdict": found["verdict"], "reasons": found["reasons"],
                         "reply": reply, "result_summary": reply[:1000], "output_tail": tail_text(log_file)}
                if found["verdict"] == results.INVALID_VERDICT:
                    event["message"] += "; " + results.INVALID_VERDICT_NOTE
                if in_folder:
                    event["reviewed_output"] = "%s/%s" % (folder.OUTPUT_ROOT, detail)
                else:
                    event["reviewed_branch"], event["reviewed_commit"] = detail
                return "reviewed (%s)" % found["verdict"], event

            Path(log_file).touch(exist_ok=True)
            if in_folder:
                # Like a chat turn: a folder workspace stays quiet meanwhile.
                try:
                    ws_lock = workspace_lock(self.home, workspace["path"]).acquire()
                except LockBusy as exc:
                    return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
                try:
                    return self._run_leased(claim, adapter, None,
                                            lambda cancel: review(prompt, out_dir, log_file, cancel), deadline, finish)
                finally:
                    ws_lock.release()
            try:
                with self._acceptance_checkout(workspace, "reviews", job_id, detail[1], deadline) as cwd:
                    return self._run_leased(claim, adapter, None,
                                            lambda cancel: review(prompt, cwd, log_file, cancel), deadline, finish)
            except LockBusy as exc:
                return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
            except Exception as exc:
                return self._fail(claim, "复核副本创建失败：%s" % exc.__class__.__name__, "failed")
        finally:
            slot.release()

    # ---- import_parse: a shared conversation → import proposal -------------
    def _import_parse(self, claim):
        """Turn a shared conversation (all of it in the prompt) into the JSON
        import proposal: the local AI runs read-only (chat_turn argv, always a
        new session) in a fresh empty directory ~/.timetrace/imports/<job>,
        removed afterwards. No workspace, worktree or checkpoint; the same
        zero-spend gate, lease and fencing as a chat turn, one slot of its
        tool plus an overall slot."""
        job = claim["job"]
        job_id = job["id"]
        import_id = job.get("import_id")
        if not isinstance(import_id, str) or not IMPORT_ID.fullmatch(import_id):
            return self._fail(claim, "invalid import", "rejected (invalid import)")
        provider = job.get("provider")
        adapter = self.adapters.get(provider)
        if adapter is None:
            return self._fail(claim, "tool unavailable", "rejected (tool unavailable)")
        parse = getattr(adapter, "parse_import", None)
        if not callable(parse):
            return self._fail(claim, "import unsupported", "rejected (import unsupported)")
        prompt = job.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return self._fail(claim, "invalid prompt", "rejected (invalid prompt)")
        self.home.joinpath("logs").mkdir(parents=True, exist_ok=True)
        log_file = str(self.home / "logs" / ("remote-%s.log" % _safe_id(job_id)))
        deadline = self._deadline(claim)
        reason = self._stop_reason() or self._gate(adapter, job, deadline)
        if reason:
            return self._blocked(claim, job_id, reason)
        try:
            slot = self._acquire_slot(provider)
        except LockBusy as exc:
            return self._busy(job_id, exc)
        try:
            def finish(result):
                if not result.ok:
                    return self._ai_unfinished(result, log_file)
                found = results.extract_import(result.output)
                reply = safe_reply(result.output)
                event = {"seq": 2, "type": "completed", "message": "completed",
                         "reply": reply, "result_summary": reply[:1000], "output_tail": tail_text(log_file)}
                if found is None:
                    event["message"] += "; " + results.INVALID_IMPORT_NOTE
                    return "parsed (invalid)", event
                event["import_result"] = found
                return "parsed", event

            Path(log_file).touch(exist_ok=True)
            path = self.home / "imports" / _safe_id(job_id)
            try:
                cwd = self._fresh_import_dir(path)
            except OSError as exc:
                return self._fail(claim, "解析目录创建失败：%s" % exc.__class__.__name__, "failed")
            try:
                return self._run_leased(claim, adapter, None,
                                        lambda cancel: parse(prompt, cwd, log_file, cancel), deadline, finish)
            finally:
                self._remove_import_dir(path)
        finally:
            slot.release()

    def _fresh_import_dir(self, path: Path) -> str:
        """An empty 0700 directory at `path` (a leftover is replaced) under a
        real, private ~/.timetrace/imports; its resolved path."""
        root = path.parent
        self.home.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise NotADirectoryError("imports root is not a plain directory")
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(str(root), 0o700)
        self._remove_import_dir(path)
        path.mkdir(mode=0o700)
        os.chmod(str(path), 0o700)
        return str(path.resolve())

    @staticmethod
    def _remove_import_dir(path: Path) -> None:
        """Delete the scratch directory without following symlinks."""
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(str(path), ignore_errors=True)
        except OSError:
            pass

    def _check_job(self, claim, workspace, log_file):
        """An automatic check: the command registered on this computer under
        (workspace, check_name) runs in a fresh detached checkout of the step
        branch (folder: its output directory). Nothing from the job becomes
        part of the command."""
        job = claim["job"]
        job_id = job["id"]
        name = job.get("check_name")
        argv = self.db.get_check(workspace["id"], name) if checks.valid_name(name) else None
        if argv is None:
            return self._fail(claim, checks.UNKNOWN, "rejected (unknown check)")
        deadline = self._deadline(claim)
        gate = lambda at: self._check_gate(job, at)
        reason = self._stop_reason() or gate(deadline)
        if reason:
            return self._blocked(claim, job_id, reason)
        target = self._acceptance_target(claim, workspace, "检查")
        if isinstance(target, str):
            return target
        out_dir, detail = target
        try:
            slot = self._acquire_slot(None)
        except LockBusy as exc:
            return self._busy(job_id, exc)

        def work(cwd):
            return lambda cancel: checks.run(name, argv, cwd, log_file, cancel,
                                             extra_drop=self.check_env_drop, keep=self.check_env_keep)

        def finish(result):
            verdict = "pass" if result.passed else "fail"
            if result.timed_out:
                message = "检查超时"
            elif result.passed:
                message = "检查通过"
            else:
                message = "检查未通过（退出码 %d）" % result.exit_code
            event = {"seq": 2, "type": "completed", "message": message, "result_summary": message,
                     "verdict": verdict, "check_result": result.as_event()}
            if out_dir is None:
                event["checked_branch"], event["checked_commit"] = detail
            else:
                event["checked_output"] = "%s/%s" % (folder.OUTPUT_ROOT, detail)
            return "check %s" % ("passed" if result.passed else "failed"), event

        try:
            if out_dir is not None:
                try:
                    ws_lock = workspace_lock(self.home, workspace["path"]).acquire()
                except LockBusy as exc:
                    return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
                try:
                    return self._run_leased(claim, None, gate, work(out_dir), deadline, finish, crash="check crashed")
                finally:
                    ws_lock.release()
            try:
                with self._acceptance_checkout(workspace, "checks", job_id, detail[1], deadline) as cwd:
                    # `git push` from the copy goes nowhere (as in task worktrees).
                    worktree.block_push(cwd)
                    return self._run_leased(claim, None, gate, work(cwd), deadline, finish, crash="check crashed")
            except LockBusy as exc:
                return "job %s → deferred (workspace busy)" % job_id + ("; " + str(exc) if isinstance(exc, UnclearedOwner) else "")
            except Exception as exc:
                return self._fail(claim, "检查副本创建失败：%s" % exc.__class__.__name__, "failed")
        finally:
            slot.release()

    def run_forever(self, interval: int = 5, log: Callable[[str], None] = None) -> None:
        """Claim and run until stop() (the SIGTERM/SIGINT handlers call it).
        A KeyboardInterrupt also cancels the running jobs before it returns."""
        log = log or (lambda message: print(message, flush=True))
        self.log = log
        if self.max_parallel > 1:
            return self._run_parallel(interval, log)
        backoff = interval
        manual_diagnostics = set()
        try:
            while not self._stop.is_set():
                try:
                    outcome = self.run_once()
                    backoff = interval
                    if outcome in ("revoked", "unpaired"):
                        # Wait for `timetrace cloud login`; the access_token callable
                        # picks up new credentials without restarting the service.
                        self._stop.wait(60)
                        continue
                    self._note_outcome(outcome, log, manual_diagnostics)
                    if outcome in HOLD_OUTCOMES:
                        # No work: periodic quota refresh keeps parked plans
                        # recovering and the phone's cards fresh.
                        self.maintain()
                        self._stop.wait(interval)
                except Exception:
                    self._stop.wait(backoff)
                    backoff = min(60, max(interval, backoff * 2))
        finally:
            # run_once ran on this thread: nothing is left running unless an
            # interrupt arrived mid-run, which stop() now cancels.
            self.stop()
        log("stopped")

    @staticmethod
    def _note_outcome(outcome, log, manual_diagnostics) -> None:
        if "; manual recovery required " in outcome:
            # Job IDs may change on every claim; deduplicate the actual
            # lock diagnostic so a persistent fence is visible once.
            diagnostic = outcome.partition("; ")[2]
            if diagnostic not in manual_diagnostics:
                log(diagnostic)
                manual_diagnostics.add(diagnostic)
        elif outcome not in HOLD_OUTCOMES and "deferred (" not in outcome:
            manual_diagnostics.clear()

    def _run_parallel(self, interval, log) -> None:
        """Claim on this thread while fewer than max_parallel jobs run; each
        job runs on a worker with its own database connection. Upkeep stays
        on this thread. On stop() or an interrupt: no new claims, every
        running job cancelled, and at most SHUTDOWN_GRACE_SECONDS of waiting."""
        backoff = interval
        manual_diagnostics = set()
        active = {}  # future → job id
        pool = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="timetrace-job")

        def reap(futures):
            for future in futures:
                job_id = active.pop(future, "?")
                try:
                    self._note_outcome(future.result(), log, manual_diagnostics)
                except Exception as exc:
                    log("job %s worker crashed: %s: %s" % (job_id, exc.__class__.__name__, exc))

        try:
            while not self._stop.is_set():
                reap({f for f in active if f.done()})
                if self._maintain_due:
                    self._maintain_due = False
                    self.maintain(force=True)
                if len(active) >= self.max_parallel:
                    wait(list(active), timeout=max(interval, .05), return_when=FIRST_COMPLETED)
                    continue
                try:
                    claim = self._next_claim()
                    backoff = interval
                except Exception:
                    self._stop.wait(backoff)
                    backoff = min(60, max(interval, backoff * 2))
                    continue
                if claim in ("revoked", "unpaired"):
                    self._stop.wait(60)
                    continue
                if claim in HOLD_OUTCOMES:
                    self.maintain()
                    self._stop.wait(interval)
                    continue
                active[pool.submit(self.handle, claim)] = (claim.get("job") or {}).get("id", "?")
        finally:
            self.stop()
            pending = list(active)
            if pending:
                log("stopping: cancelling %d running job(s)" % len(pending))
            _, stuck = wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
            reap([f for f in pending if f.done()])
            if stuck:
                log("stopping: %d job(s) did not finish within %d s; their locks stay fenced"
                    % (len(stuck), SHUTDOWN_GRACE_SECONDS))
            pool.shutdown(wait=False)
        log("stopped")

    @staticmethod
    def _observed_now():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _report(self, claim: Dict[str, Any], events: list, flush=True) -> None:
        job_id, attempt_id = claim["job"]["id"], claim["attempt_id"]
        with self._controls_lock:
            control = self._controls.get(attempt_id)
        if control is not None and getattr(control, "started", False):
            # Control acknowledgements and approval requests come first:
            # they happened before whatever this call reports.
            events = control.take_events() + list(events)
        # A job with a control has side events (protocol 2) between
        # `running` and its terminal event: each event takes the next
        # sequence number. Other jobs keep their fixed numbers (a replayed
        # event with the same seq is ignored by the outbox).
        last = self.db.last_remote_seq(job_id, attempt_id) if events and control is not None else None
        for event in events:
            event = dict(event)
            if last is not None:
                last = max(int(event.get("seq") or 0), last + 1)
                event["seq"] = last
            if event.get("type") == "failed":
                self._health_due = True  # re-check the computer after a failure
            # Stamp the phase when observed, before durable enqueue. flush_outbox
            # replays this payload unchanged even after restart/network delay.
            event = self._outbound(event)
            event.setdefault("observed_at", self._observed_now())
            self.db.queue_remote_event(job_id, attempt_id, claim["lease_epoch"], event)
        if not flush:
            return
        try:
            self.flush_outbox()
        except Exception:
            pass

    def _outbound(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """The only shape of an event that may leave this computer: secrets
        masked in every free-text field, and no output tail when the user
        switched uploads off. Applied before the durable enqueue, so the
        outbox never stores what could not be sent."""
        event = dict(event)
        if not self.upload_output_tail:
            event.pop("output_tail", None)
        for key in ("message", "result_summary", "output_tail", "summary"):
            if isinstance(event.get(key), str):
                event[key] = redact(event[key])
        if isinstance(event.get("summary"), str):
            event["summary"] = event["summary"][:approvals.SUMMARY_CHARS]
        if "input" in event:
            event["input"] = approvals.safe_input(event["input"])
        if isinstance(event.get("reply"), str):
            event["reply"] = safe_reply(event["reply"])
        for key in ("artifacts", "result", "reasons"):
            if key in event:
                event[key] = results.redact_all(event[key])
        if "import_result" in event:
            event["import_result"] = results.redact_deep(event["import_result"])
        if isinstance(event.get("check_result"), dict):
            check = results.redact_all(event["check_result"])
            check["output_tail"] = (checks.bounded_tail(check.get("output_tail") or "")
                                    if self.upload_output_tail else "")
            event["check_result"] = check
        tail = event.get("output_tail")
        if isinstance(tail, str):
            # Masks can be longer than what they hide; keep Valley's bound.
            while len(tail.encode("utf-8")) > OUTPUT_TAIL_BYTES:
                cut = tail.find("\n")
                tail = tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail[len(tail) // 4 + 1:]
            event["output_tail"] = tail
        return event

    def _post_samples(self, provider: str, adapter: Any, samples: list, now: float = None) -> int:
        """Map local vendor readings to de-identified Valley samples and post
        them so the server's quota gate reflects real availability."""
        if not samples:
            return 0
        now = now if now is not None else time.time()
        binding = self._pool_binding(provider)
        pool_id, profile_id = binding[:2]
        # One pool per tool account: the same account on two computers is one
        # card, two accounts are two. The key is an opaque digest.
        key = None
        if hasattr(adapter, "account_key"):
            try:
                key = adapter.account_key()
            except Exception:
                key = None
        if key and pool_id == "pool-" + provider:
            pool_id = "%s-%s" % (pool_id, key)
        # Two-field legacy/display bindings remain non-authoritative. Only an
        # explicitly verified third flag can identify the authenticated pool.
        authoritative = len(binding) == 3 and binding[2] is True
        payloads = []
        for s in samples:
            try:
                payloads.append(quota.payload_from_reading(
                    s.bucket_key, s.tool, s.used_pct, s.reset_at, s.window_mins,
                    pool_id, profile_id, now, source=getattr(s, "source", "runner"),
                    pool_authoritative=authoritative,
                ))
            except ValueError:
                continue
        if not payloads:
            return 0
        try:
            self.cloud.post_quota_samples(self.access_token(), payloads)
        except Exception:
            return 0
        return len(payloads)

    def report_quota(self, now: float = None) -> int:
        """Read on-demand quota from adapters that support it and report it to
        Valley. This is what lets a parked (waiting_quota) Plan be re-queued when
        its pool recovers, without needing a run to discover it."""
        return sum(self.report_quota_counts(now).values())

    def report_quota_counts(self, now: float = None) -> Dict[str, int]:
        """Samples posted per provider (0 when the adapter cannot read or failed)."""
        now = now if now is not None else time.time()
        counts: Dict[str, int] = {}
        for provider, adapter in self.adapters.items():
            caps = getattr(adapter, "capabilities", lambda: {})()
            if not caps.get("can_read_quota"):
                continue
            try:
                samples = adapter.read_limits()
            except Exception:
                counts[provider] = 0
                continue
            counts[provider] = self._post_samples(provider, adapter, samples or [], now)
        return counts

    def flush_outbox(self) -> None:
        # Parallel jobs flush too: one batch is never sent twice at once.
        with self._flush_lock:
            self._flush_outbox()

    def _flush_outbox(self) -> None:
        batches = {}
        for row in self.db.pending_remote_events():
            key = (row["job_id"], row["attempt_id"], row["lease_epoch"])
            batches.setdefault(key, []).append(row)
        # Insertion-ordered groups preserve durable attempt order (UUID lexical
        # order is not chronology). Never split an attempt's terminal batch:
        # Valley may close a cancelled attempt at the end of the first request.
        for (job, attempt, epoch), rows in batches.items():
            rows.sort(key=lambda row: row["seq"])
            self.cloud.append_events(self.access_token(), job, attempt, epoch,
                                     [row["payload"] for row in rows])
            self.db.mark_remote_events_sent([row["id"] for row in rows])
        if batches:
            self.db.prune_sent_remote_events(time.time() - OUTBOX_RETENTION_SECONDS)
