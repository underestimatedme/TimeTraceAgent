"""Configuration: spec §9 defaults deep-merged with ~/.timetrace/config.json."""
import copy
import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "cloud_base_url": "https://apis.atlaspaces.com/timetrace/api/v1",
    "interval_sec": 30,
    "jitter_sec": 300,
    "default_block_sleep_sec": 3600,
    "circuit_breaker_failures": 3,
    "circuit_window_mins": 300,
    "allowed_repos": [],
    "hook_max_tasks": 5,
    # Send the last ~8 KB of a remote run's log (secrets redacted) with the
    # completed/failed event so the phone can show what happened. false keeps
    # all run output on this computer.
    "upload_output_tail": True,
    # AI jobs (task, chat and review turns) each tool runs at the same time
    # on this computer (1..8 per tool), reported to Valley with the
    # inventory. Each task runs in its own worktree / output directory.
    "max_parallel_per_tool": {"claude": 2, "codex": 2},
    # Overall cap on jobs of any kind (checks included), 1..8. 0 = not set:
    # the sum of max_parallel_per_tool (capped at 8). A value the user set
    # explicitly is honoured as the cap (1 keeps the runner sequential).
    "max_parallel": 0,
    # Environment of `timetrace workspace check` commands. AI tool credential and
    # endpoint variables and secret-named variables are always removed;
    # `check_env_drop` names more to remove, `check_env_keep` secret-named
    # ones a check needs (never an AI tool's).
    "check_env_drop": [],
    "check_env_keep": [],
    "claude": {
        "bin": "claude",
        "permission_mode": "acceptEdits",
        # Local git operations the unattended run may perform without asking.
        # Pushing stays blocked by --disallowedTools and the worktree pushurl.
        "allowed_tools": [
            "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
            "Bash(git diff:*)", "Bash(git log:*)",
        ],
        "model": None,
        "extra_args": [],
        # Setting sources Claude Code loads for unattended runs. "user" only:
        # project settings live in the (model-writable) worktree.
        "setting_sources": "user",
        # Where Claude Code keeps its OAuth login; None = ~/.claude/.credentials.json,
        # falling back to the macOS Keychain item Claude Code uses.
        "credentials_path": None,
    },
    "codex": {
        "bin": "codex",
        "sandbox": "workspace-write",
        "model": None,
        "extra_args": [],
        # Where Codex keeps its login; None = ~/.codex/auth.json.
        "auth_path": None,
    },
}


def home() -> Path:
    """Data directory. TIMETRACE_HOME overrides ~/.timetrace (tests rely on this)."""
    return Path(os.environ.get("TIMETRACE_HOME") or Path.home() / ".timetrace")


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(home_dir: Path = None) -> Dict[str, Any]:
    home_dir = home_dir or home()
    path = home_dir / "config.json"
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with open(path, "r", encoding="utf-8") as fh:
        user = json.load(fh)
    if not isinstance(user, dict):
        raise ValueError("config.json must contain a JSON object")
    return _merge(DEFAULTS, user)


def ensure_dirs(home_dir: Path) -> None:
    for sub in ("", "logs", "inbox", "inbox/done", "inbox/rejected", "worktrees"):
        (home_dir / sub).mkdir(parents=True, exist_ok=True)
    # Prompts, run logs, the outbox and worktrees live below: owner only.
    os.chmod(home_dir, 0o700)


# ---- `timetrace config get|set` -------------------------------------------------
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def scalar_keys() -> Dict[str, Any]:
    """Top-level keys that `timetrace config set` may write, with their defaults."""
    return {key: value for key, value in DEFAULTS.items()
            if isinstance(value, (bool, int, str))}


# Tools whose parallel limit `timetrace config set max_parallel_per_tool.<tool>` writes.
PER_TOOL_KEY = "max_parallel_per_tool"
PER_TOOL_TOOLS = ("claude", "codex")
PER_TOOL_MAX = 8


# `timetrace config set claude.bin /path/to/claude`: the tool binary the Runner starts.
TOOL_BIN_KEYS = ("claude.bin", "codex.bin")
# Marks a `<tool>.bin` that setup / agent install / doctor found on their own;
# such a value is re-resolved later, one the user set is never overwritten.
BIN_SOURCE_AUTO = "auto"


