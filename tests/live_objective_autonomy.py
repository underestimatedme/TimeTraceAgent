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
from timetrace import objective_checks

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
provider = os.environ.get('TIMETRACE_A1_LIVE_PROVIDER', 'claude')
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
scenario = os.environ.get('TIMETRACE_A2_SCENARIO', 'success')
assert scenario in ('success', 'repair', 'exhausted')
objective = '只修改 calculator.py，将 add(a,b) 修为整数加法，正数、负数和零均正确；保持 test_calculator.py 不变。仅规划一个 AI 步骤，修改并提交代码。系统独立运行登记检查，不要运行测试 shell 命令，不安装依赖，不联网，不推送，不发布。'
if scenario == 'repair':
    objective = '隔离故障注入验收：首次提交只给 calculator.py 添加说明注释，必须保留 a - b 的已知缺陷；收到系统独立检查失败反馈后，再修复为 a + b 并提交。最终 add 必须通过正数、负数和零的加法检查。保持 test_calculator.py 不变。只规划一个 AI 步骤，不另建验收步骤；系统负责检查，不要执行测试命令。不安装、不联网、不推送、不发布。'
if scenario == 'exhausted':
    objective = '隔离预算耗尽验收：每轮仅给 calculator.py 添加一行新的说明注释并提交，保留 a - b 的已知错误，即使收到检查失败反馈也不要修正公式。客观检查要求整数加法正确，因此本例预期始终失败并在修复预算耗尽后交由人处理。保持 test_calculator.py 不变。只规划一个 AI 步骤，系统负责检查，不要执行测试命令，不联网不发布。'
checker = root / 'trusted_addition.py'
checker.write_text("import os, sys\nsys.path.insert(0, os.getcwd())\nfrom calculator import add\ncases = [(2,3,5),(-2,-3,-5),(4,0,4)]\nfailed = [(a,b,c,add(a,b)) for a,b,c in cases if add(a,b) != c]\nif failed:\n    print('ASSERTION FAILED: add must use a + b. Fix calculator.py and commit. Cases:', failed)\n    sys.exit(1)\nprint('PASS: all three immutable addition cases')\n")
argv=[sys.executable,'-I',str(checker)]
db.save_check('ws1','unit',argv)
contract=objective_checks.register(repo,'unit',argv,objective,checker,['test_calculator.py'])
db.save_objective_check('ws1','unit',contract)
cloud.update_inventory(token, [{'id':'ws1','name':'Acceptance repo','kind':'git','default_branch':'main','checks':['unit'],'objective_checks':objective_checks.inventory(db,'ws1')}], [{'id':profile,'provider':provider,'version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}], 1, {provider:1}, {'protocol_version':2,'agent_version':'0.5.3rc1-a1','accepting_local':True,'health':{'codex_login':'ok','disk_free_bytes':10000000000}})
agent = Agent(db,cloud,{provider:adapter},home,lambda:token,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={provider:1})
snapshot = api('/iphone/workspace')
request = {'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':profile,'mode':'autonomous','check_name':'unit','check_contract_version':contract['version'],'completion_version':1},'message':objective}
job = api('/iphone/planning/jobs','POST',request)
assert api('/iphone/planning/jobs','POST',request)['id']==job['id']
started=time.monotonic(); outcomes=[]
while time.monotonic()-started<900:
    outcome=agent.run_once(); outcomes.append(outcome); print(outcome,flush=True)
    job=api('/iphone/planning/jobs/'+job['id'])
    if job['status'] in ('failed','ready','cancelled'): raise AssertionError(json.dumps(job,ensure_ascii=False))
    if job['status']=='applied':
        graph=api('/iphone/work-items/'+job['result']['task_id']+'/autonomy')
        if graph['status']=='done' or graph.get('autonomy_reason')=='repair_budget_exhausted': break
        for stage in graph['stages']:
            if stage.get('verification',{}).get('status') == 'error': raise AssertionError(json.dumps(graph,ensure_ascii=False))
    time.sleep(1)
else: raise AssertionError('real execution timed out')
assert graph.get('repairs_used',0) == {'success':0,'repair':1,'exhausted':2}[scenario], graph
assert graph['stages'][0]['iteration'] == graph.get('repairs_used',0) + 1
repairs=[h for h in graph['delivery']['handoffs'] if h['kind']=='repair']
assert len(repairs)==graph.get('repairs_used',0)
sha=graph['stages'][0]['verification']['reviewed_commit']
assert sha and git('show',sha+':test_calculator.py')==tests.strip()
verified=root/'verified'
git('worktree','add','--detach',str(verified),sha)
check=subprocess.run(argv,cwd=verified,text=True,capture_output=True)
if scenario=='exhausted':
    assert graph['status']!='done' and not graph['delivery']['final_accepted'] and check.returncode==1
    assert agent.run_once() in ('idle', 'no job'), 'must stop after repair budget'
else:
    assert graph['status']=='done' and graph['delivery']['completion_mode']=='automatic' and check.returncode==0
    finals=[h for h in graph['delivery']['handoffs'] if h['kind']=='final']
    assert len(finals)==1 and finals[0]['actor_type']=='policy' and finals[0]['evidence']['commit']==sha
report={'scenario':scenario,'provider':provider,'model':model,'planning_id':job['id'],'remote_planning_id':job['remote_job_id'],'task_id':graph['task_id'],'pipeline_id':graph['id'],'commit':sha,'after_final':graph,'independent_tests':check.stdout+check.stderr,'elapsed_seconds':round(time.monotonic()-started,1),'outcomes':outcomes,'production_modified':False,'additional_human_actions':0,'fault_injection':scenario!='success','checker_version':contract['version']}
(root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('PASS: real objective flow '+scenario+'; no accept/redo API calls',flush=True)
db.conn.close()
