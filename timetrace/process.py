"""Safe subprocess streaming for long-running local AI tools."""
import errno
import os
import re
import signal
import subprocess
import threading
import time
from typing import IO, List, Optional, Sequence, Tuple

from timetrace.dispatch import enforce_spawn_authority
from timetrace.redact import mask_timetrace_tokens, redact

MAX_CAPTURED_LINES = 10_000
MAX_LOG_BYTES = 10 * 1024 * 1024
# A stop: SIGTERM, this long for the tool to leave, then SIGKILL. The queue's stop (StopRequest) then bounds, too,
# the wait for the exit to show up and for the group to empty, so the whole stop stays well inside launchd's 20 s
# between SIGTERM and SIGKILL.
STOP_GRACE_SECONDS = 2
STOP_CONFIRM_SECONDS = 5
GROUP_EMPTY_SECONDS = 2
# Environment variables whose names say "credential" are removed from the AI
# tool's environment: nothing the tool or `git commit` needs, and the model's
# shell commands could otherwise read and echo them into uploaded output.
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)")


class ToolExitUnconfirmed(RuntimeError):
    """A stop was requested and the tool's process group was signalled up to SIGKILL, but the tool's exit (or the
    group's emptiness) could not be confirmed within the bound. The tool may still be running: nothing may report
    it as stopped, and whatever fences its work must stay in place."""


