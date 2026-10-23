"""Version-bound, bounded review material collected from Git, not model claims."""
import json
import subprocess
from timetrace.redact import redact


def _git(root, *args):
    return subprocess.check_output(['git', '--no-pager', '-C', root, *args], timeout=15)


def collect(root, base, commit, artifacts):
    if not base or not commit:
        return artifacts
    try:
        names = _git(root, 'diff', '--name-only', '-z', base, commit, '--').decode().split('\0')
        patch = _git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-color', '--unified=3', base, commit, '--', '.', ':!*.lock', ':!package-lock.json').decode('utf-8', 'replace')
        patch = redact(patch)
        if len(patch.encode()) > 18000:
            patch = patch.encode()[:18000].decode('utf-8', 'ignore') + '\n[差异过长，已截断；请下载完整成果或查看提交]'
        own = [{'kind': 'doc', 'ref': 'review/changes.diff', 'commit_sha': commit,
                'content': 'Base: ' + base + '\nCommit: ' + commit + '\n' + patch}]
        for name in names:
            if len(own) >= 5 or not name.endswith(('.html', '.md', '.txt')) or any(p.startswith('.') for p in name.split('/')):
                continue
            # git show cannot follow symlinks into the host filesystem.
            size = int(_git(root, 'cat-file', '-s', commit + ':' + name))
            if size > 8000:
                continue
            body = redact(_git(root, 'show', commit + ':' + name).decode('utf-8'))
            own.append({'kind': 'doc', 'ref': name, 'commit_sha': commit, 'content': body})
        seen = {(a.get('ref'), a.get('commit_sha')) for a in own}
        for a in artifacts or []:
            if (a.get('ref'), a.get('commit_sha')) in seen:
                continue
            if len(json.dumps(own + [a], ensure_ascii=False).encode()) < 60000 and len(own) < 18:
                own.append(a)
        return own
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return artifacts
