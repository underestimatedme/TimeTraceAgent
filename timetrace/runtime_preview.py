"""Owner-only backend previews: immutable source, bounded Docker, no published port.
HTTP is relayed over the existing authenticated Runner channel. No host secrets,
workspace writes, Docker socket or outbound network enter the application.
"""
import base64
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from datetime import datetime

IMAGES = {'python': 'python:3.12-slim', 'node': 'node:22-bookworm-slim'}
MANIFEST = '.keji/preview.json'
MAX_SOURCE = 50 * 1024 * 1024


def command(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, timeout=kw.pop('timeout', 20), **kw).stdout


def manifest(raw):
    spec = json.loads(raw)
    if not isinstance(spec, dict) or set(spec) - {'version', 'runtime', 'command', 'port', 'health_path'}:
        raise ValueError('运行配置字段无效')
    argv = spec.get('command')
    if spec.get('version') != 1 or spec.get('runtime') not in IMAGES or not isinstance(argv, list) or not 2 <= len(argv) <= 16:
        raise ValueError('运行配置需要 version=1、runtime=python/node 和 command 参数数组')
    if any(not isinstance(s, str) or not s or len(s) > 512 or '\0' in s for s in argv) or argv[0] != {'python': 'python', 'node': 'node'}[spec['runtime']]:
        raise ValueError('运行命令必须使用所选运行时')
    if type(spec.get('port')) is not int or not 1024 <= spec['port'] <= 65535:
        raise ValueError('运行端口应介于 1024 和 65535')
    health = spec.get('health_path', '/')
    if not isinstance(health, str) or not health.startswith('/') or health.startswith('//') or any(c in health for c in '\r\n\0\\') or len(health) > 200:
        raise ValueError('健康检查路径无效')
    return spec


def snapshot(root, commit, dest):
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('成果提交无效')
    records = command(['git', '-C', str(root), 'ls-tree', '-rz', commit]).decode().split('\0')
    total = count = 0
    for record in records:
        if not record:
            continue
        meta, path = record.split('\t', 1)
        mode, kind, oid = meta.split()
        parts = PurePosixPath(path).parts
        if kind != 'blob' or mode not in ('100644', '100755') or path.startswith('/') or any(p in ('..', '.git', 'node_modules', '.env', '.venv') for p in parts):
            continue
        if any(p.startswith('.') for p in parts) and path != MANIFEST:
            continue
        size = int(command(['git', '-C', str(root), 'cat-file', '-s', oid]))
        total += size; count += 1
        if total > MAX_SOURCE or count > 2500:
            raise ValueError('预览源码超过 50 MB 或 2500 文件，请提供精简交付')
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(command(['git', '-C', str(root), 'cat-file', 'blob', oid]))
        target.chmod(0o555 if mode == '100755' else 0o444)
    cfg = dest / MANIFEST
    if not cfg.exists():
        raise ValueError('缺少 .keji/preview.json。让 AI 提供运行时、启动参数、端口和健康检查；无需在电脑复制命令。')
    return manifest(cfg.read_text())


# Helpers run INSIDE the network-isolated application container. Destination
# cannot be controlled by the web request; redirects are returned, never followed.
PY_HTTP = '''import sys,json,http.client,base64
v=json.load(sys.stdin)
c=http.client.HTTPConnection('127.0.0.1',v['port'],timeout=8)
h={'Content-Type':v.get('content_type') or 'application/json'}
c.request(v['method'],v['path'],body=base64.b64decode(v.get('body') or ''),headers=h)
r=c.getresponse();b=r.read(700001)
if len(b)>700000: raise ValueError('response too large')
print(json.dumps({'code':r.status,'body':base64.b64encode(b).decode(),'content_type':r.getheader('Content-Type','application/octet-stream')}))
'''
NODE_HTTP = '''const http=require('http');let s='';process.stdin.on('data',b=>s+=b);process.stdin.on('end',()=>{let v=JSON.parse(s);let q=http.request({hostname:'127.0.0.1',port:v.port,path:v.path,method:v.method,headers:{'content-type':v.content_type||'application/json'},timeout:8000},r=>{let chunks=[],size=0;r.on('data',b=>{size+=b.length;if(size>700000){q.destroy();process.exit(2)}chunks.push(b)});r.on('end',()=>console.log(JSON.stringify({code:r.statusCode,body:Buffer.concat(chunks).toString('base64'),content_type:r.headers['content-type']||'application/octet-stream'})))});q.on('timeout',()=>q.destroy());q.on('error',()=>process.exit(2));q.end(Buffer.from(v.body||'','base64'))})'''


