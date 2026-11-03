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
(repo/'addition.py').write_text('def add(a, b):\n    return a - b\n')
(repo/'subtraction.py').write_text('def subtract(a, b):\n    return a + b\n')
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
scenario = os.environ.get('TIMETRACE_A2_SCENARIO', 'dag_repair')
assert scenario in ('dag_success', 'dag_repair')
objective = '整合已完成的 addition.py 和 subtraction.py，在 calculator.py 中导出 add 和 subtract，两函数对正数、负数和零都正确，提交整合成果。保持 test_calculator.py 不变。系统独立检查，不运行测试命令、不安装依赖、不联网、不推送、不发布。'
contracts = []
for name, module, function, wrong, correct, cases in [
    ('addition', 'addition', 'add', 'a - b', 'a + b', [(2,3,5),(-2,-3,-5),(4,0,4)]),
    ('subtraction', 'subtraction', 'subtract', 'a + b', 'a - b', [(5,2,3),(-2,-3,1),(4,0,4)])]:
    goal = f'仅修改 {module}.py，将 {function}(a,b) 修为 {correct} 并提交，覆盖正数、负数和零；不修改其他文件。系统独立检查，不运行测试命令、不安装依赖、不联网、不推送、不发布。'
    if scenario == 'dag_repair':
        goal = f'隔离故障注入：本步骤首次执行只给 {module}.py 添加说明注释，必须保留 {wrong} 缺陷并提交。收到自动检查失败反馈后再将 {function}(a,b) 修为 {correct} 并提交。最终覆盖正数、负数和零，不修改其他文件。系统独立检查，不运行测试命令、不联网不发布。'
    checker = root / ('trusted_' + name + '.py')
    checker.write_text(f"import os, sys\nsys.path.insert(0, os.getcwd())\nfrom {module} import {function}\ncases = {cases!r}\nfailed = [(a,b,c,{function}(a,b)) for a,b,c in cases if {function}(a,b) != c]\nif failed:\n    print('ASSERTION FAILED: fix {module}.py to {correct} and commit:', failed)\n    sys.exit(1)\nprint('PASS {name}: immutable cases')\n")
    command = [sys.executable, '-I', str(checker)]
    db.save_check('ws1', name, command)
    sub = objective_checks.register(repo, name, command, goal, checker, ['test_calculator.py'])
    db.save_objective_check('ws1', name, sub)
    contracts.append(sub)
checker = root / 'trusted_complete.py'
checker.write_text("import os, sys\nsys.path.insert(0, os.getcwd())\ntry:\n    from calculator import add, subtract\n    assert [add(2,3),add(-2,-3),add(4,0)] == [5,-5,4]\n    assert [subtract(5,2),subtract(-2,-3),subtract(4,0)] == [3,1,4]\nexcept (ImportError, AssertionError) as e:\n    print('ASSERTION FAILED: calculator.py must integrate and export correct add and subtract:', e)\n    sys.exit(1)\nprint('PASS integration: all six immutable addition/subtraction cases')\n")
argv = [sys.executable, '-I', str(checker)]
db.save_check('ws1', 'unit', argv)
contract = objective_checks.register(repo, 'unit', argv, objective, checker, ['test_calculator.py'])
manifest = [{'key': c['name'], 'title': c['name'], 'check_name': c['name'], 'depends_on': []} for c in contracts]
contract = objective_checks.with_workflow(db, 'ws1', contract, manifest)
db.save_objective_check('ws1', 'unit', contract)
cloud.update_inventory(token, [{'id':'ws1','name':'Acceptance repo','kind':'git','default_branch':'main','checks':['unit','addition','subtraction'],'objective_checks':objective_checks.inventory(db,'ws1')}], [{'id':profile,'provider':provider,'version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}], 1, {provider:1}, {'protocol_version':2,'agent_version':'0.5.3rc1-a1','accepting_local':True,'health':{'codex_login':'ok','disk_free_bytes':10000000000}})
agent = Agent(db,cloud,{provider:adapter},home,lambda:token,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={provider:1})
snapshot = api('/iphone/workspace')
request = {'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':profile,'mode':'autonomous','check_name':'unit','check_contract_version':contract['version'],'completion_version':2},'message':objective}
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
assert graph.get('repairs_used', 0) == (2 if scenario == 'dag_repair' else 0), graph
assert len(graph['stages']) == 3
assert graph['status'] == 'done' and graph['delivery']['completion_mode'] == 'automatic', graph
policy = graph['autonomy_policy']
final = next(s for s in graph['stages'] if s['id'] == policy['stage_ids']['objective_final'])
sha = final['verification']['reviewed_commit']
assert sha and git('show',sha+':test_calculator.py') == tests.strip()
repairs = [h for h in graph['delivery']['handoffs'] if h['kind']=='repair']
assert len(repairs) == graph.get('repairs_used',0)
if repairs:
    assert len({h['source_step_id'] for h in repairs}) == 2
finals = [h for h in graph['delivery']['handoffs'] if h['kind']=='final']
assert len(finals)==1 and len(finals[0]['evidence']['stages'])==3
assert finals[0]['evidence']['commit']==sha
for stage in graph['stages']:
    if stage['id'] != final['id']:
        # Real ancestry proof: the final checkout contains both accepted branches.
        git('merge-base','--is-ancestor',stage['verification']['reviewed_commit'],sha)
verified = root/'verified'
git('worktree','add','--detach',str(verified),sha)
check = subprocess.run(argv,cwd=verified,text=True,capture_output=True)
assert check.returncode == 0, check.stdout + check.stderr
assert agent.run_once() in ('idle','no job')
report={'scenario':scenario,'provider':provider,'model':model,'planning_id':job['id'],'remote_planning_id':job['remote_job_id'],'task_id':graph['task_id'],'pipeline_id':graph['id'],'commit':sha,'after_final':graph,'independent_tests':check.stdout+check.stderr,'elapsed_seconds':round(time.monotonic()-started,1),'outcomes':outcomes,'production_modified':False,'additional_human_actions':0,'fault_injection':scenario=='dag_repair','checker_version':contract['version']}
(root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('PASS: real objective flow '+scenario+'; no accept/redo API calls',flush=True)
db.conn.close()
