"""Local project-root inventory and safe single-child runtime workspaces.

Registered paths stay local. A runtime child retains its parent's id (and
registered check commands), but never changes the database registration.
"""
import codecs
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import folder
from .redact import redact
from .workspace_context import README_NAMES, _description

PROJECT_ROOT = folder.PROJECT_ROOT
VERSION = 1
MAX_ENTRIES = 500
README_BYTES = 2048
TOTAL_README_BYTES = 65536
EXCLUDED = {'node_modules', 'vendor', 'build', 'dist', 'target', 'out', 'cache',
            '__pycache__', 'venv', 'env', 'coverage', folder.OUTPUT_ROOT}
DEVICE = re.compile(r'^(?:con|conin\$|conout\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)', re.I)
LOCAL_PATH = re.compile(r"(?<![\w:/\\])(?:[A-Za-z]:[\\/]|/|\\+)[^\s\"'<>`()\[\]]+")


def validate_name(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 128
            or name.startswith('.') or name.endswith(('.', ' '))
            or any(c in '/\\<>:"|?*' or unicodedata.category(c) == 'Cc' for c in name)
            or DEVICE.match(name)):
        raise ValueError('invalid project directory name')
    try:
        if len(name.encode('utf-8')) > 255:
            raise ValueError('project directory name exceeds byte limit')
    except UnicodeError as exc:
        raise ValueError('invalid project directory name') from exc
    return name


