"""Explicit, commit-bound releases using the operator's existing named checks.

The server/model supplies no commands. Deployment is one-shot: an interrupted
side effect remains unknown and is never blindly replayed. Evidence is local,
redacted and durable, suitable for attaching to the existing handoff.
"""
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from timetrace import checks, objective_checks, worktree

NAMES = ("release-prepare", "release-deploy", "release-health", "release-rollback", "release-rollback-health")


def _write(path, value):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextmanager
def _lock(home, workspace):
    root = Path(home) / "releases"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / (hashlib.sha256(workspace.encode()).hexdigest() + ".lock")
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有发布操作正在运行")
        yield root


def _read(home, release_id):
    if not re.fullmatch(r"[a-f0-9]{32}", release_id):
        raise ValueError("发布编号无效")
    path = Path(home) / "releases" / release_id / "receipt.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _pinned_commands(db, workspace):
    commands = {}
    trusted = {}
    for name in NAMES:
        argv = db.get_check(workspace["id"], name)
        if not argv or checks.argv_problem(argv) or not os.path.isabs(argv[0]):
            raise ValueError("请先在本机登记固定检查 %s，使用绝对程序路径" % name)
        commands[name] = argv
        # Pin external scripts and interpreters; repository files are bound by commit.
        for arg in argv:
            if os.path.isabs(arg) and Path(arg).is_file():
                trusted[arg] = objective_checks.file_hash(arg)
    return commands, trusted


def _step(path, receipt, name, cfg):
    receipt["state"] = name + ":running"
    _write(path, receipt)  # Persist intent BEFORE any external side effect.
    contract = receipt["contract"]
    env = dict(os.environ, TIMETRACE_RELEASE_ID=receipt["id"],
               TIMETRACE_RELEASE_COMMIT=contract["commit"],
               TIMETRACE_RELEASE_PREVIOUS=contract["previous_commit"])
    keep = list(cfg.get("check_env_keep", [])) + ["TIMETRACE_RELEASE_ID", "TIMETRACE_RELEASE_COMMIT", "TIMETRACE_RELEASE_PREVIOUS"]
    result = checks.run(name, contract["commands"][name], contract["checkout"],
                        str(path.parent / (name + ".log")), timeout=contract["timeout_seconds"],
                        extra_drop=cfg.get("check_env_drop", []), keep=keep, env=env)
    receipt["steps"].append(dict(result.as_event(), observed_at=time.time()))
    receipt["state"] = name + (":passed" if result.passed else ":failed")
    _write(path, receipt)
    return result.passed


def _snapshot(checkout):
    """Bind the prepared build as well as tracked source, without following links."""
    root = Path(checkout)
    digest = hashlib.sha256()
    size = count = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files + [d for d in dirs if (Path(parent) / d).is_symlink()]):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative == ".git":
                continue
            count += 1
            if count > 100000:
                raise ValueError("发布产物文件过多，请先缩小构建范围")
            digest.update(relative.encode() + b"\0")
            if path.is_symlink():
                digest.update(b"link:" + os.readlink(path).encode())
                continue
            import stat
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("发布副本包含非普通文件")
            size += path.stat().st_size
            if size > 512 * 1024 * 1024:
                raise ValueError("发布副本超过 512 MiB，请先缩小构建范围")
            digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
            with open(path, "rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            digest.update(b"\0")
    return digest.hexdigest()


def prepare(home, db, workspace, previous, cfg, timeout=300):
    if workspace.get("kind", "git") != "git" or not 1 <= timeout <= 1800:
        raise ValueError("发布需要 Git 项目，超时范围 1–1800 秒")
    commit = worktree.resolve_commit(workspace["path"], "HEAD")
    previous_commit = worktree.resolve_commit(workspace["path"], previous)
    if not commit or not previous_commit:
        raise ValueError("当前版本或回滚版本无法解析")
    commands, trusted = _pinned_commands(db, workspace)
    with _lock(home, workspace["id"]) as root:
        release_id = uuid.uuid4().hex
        folder = root / release_id
        folder.mkdir(mode=0o700)
        checkout = folder / "checkout"
        # Existing worktree helper disables hooks and remote push in AI copies.
        worktree.add_detached(workspace["path"], checkout, commit)
        contract = {"workspace_id": workspace["id"], "source": workspace["path"], "commit": commit,
                    "previous_commit": previous_commit, "checkout": str(checkout), "commands": commands,
                    "trusted_files": trusted, "timeout_seconds": timeout,
                    "automatic_rollback": True}
        receipt = {"id": release_id, "contract": contract, "authorization": objective_checks.digest(contract),
                   "state": "preparing", "steps": [], "created_at": time.time(), "approved_at": None}
        path = folder / "receipt.json"
        _write(path, receipt)
        if _step(path, receipt, "release-prepare", cfg):
            receipt["contract"]["prepared_tree_sha256"] = _snapshot(checkout)
            receipt["authorization"] = objective_checks.digest(receipt["contract"])
            receipt["state"] = "prepared"
            _write(path, receipt)
        return receipt


def execute(home, release_id, authorization, cfg):
    path, receipt = _read(home, release_id)
    with _lock(home, receipt["contract"]["workspace_id"]):
        path, receipt = _read(home, release_id)
        c = receipt["contract"]
        if authorization != receipt["authorization"] or authorization != objective_checks.digest(c):
            raise ValueError("授权必须匹配这份具体发布方案")
        # Return a completed receipt on a retried request. Never repeat a deploy.
        if receipt["state"] in ("healthy", "rolled_back", "rollback_failed"):
            return receipt
        if receipt["state"] != "prepared":
            raise ValueError("发布结果不确定或准备未通过；请先核对发布平台状态，禁止重放发布命令")
        if worktree.resolve_commit(c["checkout"], "HEAD") != c["commit"]:
            raise ValueError("发布副本版本已变化")
        import subprocess
        if subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=c["checkout"]).returncode:
            raise ValueError("发布副本存在源码改动，请重新准备")
        if any(objective_checks.file_hash(p) != sha for p, sha in c["trusted_files"].items()):
            raise ValueError("发布命令或检查器已变化，请重新准备并授权")
        if _snapshot(c["checkout"]) != c["prepared_tree_sha256"]:
            raise ValueError("准备后的构建产物已变化，请重新准备并授权")
        receipt["approved_at"] = time.time()
        _write(path, receipt)
        deployed = _step(path, receipt, "release-deploy", cfg)
        if receipt["steps"][-1]["timed_out"]:
            receipt["state"] = "deployment_unknown"
            _write(path, receipt)
            return receipt
        healthy = deployed and _step(path, receipt, "release-health", cfg)
        if healthy:
            receipt["state"] = "healthy"
        else:
            # The approved contract includes this exact rollback and its check.
            rolled_back = _step(path, receipt, "release-rollback", cfg)
            restored = rolled_back and _step(path, receipt, "release-rollback-health", cfg)
            receipt["state"] = "rolled_back" if restored else "rollback_failed"
        receipt["completed_at"] = time.time()
        _write(path, receipt)
        return receipt


def status(home, release_id):
    return _read(home, release_id)[1]
