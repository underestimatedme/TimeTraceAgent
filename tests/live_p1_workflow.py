"""Opt-in P1 acceptance: actual Codex delivery, browser-directed revision, final receipt and notifications."""
import json, os, subprocess, sys, time, uuid
from pathlib import Path
from timetrace.agent import Agent
from timetrace.adapters.codex import CodexAdapter
from timetrace.cloud import CloudClient
from timetrace.config import load
from timetrace.db import Database
from timetrace.remote_config import RemoteConfig
from timetrace.cli import _runner_workspaces
from timetrace.routing import identity
fixture=json.loads(Path(sys.argv[1]).read_text()); assert fixture['base_url'].startswith('http://127.0.0.1:')
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
# Maintain the existing fixture workspace id: only its directory is new.
cloud.update_inventory(token,[],tools,1,{'codex':1},{'protocol_version':2,'agent_version':'0.5.5rc1','config_version':2,'planning_version':1,'workflow_inputs_version':1,'accepting_local':True})
goal='创建并提交一个自包含的交互计数器应用：index.html 有标题“自主应用验收”、id 为 count 的数字初始为 0、按钮文字“增加”；app.js 使用 addEventListener 绑定按钮，每次点击数字加 1；styles.css 提供清晰布局。HTML 引用这两个本地文件，禁止外部资源、网络、安装依赖和发布。自动验收只覆盖三个文件已提交且含上述指定文字，交互行为由浏览器实际验收。'
body={'expected_revision':0,'idempotency_key':uuid.uuid4().hex,'desired':{'workspace':{'id':'ws1','name':'网页启用真实验收','path':'','create':True,'objective':goal,'assertions':[{'path':'index.html','kind':'contains','value':'自主应用验收'},{'path':'app.js','kind':'contains','value':'addEventListener'},{'path':'styles.css','kind':'exists','value':''}]}},'sync_routing':False}
op=api('/runners/r1/config','POST',body)
assert api('/runners/r1/config','POST',body)['latest']['id']==op['latest']['id']
assert control(agent,False) is False
v=api('/runners/r1/config');assert v['latest']['status']=='applied',v
workspace=next(w for w in api('/runners')[0]['workspaces'] if w['id']=='ws1')
contract=workspace['objective_checks'][0]
route=api('/iphone/project-routing');settings=route['settings'];settings['enabled']=True;settings['projects'][fixture['project_id']]='personal'
for category in settings['categories']:
 if category['id']=='personal':category['choices']=[{'account_id':account,'model':model}]
api('/iphone/project-routing','POST',{'expected_revision':route['revision'],'idempotency_key':uuid.uuid4().hex,'settings':settings})
snapshot=api('/iphone/workspace')
request={'expected_revision':snapshot['revision'],'idempotency_key':uuid.uuid4().hex,'scope':'work_item','context_id':fixture['project_id'],'timezone':'Asia/Dubai','daily_limit_minutes':120,'window_start_minute':540,'window_end_minute':1080,'launch':{'runner_id':'r1','tool_profile_id':'codex-default','mode':'autonomous','check_name':contract['name'],'check_contract_version':contract['version'],'completion_version':1},'message':goal}
job=api('/iphone/planning/jobs','POST',request)
started=time.monotonic();first=False
while time.monotonic()-started<1500:
 out=agent.run_once()
 if 'idle' not in out:print(out,flush=True)
 job=api('/iphone/planning/jobs/'+job['id'])
 if job['status'] in ('failed','ready','cancelled'):raise AssertionError(str(job.get('error_message',job['status'])))
 if job['status']=='applied':
  graph=api('/iphone/work-items/'+job['result']['task_id']+'/autonomy')
  artifacts=[a for st in graph['stages'] for a in st.get('artifacts',[])]
  if graph['status']=='done' and not first:
   assert any('KEJI_APP_PREVIEW_V1' in a.get('content_snapshot','') for a in artifacts),'No executable preview file'
   first=True;(root/'first.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2));print('FIRST_READY '+str(root),flush=True)
  if first and graph['status']=='done' and graph.get('feedback'):
   assert graph['delivery']['completion_mode']=='human'
   feed=api('/notifications');work=[n for n in feed['list'] if n['type']=='workflow_completed'];assert len(work)==2,work
   assert sum(n['current'] for n in work)==1,work
   (root/'accepted.json').write_text(json.dumps({'pipeline':graph,'notifications':feed,'elapsed':time.monotonic()-started},ensure_ascii=False,indent=2));print('PASS: actual preview, referenced revision, latest final review, deduplicated completion notifications',flush=True);break
 time.sleep(1)
else:raise AssertionError('P1 acceptance timed out')
