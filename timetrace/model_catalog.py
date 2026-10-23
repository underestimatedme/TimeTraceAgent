"""Read the logged-in CLI's selectable models without starting a model turn.

Only normalized display data crosses inventory; never forward initialize account
objects, commands, environment, credentials, or arbitrary provider errors.
"""
import copy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from timetrace.adapters.codex import app_server_request
from timetrace.billing import sanitized_env
from timetrace.routing import identity

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def claude_models(binary, timeout=20):
    # initialize only: no user message, no tool execution, no inference.
    cmd = [binary, '-p', '--input-format', 'stream-json', '--output-format',
           'stream-json', '--verbose', '--strict-mcp-config', '--setting-sources', 'user']
    with tempfile.TemporaryDirectory(prefix='timetrace-models-') as cwd:
        proc = subprocess.Popen(cmd, cwd=cwd, env=sanitized_env('claude', os.environ),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        answer = {}
        done = threading.Event()
        def reader():
            try:
                for line in proc.stdout:
                    if len(line) > 1_000_000:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    response = msg.get('response', {})
                    if msg.get('type') == 'control_response' and response.get('request_id') == 'timetrace-models':
                        answer.update(response)
                        break
            finally:
                done.set()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            proc.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'timetrace-models',
                                         'request': {'subtype': 'initialize'}}) + '\n')
            proc.stdin.flush()
            if not done.wait(timeout):
                raise TimeoutError('model catalog timeout')
            if answer.get('subtype') != 'success':
                raise ValueError('model catalog unavailable')
            return answer.get('response', {}).get('models', [])
        finally:
            proc.kill()
            proc.wait()
            thread.join(2)
            proc.stdin.close()
            proc.stdout.close()


def read_catalog(adapter):
    provider = adapter.name
    account, _ = identity(adapter)
    result = {'version': 1, 'account': account, 'source': provider + ('_model_list' if provider == 'codex' else '_initialize'),
              'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'status': 'error', 'models': []}
    if not account:
        result['status'] = 'login_required'
        return result
    if adapter.cfg.get('extra_args'):
        result['status'] = 'unsupported'
        return result
    try:
        # A profile fingerprint may survive logout (notably .claude.json).
        # Reuse the existing authenticated subscription check, never infer login
        # from profile metadata or an unauthenticated default model list.
        verdict = adapter.billing.verdict(force=True)
        if not verdict.verified:
            result['status'] = 'login_required' if verdict.reason == 'not_logged_in' else 'error'
            return result
        if provider == 'codex':
            rows, cursor = [], None
            for _ in range(10):
                data = app_server_request(adapter.cfg.get('bin', 'codex'), 'model/list',
                                          {'limit': 100, **({'cursor': cursor} if cursor else {})})
                rows.extend(data.get('data', []))
                if len(rows) > 100:
                    raise ValueError('catalog too large')
                cursor = data.get('nextCursor')
                if not cursor:
                    break
            if cursor:
                raise ValueError('incomplete catalog')
        else:
            rows = claude_models(adapter.cfg.get('bin', 'claude'))
        models, seen = [], set()
        for row in rows:
            model = row.get('model') or row.get('id') if provider == 'codex' else row.get('value')
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model) or model in seen or row.get('hidden'):
                continue
            seen.add(model)
            resolved = row.get('resolvedModel') or model
            if not isinstance(resolved, str) or not MODEL_ID.fullmatch(resolved):
                resolved = model
            models.append({'id': model, 'name': str(row.get('displayName') or model)[:160],
                           'description': str(row.get('description') or '')[:400], 'resolved_model': resolved})
        if not models or len(models) > 100:
            raise ValueError('invalid catalog')
        # A login can change while the CLI is answering. Never publish for the old account.
        if identity(adapter)[0] != account:
            raise ValueError('account changed')
        result.update(status='ready', models=models)
    except Exception:
        pass  # Fixed status only: provider errors may contain sensitive output.
    return result


class ModelCatalogCache:
    def __init__(self):
        self.entries = {}

    def get(self, adapter):
        key = (adapter.name, identity(adapter)[0], adapter.cfg.get('bin'), str(adapter.cfg.get('extra_args')))
        old = self.entries.get(adapter.name)
        now = time.monotonic()
        if old and old[0] == key and now < old[1]:
            return copy.deepcopy(old[2])
        result = read_catalog(adapter)
        self.entries[adapter.name] = (key, now + (240 if result['status'] == 'ready' else 30), result)
        return copy.deepcopy(result)
