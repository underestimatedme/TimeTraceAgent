"""Folder (non-git) workspaces: output directory, change detection, listing.

A folder workspace is source material (video footage, documents) with no git
history to isolate a run in. A task writes only into its own output directory
`<workspace>/timetrace-out/<name>/`; everything else in the workspace must look the
same afterwards. The before/after snapshot is the control that detects a run
that wrote elsewhere (the tools' own restrictions are best effort here).
"""
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

GIT = "git"
FOLDER = "folder"
KINDS = (GIT, FOLDER)
OUTPUT_ROOT = "timetrace-out"
OUTPUT_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}")
SNAPSHOT_LIMIT = 50_000
SHALLOW_DEPTH = 2          # levels checked when the workspace exceeds the limit
MAX_REPORTED = 20          # changed paths named in a failure message
LISTING_BYTES = 64 * 1024  # the folder artifact's file list


GIT_SCAN_DEPTH = 3         # the folder itself, its children and grandchildren
GIT_SCAN_LIMIT = 20_000    # directories looked at before the scan gives up


def nested_repository(path: str) -> Optional[str]:
    """The first `.git` entry found in `path` or up to two directory levels
    below it (symlinked directories not followed), as a path relative to
    `path`; None when there is none. A folder task can write next to such a
    repository, so a folder workspace may not hold one."""
    level, seen = [(path, "")], 0
    for depth in range(GIT_SCAN_DEPTH):
        below = []
        for directory, rel in level:
            if os.path.lexists(os.path.join(directory, ".git")):
                return (rel + "/.git") if rel else ".git"
            if depth + 1 >= GIT_SCAN_DEPTH:
                continue
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        seen += 1
                        if seen > GIT_SCAN_LIMIT:
                            return None
                        if entry.is_dir(follow_symlinks=False):
                            below.append((entry.path, (rel + "/" if rel else "") + entry.name))
            except OSError:
                continue
        level = below
    return None


def overlaps(a: str, b: str) -> bool:
    """One resolved path is the other or lies inside it."""
    pa, pb = Path(a), Path(b)
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def detect_kind(path: str) -> str:
    """`git` when the directory has a `.git` entry, otherwise `folder`."""
    return GIT if os.path.lexists(os.path.join(path, ".git")) else FOLDER


def _valid_name(name) -> bool:
    return isinstance(name, str) and bool(OUTPUT_NAME.fullmatch(name)) and name.strip(".") != ""


def output_name(job: Dict, plan_key: str) -> str:
    """The job's `output_name` when it is a plain file name, else the plan id
    reduced to the same alphabet. Never `.`, `..` or a path."""
    requested = job.get("output_name")
    if _valid_name(requested):
        return requested
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(plan_key or "job"))[:80]
    return name if _valid_name(name) else "job"


class OutputUnsafe(ValueError):
    """The output directory (or timetrace-out/) is not a plain directory."""


def _plain_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(str(path)).st_mode)
    except FileNotFoundError:
        return False


def prepare_output(workspace: str, name: str) -> str:
    """Create `<workspace>/timetrace-out/<name>/` (idempotent), never through a
    symlink, and return its resolved path."""
    if not _valid_name(name):
        raise OutputUnsafe("invalid output name")
    root = Path(workspace)
    path = root
    for part in (OUTPUT_ROOT, name):
        path = path / part
        if os.path.lexists(str(path)) and not _plain_dir(path):
            raise OutputUnsafe("%s is not a directory" % part)
        path.mkdir(exist_ok=True)
    resolved = path.resolve()
    if resolved.parent.parent != root.resolve():
        raise OutputUnsafe("output directory escapes the workspace")
    return str(resolved)


def output_intact(workspace: str, name: str) -> bool:
    """timetrace-out/ and the output directory are still plain directories."""
    root = Path(workspace)
    return _plain_dir(root / OUTPUT_ROOT) and _plain_dir(root / OUTPUT_ROOT / name)


@dataclass
class Snapshot:
    entries: Dict[str, Tuple] = field(default_factory=dict)
    capped: bool = False