def _tool_bin_value(key: str, raw: str) -> str:
    text = os.path.expanduser(str(raw).strip())
    if not text:
        raise ValueError("%s expects the path of the %s executable" % (key, key.split(".")[0]))
    if os.path.isabs(text) and not (os.path.isfile(text) and os.access(text, os.X_OK)):
        raise ValueError("%s: %s is not an executable file" % (key, text))
    return text


def _per_tool_value(key: str, raw: str) -> int:
    tool = key.split(".", 1)[1]
    if tool not in PER_TOOL_TOOLS:
        raise ValueError("%s: unknown tool %r (one of %s)" % (PER_TOOL_KEY, tool, ", ".join(PER_TOOL_TOOLS)))
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("%s expects a whole number, got %r" % (key, raw)) from None
    if not 1 <= value <= PER_TOOL_MAX:
        raise ValueError("%s must be between 1 and %d" % (key, PER_TOOL_MAX))
    return value


def parse_value(key: str, raw: str) -> Any:
    if key.startswith(PER_TOOL_KEY + "."):
        return _per_tool_value(key, raw)
    if key in TOOL_BIN_KEYS:
        return _tool_bin_value(key, raw)
    keys = scalar_keys()
    if key not in keys:
        raise ValueError("unknown or non-scalar key: %s (settable: %s)" % (key, ", ".join(sorted(keys))))
    default = keys[key]
    text = str(raw).strip()
    if isinstance(default, bool):
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError("%s expects true or false, got %r" % (key, raw))
    if isinstance(default, int):
        try:
            value = int(text)
        except ValueError:
            raise ValueError("%s expects a whole number, got %r" % (key, raw)) from None
        if value < 0:
            raise ValueError("%s must not be negative" % key)
        return value
    if key == "cloud_base_url":
        from timetrace.cloud import check_base_url  # local: cloud must not import config
        return check_base_url(text)
    return text


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def load_user(home_dir: Path) -> Dict[str, Any]:
    """config.json exactly as the user wrote it (no defaults merged in)."""
    path = Path(home_dir) / "config.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError("config.json must contain a JSON object")
    return doc


def _write_user(home_dir: Path, doc: Dict[str, Any]) -> None:
    """Atomic write of config.json with mode 0600."""
    path = Path(home_dir) / "config.json"
    Path(home_dir).mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".config.json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def explicit_tool_bin(home_dir: Path, tool: str) -> str:
    """The `<tool>.bin` the user chose, "" when unset, the bare default name
    or a value timetrace recorded on its own."""
    section = load_user(home_dir).get(tool)
    if not isinstance(section, dict) or section.get("bin_source") == BIN_SOURCE_AUTO:
        return ""
    value = section.get("bin")
    return value if isinstance(value, str) and value and value != tool else ""


def store_tool_bin(home_dir: Path, tool: str, path: str) -> None:
    """Record a discovered absolute tool path, marked as found automatically."""
    doc = load_user(home_dir)
    section = dict(doc.get(tool)) if isinstance(doc.get(tool), dict) else {}
    if section.get("bin") == path and section.get("bin_source") == BIN_SOURCE_AUTO:
        return
    section["bin"] = path
    section["bin_source"] = BIN_SOURCE_AUTO
    doc[tool] = section
    _write_user(home_dir, doc)


def set_value(home_dir: Path, key: str, raw: str) -> Any:
    """Validate and write one key into config.json (atomic, 0600)."""
    value = parse_value(key, raw)
    doc = load_user(home_dir)
    if key.startswith(PER_TOOL_KEY + ".") or key in TOOL_BIN_KEYS:
        section, sub = key.split(".", 1)
        nested = doc.get(section)
        nested = dict(nested) if isinstance(nested, dict) else {}
        nested[sub] = value
        if key in TOOL_BIN_KEYS:
            nested.pop("bin_source", None)  # the user's choice from now on
        doc[section] = nested
    else:
        doc[key] = value
    _write_user(home_dir, doc)
    return value
