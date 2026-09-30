"""Self-check reported with the inventory as `health` (protocol 2).

{claude_login, codex_login: ok|expired|missing, disk_free_gb (workspace
volume), workspaces: [{id, exists, git, clean?}], checked_at}. The agent adds
`sleep_prevention` when it builds the inventory. Every check is read-only:
the tools' own login status commands (as the zero-spend gate uses them) and
`git status` in the registered main checkout with hooks and fsmonitor off.
"""
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from timetrace import folder, tiers, worktree

GIT_TIMEOUT_SECONDS = 20
MAX_WORKSPACES = 100


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _verdict(adapter: Any):
    billing = getattr(adapter, "billing", None)
    try:
        return billing.verdict() if billing is not None else None
    except Exception:
        return None


def login_state(verdict: Any, credentials_present: bool) -> str:
    """ok | expired | missing from the zero-spend verdict (`claude auth
    status` / `codex login status`) and whether a local login exists."""
    if verdict is None:
        return "missing"
    if verdict.verified:
        return "ok"
    reason = str(verdict.reason or "")
    if reason.startswith("auth_status_unavailable"):
        return "missing"          # the tool cannot even be asked
    if reason in ("not_logged_in", "subscription_unknown"):
        return "expired" if credentials_present else "missing"
    # Billing problems (an API key in the environment, …) are not a login
    # problem: the tool entry's can_enforce_zero_spend already says so.
    return "ok"


def claude_login(adapter: Any) -> str:
    try:
        present = bool(tiers.claude_oauth(adapter.credentials_path()))
    except Exception:
        present = False
    return login_state(_verdict(adapter), present)


def codex_login(adapter: Any) -> str:
    try:
        present = adapter._auth_path().is_file()
    except Exception:
        present = False
    return login_state(_verdict(adapter), present)


def git_clean(path: str, run: Callable = subprocess.run) -> Optional[bool]:
    """True when the checkout has no tracked changes; None when unknown."""
    try:
        proc = run(["git"] + list(worktree.SAFE_GIT) + ["-C", path, "status", "--porcelain", "--untracked-files=no"],
                   capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return not proc.stdout.strip()


def disk_free_gb(paths: List[str], home: Path, usage: Callable = shutil.disk_usage) -> Optional[float]:
    """Free space of the fullest volume holding a workspace (else the home)."""
    free = []
    for path in [p for p in paths if os.path.isdir(p)] or [str(home)]:
        try:
            free.append(usage(path).free)
        except OSError:
            continue
    return round(min(free) / 1e9, 1) if free else None


def check(adapters: Dict[str, Any], workspaces: List[Dict[str, Any]], home: Path,
          usage: Callable = shutil.disk_usage, clean: Callable = git_clean) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    if "claude" in adapters:
        report["claude_login"] = claude_login(adapters["claude"])
    if "codex" in adapters:
        report["codex_login"] = codex_login(adapters["codex"])
    entries = []
    paths = []
    for ws in workspaces[:MAX_WORKSPACES]:
        path = str(ws.get("path") or "")
        exists = bool(path) and os.path.isdir(path)
        is_git = exists and (ws.get("kind") or folder.GIT) == folder.GIT and worktree.is_git_repo(path)
        entry = {"id": str(ws.get("id"))[:64], "exists": exists, "git": bool(is_git)}
        if is_git:
            state = clean(path)
            if state is not None:
                entry["clean"] = state
        if exists:
            paths.append(path)
        entries.append(entry)
    report["workspaces"] = entries
    free = disk_free_gb(paths, home, usage)
    if free is not None:
        report["disk_free_gb"] = free
    report["checked_at"] = now_rfc3339()
    return report
