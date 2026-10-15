"""dev_status jobs: read-only development facts of a registered repository.

Only fixed commands run: git (with SAFE_GIT: no fsmonitor, no hooks) and the
read-only `gh pr view` / `gh run list|view` / `gh api` calls. Nothing from the
job becomes a command: PR links are validated and passed as one argument,
document paths must resolve inside the workspace. git and gh keep their own
credentials; remotes are reported without user info and every text field is
redacted before it leaves this computer.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .redact import redact
from .worktree import SAFE_GIT

TIMEOUT = 20
MAX_FILES = 200
MAX_DOC_BYTES = 1 << 20
EXCERPT_CHARS = 1200
FAILURE_EXCERPT_CHARS = 1500
MAX_RUNS = 6
MAX_PRS = 10
MAX_DOCS = 10

PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,9})$")
SENSITIVE = re.compile(r"(?i)(^|/)\.env|secret|credential|password|token|id_rsa|\.(pem|key|p8|p12|pfx|keystore)$")
RUN_IN_URL = re.compile(r"/actions/runs/([0-9]{1,20})(?:/|$)")

Runner = Callable[..., Tuple[int, str, str]]


def run(argv: List[str], cwd: Optional[str] = None, timeout: int = TIMEOUT, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """One fixed command, no shell, bounded time."""
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return done.returncode, done.stdout, done.stderr


def _git(path: str, *args: str, run: Runner = run) -> Tuple[int, str, str]:
    return run(["git"] + list(SAFE_GIT) + ["-C", path] + list(args))


def sanitize_remote(url: str) -> str:
    """host/owner/repo of a remote, without scheme, user info or `.git`."""
    url = (url or "").strip()
    if not url:
        return ""
    scp = re.match(r"^[A-Za-z0-9_.-]+@([A-Za-z0-9.-]+):(.+)$", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return ""
        host, path = parts.hostname, parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.match(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$", path):
        return ""
    return "%s/%s" % (host.lower(), path)


def _status_entries(raw: str) -> List[Dict[str, str]]:
    """`git status --porcelain=v1 -z` entries (a rename's source is skipped)."""
    out, items, i = [], raw.split("\0"), 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        state, path = item[:2], item[3:]
        if "R" in state or "C" in state:
            i += 1  # the original path follows
        out.append({"path": path, "state": state.strip() or "?"})
    return out


def local_git(path: str, run: Runner = run) -> Dict[str, Any]:
    code, branch, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD", run=run)
    code2, head, _ = _git(path, "rev-parse", "HEAD", run=run)
    if code != 0 or code2 != 0:
        return {"status": "failed", "message": "读取仓库失败（不是 Git 仓库或还没有提交）"}
    _, status, _ = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", run=run)
    entries = _status_entries(status)
    code, remote, _ = _git(path, "remote", "get-url", "origin", run=run)
    _, subject, _ = _git(path, "log", "-1", "--format=%s", "HEAD", run=run)
    return {
        "status": "ok", "remote": sanitize_remote(remote) if code == 0 else "", "branch": branch.strip(),
        "head": head.strip(), "head_subject": redact(subject.strip())[:200], "dirty": bool(entries),
        "changed_count": len(entries), "changed_files": entries[:MAX_FILES],
    }


def valid_doc_path(rel: str) -> bool:
    if not isinstance(rel, str) or not rel or len(rel) > 300 or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return False
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        return False
    return not SENSITIVE.search(rel)


def doc_facts(root: str, rel: str, run: Runner = run) -> Dict[str, Any]:
    if not valid_doc_path(rel):
        return {"path": rel if isinstance(rel, str) else "", "status": "failed", "message": "路径不安全或可能含凭据，未读取"}
    base = Path(root).resolve()
    target = base / rel
    if target.is_symlink() or not target.exists():
        return {"path": rel, "status": "unavailable", "message": "文件不存在" if not target.is_symlink() else "符号链接不读取"}
    resolved = target.resolve()
    if base not in resolved.parents or not resolved.is_file():
        return {"path": rel, "status": "failed", "message": "文件不在仓库内"}
    size = resolved.stat().st_size
    if size > MAX_DOC_BYTES:
        return {"path": rel, "status": "unavailable", "message": "文件超过 1MB，未读取"}
    data = resolved.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": rel, "status": "failed", "message": "不是文本文件"}
    _, log, _ = _git(root, "log", "-1", "--format=%H%x09%cI", "--", rel, run=run)
    last, last_at = (log.strip().split("\t") + ["", ""])[:2] if log.strip() else ("", "")
    since = 0
    if last:
        code, count, _ = _git(root, "rev-list", "--count", "%s..HEAD" % last, run=run)
        since = int(count.strip()) if code == 0 and count.strip().isdigit() else 0
    _, status, _ = _git(root, "status", "--porcelain=v1", "--untracked-files=normal", "--", rel, run=run)
    return {
        "path": rel, "status": "ok", "sha256": hashlib.sha256(data).hexdigest(), "size": size,
        "excerpt": redact(text[:EXCERPT_CHARS]), "last_commit": last, "last_commit_at": last_at,
        "commits_since": since, "dirty": bool(status.strip()),
    }


def _gh_env() -> Dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "TMPDIR", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(GH_PROMPT_DISABLED="1", NO_COLOR="1", GH_NO_UPDATE_NOTIFIER="1", GH_PAGER="cat")
    return env


def _gh(run: Runner, *args: str) -> Tuple[int, str, str]:
    return run(["gh"] + list(args), env=_gh_env())


def _gh_error(stderr: str) -> Tuple[str, str]:
    text = (stderr or "").lower()
    if "rate limit" in text:
        return "unavailable", "GitHub 限流，稍后再试"
    if "could not resolve" in text or "not found" in text or "404" in text:
        return "failed", "PR 不存在或没有权限访问"
    if "401" in text or "authentication" in text or "not logged" in text or "403" in text:
        return "unauthorized", "电脑上的 gh 没有权限读取这个仓库"
    if "timed out" in text:
        return "unavailable", "读取 GitHub 超时"
    return "failed", "读取 PR 失败"


def _run_entry(run: Runner, repo: str, run_id: int) -> Dict[str, Any]:
    code, out, _ = _gh(run, "run", "view", str(run_id), "--repo", repo, "--json",
                       "databaseId,headSha,event,status,conclusion,workflowName,url,jobs,createdAt,updatedAt")
    if code != 0:
        return {"id": run_id, "status": "unknown", "message": "读取检查运行失败"}
    try:
        data = json.loads(out)
    except ValueError:
        return {"id": run_id, "status": "unknown", "message": "检查运行结果无法解析"}
    entry = {
        "id": run_id, "workflow": data.get("workflowName") or "", "event": data.get("event") or "",
        "head_sha": data.get("headSha") or "", "status": data.get("status") or "", "conclusion": data.get("conclusion") or "",
        "url": data.get("url") or "", "created_at": data.get("createdAt") or "", "updated_at": data.get("updatedAt") or "",
        "jobs": [{"name": j.get("name") or "", "conclusion": j.get("conclusion") or "",
                  "steps": [{"name": s.get("name") or "", "conclusion": s.get("conclusion") or ""} for s in j.get("steps") or []]}
                 for j in data.get("jobs") or []],
    }
    code, path, _ = _gh(run, "api", "repos/%s/actions/runs/%d" % (repo, run_id), "--jq", ".path")
    entry["workflow_path"] = path.strip() if code == 0 else ""
    if entry["conclusion"] in ("failure", "timed_out"):
        code, log, _ = _gh(run, "run", "view", str(run_id), "--repo", repo, "--log-failed")
        if code == 0 and log:
            entry["failure_excerpt"] = redact(log[-FAILURE_EXCERPT_CHARS:])
    return entry


def pr_facts(urls: List[str], run: Runner = run, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    urls = list(urls)[:MAX_PRS]
    if not urls:
        return []
    try:
        installed = _gh(run, "--version")[0] == 0
    except (FileNotFoundError, PermissionError, OSError):
        installed = False
    if not installed:
        return [{"url": u, "status": "unavailable", "message": "电脑上未安装 gh（GitHub CLI）"} for u in urls]
    if _gh(run, "auth", "status", "--hostname", "github.com")[0] != 0:
        return [{"url": u, "status": "unauthorized", "message": "电脑上的 gh 未登录 GitHub"} for u in urls]
    out = []
    for url in urls:
        match = PR_URL.match(url) if isinstance(url, str) else None
        if not match:
            out.append({"url": url if isinstance(url, str) else "", "status": "failed", "message": "PR 链接无效"})
            continue
        if deadline is not None and time.time() > deadline:
            out.append({"url": url, "status": "unavailable", "message": "采集超时，未读取"})
            continue
        repo = "%s/%s" % (match.group(1), match.group(2))
        code, raw, err = _gh(run, "pr", "view", url, "--json",
                             "number,state,isDraft,title,url,headRefName,headRefOid,baseRefName,baseRefOid,updatedAt,statusCheckRollup")
        if code != 0:
            status, message = _gh_error(err)
            out.append({"url": url, "status": status, "message": message})
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            out.append({"url": url, "status": "failed", "message": "PR 结果无法解析"})
            continue
        pr = {"number": data.get("number"), "state": data.get("state") or "", "is_draft": bool(data.get("isDraft")),
              "title": redact(data.get("title") or "")[:200], "url": data.get("url") or url,
              "head_ref": data.get("headRefName") or "", "head_sha": data.get("headRefOid") or "",
              "base_ref": data.get("baseRefName") or "", "base_sha": data.get("baseRefOid") or "",
              "updated_at": data.get("updatedAt") or ""}
        ids: List[int] = []
        for item in data.get("statusCheckRollup") or []:
            found = RUN_IN_URL.search(str(item.get("detailsUrl") or ""))
            if found and int(found.group(1)) not in ids:
                ids.append(int(found.group(1)))
        if pr["head_ref"]:
            # Earlier heads' runs, so a newer push shows the old result as history.
            code, listed, _ = _gh(run, "run", "list", "--repo", repo, "--branch", pr["head_ref"], "--limit", str(MAX_RUNS),
                                  "--json", "databaseId")
            if code == 0:
                try:
                    for item in json.loads(listed):
                        if isinstance(item.get("databaseId"), int) and item["databaseId"] not in ids:
                            ids.append(item["databaseId"])
                except ValueError:
                    pass
        runs = []
        for run_id in ids[:MAX_RUNS]:
            if deadline is not None and time.time() > deadline:
                break
            runs.append(_run_entry(run, repo, run_id))
        out.append({"url": url, "status": "ok", "pr": pr, "runs": runs})
    return out


def collect(path: str, spec: Dict[str, Any], run: Runner = run, deadline: Optional[float] = None) -> Dict[str, Any]:
    docs = [d for d in spec.get("docs") or [] if isinstance(d, str)][:MAX_DOCS]
    prs = [u for u in spec.get("prs") or [] if isinstance(u, str)][:MAX_PRS]
    return {"local_git": local_git(path, run=run), "docs": [doc_facts(path, d, run=run) for d in docs],
            "prs": pr_facts(prs, run=run, deadline=deadline)}
