"""Opt-in product-owned self-operations with three real model repairs.
Only loopback fixtures and a new QA directory; never production data.
"""
import json, os, subprocess, sys, time, uuid
from pathlib import Path
from timetrace import __version__
from timetrace.agent import Agent
from timetrace.adapters.codex import CodexAdapter
from timetrace.cloud import CloudClient
from timetrace.config import load
from timetrace.db import Database
from timetrace.remote_config import RemoteConfig
from timetrace.cli import _runner_workspaces
from timetrace.routing import identity
from timetrace.web_workspace import apply

fixture=json.loads(Path(sys.argv[1]).read_text());assert fixture['base_url'].startswith('http://127.0.0.1:')
root=Path(fixture['directory']);root.mkdir(parents=True,exist_ok=True)
home=root/'agent';home.mkdir(exist_ok=True)
db=Database(home/'timetrace.db');cfg=load();adapter=CodexAdapter(cfg['codex']);adapters={'codex':adapter}
account,model=identity(adapter);assert account and model
cloud=CloudClient(fixture['base_url']);token=fixture['runner_token']
def api(path,method='GET',body=None):return cloud.request(method,path,body,fixture['session']['access_token'])
tools=[{'id':'codex-default','provider':'codex','version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}]
def inventory():return (_runner_workspaces(db),tools,1,{'codex':1})
control=RemoteConfig(home,cfg,adapters)
agent=Agent(db,cloud,adapters,home,lambda:token,inventory=inventory,remote_config=control,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={'codex':1},report_protocol=True)
repo=Path(apply(home,db,dict(id='ws1',name='独立自运营测试项目',path='',create=True,objective='',assertions=[]))['path'])
def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
git('config','user.name','QA');git('config','user.email','qa@example.invalid')
(repo/'calculator.py').write_text('def add(a,b): return a-b\n')
(repo/'test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass Test(unittest.TestCase):\n def test_positive(self): self.assertEqual(add(3,2),5)\n def test_negative(self): self.assertEqual(add(-3,-2),-5)\n def test_zero(self): self.assertEqual(add(3,0),3)\n')
(repo/'docs').mkdir();(repo/'docs/release.md').write_text('# Changes\nTODO\n')
(repo/'index.html').write_text('<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>问候</title></head><body><input id="name"><button id="submit">显示问候</button><p id="status">等待输入</p></body></html>')
git('add','.');git('commit','-qm','Seed three defects')
db.save_check('ws1','project-test',[sys.executable,'-B','-m','unittest','test_calculator'])
cloud.update_inventory(token,*inventory(),{'protocol_version':2,'agent_version':__version__,'config_version':3,'planning_version':1,'workflow_inputs_version':1,'accepting_local':True})
route=api('/iphone/project-routing');settings=route['settings'];settings['enabled']=True;settings['projects'][fixture['project_id']]='personal'
for category in settings['categories']:
 if category['id']=='personal':category['choices']=[{'account_id':account,'model':model}]
api('/iphone/project-routing','POST',{'expected_revision':route['revision'],'idempotency_key':uuid.uuid4().hex,'settings':settings})
common='只规划一个 AI 步骤并提交改动，系统独立验收；不安装依赖、不联网、不推送、不发布。'
cases=[
 ('code','仅修改 calculator.py，把 add(a,b) 修为整数加法。保留测试和其他文件不变。'+common,dict(path='calculator.py',kind='regression',value=json.dumps({'check':'project-test','allow_changes':['calculator.py']}))),
 ('docs','仅更新并提交 docs/release.md，使用真实 Markdown 标题 Changes、Checks、Limitations。说明本项目整数加法修复、测试覆盖正负数与零、只覆盖整数的局限，每章有实际正文而非 TODO。自动验收仅覆盖结构，事实准确性不在自动判定范围。'+common,dict(path='docs/release.md',kind='markdown_sections',value=json.dumps(['Changes','Checks','Limitations']))),
 ('web','仅修改 index.html，给 #submit 添加显式点击处理，从 #name 读取名字，并让 #status 恰好显示 Hello 加一个空格加名字。保留元素 id，不使用原生表单提交，页面自包含。自动验收只覆盖输入 Ada 后点击显示 Hello Ada 和唯一状态元素，不评价视觉质量。'+common,dict(path='index.html',kind='web_flow',value=json.dumps([{'action':'fill','selector':'#name','value':'Ada'},{'action':'click','selector':'#submit'},{'action':'text','selector':'#status','value':'Hello Ada'},{'action':'count','selector':'#status','value':1}]))),
]
for category,goal,rule in cases:
 if os.environ.get('TIMETRACE_PACK_CATEGORY') and category!=os.environ['TIMETRACE_PACK_CATEGORY']:continue
 config=api('/runners/r1/config')
 request={'expected_revision':config['revision'],'idempotency_key':uuid.uuid4().hex,'desired':{'workspace':{'id':'ws1','name':'P0 三类验收','path':str(repo),'create':False,'objective':goal,'assertions':[rule]}}}
 op=api('/runners/r1/config','POST',request);assert api('/runners/r1/config','POST',request)['latest']['id']==op['latest']['id']
 deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  control(agent,False)
  observed=api('/runners/r1/config')
  if observed['latest']['status'] in ('applied','failed'):break
  time.sleep(1)
 assert observed['latest']['status']=='applied',observed['latest']
 workspace=next(w for w in api('/runners')[0]['workspaces'] if w['id']=='ws1')
 contract=next(c for c in workspace['objective_checks'] if c['objective']==goal)
 policy=api('/projects/'+fixture['project_id']+'/operations')['policy']
 request={'action':'configure','expected_revision':policy['revision'],'idempotency_key':uuid.uuid4().hex,'enabled':True,'self_repair':True,'interval_minutes':15,'timezone':'Asia/Dubai','hour':9,'repair_target':{'runner_id':'r1','tool_profile_id':'codex-default','mode':'autonomous','check_name':contract['name'],'check_contract_version':contract['version'],'completion_version':1}}
 configured=api('/projects/'+fixture['project_id']+'/operations','POST',request)
 assert api('/projects/'+fixture['project_id']+'/operations','POST',request)['policy']['revision']==configured['policy']['revision']
 # No planning/task/scan/receipt POST follows. Product scheduler and event
 # processing own discovery, task creation, execution, repair and final receipt.
 started=time.monotonic();outcomes=[];graph={};cycle={}
 try:
  while time.monotonic()-started<900:
   outcome=agent.run_once();outcomes.append(outcome)
   if 'idle' not in outcome:print(category,outcome,flush=True)
   observed=api('/projects/'+fixture['project_id']+'/operations')
   candidates=[c for c in observed['cycles'] if c['policy_revision']==configured['policy']['revision']]
   if candidates:
    cycle=candidates[0]
    if cycle['status'] in ('blocked','stopped'):raise AssertionError(cycle)
    if cycle['status']=='delivered':break
   time.sleep(1)
  else:raise AssertionError('timeout')
  graph=api('/iphone/work-items/'+cycle['task_id']+'/autonomy')
  assert graph['status']=='done' and graph['delivery']['completion_mode']=='automatic',graph
  finals=[h for h in graph['delivery']['handoffs'] if h['kind']=='final'];assert len(finals)==1
  assert cycle['receipt_id']==finals[0]['id'] and cycle['receipt_commit']==finals[0]['evidence']['commit']
  assert cycle['check_result']['exit_code']==1 and cycle['checked_commit']!=cycle['receipt_commit']
  assert git('rev-parse','HEAD')==cycle['baseline_head'],'product must not silently merge'
  record={'category':category,'status':'passed','origin':'product_scheduler','actor':'policy','environment':'loopback','production_modified':False,'agent_version':__version__,'elapsed_seconds':time.monotonic()-started,'cycle':cycle,'pipeline':graph,'outcomes':outcomes}
  (root/(category+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
  print('PASS '+category+' product discovery -> '+cycle['task_id']+' -> '+cycle['receipt_id'],flush=True)
 except Exception as exc:
  (root/(category+'-failure.json')).write_text(json.dumps({'category':category,'error':str(exc),'cycle':cycle,'pipeline':graph,'outcomes':outcomes},ensure_ascii=False,indent=2));raise
# Pause at the end; all evidence remains available for local inspection.
policy=api('/projects/'+fixture['project_id']+'/operations')['policy']
api('/projects/'+fixture['project_id']+'/operations','POST',{'action':'configure','expected_revision':policy['revision'],'idempotency_key':uuid.uuid4().hex,'enabled':False,'timezone':'Asia/Dubai','hour':9})
print('PASS: selected product-owned discovery/repair/independent-check/receipt cycles',flush=True)
db.close()
