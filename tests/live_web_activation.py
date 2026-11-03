"""Opt-in UI acceptance runner. No project, routing or task is created by this driver."""
import json, sys, time, os
from pathlib import Path
from timetrace.agent import Agent
from timetrace.adapters.codex import CodexAdapter
from timetrace.cloud import CloudClient
from timetrace.config import load, ensure_dirs
from timetrace.db import Database
from timetrace.remote_config import RemoteConfig
from timetrace.cli import _runner_workspaces
from timetrace.routing import identity
fixture=json.loads(Path(sys.argv[1]).read_text())
assert fixture['base_url'].startswith('http://127.0.0.1:')
root=Path(fixture['directory']);root.mkdir(parents=True,exist_ok=True)
home=root/'agent';ensure_dirs(home)
cfg=load(home) if (home/'config.json').exists() else load();cfg['codex']['timeout_seconds']=240
(home/'config.json').write_text(json.dumps(cfg));os.chmod(home/'config.json',0o600)
adapter=CodexAdapter(cfg['codex']);account,model=identity(adapter)
assert account and model
cloud=CloudClient(fixture['base_url']);token=fixture['runner_token'];db=Database(home/'timetrace.db')
tools=[{'id':'codex-default','provider':'codex','version':'local','status':'available','can_enforce_zero_spend':True,'routing_version':1,'routing_account':account,'routing_model':model}]
def inventory():return (_runner_workspaces(db),tools,1,{'codex':1})
adapters={'codex':adapter};control=RemoteConfig(home,cfg,adapters)
agent=Agent(db,cloud,adapters,home,lambda:token,inventory=inventory,remote_config=control,heartbeat_interval=5,max_parallel=1,max_parallel_per_tool={'codex':1},report_protocol=True)
from timetrace.runtime_preview import RuntimePreviews
previews=RuntimePreviews(agent)
if "--preview-only" in sys.argv:
    previews.loop();sys.exit(0)
previews.start()
agent.maintain(force=True)
(root/'ui-session.json').write_text(json.dumps(fixture));os.chmod(root/'ui-session.json',0o600)
print('UI_FIXTURE_READY '+str(root),flush=True)
last='';deadline=time.monotonic()+1800
while time.monotonic()<deadline and not (root/'stop').exists():
 result=agent.run_once()
 if result!=last:print(result,flush=True);last=result
 time.sleep(1)
agent.stop()
if previews.thread: previews.thread.join(timeout=10)
jobs=cloud.request('GET','/iphone/planning/jobs',token=fixture['session']['access_token'])['jobs']
created=[j for j in jobs if j['context_id']!=fixture['project_id']]
assert len(created)==1 and created[0]['status']=='applied','UI must create and complete one new-project delegation'
graph=cloud.request('GET','/iphone/work-items/'+created[0]['result']['task_id']+'/autonomy',token=fixture['session']['access_token'])
assert graph['status']=='done' and graph['delivery']['final_current'] and graph['delivery']['final_accepted']
assert graph['delivery']['completion_mode']=='automatic' and not graph.get('feedback')
assert any(h['kind']=='final' and h['actor_type']=='policy' and h['current'] for h in graph['delivery']['handoffs'])
(root/'acceptance.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2))
print('PASS: browser-created new project completed autonomously',flush=True)
db.conn.close()
