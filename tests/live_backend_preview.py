"""Real backend preview fixture; no AI mock or production credentials."""
import json,sys,time,threading,subprocess
from pathlib import Path
from types import SimpleNamespace
from timetrace.runtime_preview import RuntimePreviews
from timetrace.cloud import CloudClient
from timetrace.app_preview import collect
root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True)
repo=root/'repo';repo.mkdir(exist_ok=True);(repo/'.keji').mkdir(exist_ok=True)
(repo/'.keji/preview.json').write_text(json.dumps({'version':1,'runtime':'python','command':['python','server.py'],'port':8080,'health_path':'/api/count'}))
(repo/'index.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>真实后端验收</title><style>body{font:20px system-ui;padding:40px;background:#f5f2eb;color:#243d34}button{padding:12px 24px;margin:10px;background:#6054bd;color:white;border:0;border-radius:9px}</style><h1>隔离后端计数器</h1><p>每次操作都会请求实际 Python HTTP 服务。</p><strong id="count">加载中</strong><button id="add">后端增加 2</button><button id="load">重新读取后端</button><script src="app.js"></script></html>''')
(repo/'app.js').write_text('''async function load(){let r=await fetch('/api/count');document.querySelector('#count').textContent='后端值：'+(await r.json()).count}document.querySelector('#add').onclick=async()=>{await fetch('/api/count',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({add:2})});await load()};document.querySelector('#load').onclick=load;load();''')
(repo/'server.py').write_text('''from http.server import SimpleHTTPRequestHandler,HTTPServer
import json
count=0
class H(SimpleHTTPRequestHandler):
 def do_GET(self):
  if self.path.startswith('/api/count'):
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'count':count}).encode())
  else: super().do_GET()
 def do_POST(self):
  global count
  d=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));count+=d.get('add',1);self.do_GET()
HTTPServer(('127.0.0.1',8080),H).serve_forever()
''')
def git(*args): return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
git('init');git('add','.');git('-c','user.name=QA','-c','user.email=qa@localhost','commit','-m','Backend preview acceptance');sha=git('rev-parse','HEAD');bundle,_=collect(str(repo),sha);(root/'bundle.json').write_bytes(bundle);(root/'commit').write_text(sha)
print('FIXTURE_READY',flush=True)
while not (root/'session.json').exists():time.sleep(.2)
s=json.loads((root/'session.json').read_text());cloud=CloudClient(s['base_url']);agent=SimpleNamespace(db=SimpleNamespace(get_workspace=lambda _: {'path':str(repo)}),_stop=threading.Event(),cloud=cloud,access_token=lambda:s['runner_token'],locally_paused=lambda:False)
runtime=RuntimePreviews(agent);runtime.start()
try:
 while not (root/'stop').exists():time.sleep(.5)
finally:
 agent._stop.set();runtime.thread.join(15)