class StopRequest(threading.Event):
    """The local queue's request to stop (SIGINT / SIGTERM), handed to `run_streaming` as its `cancel_event`.

    A signal handler can run at any point of the main thread, even inside `Event.wait` while it holds the event's
    own lock, so `request` touches plain attributes only (no lock, no I/O) and `wait` polls. The first request
    names the reason; later ones change nothing. `run_streaming` reports back here: `tool_signalled` is True only
    when it signalled a tool that was still running, so a tool that had already exited by itself keeps its own
    result."""

    def __init__(self) -> None:
        super().__init__()
        self.reason: Optional[str] = None
        self.tool_signalled = False
        self._requested = False

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self._requested = True

    def set(self) -> None:
        self.request("stop")

    def clear(self) -> None:
        self._requested, self.reason = False, None

    def is_set(self) -> bool:
        return self._requested

    def wait(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._requested:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            time.sleep(.05 if left is None else min(.05, left))
        return True


def child_env(drop_env: Optional[Sequence[str]] = None, env: Optional[dict] = None) -> dict:
    """The AI tool's environment: this process's, minus `CLAUDE*`, the
    billing keys in `drop_env` and every secret-named variable (M-2)."""
    run_env = dict(os.environ)
    for key in list(run_env):
        if key.startswith("CLAUDE") or key in set(drop_env or ()) or SECRET_ENV_NAME.search(key):
            run_env.pop(key, None)
    if env:
        run_env.update(env)
    return run_env


def run_streaming(
    cmd: Sequence[str], cwd: str, log_file: str, env: Optional[dict] = None,
    timeout: Optional[float] = None, cancel_event: Optional[threading.Event] = None,
    drop_env: Optional[Sequence[str]] = None,
) -> Tuple[int, List[str]]:
    """`drop_env` names variables the child must not inherit (billing keys and
    endpoints that could switch a subscription tool to metered API usage)."""
    lines: List[str] = []
    run_env = child_env(drop_env, env)
    with open(log_file, "a", encoding="utf-8") as log, open(os.devnull, "rb") as devnull:
        log.write("$ " + " ".join(_shell_quote(part) for part in cmd) + "\n")
        log.flush()
        # Check after command/environment/log preparation, at the shared OS
        # boundary used by both provider adapters. This check and Popen are not
        # atomic; callers must handle revocation after the process starts.
        enforce_spawn_authority(cancel_event)
        proc = subprocess.Popen(
            list(cmd), cwd=cwd, stdin=devnull, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=run_env, text=True, bufsize=1, start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        lock = threading.Lock()
        stdout_thread = threading.Thread(target=_tee, args=(proc.stdout, log, lines, lock, ""), daemon=True)
        stderr_thread = threading.Thread(target=_tee, args=(proc.stderr, log, None, lock, "[stderr] "), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        stopping = isinstance(cancel_event, StopRequest)
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.wait(.2):
                if stopping:
                    _stop_on_request(proc, cancel_event, log, lock)
                else:
                    _terminate_group(proc)
                break
            if deadline is not None and time.monotonic() >= deadline:
                _terminate_group(proc)
                break
            if cancel_event is None:
                try:
                    proc.wait(timeout=min(.2, timeout) if timeout is not None else .2)
                except subprocess.TimeoutExpired:
                    pass
        # However the tool ended, nothing it started may keep running: a
        # detached child could otherwise write after the runner's checks.
        _kill_group(proc)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        proc.stdout.close()
        proc.stderr.close()
        if stopping and cancel_event.tool_signalled and not _group_emptied(proc):
            with lock:
                log.write("--- stop requested: the tool's process group is NOT confirmed gone ---\n")
            raise ToolExitUnconfirmed("the tool's process group (%d) is not confirmed gone after SIGKILL: the "
                                      "kernel did not report it as non-existent" % proc.pid)
        log.write("--- exit %s ---\n" % proc.returncode)
    return int(proc.returncode), lines


def _stop_on_request(proc: subprocess.Popen, stop: StopRequest, log: IO[str], lock: threading.Lock) -> None:
    """Stop the tool because the queue was told to. A tool that has already exited keeps its own result and is not
    counted as signalled. Otherwise its group is stopped within the bound or ToolExitUnconfirmed is raised."""
    if proc.poll() is not None:
        return
    stop.tool_signalled = True
    with lock:
        log.write("--- stop requested (%s): signalling the tool's process group ---\n" % stop.reason)
        log.flush()
    try:
        _terminate_group(proc, confirm=STOP_CONFIRM_SECONDS)
    except ToolExitUnconfirmed:
        with lock:
            log.write("--- stop requested: the tool's exit is NOT confirmed ---\n")
        raise


def _terminate_group(proc: subprocess.Popen, confirm: Optional[float] = None) -> None:
    """SIGTERM the tool's group, SIGKILL it after STOP_GRACE_SECONDS. With `confirm` the wait for the exit that
    follows the SIGKILL is bounded to that many seconds and ToolExitUnconfirmed is raised if the tool is still not
    gone; without it (the cloud agent, a timeout) it waits for as long as it always did."""
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        if confirm is None:
            proc.wait()
            return
        try:
            proc.wait(timeout=confirm)
        except subprocess.TimeoutExpired:
            raise ToolExitUnconfirmed("the tool (process group %d) did not exit within %gs of SIGKILL"
                                      % (proc.pid, confirm)) from None


def _signal_group(proc: subprocess.Popen, signum: int) -> bool:
    """Signal the process group this module created for `proc`: the tool is started as its own session leader, so
    that group's id is proc.pid, and it is the only thing ever signalled here (never a pid from anywhere else).
    False when the signal was not delivered, whatever the reason: that says nothing about whether the group is gone
    (see `_group_emptied` for that), and no caller reads it as such."""
    if proc.pid <= 1:
        return False
    try:
        os.killpg(proc.pid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL what is left of the tool's session / process group. The group
    id stays reserved while any member lives, so this reaches only them."""
    _signal_group(proc, signal.SIGKILL)


def _group_is_gone(proc: subprocess.Popen) -> bool:
    """One existence probe (signal 0 delivers nothing). True only when the kernel says the group does not exist
    (ESRCH). Every other answer is "not shown to be gone":
      * success: a member is still there;
      * EPERM: the group exists but may not be signalled by us. That is the opposite of absent, so it must never be
        read as "nobody is there" (it is what a group of another user's, or one that changed its credentials, looks
        like);
      * any other error: we do not know.
    A pid that cannot be a group this module created (<= 1; killpg(0, ...) would hit our own group) is never
    probed and never confirmed."""
    if proc.pid <= 1:
        return False
    try:
        os.killpg(proc.pid, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _group_emptied(proc: subprocess.Popen) -> bool:
    """Whether the tool's group is confirmed gone, waiting up to GROUP_EMPTY_SECONDS for the members that were just
    killed to disappear. Only ESRCH confirms it (see `_group_is_gone`); anything else for that long, EPERM included,
    leaves the exit unconfirmed and the caller keeps the slot fenced.

    (An earlier version read EPERM as "only exited members are left to be reaped". That is unproven, and being wrong
    is not symmetric: if a group that really is empty answers EPERM, the cost is a slot a person has to clear; if
    one that is not empty does, a second tool would be allowed to start over a live one.)"""
    deadline = time.monotonic() + GROUP_EMPTY_SECONDS
    while not _group_is_gone(proc):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)
    return True


def _tee(stream: IO[str], log: IO[str], lines: Optional[List[str]], lock: threading.Lock, prefix: str) -> None:
    for line in iter(stream.readline, ""):
        line = mask_timetrace_tokens(line)
        if lines is not None:
            lines.append(line.rstrip("\n"))
            if len(lines) > MAX_CAPTURED_LINES:
                del lines[:len(lines) - MAX_CAPTURED_LINES]
        with lock:
            if log.tell() < MAX_LOG_BYTES:
                log.write((prefix + line)[:max(0, MAX_LOG_BYTES - log.tell())])
                log.flush()


def _shell_quote(value: str) -> str:
    if not value or any(ch in value for ch in " \t\n\"'$`\\"):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


_EXIT_MARKER = re.compile(r"^--- exit -?\d+ ---$")


def last_log_line(path, limit: int = 200) -> str:
    """The latest run's last line of output: after the last `$ cmd` echo,
    not our own `--- exit N ---` marker, without the `[stderr] ` prefix,
    redacted and then cut to `limit` characters; "" when the run printed
    nothing."""
    lines = tail_text(path).splitlines()
    echoes = [i for i, line in enumerate(lines) if line.startswith("$ ")]
    if echoes:
        lines = lines[echoes[-1] + 1:]
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("[stderr] "):
            line = line[len("[stderr] "):].strip()
        if line and not _EXIT_MARKER.match(line):
            return redact(line)[:limit]
    return ""


def tail_text(path, limit: int = 8000) -> str:
    """Last `limit` bytes of a log as text; invalid UTF-8 is replaced, and a
    partial first line is dropped so the tail starts at a line boundary.
    Valley caps output_tail at 8192 bytes, so callers keep `limit` below that."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return ""
    if size > limit and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    text = data.decode("utf-8", errors="replace")
    # Replacement characters are 3 bytes each; keep the encoded size inside
    # the budget so Valley's 8192-byte bound can never reject the event.
    while len(text.encode("utf-8")) > limit:
        text = text[len(text) // 4 + 1:]
    return text

