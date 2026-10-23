"""Immutable browser application bundles, collected only from the output commit.
No dev server, shell command, credentials, ignored files or host paths are exposed.
"""
import base64
import json
import mimetypes
import re
import posixpath
from urllib.parse import urlsplit, unquote
import subprocess

MAX_BYTES = 1400000
MAX_FILES = 120
EXTENSIONS = {'.html', '.js', '.mjs', '.css', '.json', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.woff', '.woff2'}
MARKER = 'KEJI_APP_PREVIEW_V1'


def _git(root, *args):
    return subprocess.check_output(['git', '--no-pager', '-C', root, *args], timeout=15)


def collect(root, commit):
    """Return (bundle, diagnostic). Unsupported projects get an actionable note."""
    if not commit:
        return None, ''
    try:
        from pathlib import PurePosixPath
        records = _git(root, 'ls-tree', '-rz', commit).decode().split('\0')
        entries = {}
        for record in records:
            if not record:
                continue
            meta, name = record.split('\t', 1)
            mode, kind, oid = meta.split()
            if mode not in ('100644', '100755') or kind != 'blob':
                continue
            entries[name] = oid
        roots = [p for p in ('dist/', 'build/', 'out/', '') if p + 'index.html' in entries]
        if not roots:
            return None, '当前提交没有浏览器入口。让 AI 生成并提交可运行的前端产物（index.html、脚本和样式）；独立后端仍需另行提供运行环境。' if 'package.json' in entries else ''
        root_prefix = roots[0]
        # A repository-root app only publishes its reachable browser assets;
        # unrelated JSON/documents must never become preview payloads.
        reachable = set()
        queue = [root_prefix + 'index.html']
        if not root_prefix:
            while queue:
                name = queue.pop()
                if name in reachable or name not in entries:
                    continue
                reachable.add(name)
                if len(reachable) > MAX_FILES:
                    return None, '应用依赖超过 120 个，请提供精简构建产物。'
                if name.endswith(('.html', '.css', '.js', '.mjs')):
                    if int(_git(root, 'cat-file', '-s', entries[name])) > MAX_BYTES:
                        return None, '应用产物超过 1.4 MB，请让 AI 缩小构建包后重试。'
                    source = _git(root, 'cat-file', 'blob', entries[name]).decode('utf-8', 'replace')
                    patterns = [r'(?:src|href)\s*=\s*[\'"]([^\'"]+)', r'url\(\s*[\'"]?([^\)\'"\s]+)', r'(?:from|import)\s*[\'"]([^\'"]+)', r'(?:import|fetch|URL)\(\s*[\'"]([^\'"]+)']
                    for pattern in patterns:
                        for ref in re.findall(pattern, source):
                            u = urlsplit(ref)
                            if u.scheme or u.netloc or not u.path:
                                continue
                            path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(u.path))) if not u.path.startswith('/') else unquote(u.path).lstrip('/')
                            if not path.startswith('../') and path in entries:
                                queue.append(path)
        files, size = [], 0
        for name, oid in sorted(entries.items()):
            if not root_prefix and name not in reachable:
                continue
            if not name.startswith(root_prefix):
                continue
            relative = name[len(root_prefix):]
            parts = PurePosixPath(relative).parts
            if any(p.startswith('.') or p in ('node_modules', 'vendor') for p in parts):
                continue
            suffix = PurePosixPath(relative).suffix.lower()
            if suffix not in EXTENSIONS or relative in ('package.json', 'package-lock.json'):
                continue
            if len(files) >= MAX_FILES:
                return None, '应用文件超过 120 个，请让 AI 提供精简构建产物。'
            n = int(_git(root, 'cat-file', '-s', oid))
            size += n
            if size > MAX_BYTES:
                return None, '应用产物超过 1.4 MB，请让 AI 缩小构建包后重试。'
            data = _git(root, 'cat-file', 'blob', oid)
            mime = {'.js':'text/javascript','.mjs':'text/javascript','.html':'text/html','.css':'text/css','.json':'application/json'}.get(suffix) or mimetypes.guess_type(relative)[0] or 'application/octet-stream'
            files.append({'path': relative, 'mime': mime, 'data': base64.b64encode(data).decode('ascii')})
        body = json.dumps({'format':MARKER, 'commit':commit, 'entry':'index.html', 'files':files}, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > 2 * 1024 * 1024:
            return None, '应用预览包超过读取上限，请让 AI 精简产物。'
        return body, ''
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None, '无法从该提交打包预览。原始成果保留，可让 AI 补齐可运行产物。'
