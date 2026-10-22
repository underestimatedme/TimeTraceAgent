import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from timetrace import objective_checks
from timetrace.db import Database
from timetrace.agent import Agent


class ObjectiveChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.checker = self.root / 'trusted.py'
        self.checker.write_text("print('PASS')\n")
        self.fixture = self.repo / 'test.txt'
        self.fixture.write_text('trusted fixture')
        for args in [('init', '-q'), ('config', 'user.email', 'qa@example.invalid'),
                     ('config', 'user.name', 'QA'), ('add', '.'), ('commit', '-qm', 'fixture')]:
            subprocess.run(['git', '-C', str(self.repo), *args], check=True)
        self.argv = [sys.executable, '-I', str(self.checker)]
        self.contract = objective_checks.register(self.repo, 'unit', self.argv, 'finite goal', self.checker, ['test.txt'])

    def execute(self):
        return objective_checks.run(self.contract, self.argv, str(self.repo), str(self.root/'log'), threading.Event())

    def test_fixed_checker_passes_on_clean_commit(self):
        result = self.execute()
        self.assertTrue(result.passed)
        self.assertTrue(result.objective_evidence['integrity_valid'])
        self.assertTrue(result.objective_evidence['snapshot_clean'])

    def test_changed_checker_or_protected_test_never_passes(self):
        for path in (self.checker, self.fixture):
            with self.subTest(path=path.name):
                original = path.read_text()
                path.write_text('changed')
                result = self.execute()
                self.assertFalse(result.objective_evidence['integrity_valid'])
                self.assertFalse(result.passed)
                path.write_text(original)

    def test_checker_mutating_tracked_source_is_not_valid_evidence(self):
        self.checker.write_text("from pathlib import Path\nPath('test.txt').write_text('changed during check')\n")
        self.contract = objective_checks.register(self.repo, 'unit', self.argv, 'finite goal', self.checker, ['test.txt'])
        result = self.execute()
        self.assertTrue(result.passed)  # the command's label alone is insufficient
        self.assertFalse(result.objective_evidence['snapshot_clean'])
        self.assertFalse(result.objective_evidence['integrity_valid'])

    def test_untracked_source_invalidates_committed_snapshot(self):
        self.checker.write_text("from pathlib import Path\nPath('extra.py').write_text('changed during check')\n")
        self.contract = objective_checks.register(self.repo, 'unit', self.argv, 'finite goal', self.checker)
        self.assertFalse(self.execute().objective_evidence['snapshot_clean'])

    def test_classifies_assertions_and_infrastructure_separately(self):
        for code, category in ((1, 'assertion'), (127, 'infrastructure')):
            self.checker.write_text('import sys\nsys.exit(%d)\n' % code)
            self.contract = objective_checks.register(self.repo, 'unit', self.argv, 'finite goal', self.checker)
            self.assertEqual(category, self.execute().objective_evidence['failure_class'])

    def test_model_writable_checker_is_rejected(self):
        path = self.repo/'checker.py';path.write_text('print(1)')
        with self.assertRaises(ValueError):
            objective_checks.register(self.repo,'unit',[sys.executable,str(path)],'goal',path)

    def test_local_registration_is_revoked_when_command_changes(self):
        db=Database(self.root/'db.sqlite');self.addCleanup(db.conn.close)
        db.save_check('w','unit',self.argv)
        db.save_objective_check('w','unit',self.contract)
        public=objective_checks.inventory(db,'w')
        self.assertEqual(set(public[0]),{'name','version','objective'})
        db.save_check('w','unit',[sys.executable,'--version'])
        self.assertEqual([],objective_checks.inventory(db,'w'))

    def test_round_deadline_caps_renewable_lease(self):
        claim={'lease_expires_at':'2030-01-01T12:00:00Z','job':{'autonomy_deadline':'2030-01-01T11:00:00Z'}}
        self.assertEqual(Agent._deadline({'lease_expires_at':'2030-01-01T11:00:00Z'}),Agent._deadline(claim))
        claim['job']['autonomy_deadline']='invalid'
        self.assertEqual(0,Agent._deadline(claim))
