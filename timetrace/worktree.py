"""Per-task git worktree isolation with push disabled (spec §8)."""
import subprocess
import hashlib
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

NO_PUSH_URL = "no_push://blocked"

# Every git call the runner makes inside a worktree a model has touched runs
# with these overrides: no fsmonitor command, no hooks. Config-driven command
# execution is otherwise closed by verify_metadata().
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")

_CONFIG_WORKTREE_LINE = re.compile(r'^(?:\[remote "[^"\\\n]*"\]|pushurl = ' + re.escape(NO_PUSH_URL) + r')$')


class MetadataTampered(ValueError):
    """The worktree's git metadata no longer matches what the runner created."""


def snapshot(path: str) -> Tuple[str, str]:
    """Bind HEAD, index, tracked changes and untracked content (including ignored
    files). Filenames alone cannot detect edits to an already-dirty file."""
    head = _git(*SAFE_GIT, "-C", path, "rev-parse", "HEAD")
    digest = hashlib.sha256()
    for args in (("diff", "--binary", "HEAD", "--"), ("diff", "--cached", "--binary", "HEAD", "--")):
        digest.update(subprocess.check_output(["git"] + list(SAFE_GIT) + ["-C", path, args[0], "--no-ext-diff",
                                               "--ignore-submodules=all"] + list(args[1:])))
    # A gitlink belongs to the parent index, not to the child working tree.
    # Bind its commit (and conflict stage) without opening the child directory.
    gitlinks = {}
    index = subprocess.check_output(["git"] + list(SAFE_GIT) + ["-C", path, "ls-files", "--stage", "-z"])
    for entry in index.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, commit, stage = metadata.split()
        if mode == b"160000":
            gitlinks.setdefault(name, []).append(stage + b":" + commit)
    # Read tracked bytes too: git diff deliberately hides assume-unchanged and
    # skip-worktree entries and therefore cannot be our content authority.
    files = subprocess.check_output(["git"] + list(SAFE_GIT) + ["-C", path, "ls-files", "--cached", "--others", "-z"])
    for name in sorted(set(files.split(b"\0"))):
        if not name:
            continue
        digest.update(name + b"\0")
        if name in gitlinks:
            digest.update(b"gitlink\0" + b"\0".join(sorted(gitlinks[name])) + b"\0")
            continue
        file = Path(path) / os.fsdecode(name)
        if not file.exists() and not file.is_symlink():
            digest.update(b"missing\0")
            continue
        digest.update(str(file.lstat().st_mode).encode() + b"\0")
        if file.is_symlink():
            digest.update(os.fsencode(os.readlink(file)))
        elif file.is_file():
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ValueError("cannot checkpoint untracked non-file: %s" % file)
        digest.update(b"\0")
    return head, digest.hexdigest()


def same_repository(path: str, registered: str) -> bool:
    """A saved execution path must still belong to the registered repository."""
    def common(p):
        value = _git("-C", p, "rev-parse", "--git-common-dir")
        return (Path(p) / value).resolve()
    return common(path) == common(registered)


def git_common_dir(path: str) -> str:
    """Absolute path of the repository metadata a checkout writes to. For a
    linked worktree that is the main repo's .git, outside the worktree itself."""
    common = _git("-C", path, "rev-parse", "--git-common-dir")
    return str((Path(path) / common).resolve()) if not os.path.isabs(common) else str(Path(common).resolve())


