import hashlib
import tempfile
import unittest
from pathlib import Path

from timetrace.workspace_context import collect_workspace_context


class WorkspaceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()

    def collect(self):
        return collect_workspace_context(str(self.root), '2026-10-02T10:00:00Z')

    def test_empty_and_metadata_only(self):
        self.assertEqual(self.collect()['content_state'], 'empty')
        for name in ('.git', 'timetrace-out'):
            (self.root / name).mkdir()
        (self.root / '.DS_Store').write_bytes(b'metadata')
        context = self.collect()
        self.assertEqual(context['content_state'], 'empty')
        self.assertEqual(context['checked_at'], '2026-10-02T10:00:00Z')
        self.assertEqual(context['readme']['status'], 'missing')
        (self.root / 'src').mkdir()
        self.assertEqual(self.collect()['content_state'], 'populated')

    def test_readme_priority_and_description(self):
        (self.root / 'README').write_text('other description', encoding='utf-8')
        source = '# 刻迹\n\n管理 **人和 AI** 的工作。\n\n支持 [项目](https://example.test)。\n'
        (self.root / 'readme.MD').write_text(source, encoding='utf-8')
        context = self.collect()
        self.assertEqual(context['content_state'], 'populated')
        self.assertEqual(context['description'], '管理 人和 AI 的工作。 支持 项目。')
        readme = context['readme']
        self.assertEqual(readme['filename'], 'readme.MD')
        self.assertEqual(readme['content'], source)
        self.assertEqual(readme['sha256'], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(readme['status'], 'ready')
        self.assertFalse(readme['truncated'])

    def test_unicode_byte_boundary(self):
        (self.root / 'README.md').write_text('# 标题\n\n' + '中文🙂' * 9000, encoding='utf-8')
        context = self.collect()
        readme = context['readme']
        self.assertEqual(readme['status'], 'ready')
        self.assertTrue(readme['truncated'])
        self.assertLessEqual(len(readme['content'].encode('utf-8')), 32768)
        self.assertGreater(len(readme['content'].encode('utf-8')), 32760)
        self.assertLessEqual(len(context['description']), 500)

    def test_external_symlink_and_decode_failure(self):
        external = Path(self.temp.name) / 'private.txt'
        external.write_text('must not leak', encoding='utf-8')
        link = self.root / 'README.md'
        link.symlink_to(external)
        context = self.collect()
        self.assertEqual(context['readme']['status'], 'unreadable')
        self.assertEqual(context['readme']['content'], '')
        link.unlink()
        link.write_bytes(b'# title\n\xff\xfeprivate')
        self.assertEqual(self.collect()['readme']['status'], 'invalid_encoding')
        self.assertEqual(self.collect()['description'], '')

    def test_missing_directory_and_readme_folder_are_unknown_and_unreadable(self):
        (self.root / 'README.md').mkdir()
        self.assertEqual(self.collect()['readme']['status'], 'unreadable')
        self.assertEqual(collect_workspace_context(str(self.root / 'missing'))['content_state'], 'unknown')

    def test_nested_readme_not_read_and_case_tie_is_stable(self):
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'README.md').write_text('nested', encoding='utf-8')
        self.assertEqual(self.collect()['readme']['status'], 'missing')
        (self.root / 'README.txt').write_text('root', encoding='utf-8')
        self.assertEqual(self.collect()['readme']['filename'], 'README.txt')

    def test_inventory_includes_context_without_path(self):
        from timetrace.cli import _runner_workspaces
        from timetrace.db import Database
        db = Database(Path(self.temp.name) / 'state.sqlite')
        self.addCleanup(db.close)
        db.upsert_workspace('empty', 'New App', str(self.root), '', kind='folder')
        rows = _runner_workspaces(db)
        self.assertEqual(rows[0]['context']['content_state'], 'empty')
        self.assertNotIn('path', rows[0])
        self.assertEqual(rows[0]['id'], 'empty')
