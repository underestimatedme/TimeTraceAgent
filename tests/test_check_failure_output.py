import subprocess
import tempfile
import unittest
from pathlib import Path

from timetrace.db import Database
from timetrace.objective_checks import inventory
from timetrace.web_workspace import apply

# S3-11: a web-registered "contains" check failed with "required text missing"
# and nothing else. Neither the person reading the page nor the AI repairing the
# round could tell which text to add, so two automatic repairs changed nothing.


class CheckFailureOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / 'db')
        self.addCleanup(self.db.close)

    def run_check(self, required, content):
        spec = dict(id='web-test', name='网页项目', path='', create=True, objective='生成 docs/result.md',
                    assertions=[dict(path='docs/result.md', kind='contains', value=required)])
        row = apply(self.home, self.db, spec)
        root = Path(row['path'])
        (root / 'docs').mkdir()
        (root / 'docs/result.md').write_text(content, encoding='utf-8')
        subprocess.check_call(['git', '-C', str(root), 'add', '.'])
        subprocess.check_call(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               'commit', '-q', '-m', 'result'])
        argv = self.db.get_check(row['id'], inventory(self.db, row['id'])[0]['name'])
        return subprocess.run(argv, cwd=root, capture_output=True, text=True)

    def failure_lines(self, done):
        return [line for line in done.stdout.splitlines() if line.startswith('docs/result.md:')]

    def test_the_missing_text_is_named_in_the_failure(self):
        done = self.run_check('## 验收结论', '# 标题\n正文\n')
        self.assertEqual(done.returncode, 1)
        lines = self.failure_lines(done)
        self.assertEqual(len(lines), 1, done.stdout)
        self.assertIn('required text missing', lines[0])
        self.assertIn('## 验收结论', lines[0])

    def test_a_long_or_multiline_text_stays_one_bounded_line(self):
        done = self.run_check('第一行\n' + 'x' * 500 + '\n末行', '无关内容\n')
        self.assertEqual(done.returncode, 1)
        lines = self.failure_lines(done)
        self.assertEqual(len(lines), 1, done.stdout)
        self.assertIn('第一行', lines[0])
        self.assertLessEqual(len(lines[0]), 300)
        self.assertNotIn('x' * 500, done.stdout)
        # Nothing of the failure may spill onto lines of its own.
        self.assertEqual(len(done.stdout.strip().splitlines()), 1, done.stdout)

    def test_a_check_that_finds_its_text_still_passes(self):
        done = self.run_check('验收结论', '# 标题\n验收结论：通过\n')
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn('PASS docs/result.md contains', done.stdout)
