"""Safe subprocess streaming for long-running local AI tools."""
import os
import re
import signal
import subprocess
import threading
import time
from typing import IO, List, Optional, Sequence, Tuple

from timetrace.dispatch import enforce_spawn_authority

MAX_CAPTURED_LINES = 10_000
MAX_LOG_BYTES = 10 * 1024 * 1024
# Environment variables whose names say "credential" are removed from the AI
# tool's environment: nothing the tool or `git commit` needs, and the model's
# shell commands could otherwise read and echo them into uploaded output.
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)")


def run_streaming(
    cmd: Sequence[str], cwd: str, log_file: str, env: Optional[dict] = None,
    timeout: Optional[float] = None, cancel_event: Optional[threading.Event] = None,
    drop_env: Optional[Sequence[str]] = None,
) -> Tuple[int, List[str]]:
    """`drop_env` names variables the child must not inherit (billing keys and
    endpoints that could switch a subscription tool to metered API usage)."""
    lines: List[str] = []
    run_env = dict(os.environ)
    for key in list(run_env):
        if key.startswith("CLAUDE") or key in set(drop_env or ()) or SECRET_ENV_NAME.search(key):
            run_env.pop(key, None)
    if env:
        run_env.update(env)
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
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.wait(.2):
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
        log.write("--- exit %s ---\n" % proc.returncode)
    return int(proc.returncode), lines


def _terminate_group(proc: subprocess.Popen) -> None:
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL what is left of the tool's session / process group. The group
    id stays reserved while any member lives, so this reaches only them."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tee(stream: IO[str], log: IO[str], lines: Optional[List[str]], lock: threading.Lock, prefix: str) -> None:
    for line in iter(stream.readline, ""):
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

