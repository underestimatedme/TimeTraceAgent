"""Bounded, local workspace metadata for Runner inventory (never contains paths)."""
import codecs
import hashlib
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 32768
README_NAMES = ('readme.md', 'readme.markdown', 'readme', 'readme.txt')
IGNORED = {'.git', '.DS_Store', 'timetrace-out'}


def _description(content):
    lines = []
    fenced = False
    for line in content.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith('#'):
            continue
        line = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', line)
        line = re.sub(r'<[^>]*>', '', line)
        line = re.sub(r'[*_`~]', '', line).strip()
        if line:
            lines.append(line)
    return ' '.join(lines)[:500]


def collect_workspace_context(path: str, checked_at: str = None) -> dict:
    context = {'content_state': 'unknown',
               'checked_at': checked_at or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
               'description': '',
               'readme': {'status': 'missing', 'filename': '', 'content': '', 'sha256': '', 'truncated': False}}
    root = Path(path)
    try:
        entries = list(root.iterdir())
    except (OSError, ValueError):
        return context
    context['content_state'] = 'populated' if any(p.name not in IGNORED for p in entries) else 'empty'
    candidates = [p for p in entries if p.name.lower() in README_NAMES]
    if not candidates:
        return context
    candidate = min(candidates, key=lambda p: (README_NAMES.index(p.name.lower()), p.name))
    readme = context['readme']
    readme['filename'] = candidate.name
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
        # Reject links, including an entry replaced with a link before opening.
        import os
        fd = os.open(str(candidate), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                readme['status'] = 'unreadable'
                return context
            data = stream.read(MAX_BYTES + 4)
        truncated = len(data) > MAX_BYTES
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        decoded = decoder.decode(data, final=not truncated)
        content = decoded.encode('utf-8')[:MAX_BYTES].decode('utf-8', errors='ignore')
    except UnicodeDecodeError:
        readme['status'] = 'invalid_encoding'
        return context
    except (OSError, ValueError):
        readme['status'] = 'unreadable'
        return context
    readme.update(status='ready', content=content, sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                  truncated=truncated)
    context['description'] = _description(content)
    return context
