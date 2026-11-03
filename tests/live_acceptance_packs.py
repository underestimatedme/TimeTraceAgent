"""Opt-in three real model tasks via the actual config/planning/review/receipt APIs.
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
repo=Path(apply(home,db,dict(id='ws1',name='P0 三类验收',path='',create=True,objective='',assertions=[]))['path'])
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
 snapshot=api('/iphone/workspace');assert snapshot['runtime']['acceptance_packs']==1
 request={'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':'codex-default','mode':'autonomous','check_name':contract['name'],'check_contract_version':contract['version'],'completion_version':1},'message':goal}
 job=api('/iphone/planning/jobs','POST',request);assert api('/iphone/planning/jobs','POST',request)['id']==job['id']
 started=time.monotonic();outcomes=[];graph={}
 try:
  while time.monotonic()-started<900:
   outcome=agent.run_once();outcomes.append(outcome)
   if 'idle' not in outcome:print(category,outcome,flush=True)
   job=api('/iphone/planning/jobs/'+job['id'])
   if job['status'] in ('failed','ready','cancelled'):raise AssertionError({k:job.get(k) for k in ('status','error_code','error_message')})
   if job['status']=='applied':
    graph=api('/iphone/work-items/'+job['result']['task_id']+'/autonomy')
    if graph['status']=='done':break
    if graph.get('autonomy_reason') in ('repair_budget_exhausted','verification_budget_exhausted'):raise AssertionError(graph['autonomy_reason'])
   time.sleep(1)
  else:raise AssertionError('timeout')
  assert graph['delivery']['completion_mode']=='automatic',graph['delivery']
  finals=[h for h in graph['delivery']['handoffs'] if h['kind']=='final'];assert len(finals)==1
  evidence=finals[0]['evidence'];assert evidence['commit']
  record={'sample_id':category+'-'+job['id'],'release':'p0-delivery-1','status':'passed','category':category,'task_id':graph['task_id'],'execution_job':next(o.split()[1] for o in outcomes if '→ awaiting_review' in o),'commit':evidence['commit'],'review_evidence':str(root/(category+'.json')),'receipt_evidence':finals[0]['id'],'actor':'assistant','environment':'loopback','production_modified':False,'fault_injection':False,'agent_version':__version__,'elapsed_seconds':time.monotonic()-started,'contract':contract['version'],'pipeline':graph}
  (root/(category+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
  with (root/'cases.jsonl').open('a') as out:out.write(json.dumps({k:v for k,v in record.items() if k!='pipeline'},ensure_ascii=False)+'\n')
  print('PASS '+category+' '+evidence['commit'],flush=True)
 except Exception as exc:
  (root/(category+'-failure.json')).write_text(json.dumps({'category':category,'error':str(exc),'pipeline':graph,'outcomes':outcomes},ensure_ascii=False,indent=2));raise
if os.environ.get('TIMETRACE_PACK_FEEDBACK')=='1':
 assert category=='web'
 original=graph;stage=graph['stages'][0]
 feedback={'expected_revision':graph['revision'],'expected_step_revision':stage['revision'],'step_id':stage['id'],'iteration':stage['iteration'],'artifact_id':stage['delivery_artifact_id'],'message':'仅修改当前网页成果：把 #submit 按钮文字改为“再次问候”，新增可见段落“已按反馈修改”；保留输入 Ada 点击后 Hello Ada 行为。提交改动，不安装不联网不发布。','action':'revise','idempotency_key':uuid.uuid4().hex}
 path='/iphone/pipelines/'+graph['id']+'/result-feedback'
 revised=api(path,'POST',feedback);again=api(path,'POST',feedback)
 assert revised['id']==original['id']==again['id'] and len(again['feedback'])==1 and not revised['delivery']['final_accepted']
 deadline=time.monotonic()+900
 while time.monotonic()<deadline:
  outcome=agent.run_once()
  if 'idle' not in outcome:print('feedback',outcome,flush=True)
  graph=api('/iphone/work-items/'+original['task_id']+'/autonomy')
  if graph['delivery']['final_current'] and graph['delivery']['all_delivered']:break
  time.sleep(1)
 else:raise AssertionError('feedback delivery timeout')
 assert graph['status']!='done' and graph['task_id']==original['task_id']
 assert graph['stages'][0]['iteration']>original['stages'][0]['iteration']
 commit=graph['stages'][0]['verification']['reviewed_commit']
 html=git('show',commit+':index.html')
 assert '再次问候' in html and '已按反馈修改' in html and commit!=record['commit']
 graph=api('/iphone/pipelines/'+graph['id']+'/delivery-operations','POST',{'expected_revision':graph['revision'],'idempotency_key':uuid.uuid4().hex,'kind':'review_final','artifact_id':graph['final_artifact_id'],'decision':'pass','reason':'隔离测试操作员核对当前提交含再次问候及反馈说明，原交互通过独立检查；仅确认这些有限要求。'})
 assert graph['status']=='done' and graph['delivery']['completion_mode']=='human'
 assert any(h['kind']=='final' and not h['current'] for h in graph['delivery']['handoffs'])
 (root/'feedback.json').write_text(json.dumps({'actor':'assistant','environment':'loopback','task_id':graph['task_id'],'original_commit':record['commit'],'revised_commit':commit,'pipeline':graph,'independent_new_user':False},ensure_ascii=False,indent=2))
 print('PASS: same-task revision, idempotent feedback, new commit and receipt, old receipt retained',flush=True)
print('PASS: requested real model task categories configured, dispatched, independently checked and receipted',flush=True)
db.close()
