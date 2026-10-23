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
import shutil
shutil.copytree(Path(__file__).parent/'fixtures/browser_todo', repo, dirs_exist_ok=True)
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
scenario = 'repair'
objective = '真实预览交互故障注入验收：首次提交只给 app.js 加一行说明注释，必须保留现有 submit 处理缺陷。收到系统独立浏览器检查失败后，才修复应用并提交。最终目标：点击与 Enter 各新增一条且不重复；完成/恢复；全部、未完成、已完成筛选；空标题显示提示；重新打开页面后新增及完成状态仍保存。提供现有隔离后端与 index.html。固定验收入口 #todoInput、#addTask、.todo-item（标题及 checkbox）、[data-filter=all/active/completed]、#todoStatus。真实预览 sandbox=allow-scripts，不允许原生表单提交或 localStorage。只规划一个 AI 步骤，系统独立验收；不要运行测试命令、安装依赖、修改验收器、联网、推送或发布。'
from timetrace.web_workspace import apply
from timetrace.browser_checks import DEFAULT_SELECTORS
apply(home,db,{'id':'ws1','name':'Acceptance repo','path':str(repo),'create':False,'objective':objective,'assertions':[{'path':'index.html','kind':'todo_browser','value':json.dumps(DEFAULT_SELECTORS)}]})
check_name=objective_checks.inventory(db,'ws1')[0]['name']
contract=db.get_objective_check('ws1',check_name)
argv=db.get_check('ws1',check_name)
cloud.update_inventory(token, [{'id':'ws1','name':'Acceptance repo','kind':'git','default_branch':'main','checks':[check_name],'objective_checks':objective_checks.inventory(db,'ws1')}], [{'id':profile,'provider':provider,'version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}], 1, {provider:1}, {'protocol_version':2,'agent_version':'0.5.3rc1-a1','accepting_local':True,'health':{'codex_login':'ok','disk_free_bytes':10000000000}})
agent = Agent(db,cloud,{provider:adapter},home,lambda:token,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={provider:1})
snapshot = api('/iphone/workspace')
request = {'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':profile,'mode':'autonomous','check_name':check_name,'check_contract_version':contract['version'],'completion_version':1},'message':objective}
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
assert sha
assert graph.get('repairs_used')==1,graph
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
report={'scenario':scenario,'provider':provider,'model':model,'planning_id':job['id'],'remote_planning_id':job['remote_job_id'],'task_id':graph['task_id'],'pipeline_id':graph['id'],'commit':sha,'after_final':graph,'independent_tests':check.stdout+check.stderr,'elapsed_seconds':round(time.monotonic()-started,1),'outcomes':outcomes,'production_modified':False,'additional_human_actions':0,'fault_injection':True, 'assistant_repair_feedback':0,'checker_version':contract['version']}
(root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('PASS: real browser objective flow '+scenario+'; no accept/redo API calls',flush=True)
db.conn.close()
