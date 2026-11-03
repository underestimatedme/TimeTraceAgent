"""Opt-in actual Chromium renderer checks, isolated temporary Git fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from timetrace.browser_checks import prepare, run

@unittest.skipUnless(os.environ.get('TIMETRACE_WEB_FLOW_LIVE') == '1', 'opt-in real Chromium')
class WebFlowLive(unittest.TestCase):
    def test_detects_missing_handler_and_checks_exact_commit(self):
        prepare(backend_required=False)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            def git(*args): return subprocess.check_output(['git',*args],cwd=root,text=True).strip()
            git('init','-b','main')
            steps=[{'action':'fill','selector':'#name','value':'Ada'}, {'action':'click','selector':'#submit'}, {'action':'text','selector':'#status','value':'Hello Ada'}, {'action':'count','selector':'#status','value':1}]
            html='<input id="name"><button id="submit">Save</button><p id="status">Waiting</p>'
            previous=Path.cwd()
            try:
                os.chdir(root)
                for fixed in (False,True):
                    (root/'index.html').write_text(html+('''<script>document.querySelector('#submit').onclick=()=>{document.querySelector('#status').textContent='Hello '+document.querySelector('#name').value}</script>''' if fixed else ''))
                    git('add','.');git('-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-qm',str(fixed))
                    result=run({'path':'index.html','kind':'web_flow','value':json.dumps(steps)},Path(temp)/str(fixed))
                    self.assertEqual(result['status'],'passed' if fixed else 'failed',result)
                    self.assertEqual(result['commit'],git('rev-parse','HEAD'))
                    if fixed:self.assertEqual(len(result['steps']),4)
            finally:os.chdir(previous)
