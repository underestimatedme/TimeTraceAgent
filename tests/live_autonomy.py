"""Opt-in real subscription smoke test, driven by Valley's loopback test fixture.
Never supplies production credentials to Valley; changes only a fresh test repo.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from timetrace.agent import Agent
from timetrace.adapters.codex import CodexAdapter
from timetrace.adapters.claude import ClaudeAdapter
from timetrace.cloud import CloudClient
from timetrace.config import load
from timetrace.db import Database
from timetrace.routing import identity

fixture = json.loads(Path(sys.argv[1]).read_text())
assert fixture['base_url'].startswith('http://127.0.0.1:')
root = Path(fixture['directory']).resolve()
root.mkdir(parents=True, exist_ok=True)
repo = root / 'repository'
repo.mkdir()
def git(*args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
git('init', '-b', 'main')
git('config', 'user.name', 'TimeTrace Acceptance')
git('config', 'user.email', 'acceptance@example.invalid')
(repo/'calculator.py').write_text('def add(a, b):\n    return a - b\n')
tests = 'import unittest\nfrom calculator import add\nclass Addition(unittest.TestCase):\n    def test_positive(self): self.assertEqual(add(2,3),5)\n    def test_negative(self): self.assertEqual(add(-2,-3),-5)\n    def test_zero(self): self.assertEqual(add(4,0),4)\n'
(repo/'test_calculator.py').write_text(tests)
git('add', '.')
git('commit', '-m', 'Seed intentionally failing addition fixture')
provider = os.environ.get('TIMETRACE_A1_LIVE_PROVIDER', 'codex')
assert provider in ('codex','claude')
profile = provider + '-default'
cfg = dict(load()[provider])
cfg['timeout_seconds'] = 240
adapter = (CodexAdapter if provider == 'codex' else ClaudeAdapter)(cfg)
account, model = identity(adapter)
assert account and model and adapter.capabilities()['can_enforce_zero_spend']
cloud = CloudClient(fixture['base_url'])
token = fixture['runner_token']
cloud.update_inventory(token, [{'id':'ws1','name':'Acceptance repo','kind':'git','default_branch':'main','checks':['unit']}], [{'id':profile,'provider':provider,'version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}], 1, {provider:1}, {'protocol_version':2,'agent_version':'0.5.3rc1-a1','accepting_local':True,'health':{'codex_login':'ok','disk_free_bytes':10000000000}})
def api(path, method='GET', body=None):
    return cloud.request(method, path, body, fixture['session']['access_token'])
route = api('/iphone/project-routing')
settings = route['settings']
settings['enabled'] = True
settings['projects'][fixture['project_id']] = 'personal'
for category in settings['categories']:
    if category['id']=='personal': category['choices']=[{'account_id':account,'model':model}]
api('/iphone/project-routing', 'POST', {'expected_revision':route['revision'],'idempotency_key':uuid.uuid4().hex,'settings':settings})
home = root/'agent'
home.mkdir(mode=0o700)
db = Database(home/'timetrace.db')
db.upsert_workspace('ws1','Acceptance repo',str(repo),'main')
db.save_check('ws1','unit',[sys.executable,'-m','unittest','-v'])
agent = Agent(db,cloud,{provider:adapter},home,lambda:token,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={provider:1})
snapshot = api('/iphone/workspace')
request = {'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':profile,'mode':'final_review','check_name':'unit'},'message':'这是隔离验收项目。请只规划一个 AI 步骤完成这项小修复：calculator.py 的 add(a,b) 错误地做了减法，改为加法。不要修改 test_calculator.py。完成修改并提交代码后，测试由系统自动运行已登记的 unit 检查（python3 -m unittest -v）；AI 不要自行执行任何测试 shell 命令，也不要声称测试已通过。只修改本仓库；不安装依赖、不访问外部服务、不发布、不推送。最终由我在任务外确认成果，不要另建人工确认步骤。'}
job = api('/iphone/planning/jobs','POST',request)
assert api('/iphone/planning/jobs','POST',request)['id']==job['id']
started=time.monotonic(); outcomes=[]
while time.monotonic()-started<900:
    outcome=agent.run_once(); outcomes.append(outcome); print(outcome,flush=True)
    job=api('/iphone/planning/jobs/'+job['id'])
    if job['status'] in ('failed','ready','cancelled'): raise AssertionError(json.dumps(job,ensure_ascii=False))
    if job['status']=='applied':
        graph=api('/iphone/work-items/'+job['result']['task_id']+'/autonomy')
        if graph.get('delivery',{}).get('all_delivered'): break
        for stage in graph['stages']:
            if stage.get('verification',{}).get('status') in ('fail','error'): raise AssertionError(json.dumps(graph,ensure_ascii=False))
    time.sleep(1)
else: raise AssertionError('real execution timed out')
assert graph['status']!='done' and not graph.get('final_accepted_at')
artifact=graph['delivery']['final_artifact']; sha=graph['stages'][-1]['verification']['reviewed_commit']
assert sha, 'registered check must bind the actual output commit'
assert git('show',sha+':test_calculator.py')==tests.strip(), 'acceptance tests were changed'
verified=root/'verified'
git('worktree','add','--detach',str(verified),sha)
check=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=verified,text=True,capture_output=True)
assert check.returncode==0, check.stdout+check.stderr
final=api('/iphone/pipelines/'+graph['id']+'/delivery-operations','POST',{'expected_revision':graph['revision'],'idempotency_key':uuid.uuid4().hex,'kind':'review_final','artifact_id':artifact['id'],'decision':'pass','reason':'验收脚本代表测试用户：独立复跑三个加法用例通过，接受当前成果'})
assert final['status']=='done'
report={'provider':provider,'model':model,'planning_id':job['id'],'remote_planning_id':job['remote_job_id'],'task_id':graph['task_id'],'pipeline_id':graph['id'],'commit':sha,'before_final':graph,'after_final':final,'independent_tests':check.stdout+check.stderr,'elapsed_seconds':round(time.monotonic()-started,1),'outcomes':outcomes,'production_modified':False,'final_review_actor':'test user via explicit API, not a human UI acceptance'}
(root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('PASS: real planning → execution → independent review → explicit test-user final acceptance',flush=True)
db.conn.close()
