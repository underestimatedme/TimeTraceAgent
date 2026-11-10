import json
import unittest
import subprocess
import sys
from unittest.mock import patch
from timetrace.browser_checks import selectors, DEFAULT_SELECTORS
from pathlib import Path
import tempfile
from timetrace.db import Database
from timetrace.web_workspace import validate, apply
from timetrace.objective_checks import inventory, valid

class BrowserContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.db=Database(self.home/'db'); self.addCleanup(self.db.close)
        self.spec=dict(id='web-test',name='网页项目',path='',create=True,objective='待办交互',assertions=[{'path':'index.html','kind':'todo_browser','value':json.dumps(DEFAULT_SELECTORS)}])
    def test_contract_pins_renderer_and_engine(self):
        with patch('timetrace.browser_checks.prepare'):
            row=apply(self.home,self.db,self.spec)
        c=inventory(self.db,row['id'])[0]
        contract=self.db.get_objective_check(row['id'],c['name'])
        self.assertTrue(valid(contract,self.db.get_check(row['id'],c['name'])))
        self.assertEqual(contract['timeout_seconds'],180)
        argv=self.db.get_check(row['id'],c['name'])
        self.assertEqual(argv[0],sys.executable)
        result=subprocess.run(argv,cwd=row['path'],capture_output=True,text=True)
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertIn('缺少可预览',result.stdout)
        self.assertTrue(any(p.endswith('preview_renderer.js') for p in contract['trusted_files']))
        self.assertTrue(any(p.endswith('browser_checks.py') for p in contract['trusted_files']))
    def test_invalid_schema_and_duplicate_suite(self):
        for raw in ('{}','[]','null',json.dumps(dict(DEFAULT_SELECTORS,command='run'))):
            with self.subTest(raw=raw),self.assertRaises(ValueError): selectors(raw)
        self.spec['assertions']*=2
        with self.assertRaises(ValueError): validate(self.spec)
    def test_unavailable_browser_cannot_register_or_ack(self):
        with patch('timetrace.browser_checks.prepare',side_effect=ValueError('browser_runtime_unavailable')):
            with self.assertRaisesRegex(ValueError,'browser_runtime_unavailable'): apply(self.home,self.db,self.spec)
        self.assertFalse(inventory(self.db,self.spec['id']))
