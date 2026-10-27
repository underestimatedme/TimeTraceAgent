"""Finite operator-owned acceptance packs; no remotely supplied commands.

Regression reuses a locally registered check and freezes every tracked file
outside the explicit repair scope. It proves that check, not arbitrary quality.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

KINDS = ("markdown_sections", "regression", "web_flow")


def relative_file(value):
    return (isinstance(value, str) and 0 < len(value) <= 240
            and not any(c in value for c in "\\\0\n\r")
            and all(p and not p.startswith('.') for p in value.split('/')))


def parse(kind, raw):
    try:
        value = json.loads(raw)
        if kind == "markdown_sections":
            if not isinstance(value, list) or not 1 <= len(value) <= 12:
                raise ValueError()
            if any(not isinstance(s, str) or not s.strip() or len(s) > 100 or any(c in s for c in '\n\r\0') for s in value) or len(set(value)) != len(value):
                raise ValueError()
        elif kind == "regression":
            if not isinstance(value, dict) or set(value) != {'check', 'allow_changes'}:
                raise ValueError()
            if not isinstance(value['check'], str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', value['check']):
                raise ValueError()
            paths = value['allow_changes']
            if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or any(not relative_file(p) for p in paths) or len(set(paths)) != len(paths):
                raise ValueError()
        elif kind == "web_flow":
            if not isinstance(value, list) or not 1 <= len(value) <= 20:
                raise ValueError()
            assertions = 0
            for step in value:
                if not isinstance(step, dict) or step.get('action') not in ('click', 'fill', 'press', 'check', 'uncheck', 'text', 'count', 'visible', 'hidden', 'reopen'):
                    raise ValueError()
                action = step['action']
                fields = {'action'} if action == 'reopen' else {'action', 'selector'}
                if action in ('fill', 'press', 'text', 'count'):
                    fields.add('value')
                if set(step) != fields:
                    raise ValueError()
                if action != 'reopen' and (not isinstance(step['selector'], str) or not step['selector'].strip() or len(step['selector']) > 200 or '\0' in step['selector']):
                    raise ValueError()
                if action == 'count':
                    if type(step['value']) is not int or not 0 <= step['value'] <= 100:
                        raise ValueError()
                elif 'value' in fields:
                    if not isinstance(step['value'], str) or len(step['value']) > 500 or '\0' in step['value']:
                        raise ValueError()
                    if action == 'press' and step['value'] not in ('Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp'):
                        raise ValueError()
                    if action == 'text' and not step['value'].strip():
                        raise ValueError()
                assertions += action in ('text', 'count', 'visible', 'hidden')
            if not assertions:
                raise ValueError()
        else:
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ValueError('invalid_config') from None


def markdown_sections(text, required):
    sections, current, fence = {}, None, None
    for line in text.splitlines():
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            char = marker[1][0]
            if fence is None:
                fence = (char, len(marker[1]))
            elif char == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            if current:
                sections[current].append(line)
            continue
        heading = re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', line) if fence is None else None
        if heading:
            current = heading[1].strip()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    for title in required:
        body = '\n'.join(sections.get(title, [])).strip()
        if not body or re.fullmatch(r'(?i)(todo|tbd|待填写|待补充|\.\.\.)[.!。\s]*', body):
            raise AssertionError('Missing heading or content: ' + title)


def tree(root):
    raw = subprocess.check_output(['git', 'ls-tree', '-rz', 'HEAD'], cwd=root)
    entries = {}
    for entry in raw.split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            entries[name.decode('utf-8')] = meta.decode('ascii')
    return entries


def regression_contract(db, workspace_id, root, value):
    argv = db.get_check(workspace_id, value['check'])
    if not argv:
        raise ValueError('registered_check_missing')
    executable = shutil.which(argv[0])
    if not executable or Path(root).resolve() in Path(executable).resolve().parents:
        raise ValueError('registered_check_missing')
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root).returncode:
        raise ValueError('repair_scope_unavailable')
    baseline = tree(root)
    if len(baseline) > 20000:
        raise ValueError('repair_scope_unavailable')
    for name in value['allow_changes']:
        if name not in baseline or not baseline[name].startswith(('100644 blob ', '100755 blob ')):
            raise ValueError('repair_scope_unavailable')
        # Keep tests and build/dependency configuration outside the repair scope.
        if re.search(r'(^|/)(__tests__|tests?|specs?)(/|$)|(^|/)(test_|spec_)[^/]*|[._](test|spec)\.|(^|/)(package\.json|[^/]*lock[^/]*|.*config\.[^/]+)$', name, re.I):
            raise ValueError('repair_scope_unavailable')
    return {'check': value['check'], 'registered_argv': argv,
            'argv': [executable, *argv[1:]], 'baseline': baseline,
            'allow_changes': value['allow_changes']}


def run_regression(contract):
    current = tree(Path.cwd())
    allowed = set(contract['allow_changes'])
    if any(current.get(p) != contract['baseline'].get(p)
           for p in (set(current) | set(contract['baseline'])) - allowed):
        print('受保护测试、配置或范围外文件已变化；不能用于自动收件。', flush=True)
        return 126
    if any(not current.get(p, '').startswith(('100644 blob ', '100755 blob ')) for p in allowed):
        print('修复文件缺失或已变为非普通文件。', flush=True)
        return 126
    # The outer check supervisor owns timeout, cancellation and credential stripping.
    code = subprocess.run(contract['argv'], stdin=subprocess.DEVNULL).returncode
    print('登记回归检查通过；仅覆盖已登记测试和明确修复范围。' if code == 0 else '登记回归检查未通过。', flush=True)
    return code if code in (0, 1) else 2
