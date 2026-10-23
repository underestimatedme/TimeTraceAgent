"""Opt-in real P0 acceptance: cloud setup -> idle application -> actual Codex -> bounded repair -> delivery."""
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
cloud.update_inventory(token,[],tools,1,{'codex':1},{'protocol_version':2,'agent_version':'0.5.4rc1','config_version':2,'planning_version':1,'workflow_inputs_version':1,'accepting_local':True})
goal='在新项目创建并提交 index.html，页面使用中文标题“自主交付验收”，包含文字“P0_READY”，带清晰的标题和蓝色按钮。隔离验收故障注入：首次运行只提交缺少 P0_READY 的版本，然后立即结束本次回复，不要自行检查或补齐。仅当后续新一轮任务提示包含系统检查失败与修改意见时，才补齐并提交。仅本地交付，不安装依赖、不联网、不发布。最终验收只要求 index.html 已提交且包含自主交付验收和 P0_READY；视觉质量不在自动判定范围。'
body={'expected_revision':0,'idempotency_key':uuid.uuid4().hex,'desired':{'workspace':{'id':'ws1','name':'网页启用真实验收','path':'','create':True,'objective':goal,'assertions':[{'path':'index.html','kind':'contains','value':'自主交付验收'},{'path':'index.html','kind':'contains','value':'P0_READY'}]}},'sync_routing':False}
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
started=time.monotonic();outcomes=[]
while time.monotonic()-started<900:
 out=agent.run_once();outcomes.append(out);print(out,flush=True)
 job=api('/iphone/planning/jobs/'+job['id'])
 if job['status'] in ('failed','ready','cancelled'):raise AssertionError({k:job.get(k) for k in ('status','error_code','error_message')})
 if job['status']=='applied':
  graph=api('/iphone/work-items/'+job['result']['task_id']+'/autonomy')
  if graph['status']=='done':break
  if graph.get('autonomy_reason') in ('repair_budget_exhausted','verification_budget_exhausted'):raise AssertionError(json.dumps(graph,ensure_ascii=False))
 time.sleep(1)
else:raise AssertionError('timeout')
assert graph['delivery']['completion_mode']=='automatic',graph
assert graph.get('repairs_used')==1, 'Expected one platform repair; got '+str(graph.get('repairs_used',0))
artifacts=[a for s in graph['stages'] for a in s.get('artifacts',[])]
assert any(a['ref']=='review/changes.diff' and a.get('commit_sha') for a in artifacts)
assert any(a['ref']=='index.html' and 'P0_READY' in a.get('content_snapshot','') for a in artifacts)
(root/'acceptance.json').write_text(json.dumps({'pipeline':graph,'config_revision':v['revision'],'outcomes':outcomes,'elapsed':time.monotonic()-started,'additional_human_actions':0,'fault_injection':True,'production_modified':False},ensure_ascii=False,indent=2))
print('PASS: cloud workspace setup, real Codex, one bounded repair, automatic receipt, commit-bound HTML/diff',flush=True)
# Real subjective delivery: checks pass automatically but the final receipt
# must remain pending until the operator examines the version-bound preview.
snapshot=api('/iphone/workspace')
request.update(expected_revision=snapshot['revision'],idempotency_key=uuid.uuid4().hex,message='仅用一个 AI 步骤创建并提交 index.html：中文标题“自主交付验收”、文字 P0_READY，浅色背景、清晰说明和蓝色圆角按钮。自包含 HTML 与 CSS，不联网、不安装依赖、不发布；视觉质量由最后人工判断。',launch={'runner_id':'r1','tool_profile_id':'codex-default','mode':'final_review','check_name':contract['name']})
visual=api('/iphone/planning/jobs','POST',request)
visual_start=time.monotonic()
while time.monotonic()-visual_start<900:
 print(agent.run_once(),flush=True)
 visual=api('/iphone/planning/jobs/'+visual['id'])
 if visual['status'] in ('failed','ready','cancelled'):raise AssertionError('visual planning '+visual['status'])
 if visual['status']=='applied':
  vg=api('/iphone/work-items/'+visual['result']['task_id']+'/autonomy')
  assert vg['status']!='done','subjective delivery was accepted automatically'
  if vg['delivery']['final_current'] and vg['delivery']['all_delivered']:break
 time.sleep(1)
else:raise AssertionError('visual delivery timeout')
(root/'visual-review.json').write_text(json.dumps(vg,ensure_ascii=False,indent=2))
print('VISUAL_REVIEW_READY '+str(root/'visual-review.json'),flush=True)
deadline=time.monotonic()+900
while not (root/'visual-reviewed').exists():
 assert time.monotonic()<deadline,'visual review not completed'
 time.sleep(1)
vg=api('/iphone/pipelines/'+vg['id']+'/delivery-operations','POST',{'expected_revision':vg['revision'],'idempotency_key':uuid.uuid4().hex,'kind':'review_final','artifact_id':vg['final_artifact_id'],'decision':'pass','reason':'隔离验收操作员已查看该提交的静态成果，确认标题、说明和按钮符合本轮视觉目标。'})
assert vg['status']=='done' and vg['delivery']['completion_mode']=='human',vg
(root/'visual-accepted.json').write_text(json.dumps(vg,ensure_ascii=False,indent=2))
print('PASS: real subjective flow, one operator review after objective checks',flush=True)
