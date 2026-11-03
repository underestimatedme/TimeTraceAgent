"""Opt-in real browser + isolated backend. No user's profile or project data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from timetrace.browser_checks import run, prepare, DEFAULT_SELECTORS

@unittest.skipUnless(os.environ.get('TIMETRACE_BROWSER_LIVE')=='1','opt-in real browser and Docker')
class LiveBrowserContract(unittest.TestCase):
    def test_catches_form_filter_and_persistence_defects(self):
        prepare()
        source=Path(__file__).parent/'fixtures'
        for fault,phase in [('form','点击新增'),('filter','完成与筛选'),('save','关闭页面再打开'),('none','')]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                repo=Path(temp)/'repo';shutil.copytree(source/'browser_todo',repo)
                if fault!='form': shutil.copy(source/'browser_todo_fixed_app.js',repo/'app.js')
                if fault=='filter':
                    p=repo/'app.js';p.write_text(p.read_text().replace('function visibleTasks() {','function visibleTasks() { return tasks;'))
                if fault=='save':
                    p=repo/'server.py';p.write_text(p.read_text().replace('tasks = list_tasks()','tasks = []'))
                def git(*args): subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
                git('init','-b','main');git('add','.');git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','Fixture')
                previous=Path.cwd()
                try:
                    os.chdir(repo)
                    result=run({'path':'index.html','kind':'todo_browser','value':json.dumps(DEFAULT_SELECTORS)}, Path(temp)/'evidence')
                finally: os.chdir(previous)
                self.assertEqual(result['status'],'passed' if fault=='none' else 'failed',result)
                if phase:self.assertIn(phase,result['phase'])
                if fault=='none': self.assertEqual(len(result['steps']),6)
