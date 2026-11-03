import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from timetrace import acceptance_packs as packs, objective_checks
from timetrace.db import Database
from timetrace.web_workspace import apply, validate


class AcceptancePacksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.db = Database(self.home / 'db'); self.addCleanup(self.db.close)
        self.spec = dict(id='pack', name='Pack QA', path='', create=True, objective='', assertions=[])
        self.root = Path(apply(self.home, self.db, self.spec)['path'])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-qm', 'sample')

    def register(self, kind, value, path='docs/release.md'):
        spec = dict(self.spec, create=False, path=str(self.root), objective='Finite ' + kind,
                    assertions=[dict(path=path, kind=kind, value=json.dumps(value))])
        apply(self.home, self.db, spec)
        item = objective_checks.inventory(self.db, 'pack')[-1]
        self.contract = self.db.get_objective_check('pack', item['name'])
        self.argv = self.db.get_check('pack', item['name'])

    def run_check(self):
        return objective_checks.run(self.contract, self.argv, str(self.root), str(self.home/'check.log'), threading.Event())

    def test_markdown_requires_real_headings_and_nonempty_body(self):
        (self.root/'docs').mkdir()
        target = self.root/'docs/release.md'
        self.register('markdown_sections', ['Changes', 'Checks'])
        for text, expected in [('Changes and Checks', False), ('# Changes\nTODO\n# Checks\nPassed', False),
                               ('```\n# Changes\nfoo\n# Checks\nbar\n```', False),
                               ('# Changes\nFix issue 42.\n## Checks\nRegression passed.', True)]:
            target.write_text(text); self.commit()
            self.assertEqual(self.run_check().passed, expected, text)

    def test_regression_failing_then_fixed_and_tampered_tests_rejected(self):
        (self.root/'calc.py').write_text('def add(a,b): return a-b\n')
        (self.root/'test_calc.py').write_text('import unittest\nfrom calc import add\nclass Test(unittest.TestCase):\n def test_add(self): self.assertEqual(add(3,2),5)\n')
        self.commit()
        self.db.save_check('pack', 'project-test', [sys.executable, '-B', '-m', 'unittest', 'test_calc'])
        self.register('regression', {'check':'project-test', 'allow_changes':['calc.py']}, 'calc.py')
        failed = self.run_check()
        self.assertFalse(failed.passed); self.assertEqual(failed.objective_evidence['failure_class'], 'assertion')
        (self.root/'calc.py').write_text('def add(a,b): return a+b\n'); self.commit()
        result = self.run_check()
        self.assertTrue(result.passed); self.assertTrue(result.objective_evidence['snapshot_clean'])
        (self.root/'test_calc.py').write_text('# bypass tests\n'); self.commit()
        changed = self.run_check()
        self.assertFalse(changed.passed); self.assertEqual(changed.objective_evidence['failure_class'], 'infrastructure')

    def test_new_out_of_scope_file_rejected_and_check_replacement_revokes(self):
        (self.root/'app.py').write_text('print(1)\n'); self.commit()
        self.db.save_check('pack', 'unit', [sys.executable, '-c', 'pass'])
        self.register('regression', {'check':'unit', 'allow_changes':['app.py']}, 'app.py')
        self.assertTrue(self.run_check().passed)
        (self.root/'injected.py').write_text('print(2)'); self.commit()
        self.assertFalse(self.run_check().passed)
        self.db.save_check('pack', 'unit', [sys.executable, '--version'])
        self.assertEqual(objective_checks.inventory(self.db, 'pack'), [])

    def test_invalid_data_cannot_supply_commands_or_unbounded_actions(self):
        cases = [('regression', {'check':'unit','allow_changes':['../secret']}),
                 ('regression', {'check':'unit','allow_changes':['app.py'], 'command':'sh'}),
                 ('web_flow', [{'action':'evaluate','selector':'body','value':'fetch("https://x")'}]),
                 ('web_flow', [{'action':'click','selector':'button'}]),
                 ('web_flow', [{'action':'count','selector':'li','value':True}]),
                 ('web_flow', [{'action':'reopen','url':'https://example.com'}]),
                 ('markdown_sections', ['A','A'])]
        for kind, value in cases:
            with self.subTest(kind=kind,value=value), self.assertRaises(ValueError): packs.parse(kind, json.dumps(value))
        steps = [{'action':'fill','selector':'#query','value':'hello'}, {'action':'click','selector':'#go'}, {'action':'text','selector':'#result','value':'hello'}]
        self.assertEqual(packs.parse('web_flow',json.dumps(steps)),steps)

    def test_changed_pack_implementation_invalidates_contract(self):
        self.register('markdown_sections', ['Changes'])
        self.assertIn(str(Path(packs.__file__).resolve()), self.contract['trusted_files'])
        altered = copy.deepcopy(self.contract)
        altered['trusted_files'][str(Path(packs.__file__).resolve())] = '0'*64
        altered.pop('version'); altered['version'] = objective_checks.digest(altered)
        self.assertFalse(objective_checks.valid(altered,self.argv))

    def test_multiple_browser_suites_rejected(self):
        rule = dict(path='index.html',kind='web_flow',value='[{"action":"visible","selector":"h1"}]')
        with self.assertRaises(ValueError): validate(dict(self.spec,objective='Web',assertions=[rule,rule]))