@contextmanager
def _directory(path=None, *, parent=None, name=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(name if parent is not None else str(path), flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _git(fd, *args):
    # The descriptor pins the directory during git writes, even if a local
    # process replaces its pathname between validation and subprocess launch.
    script = 'import os,sys; os.fchdir(int(sys.argv[1])); os.execvp("git", ["git"] + sys.argv[2:])'
    return subprocess.run([sys.executable, '-c', script, str(fd)] + list(args),
                          pass_fds=(fd,), env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                          check=True, capture_output=True, text=True).stdout.strip()


def _nested_git(fd, depth=0, seen=None):
    seen = [0] if seen is None else seen
    names = os.listdir(fd)
    if '.git' in names:
        return True
    if depth + 1 >= folder.GIT_SCAN_DEPTH:
        return False
    for name in names:
        seen[0] += 1
        if seen[0] > folder.GIT_SCAN_LIMIT:
            raise ValueError('project folder safety scan exceeds limit')
        if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            with _directory(parent=fd, name=name) as child:
                if _nested_git(child, depth + 1, seen):
                    return True
    return False


def _kind(fd):
    try:
        metadata = os.stat('.git', dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if _nested_git(fd):
            raise ValueError('project folder contains a nested repository')
        return folder.FOLDER, ''
    # Worktrees/submodules use a .git file and would share another repository.
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('project directory must be an independent repository')
    with _directory(parent=fd, name='.git'):
        pass
    try:
        if _git(fd, 'rev-parse', '--is-inside-work-tree') != 'true':
            raise ValueError('invalid project repository')
        top = os.stat(_git(fd, 'rev-parse', '--show-toplevel'))
        current = os.fstat(fd)
        if (top.st_dev, top.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('project repository points outside its directory')
        return folder.GIT, _git(fd, 'branch', '--show-current') or 'HEAD'
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError('invalid project repository') from exc


def _root(workspace):
    if not workspace or workspace.get('kind') != PROJECT_ROOT:
        raise ValueError('project directory requires a registered project root')
    root = Path(workspace['path'])
    if not root.is_absolute() or str(root.resolve()) != str(root):
        raise ValueError('project root is unavailable')
    return root


def resolve(workspace, name, allow_missing=False):
    """Read-only resolution; missing children are only planned, never created."""
    root = _root(workspace)
    validate_name(name)
    path = root / name
    try:
        with _directory(root) as parent:
            try:
                with _directory(parent=parent, name=name) as fd:
                    kind, branch = _kind(fd)
            except FileNotFoundError:
                if not allow_missing:
                    raise ValueError('project directory is unavailable')
                kind, branch = folder.GIT, 'main'
        if path.exists() and str(path.resolve()) != str(path):
            raise ValueError('project directory escapes registered root')
    except OSError as exc:
        raise ValueError('project directory is unavailable or unsafe') from exc
    return dict(workspace, path=str(path), kind=kind, default_branch=branch,
                _project_root=dict(workspace), _project_directory=name, _project_can_create=allow_missing)


def prepare(workspace):
    """Under the child workspace lock, initialize only a newly created child."""
    parent_workspace = workspace['_project_root']
    root = _root(parent_workspace)
    name = validate_name(workspace['_project_directory'])
    if not workspace.get('_project_can_create'):
        workspace.update(resolve(parent_workspace, name))
        return workspace
    try:
        with _directory(root) as parent:
            created = False
            try:
                os.mkdir(name, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
            with _directory(parent=parent, name=name) as child:
                if created:
                    _git(child, 'init', '-q', '--template=', '-b', 'main')
                    _git(child, '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=TimeTrace', '-c', 'user.email=agent@timetrace.local',
                         'commit', '--allow-empty', '-qm', 'Initialize project directory')
                kind, branch = _kind(child)
                # Reject local pathname substitution before handing it to a tool.
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                pinned = os.fstat(child)
                if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise ValueError('project directory changed during preparation')
        resolved = resolve(parent_workspace, name)
        workspace.update(resolved)
        return workspace
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError('project directory preparation failed') from exc


def _readme(fd, budget):
    candidates = sorted((n for n in os.listdir(fd) if n.lower() in README_NAMES),
                        key=lambda n: (README_NAMES.index(n.lower()), n))
    if not candidates:
        return {'status': 'absent'}, 0
    name = candidates[0]
    try:
        read_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(read_fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {'status': 'unreadable', 'filename': name}, 0
            limit = min(README_BYTES, budget)
            data = stream.read(limit + 1)
        truncated = len(data) > limit
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        content = decoder.decode(data[:limit], final=not truncated)
        content = LOCAL_PATH.sub('[local]', redact(content))
        # Redaction can lengthen text; apply the cap after sanitization too.
        content = content.encode()[:limit].decode('utf-8', errors='ignore')
        return {'status': 'ok', 'filename': name, 'content': content, 'truncated': truncated}, len(content.encode())
    except (OSError, UnicodeError):
        return {'status': 'unreadable', 'filename': name}, 0


def scan(path):
    """Bounded direct-child metadata; an incomplete scan is never importable."""
    inventory = {'status': 'ok', 'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                 'fingerprint': '', 'truncated': False, 'excluded_count': 0, 'entries': []}
    entries = inventory['entries']
    budget = TOTAL_README_BYTES
    try:
        root = Path(path)
        if not root.is_absolute() or str(root.resolve()) != str(root):
            raise ValueError('unsafe project root')
        with _directory(root) as parent:
            for name in sorted(os.listdir(parent)):
                mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    inventory['excluded_count'] += 1
                    continue
                if not stat.S_ISDIR(mode):
                    continue
                try:
                    validate_name(name)
                    if name.casefold() in EXCLUDED:
                        raise ValueError('excluded directory')
                    with _directory(parent=parent, name=name) as child:
                        kind, _ = _kind(child)
                        if len(entries) >= MAX_ENTRIES:
                            inventory['truncated'] = True
                            continue
                        readme, used = _readme(child, budget)
                except ValueError:
                    inventory['excluded_count'] += 1
                    continue
                budget -= used
                entries.append({'directory_name': name, 'kind': kind,
                                'description': _description(readme.get('content', '')), 'readme': readme})
    except (OSError, ValueError):
        inventory.update(status='unreadable', entries=[], truncated=False)
    canonical = json.dumps(inventory['entries'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    inventory['fingerprint'] = hashlib.sha256(canonical.encode()).hexdigest()
    return inventory
