"""Check commands registered on this computer (`timetrace workspace check add`).

A pipeline step whose acceptance is an automatic check names only the check;
Valley sends that name in a `check` job and this module runs the command the
user registered locally under it. No string from the server ever becomes part
of a command line.
"""
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from timetrace.billing import billing_env_keys
from timetrace.process import SECRET_ENV_NAME, _shell_quote, run_streaming, tail_text
from timetrace.redact import redact

NAME = re.compile(r"[a-z0-9_-]{1,40}")
MAX_ARGS = 100
MAX_ARG_CHARS = 4096
TIMEOUT_SECONDS = 30 * 60
OUTPUT_TAIL_BYTES = 16 * 1024
# Credential variables of the AI tools never reach a check: it runs code the
# model wrote (tests, build scripts) outside any sandbox.
AI_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE", "CODEX_", "GEMINI_", "CURSOR_")
UNKNOWN = "本机没有这个检查"


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name))


def argv_problem(argv: Sequence[str]) -> str:
    """Why `argv` cannot be registered, or ""."""
    if not argv:
        return "check command is empty (put it after --)"
    if len(argv) > MAX_ARGS:
        return "check command has more than %d arguments" % MAX_ARGS
    for arg in argv:
        if not isinstance(arg, str) or "\0" in arg or len(arg) > MAX_ARG_CHARS:
            return "check command arguments must be text without NUL, at most %d characters" % MAX_ARG_CHARS
    if not argv[0].strip():
        return "check command must start with a program"
    return ""


def dropped_env(env: Mapping[str, str], extra: Sequence[str] = (), keep: Sequence[str] = ()) -> List[str]:
    """Variables removed from a check's environment: every AI tool credential
    or endpoint variable (by prefix and the billing lists), every
    secret-named variable (as for the AI tools, M-2) unless the user listed
    it in config `check_env_keep`, and anything in config `check_env_drop`."""
    keep = set(keep or ())
    always = set(billing_env_keys("")) | set(extra or ())
    names = []
    for key in env:
        if key in always or key.upper().startswith(AI_ENV_PREFIXES):
            names.append(key)
        elif SECRET_ENV_NAME.search(key) and key not in keep:
            names.append(key)
    return names


class CheckResult:
    """What one check run produced (the `check_result` event field)."""

    def __init__(self, name: str, exit_code: int, output_tail: str, duration_seconds: float, timed_out: bool):
        self.name, self.exit_code, self.output_tail = name, exit_code, output_tail
        self.duration_seconds, self.timed_out = duration_seconds, timed_out

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_event(self) -> Dict[str, object]:
        return {"name": self.name, "exit_code": self.exit_code, "output_tail": self.output_tail,
                "duration_seconds": round(self.duration_seconds, 1), "timed_out": self.timed_out}


def bounded_tail(text: str, limit: int = OUTPUT_TAIL_BYTES) -> str:
    """Redacted, at most `limit` UTF-8 bytes, cut at line starts from the front."""
    text = redact(text or "")
    while len(text.encode("utf-8")) > limit:
        cut = text.find("\n")
        text = text[cut + 1:] if 0 <= cut < len(text) - 1 else text[len(text) // 4 + 1:]
    return text


def run(name: str, argv: Sequence[str], cwd: str, log_file: str, cancel_event=None,
        timeout: float = TIMEOUT_SECONDS, extra_drop: Sequence[str] = (), keep: Sequence[str] = (),
        env: Optional[Mapping[str, str]] = None) -> CheckResult:
    """Run the registered argv (no shell) in `cwd` with its own process
    group, stdin closed, at most `timeout` seconds. The log is started
    fresh; its tail, without the command line, is the reported output."""
    env = os.environ if env is None else env
    Path(log_file).write_text("", encoding="utf-8")
    drop = dropped_env(env, extra_drop, keep)
    # run_streaming removes every secret-named variable; put back only the
    # ones the user chose to keep (never an AI tool's).
    restore = {key: env[key] for key in (keep or ()) if key in env and key not in drop}
    started = time.monotonic()
    try:
        code, _ = run_streaming(list(argv), cwd, log_file, env=restore, timeout=timeout,
                                cancel_event=cancel_event, drop_env=drop)
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        # The program itself is not named: the command stays on this computer.
        return CheckResult(name, 127, "无法启动检查命令（%s）" % exc.__class__.__name__,
                           time.monotonic() - started, False)
    duration = time.monotonic() - started
    timed_out = duration >= timeout and code != 0
    tail = tail_text(log_file, OUTPUT_TAIL_BYTES + 4096)
    header = "$ " + " ".join(_shell_quote(part) for part in argv) + "\n"
    if tail.startswith(header):
        # The whole log fits: drop run_streaming's "$ <command>" header.
        tail = tail[len(header):]
    if timed_out:
        tail += "\n检查超时（%d 分钟）已终止\n" % int(timeout // 60)
    return CheckResult(name, code, bounded_tail(tail), duration, timed_out)
