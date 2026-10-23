import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from timetrace.db import Database
from timetrace.web_workspace import apply, validate, candidates
from timetrace.objective_checks import inventory, valid
from timetrace.review_artifacts import collect

class WebWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name); self.db = Database(self.home/'db'); self.addCleanup(self.db.close)
        self.spec = dict(id='web-test',name='网页项目',path='',create=True,objective='生成 docs/result.md 包含 Ready',assertions=[dict(path='docs/result.md',kind='contains',value='Ready')])
    def git(self, root, *args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
    def test_create_register_replay_and_independent_checker(self):
        row=apply(self.home,self.db,self.spec); root=Path(row['path'])
        base=self.git(root,'rev-parse','HEAD'); apply(self.home,self.db,self.spec)
        self.assertEqual(len(self.db.list_workspaces()),1)
        c=inventory(self.db,row['id'])[0]; argv=self.db.get_check(row['id'],c['name'])
        self.assertEqual(subprocess.run(argv,cwd=root,capture_output=True).returncode,1)
        (root/'docs').mkdir();(root/'docs/result.md').write_text('Ready\n')
        self.git(root,'add','.');self.git(root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','result')
        self.assertEqual(subprocess.run(argv,cwd=root,capture_output=True).returncode,0)
        head=self.git(root,'rev-parse','HEAD');arts=collect(str(root),base,head,[])
        self.assertIn('+Ready',arts[0]['content']);self.assertEqual(arts[0]['commit_sha'],head)
        checker=Path(argv[-1]);checker.write_text('print("forged")')
        self.assertFalse(valid(self.db.get_objective_check(row['id'],c['name']),argv))
    def test_invalid_assertions_cannot_execute_code_or_read_parent(self):
        for path in ('../secret','/etc/passwd','.git/config','a/.env','a//b','a\\b'):
            s=copy.deepcopy(self.spec);s['assertions'][0]['path']=path
            with self.subTest(path=path),self.assertRaises(ValueError):validate(s)
        s=copy.deepcopy(self.spec);s['command']='rm -rf /'
        with self.assertRaises(ValueError):validate(s)
    def test_existing_other_id_and_nonempty_create_refused(self):
        root=self.home/'projects/web-test';root.mkdir(parents=True);(root/'keep').write_text('data')
        with self.assertRaises(ValueError):apply(self.home,self.db,self.spec)
        self.assertEqual((root/'keep').read_text(),'data')
    def test_symlink_assertion_fails(self):
        row=apply(self.home,self.db,self.spec); root=Path(row['path']);(root/'docs').mkdir()
        outside=self.home/'secret';outside.write_text('Ready');(root/'docs/result.md').symlink_to(outside)
        self.git(root,'add','.');self.git(root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','link')
        c=inventory(self.db,row['id'])[0];argv=self.db.get_check(row['id'],c['name'])
        self.assertEqual(subprocess.run(argv,cwd=root,capture_output=True).returncode,1)