def _read_small(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise MetadataTampered("%s is not a regular file" % path.name)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(64 * 1024)


def verify_metadata(path: str, repo: str) -> None:
    """Refuse to run git in `path` unless its metadata is what `ensure` made.

    A sandboxed run can write the worktree and its per-worktree admin dir.
    Redirecting `.git` / `commondir`, or adding keys such as core.fsmonitor or
    include.path to config.worktree, would make the runner's own unsandboxed
    git calls execute attacker-chosen commands. The registered repository is
    the trusted anchor: its common dir is not writable from the sandbox."""
    root = Path(path)
    common = Path(git_common_dir(repo)).resolve()
    dotgit = root / ".git"
    if dotgit.is_dir() and not dotgit.is_symlink():
        if root.resolve() == Path(repo).resolve():
            return  # the registered main checkout itself
        raise MetadataTampered("not a linked worktree of the registered repository")
    match = re.fullmatch(r"gitdir: (.+?)\n?", _read_small(dotgit))
    if not match:
        raise MetadataTampered(".git file is malformed")
    admin = Path(match.group(1))
    admin = (admin if admin.is_absolute() else root / admin)
    if admin.is_symlink() or admin.resolve().parent != common / "worktrees":
        raise MetadataTampered(".git points outside the registered repository")
    admin = admin.resolve()
    commondir = _read_small(admin / "commondir").strip()
    target = Path(commondir) if os.path.isabs(commondir) else admin / commondir
    if target.resolve() != common:
        raise MetadataTampered("commondir points outside the registered repository")
    config_worktree = admin / "config.worktree"
    if config_worktree.exists() or config_worktree.is_symlink():
        for line in _read_small(config_worktree).splitlines():
            line = line.strip()
            if line and not _CONFIG_WORKTREE_LINE.match(line):
                raise MetadataTampered("config.worktree holds keys the runner did not write")


def sandbox_write_roots(path: str) -> List[str]:
    """Directories outside a linked worktree that `git add`/`git commit` in it
    must write: its own admin dir, the object store, and the directory of its
    branch ref (plus that ref's reflog dir). Never the whole common dir: the
    shared config and hooks there are executed by unsandboxed git later."""
    root = Path(path).resolve()
    common = Path(git_common_dir(path)).resolve()
    if root == common or root in common.parents:
        return []
    admin = Path(_git(*SAFE_GIT, "-C", path, "rev-parse", "--absolute-git-dir")).resolve()
    if admin.parent != common / "worktrees":
        return []
    roots = [admin, common / "objects"]
    branch = _git(*SAFE_GIT, "-C", path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch and ".." not in branch.split("/"):
        for base in (common / "refs" / "heads", common / "logs" / "refs" / "heads"):
            ref_dir = (base / branch).parent
            ref_dir.mkdir(parents=True, exist_ok=True)
            roots.append(ref_dir)
    return [str(r) for r in roots]


def _git(*args: str, cwd: str = None) -> str:
    out = subprocess.run(
        ["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def is_git_repo(path: str) -> bool:
    try:
        return _git("-C", path, "rev-parse", "--is-inside-work-tree") == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def branch_name(task_id: int) -> str:
    return "timetrace/%d" % task_id


_TASK_BRANCH = re.compile(r"timetrace/[a-z0-9_./-]{1,100}")


def valid_task_branch(name) -> bool:
    """A server-chosen branch (sub-tasks: `timetrace/<stage>/<sub>`) is used only
    when it stays under timetrace/ and is a plain, valid git branch name."""
    if not isinstance(name, str) or not _TASK_BRANCH.fullmatch(name):
        return False
    parts = name.split("/")
    return (".." not in name and all(parts) and not name.endswith(".")
            and not any(p.startswith(".") or p.endswith(".lock") for p in parts))


class BranchConflict(ValueError):
    """The task branch cannot exist next to an existing branch: git stores
    `timetrace/bd` as a file, so `timetrace/bd/ui` (a directory entry) is impossible."""


def unique_branch(requested, job_key: str) -> Optional[str]:
    """A server-chosen sub-task branch made unique per execution job
    (`<requested>-<8 hex of the job id>`): two jobs given the same name never
    share, and so never fight over, a branch. None when either name is invalid
    (the caller falls back to `timetrace/<id>`)."""
    if not valid_task_branch(requested):
        return None
    name = "%s-%s" % (requested, hashlib.sha256(str(job_key).encode("utf-8")).hexdigest()[:8])
    return name if valid_task_branch(name) else None


def branch_conflict(repo: str, branch: str) -> Optional[str]:
    """An existing branch that is a directory prefix of `branch` or has
    `branch` as its directory prefix, if any."""
    refs = _git(*SAFE_GIT, "-C", repo, "for-each-ref", "--format=%(refname)", "refs/heads/")
    for ref in refs.splitlines():
        other = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if branch.startswith(other + "/") or other.startswith(branch + "/"):
            return other
    return None


def ensure(repo: str, task_id: int, home: Path, base: str = "HEAD", branch: str = None) -> Tuple[str, str]:
    """Create (once) the worktree for task_id and return (path, branch).

    `base` is the ref the task branch starts from: HEAD of the repo by default, or the
    branch of the task this one depends on, so follow-up work builds on the previous step.
    `branch` overrides the default `timetrace/<task_id>` (must pass valid_task_branch).
    """
    path = Path(home) / "worktrees" / str(task_id)
    if branch is not None and not valid_task_branch(branch):
        raise ValueError("invalid task branch name")
    branch = branch or branch_name(task_id)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if _branch_exists(repo, branch):
            # SAFE_GIT: `worktree add` runs the repository's post-checkout hook.
            _git(*SAFE_GIT, "-C", repo, "worktree", "add", str(path), branch)
        else:
            if base != "HEAD" and not _branch_exists(repo, base):
                raise ValueError("registered base branch no longer exists: %s" % base)
            conflict = branch_conflict(repo, branch)
            if conflict:
                raise BranchConflict("无法创建任务分支 %s：与仓库里已有的分支 %s 冲突（git 不允许一个分支名同时是"
                                     "另一个分支的目录）。请删除或改名旧分支，或给子任务换一个名字后重试。" % (branch, conflict))
            _git(*SAFE_GIT, "-C", repo, "worktree", "add", "-b", branch, str(path), base)
    else:
        verify_metadata(str(path), repo)
        if _git(*SAFE_GIT, "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD") != branch:
            raise ValueError("remote worktree is on an unexpected branch: %s" % path)
    block_push(str(path))
    exclude_output_dir(str(path))
    return str(path), branch


OUTPUT_EXCLUDE = "/.timetrace/out/"
# Earlier runs' `.timetrace/out`, moved aside before a fresh run (results.prepare_out_dir).
PREVIOUS_EXCLUDE = "/.timetrace/out.prev-*/"


def exclude_output_dir(path: str) -> None:
    """Keep the runner's `.timetrace/out/` (structured results) and the moved-aside
    copies of earlier runs out of every commit. `info/exclude` lives in the
    common dir, outside the Codex sandbox's writable roots, so the model
    cannot undo it."""
    exclude = Path(_git(*SAFE_GIT, "-C", path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = Path(path) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    missing = [p for p in (OUTPUT_EXCLUDE, PREVIOUS_EXCLUDE) if p not in current.splitlines()]
    if not missing:
        return
    with open(exclude, "a", encoding="utf-8") as fh:
        if current and not current.endswith("\n"):
            fh.write("\n")
        for pattern in missing:
            fh.write(pattern + "\n")


def head(path: str, repo: str) -> str:
    """HEAD of an execution worktree, read only after its metadata is verified
    (H-2): the model had write access to that directory."""
    verify_metadata(path, repo)
    return _git(*SAFE_GIT, "-C", path, "rev-parse", "HEAD")


def _branch_exists(repo: str, branch: str) -> bool:
    try:
        _git(*SAFE_GIT, "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
        return True
    except subprocess.CalledProcessError:
        return False


def remotes(path: str) -> List[str]:
    out = _git(*SAFE_GIT, "-C", path, "remote")
    return [r for r in out.splitlines() if r.strip()]


def block_push(path: str) -> None:
    """Point every remote's pushurl at an invalid scheme so `git push` cannot work.

    Written with `--worktree` so only this worktree is affected; the user's main
    checkout keeps its normal push URL. Requires extensions.worktreeConfig, which
    is switched on in the shared config (harmless for the main checkout).
    """
    rs = remotes(path)
    if not rs:
        return
    _git(*SAFE_GIT, "-C", path, "config", "extensions.worktreeConfig", "true")
    for r in rs:
        _git(*SAFE_GIT, "-C", path, "config", "--worktree", "remote.%s.pushurl" % r, NO_PUSH_URL)


# ---- read-only copies for review / check jobs ---------------------------------
_HEX = re.compile(r"[0-9a-fA-F]{7,64}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
DIFF_STAT_BYTES = 8 * 1024


def branch_commit(repo: str, branch) -> Optional[str]:
    """The commit a local timetrace/* branch points at, or None."""
    if not valid_task_branch(branch):
        return None
    try:
        return _git(*SAFE_GIT, "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch)
    except subprocess.CalledProcessError:
        return None


def resolve_commit(repo: str, ref) -> Optional[str]:
    """A server-named base (`base_ref`): a hex commit id, a local branch
    name or HEAD, resolved to a commit id; None when it is none of these.
    Only the resolved id is ever passed on to git."""
    if not isinstance(ref, str):
        return None
    if ref == "HEAD":
        spec = "HEAD^{commit}"
    elif _HEX.fullmatch(ref):
        spec = ref + "^{commit}"
    elif (_REF.fullmatch(ref) and ".." not in ref and "//" not in ref and not ref.endswith(("/", ".", ".lock"))
          and "@{" not in ref):
        spec = "refs/heads/%s^{commit}" % ref
    else:
        return None
    try:
        return _git(*SAFE_GIT, "-C", repo, "rev-parse", "--verify", "--quiet", spec) or None
    except subprocess.CalledProcessError:
        return None


def diff_stat(repo: str, base: str, head_commit: str, limit: int = DIFF_STAT_BYTES) -> str:
    """`git diff --stat <base>...<head>` of two resolved commit ids, at most
    `limit` UTF-8 bytes; "" when git cannot compute it."""
    if not (_HEX.fullmatch(base or "") and _HEX.fullmatch(head_commit or "")):
        return ""
    try:
        out = _git(*SAFE_GIT, "-C", repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                   "--stat=200,160", "%s...%s" % (base, head_commit), "--")
    except subprocess.CalledProcessError:
        return ""
    data = out.encode("utf-8")
    if len(data) <= limit:
        return out
    note = "\n…（差异摘要过长，已截断）"
    cut = data[:limit - len(note.encode("utf-8"))].decode("utf-8", errors="ignore")
    return cut[:cut.rfind("\n")] + note if "\n" in cut else cut + note


def remove_tree(path) -> None:
    """Delete a runner-made directory without following symlinks in it."""
    import shutil
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(str(path))


def add_detached(repo: str, path, commit: str) -> str:
    """A fresh detached checkout of `commit` at `path` (anything there is
    removed first). Stale worktree registrations are pruned so a reused path
    can be added again."""
    if not _HEX.fullmatch(commit or ""):
        raise ValueError("invalid commit")
    path = Path(path)
    remove_tree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(*SAFE_GIT, "-C", repo, "worktree", "prune")
    _git(*SAFE_GIT, "-C", repo, "worktree", "add", "--detach", str(path), commit)
    return str(path.resolve())


def remove_detached(repo: str, path) -> None:
    """Delete a detached checkout and its registration (best effort)."""
    try:
        remove_tree(path)
    finally:
        try:
            _git(*SAFE_GIT, "-C", repo, "worktree", "prune")
        except (subprocess.CalledProcessError, OSError):
            pass