def _walk(root: str, max_depth: Optional[int], limit: Optional[int], shallow: bool,
          skip: Optional[str] = OUTPUT_ROOT) -> Optional[Dict]:
    """`skip`: one relative path (e.g. `timetrace-out/<name>`) left out, or None."""
    entries: Dict[str, Tuple] = {}
    stack: List[Tuple[str, str, int]] = [(root, "", 1)]
    while stack:
        directory, prefix, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                children = list(it)
        except OSError:
            entries[prefix or "."] = ("unreadable", 0, 0)
            continue
        for entry in children:
            rel = prefix + entry.name
            if rel == skip:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                entries[rel] = ("missing", 0, 0)
                continue
            if stat.S_ISDIR(st.st_mode):
                # A directory's mtime changes with every entry added below it:
                # recorded only when deeper levels are not walked.
                entries[rel] = ("d", 0, st.st_mtime_ns if shallow else 0)
                if max_depth is None or depth < max_depth:
                    stack.append((entry.path, rel + "/", depth + 1))
            elif stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    target = ""
                entries[rel] = ("l:" + target, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            else:
                # ctime too: mtime can be set back (touch -r), ctime cannot.
                entries[rel] = ("f", st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if limit is not None and len(entries) > limit:
                return None
    return entries


def snapshot(workspace: str, limit: int = SNAPSHOT_LIMIT, own: Optional[str] = None) -> Snapshot:
    """Path → (type, size, mtime, ctime) for everything except the run's own
    output directory `timetrace-out/<own>` (other runs' outputs are covered; with
    no `own`, all of timetrace-out/ is left out). Symlinks are recorded by their
    target path, never followed. Above `limit` entries only the top two levels
    are recorded (`capped`)."""
    skip = OUTPUT_ROOT + "/" + own if own else OUTPUT_ROOT
    full = _walk(workspace, None, limit, shallow=False, skip=skip)
    if full is not None:
        return Snapshot(full, False)
    return Snapshot(_walk(workspace, SHALLOW_DEPTH, None, shallow=True, skip=skip) or {}, True)


def changes(before: Snapshot, after: Snapshot) -> List[str]:
    """Paths added, removed or modified between the two snapshots."""
    keys = set(before.entries) | set(after.entries)
    return sorted(k for k in keys if before.entries.get(k) != after.entries.get(k))


def change_message(changed: List[str]) -> str:
    shown = changed[:MAX_REPORTED]
    more = "" if len(changed) <= MAX_REPORTED else " 等"
    return "工作区中本任务输出目录以外的文件被改动（%d 个）：%s%s" % (len(changed), ", ".join(shown), more)


CAPPED_NOTE = "工作区文件超过 %d 个，只校验了顶层两级" % SNAPSHOT_LIMIT


def _output_files(outdir: str) -> List[Tuple[str, int]]:
    files = []
    for directory, dirs, names in os.walk(outdir):
        rel_dir = os.path.relpath(directory, outdir)
        if rel_dir == ".":
            dirs[:] = [d for d in dirs if d != ".timetrace"]
            rel_dir = ""
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(directory, name)
            try:
                size = os.lstat(path).st_size
            except OSError:
                continue
            files.append((os.path.join(rel_dir, name) if rel_dir else name, size))
    return sorted(files)


def listing(outdir: str, limit: int = LISTING_BYTES) -> str:
    """`<relative path>\\t<bytes>` per output file (without `.timetrace/`), cut on a
    line boundary to `limit` UTF-8 bytes."""
    lines, used = [], 0
    files = _output_files(outdir)
    for rel, size in files:
        line = "%s\t%d\n" % (rel, size)
        length = len(line.encode("utf-8"))
        if used + length > limit - 64:
            lines.append("… 共 %d 个文件\n" % len(files))
            break
        lines.append(line)
        used += length
    return "".join(lines)


def digest(outdir: str) -> str:
    """Checkpoint evidence for an output directory: every entry's path, size
    and mtime (including `.timetrace/`)."""
    h = hashlib.sha256()
    snap = _walk(outdir, None, None, shallow=False, skip=None) or {}
    for key in sorted(snap):
        h.update(repr((key, snap[key])).encode("utf-8") + b"\0")
    return h.hexdigest()
