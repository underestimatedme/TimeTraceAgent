"""Typed workspace setup from the paired owner; no remotely supplied commands.

The existing idle/revision/ACK config channel carries this operation. Objective
assertions are data compiled into an external pinned checker, never model code.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from timetrace import config, objective_checks


def candidates(db):
    known = {r['path']: r for r in db.list_workspaces()}
    roots = {Path(p).parent for p in known}
    roots.update(Path.home() / name for name in ('Projects', 'projects', 'Developer', 'code'))
    paths = set(known)
    for root in sorted(roots):
        if not root.is_dir() or root == Path.home() or root == Path('/'):
            continue
        try:
            for child in sorted(root.iterdir())[:150]:
                if not child.name.startswith('.') and child.is_dir() and not child.is_symlink() and (child / '.git').exists():
                    paths.add(str(child.resolve()))
        except OSError:
            continue
    return [{'path': p, 'name': known.get(p, {}).get('name') or Path(p).name,
             'id': known.get(p, {}).get('id', '')} for p in sorted(paths)[:60]]


def validate(spec):
    if not isinstance(spec, dict) or set(spec) - {'id', 'name', 'path', 'create', 'objective', 'assertions'}:
        raise ValueError('invalid_config')
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', spec.get('id', '')) or not 1 <= len(spec.get('name', '')) <= 100:
        raise ValueError('invalid_config')
    if type(spec.get('create', False)) is not bool or not isinstance(spec.get('path', ''), str):
        raise ValueError('invalid_config')
    assertions = spec.get('assertions') or []
    objective = spec.get('objective') or ''
    if not isinstance(objective, str) or len(objective) > 4000 or bool(objective.strip()) != bool(assertions) or not isinstance(assertions, list) or len(assertions) > 20:
        raise ValueError('invalid_config')
    for a in assertions:
        if not isinstance(a, dict) or set(a) != {'path', 'kind', 'value'}:
            raise ValueError('invalid_config')
        p = a['path']
        if not isinstance(p, str) or not p or len(p) > 240 or Path(p).is_absolute() or any(x.startswith('.') or not x for x in p.split('/')) or '\\' in p or '\0' in p:
            raise ValueError('invalid_config')
        if a['kind'] not in ('exists', 'contains', 'json') or not isinstance(a['value'], str) or len(a['value']) > 2000 or (a['kind'] == 'contains' and not a['value'].strip()):
            raise ValueError('invalid_config')
    return assertions


# Runs under python -I -B, and reads only committed regular files within cwd.
# No eval, user command, subprocess, imports from the task, or network.
CHECKER = '''import json, pathlib, subprocess, sys
rules = json.loads(RULES)
root = pathlib.Path.cwd().resolve()
failed = []
for a in rules:
    p = root / a['path']
    try:
        if any(x.is_symlink() for x in [p, *p.parents] if x != root and root in x.parents):
            raise ValueError('symlink')
        if root not in p.resolve().parents or not p.is_file() or p.stat().st_size > 1048576:
            raise ValueError('missing or oversized')
        subprocess.run(['git', 'ls-files', '--error-unmatch', '--', a['path']], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if a['kind'] == 'contains' and a['value'] not in p.read_text(encoding='utf-8'):
            raise ValueError('required text missing')
        if a['kind'] == 'json':
            json.loads(p.read_text(encoding='utf-8'))
        print('PASS', a['path'], a['kind'])
    except Exception as e:
        failed.append(a['path'] + ': ' + str(e))
print('\\n'.join(failed) if failed else 'All declared file assertions passed; no undeclared semantic or visual claims.')
sys.exit(1 if failed else 0)
'''


def apply(home, db, spec):
    from timetrace.cli import add_workspace
    assertions = validate(spec)
    root = (Path(home) / 'projects' / spec['id']) if spec.get('create') else Path(spec.get('path', '')).expanduser()
    if not root.is_absolute():
        raise ValueError('workspace_unavailable')
    root = root.resolve()
    if not spec.get('create') and (root == Path.home().resolve() or root == Path('/') or Path(home).resolve() in root.parents or root == Path(home).resolve()):
        raise ValueError('workspace_unavailable')
    existing = db.get_workspace(spec['id'])
    if existing and existing['path'] != str(root):
        raise ValueError('local_conflict')
    for row in db.list_workspaces():
        if row['path'] == str(root) and row['id'] != spec['id']:
            raise ValueError('local_conflict')
    if spec.get('create'):
        if not root.exists():
            root.mkdir(parents=True)
        if not (root / '.git').exists():
            if list(root.iterdir()):
                raise ValueError('local_conflict')
            subprocess.run(['git', '-c', 'init.templateDir=', 'init', '-b', 'main', str(root)], check=True, capture_output=True, timeout=30)
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false', '-c', 'user.name=TimeTrace', '-c', 'user.email=timetrace@localhost', 'commit', '--allow-empty', '-m', 'Initialize project'], cwd=root, check=True, capture_output=True, timeout=30)
    if not root.is_dir() or not (root / '.git').exists():
        raise ValueError('workspace_unavailable')
    # Prepare everything before the SQLite transaction; retry uses stable IDs.
    contract = argv = None
    if assertions:
        content = CHECKER.replace('RULES', repr(json.dumps(assertions, ensure_ascii=False)))
        digest = hashlib.sha256(content.encode()).hexdigest()
        checker = Path(home) / 'web-checks' / (digest + '.py')
        checker.parent.mkdir(parents=True, exist_ok=True)
        if not checker.exists():
            checker.write_text(content, encoding='utf-8'); checker.chmod(0o600)
        elif checker.read_text(encoding='utf-8') != content:
            raise ValueError('local_conflict')
        name = 'web-' + hashlib.sha256((spec['objective'] + digest).encode()).hexdigest()[:20]
        argv = [str(Path(sys.executable).resolve()), '-I', '-B', str(checker)]
        contract = objective_checks.register(str(root), name, argv, spec['objective'], str(checker))
    detected = []
    package = root / 'package.json'
    if package.is_file() and not package.is_symlink() and package.stat().st_size <= 100000:
        try:
            scripts = json.loads(package.read_text()).get('scripts', {})
            npm = shutil.which('npm')
            if npm:
                for kind in ('test', 'build'):
                    if kind in scripts and isinstance(scripts[kind], str):
                        detected.append(('project-' + kind, [npm, 'run', kind]))
        except (ValueError, OSError):
            pass
    if (root / 'go.mod').is_file() and shutil.which('go'):
        detected.append(('project-test', [shutil.which('go'), 'test', './...']))
    with db.conn:
        row = add_workspace(db, str(root), spec['id'], spec['name'], 'git')
        for name, command in detected:
            if db.get_check(row['id'], name) is None:
                db.save_check(row['id'], name, command)
        if contract:
            db.save_check(row['id'], contract['name'], argv)
            db.save_objective_check(row['id'], contract['name'], contract)
    return row