class RuntimePreviews:
    def __init__(self, agent):
        self.agent = agent
        self.instances = {}
        self.thread = None
        self.last_contact = time.monotonic()
        self.next_health = 0

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name='keji-backend-preview')
        self.thread.start()

    def stop(self, key):
        item = self.instances.get(key)
        if item:
            try: command(['docker', 'rm', '-f', item['name']])
            except subprocess.CalledProcessError as exc:
                if b'No such container' not in (exc.stderr or b'') and b'No such object' not in (exc.stderr or b''): return False
            except (OSError, subprocess.SubprocessError): return False
            self.instances.pop(key,None)
            shutil.rmtree(item['root'], ignore_errors=True)
        return True

    def launch(self, p):
        key = p['id']
        if key in self.instances:
            return
        if len(self.instances) >= 2:
            raise ValueError('电脑最多同时运行两个预览，请先停止旧预览')
        workspace = self.agent.db.get_workspace(p['workspace_id'])
        if not workspace:
            raise ValueError('电脑上未找到成果工作区，请检查项目连接')
        if not shutil.which('docker'):
            raise ValueError('此电脑尚无 Docker 预览运行时，请在电脑管理中选择具备运行时的电脑')
        try: command(['docker', 'info', '--format', '{{.ServerVersion}}'])
        except (OSError, subprocess.SubprocessError): raise ValueError('Docker 未启动，预览暂不可用；已保留成果') from None
        root = Path(tempfile.mkdtemp(prefix='keji-preview-'))
        root.chmod(0o755)
        try:
            spec = snapshot(workspace['path'], p['commit'], root)
            image = IMAGES[spec['runtime']]
            try: command(['docker', 'image', 'inspect', image])
            except subprocess.SubprocessError:
                try: command(['docker', 'pull', image], timeout=45)
                except subprocess.SubprocessError: raise ValueError('官方运行镜像准备失败，请检查电脑网络后在网页重试') from None
            lifetime = min(1800, max(60, int(p.get('lifetime_seconds', 1800))))
            name = 'keji-preview-' + key
            # Container has a hard expiry even if the Runner exits or loses contact.
            command(['docker', 'run', '-d', '--rm', '--name', name, '--label', 'keji.preview=1',
                     '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                     '--log-opt','max-size=1m','--log-opt','max-file=1',
                     '--memory', '256m', '--cpus', '1', '--pids-limit', '64', '--read-only',
                     '--user', '65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                     '--mount', 'type=bind,src='+str(root)+',dst=/app,readonly', '-w', '/app',
                     '-e', 'PORT='+str(spec['port']), '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                     image, 'timeout', '-k', '5', str(lifetime), *spec['command']])
            self.instances[key] = {'name':name, 'root':root, 'spec':spec, 'expires':time.monotonic()+lifetime}
            deadline = time.monotonic()+25
            while time.monotonic()<deadline:
                try:
                    if 200 <= self.http(key, {'method':'GET', 'path':spec.get('health_path','/'), 'body':''})['code'] < 400:
                        return
                except (ValueError, OSError, subprocess.SubprocessError): pass
                if self.agent._stop.wait(.5): break
            raise ValueError('后端健康检查未通过。检查启动文件、依赖及 PORT；运行输出请在本次成果中修复后重试。')
        except Exception:
            self.stop(key); shutil.rmtree(root, ignore_errors=True); raise

    def http(self, key, request):
        item = self.instances.get(key)
        if not item:
            raise ValueError('实例已停止或电脑已重启，请重新启动预览')
        spec = item['spec']
        q = {k: request.get(k,'') for k in ('method','path','body','content_type')}
        if q['method'] not in ('GET','HEAD','POST','PUT','PATCH','DELETE') or not q['path'].startswith('/') or q['path'].startswith('//') or any(c in q['path'] for c in '\r\n\0\\'):
            raise ValueError('预览请求无效')
        q['port'] = spec['port']
        helper = ['python','-I','-c',PY_HTTP] if spec['runtime']=='python' else ['node','-e',NODE_HTTP]
        raw = command(['docker','exec','-i',item['name'],*helper],input=json.dumps(q).encode(),timeout=12)
        return json.loads(raw)

    def loop(self):
        try:
            while not self.agent._stop.is_set():
                try:
                    view = self.agent.cloud.request('GET','/runner/preview-requests',token=self.agent.access_token())
                    self.last_contact = time.monotonic()
                    allowed = {p['id']:p for p in view.get('previews') or []}
                    for key,p in list(allowed.items()):
                        if p['status']=='stopping':
                            if self.stop(key): self.agent.cloud.request('POST','/runner/preview-status',{'id':key,'status':'stopped','diagnostic':'隔离实例已清理'},self.agent.access_token())
                            del allowed[key]
                    for key in list(self.instances):
                        if key not in allowed or self.instances[key]['expires']<time.monotonic(): self.stop(key)
                    if time.monotonic() >= self.next_health:
                        self.next_health = time.monotonic()+5
                        for key in list(self.instances):
                            try:
                                state = command(['docker','inspect','--format','{{.State.Running}}',self.instances[key]['name']]).decode().strip()
                                if state != 'true': raise ValueError('stopped')
                            except (OSError,ValueError,subprocess.SubprocessError):
                                self.agent.cloud.request('POST','/runner/preview-status',{'id':key,'status':'failed','diagnostic':'隔离后端进程已退出，请让 AI 检查运行配置、依赖与启动代码后重试。'},self.agent.access_token())
                                self.stop(key)
                    for q in view.get('requests') or []:
                        p = allowed.get(q['preview_id'])
                        if not p: continue
                        try:
                            if self.agent.locally_paused(): raise ValueError('电脑已暂停，请从网页恢复后重新启动预览')
                            if q['action']=='start': self.launch(p); result={'code':200}
                            else: result=self.http(p['id'],q)
                        except Exception as exc:
                            result={'error':str(exc)[:600] if isinstance(exc,ValueError) else '运行条件未满足或后端退出，请让 AI 检查运行配置和依赖。'}
                        # Repeat acknowledgement only; never repeat a mutating HTTP request.
                        for retry in range(3):
                            try:
                                self.agent.cloud.request('POST','/runner/preview-requests/'+q['id'],result,self.agent.access_token());break
                            except Exception:
                                if self.agent._stop.wait(1): break
                except Exception:
                    # Local hard TTL remains enforced while transport is offline.
                    for key in list(self.instances):
                        if self.instances[key]['expires']<time.monotonic() or time.monotonic()-self.last_contact>60: self.stop(key)
                self.agent._stop.wait(1)
        finally:
            for key in list(self.instances): self.stop(key)
