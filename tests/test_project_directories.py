"""Registered project containers publish bounded children and never execute at the root."""
import importlib
import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, worktree
from timetrace.agent import Agent
from timetrace.cloud import CloudClient
from timetrace.db import Database
from timetrace.dispatch import LockBusy, workspace_lock
from timetrace.models import RunResult
from tests.test_parallel import QueueCloud, init_repo, git
from tests.test_folder_workspace import FolderAdapter

try:
    directories = importlib.import_module('timetrace.project_directories')
except ModuleNotFoundError:
    directories = None


class RootFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'apps'
        self.root.mkdir()
        self.db = Database(self.base / 'db.sqlite')
        self.addCleanup(self.db.close)

    def register(self):
        return cli.add_workspace(self.db, str(self.root), workspace_id='root', kind='project_root')

    def module(self):
        self.assertIsNotNone(directories, 'project directory support is missing')
        return directories



class DirectoryTest(RootFixture):
    def test_multiple_registered_roots_preserve_kind_and_publish_only_children(self):
        init_repo(self.root / 'sleep')
        other = self.base / 'videos'; other.mkdir()
        (other / '旅行 视频').mkdir()
        self.register()
        cli.add_workspace(self.db, str(other), workspace_id='videos', kind='project_root')
        rows = cli._runner_workspaces(self.db)
        self.assertEqual({r['id'] for r in rows}, {'root', 'videos'})
        self.assertEqual({r['kind'] for r in rows}, {'project_root'})
        self.assertNotIn(str(self.base), json.dumps(rows))
        self.assertTrue(all(r['enabled'] for r in rows))
        self.assertEqual(next(r for r in rows if r['id'] == 'videos')['directory_inventory']['entries'][0]['directory_name'], '旅行 视频')
        self.assertTrue(all('context' not in r for r in rows))

    def test_inventory_excludes_links_hidden_builds_and_worktrees(self):
        m = self.module()
        repo = self.root / 'sleep'; init_repo(repo)
        (repo / 'README.md').write_text('# Sleep\n睡眠计划')
        (self.root / '旅行 视频').mkdir()
        for name in ('.hidden', 'node_modules', 'build', 'timetrace-out'):
            (self.root / name).mkdir()
        (self.root / 'linked').symlink_to(repo, target_is_directory=True)
        subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(self.root / 'tree'), 'HEAD'], cwd=repo, check=True)
        inventory = m.scan(str(self.root))
        self.assertEqual(inventory['status'], 'ok')
        self.assertEqual(inventory['excluded_count'], 6)
        self.assertEqual([(e['directory_name'], e['kind']) for e in inventory['entries']], [('sleep', 'git'), ('旅行 视频', 'folder')])
        self.assertEqual(inventory['entries'][0]['description'], '睡眠计划')
        self.assertEqual(inventory['entries'][0]['readme']['status'], 'ok')
        self.assertEqual(inventory['entries'][1]['readme'], {'status': 'absent'})
        self.assertEqual(len(inventory['fingerprint']), 64)
        self.assertNotIn(str(self.base), json.dumps(inventory))

    def test_inventory_candidate_cap_marks_incomplete(self):
        m = self.module()
        for i in range(501): (self.root / ('p%03d' % i)).mkdir()
        inventory = m.scan(str(self.root))
        self.assertEqual(len(inventory['entries']), 500)
        self.assertTrue(inventory['truncated'])
        self.assertEqual(inventory['excluded_count'], 0)

    def test_readme_byte_and_aggregate_limits_and_no_follow(self):
        m = self.module()
        for i in range(34):
            p = self.root / ('p%03d' % i); p.mkdir()
            (p / 'README.md').write_text('中' * 2000)
        linked = self.root / 'zlink'; linked.mkdir()
        (linked / 'README.md').symlink_to(self.root / 'p000' / 'README.md')
        entries = m.scan(str(self.root))['entries']
        contents = [e['readme'].get('content', '').encode() for e in entries]
        self.assertLessEqual(sum(map(len, contents)), 65536)
        self.assertTrue(all(len(c) <= 2048 for c in contents))
        self.assertTrue(entries[0]['readme']['truncated'])
        self.assertNotIn('content', entries[-1]['readme'])
        self.assertNotEqual(entries[-1]['readme']['status'], 'ok')

    def test_nested_repository_folder_is_excluded_from_inventory(self):
        m = self.module()
        container = self.root / 'unsafe'; container.mkdir()
        init_repo(container / 'repo')
        inventory = m.scan(str(self.root))
        self.assertEqual(inventory['entries'], [])
        self.assertEqual(inventory['excluded_count'], 1)
        self.register()
        with self.assertRaises(ValueError): m.resolve(self.db.get_workspace('root'), 'unsafe')

    def test_prepare_revalidates_missing_readonly_child_without_creation(self):
        m = self.module(); self.register()
        child = self.root / 'plain'; child.mkdir()
        workspace = m.resolve(self.db.get_workspace('root'), 'plain')
        child.rmdir()
        with self.assertRaises(ValueError): m.prepare(workspace)
        self.assertFalse(child.exists())

    def test_concurrent_child_preparation_initializes_once_and_retains_identity(self):
        m = self.module(); self.register()
        workspace = m.resolve(self.db.get_workspace('root'), 'new', allow_missing=True)
        errors = []
        def prepare():
            try:
                with workspace_lock(self.base / 'home', workspace['path']):
                    m.prepare(dict(workspace))
            except LockBusy:
                pass
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=prepare) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(git('rev-list', '--count', 'HEAD', cwd=self.root / 'new'), '1')

    def test_readme_invalid_encoding_is_unreadable_and_secrets_stay_local(self):
        m = self.module()
        bad = self.root / 'bad'; bad.mkdir(); (bad / 'README.md').write_bytes(b'bad\xffbytes')
        good = self.root / 'good'; good.mkdir()
        secret = 'sk-proj-' + 'a' * 30
        (good / 'README.md').write_text('Secret: ' + secret + '\nLocation: ' + str(self.root) + '\nElsewhere: /opt/private-project/assets \\server\\private')
        inventory = m.scan(str(self.root))
        self.assertEqual(inventory['entries'][0]['readme']['status'], 'unreadable')
        raw = json.dumps(inventory)
        self.assertNotIn(secret, raw)
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn('/opt/private-project/assets', raw)
        self.assertNotIn('server', raw)

    def test_git_environment_cannot_redirect_new_child_initialization(self):
        m = self.module(); self.register()
        external = self.base / 'external'; external.mkdir()
        workspace = m.resolve(self.db.get_workspace('root'), 'new', allow_missing=True)
        with patch.dict(os.environ, {'GIT_DIR': str(external / '.git'), 'GIT_WORK_TREE': str(external)}):
            with workspace_lock(self.base / 'home', workspace['path']): m.prepare(workspace)
        self.assertTrue((self.root / 'new' / '.git').is_dir())
        self.assertEqual(list(external.iterdir()), [])

    def test_disabled_inventory_on_removed_root_and_stable_fingerprint(self):
        m = self.module(); self.register(); (self.root / 'one').mkdir()
        self.assertEqual(m.scan(str(self.root))['fingerprint'], m.scan(str(self.root))['fingerprint'])
        (self.root / 'one').rmdir(); self.root.rmdir()
        inventory = cli._runner_workspaces(self.db)[0]
        self.assertFalse(inventory['enabled'])
        self.assertEqual(inventory['directory_inventory']['status'], 'unreadable')

    def test_git_metadata_pointing_at_parent_is_not_an_independent_child(self):
        m = self.module(); self.register(); child = self.root / 'misdirected'; init_repo(child)
        git('config', 'core.worktree', str(self.root), cwd=child)
        with self.assertRaises(ValueError): m.resolve(self.db.get_workspace('root'), 'misdirected')
        inventory = m.scan(str(self.root))
        self.assertEqual(inventory['entries'], [])
        self.assertEqual(inventory['excluded_count'], 1)

    def test_plain_child_inside_ancestor_git_is_still_folder(self):
        m = self.module()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.root, check=True)
        self.register(); child = self.root / 'plain'; child.mkdir()
        resolved = m.resolve(self.db.get_workspace('root'), 'plain')
        self.assertEqual(resolved['kind'], 'folder')
        self.assertEqual(m.scan(str(self.root))['entries'][0]['kind'], 'folder')

    def test_failed_root_scan_cannot_publish_partial_candidates(self):
        m = self.module()
        inventory = m.scan(str(self.root / 'missing'))
        self.assertEqual(inventory['status'], 'unreadable')
        self.assertEqual(inventory['entries'], [])

    def test_safe_unicode_basename_and_cross_platform_alias_rejection(self):
        m = self.module()
        for name in ('旅行 视频', 'sleepearlier', 'é' * 127, 'a' * 128, '👨‍👩‍👧', 'x\ue000'):
            self.assertEqual(m.validate_name(name), name)
        for name in ('', '.', '..', '.hidden', 'a/b', 'a\\b', 'a\x00b', 'a\x7fb', 'a\nb', 'CON', 'con.txt', 'LPT1', 'COM².txt', 'x:', 'x?', 'x*', 'x|', 'x<', 'x>', 'x"', 'x.', 'x ', 'a' * 129, '中' * 86, None, {}):
            with self.subTest(name=name), self.assertRaises(ValueError): m.validate_name(name)

    def test_console_device_names_are_excluded_without_poisoning_inventory(self):
        m = self.module()
        unsafe = ('CONIN$', 'CONOUT$', 'conin$.txt', 'cOnOuT$.video')
        for name in unsafe:
            (self.root / name).mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError):
                m.validate_name(name)
        (self.root / 'safe').mkdir()
        inventory = m.scan(str(self.root))
        self.assertEqual(inventory['status'], 'ok')
        self.assertEqual(inventory['excluded_count'], 4)
        self.assertEqual([e['directory_name'] for e in inventory['entries']], ['safe'])

    def test_resolver_reuses_git_folder_and_never_overwrites_root(self):
        m = self.module(); self.register()
        init_repo(self.root / 'sleep')
        (self.root / '旅行 视频').mkdir()
        root = self.db.get_workspace('root')
        repo = m.resolve(root, 'sleep')
        plain = m.resolve(root, '旅行 视频')
        self.assertEqual((repo['path'], repo['kind'], repo['default_branch']), (str(self.root / 'sleep'), 'git', 'main'))
        self.assertEqual((plain['path'], plain['kind']), (str(self.root / '旅行 视频'), 'folder'))
        self.assertEqual(self.db.get_workspace('root')['path'], str(self.root))
        self.assertFalse((self.root / '旅行 视频' / '.git').exists())

    def test_resolver_rejects_link_worktree_and_missing_readonly_child(self):
        m = self.module(); self.register(); init_repo(self.root / 'repo')
        (self.root / 'link').symlink_to(self.root / 'repo', target_is_directory=True)
        subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(self.root / 'tree'), 'HEAD'], cwd=self.root / 'repo', check=True)
        for name in ('link', 'tree', '../outside', 'missing'):
            with self.subTest(name=name), self.assertRaises(ValueError): m.resolve(self.db.get_workspace('root'), name)
        self.assertFalse((self.root / 'missing').exists())
        with self.assertRaises(ValueError): m.resolve(dict(self.db.get_workspace('root'), kind='folder'), 'repo')


class GitAdapter(FolderAdapter):
    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append(('git', cwd))
        (Path(cwd) / 'done.txt').write_text('done')
        return RunResult(exit_code=0, ok=True, output='done', session_id=session_id)


class ChildExecutionTest(RootFixture):
    def run_job(self, job, adapter=None, **kw):
        cloud = QueueCloud([dict({'id': 'j1', 'plan_id': 'p1', 'workspace_id': 'root'}, **job)])
        adapter = adapter or GitAdapter()
        agent = Agent(self.db, cloud, {'codex': adapter}, self.base / 'home', lambda: 't', **kw)
        return agent.run_once(), cloud, adapter

    def test_missing_child_created_under_lock_and_provider_uses_own_worktree(self):
        self.register()
        home = self.base / 'home'
        observed = []
        original = worktree.ensure
        def prepare(path, *args, **kw):
            observed.append(path)
            with self.assertRaises(LockBusy): workspace_lock(home, path).acquire()
            self.assertTrue((Path(path) / '.git').is_dir())
            return original(path, *args, **kw)
        result, cloud, adapter = self.run_job({'project_directory': '新项目'}, prepare_workspace=prepare)
        self.assertEqual(result, 'job j1 → awaiting_review')
        child = self.root / '新项目'
        self.assertEqual(observed, [str(child)])
        self.assertEqual(git('rev-parse', '--show-toplevel', cwd=adapter.calls[0][1]), adapter.calls[0][1])
        self.assertTrue(worktree.same_repository(adapter.calls[0][1], str(child)))
        self.assertFalse((self.root / '.git').exists())
        self.assertEqual(self.db.get_workspace('root')['kind'], 'project_root')
        self.assertEqual(git('log', '--format=%s', 'main', cwd=child), 'Initialize project directory')

    def test_existing_folder_executes_only_in_child_output(self):
        self.register(); child = self.root / '旅行 视频'; child.mkdir()
        (child / 'raw.mov').write_bytes(b'raw')
        result, cloud, adapter = self.run_job({'project_directory': '旅行 视频', 'output_name': 'cut'}, FolderAdapter())
        self.assertEqual(result, 'job j1 → awaiting_review')
        self.assertEqual(adapter.calls, [('start', str(child / 'timetrace-out' / 'cut'), str(child))])
        self.assertEqual((child / 'raw.mov').read_bytes(), b'raw')
        self.assertFalse((self.root / 'timetrace-out').exists())

    def test_root_without_directory_and_nonroot_with_directory_fail_closed(self):
        self.register()
        result, cloud, adapter = self.run_job({})
        self.assertIn('rejected', result); self.assertEqual(adapter.calls, [])
        self.db.upsert_workspace('root', 'plain', str(self.root), '', kind='folder')
        result, _, adapter = self.run_job({'id': 'j2', 'project_directory': 'child'})
        self.assertIn('rejected', result); self.assertEqual(adapter.calls, [])
        self.assertFalse((self.root / 'child').exists())

    def test_readonly_kinds_and_resume_never_create_missing_child(self):
        self.register()
        for i, job in enumerate(({'kind': 'chat_turn'}, {'kind': 'review_turn'}, {'kind': 'check'}, {'resume_of_job_id': 'old'})):
            result, cloud, adapter = self.run_job(dict(job, id='j%d' % i, project_directory='absent'))
            self.assertIn('rejected', result); self.assertEqual(adapter.calls, [])
            self.assertFalse((self.root / 'absent').exists())

    def test_chat_reads_existing_child_not_registered_parent(self):
        self.register(); (self.root / '旅行 视频').mkdir()
        result, cloud, adapter = self.run_job({'project_directory': '旅行 视频', 'kind': 'chat_turn', 'chat_id': 'chat', 'session_id': 's'})
        self.assertEqual(adapter.calls[0], ('chat', str(self.root / '旅行 视频')))

    def test_busy_child_lock_prevents_creation(self):
        self.register()
        with workspace_lock(self.base / 'home', str(self.root / 'new')):
            result, cloud, adapter = self.run_job({'project_directory': 'new'})
        self.assertIn('workspace busy', result)
        self.assertFalse((self.root / 'new').exists())
        self.assertEqual(adapter.calls, [])

    def test_folder_checkpoint_resumes_at_child_output_directory(self):
        self.register(); child = self.root / '旅行 视频'; child.mkdir()
        adapter = FolderAdapter(result=RunResult(exit_code=1, blocked=True, session_id='native', error='quota'))
        result, _, _ = self.run_job({'project_directory': '旅行 视频'}, adapter)
        self.assertEqual(result, 'job j1 → waiting_quota')
        checkpoint = self.db.get_checkpoint('p1')
        self.assertEqual(checkpoint.canonical_workspace, str(child))
        result, _, _ = self.run_job({'id': 'j2', 'project_directory': '旅行 视频', 'resume_of_job_id': 'j1'}, adapter)
        self.assertEqual(result, 'job j2 → awaiting_review')
        self.assertEqual(adapter.calls[-1], ('resume', str(child / 'timetrace-out' / 'p1'), str(child)))

    def test_review_and_check_use_child_output_and_parent_registered_command(self):
        from tests.test_review_and_check import Reviewer
        self.register(); child = self.root / '旅行 视频'; child.mkdir()
        out = child / 'timetrace-out' / 'cut'; out.mkdir(parents=True)
        (out / 'cut.txt').write_text('done')
        reviewer = Reviewer()
        result, _, _ = self.run_job({'kind': 'review_turn', 'project_directory': '旅行 视频', 'output_name': 'cut'}, reviewer)
        self.assertEqual(result, 'job j1 → reviewed (pass)')
        self.assertEqual(reviewer.calls[0]['cwd'], str(out))
        self.db.save_check('root', 'output', [sys.executable, '-c', "from pathlib import Path; assert Path('cut.txt').read_text() == 'done'"])
        result, _, _ = self.run_job({'id': 'j2', 'kind': 'check', 'project_directory': '旅行 视频', 'output_name': 'cut', 'check_name': 'output'})
        self.assertEqual(result, 'job j2 → check passed')
        self.assertEqual((out / 'cut.txt').read_text(), 'done')

    def test_actual_job_rejects_symlink_worktree_and_nested_repository(self):
        self.register(); init_repo(self.root / 'repo')
        (self.root / 'link').symlink_to(self.root / 'repo', target_is_directory=True)
        subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(self.root / 'tree'), 'HEAD'], cwd=self.root / 'repo', check=True)
        nested = self.root / 'nested'; nested.mkdir(); init_repo(nested / 'repo')
        for i, name in enumerate(('link', 'tree', 'nested', '../escape', {'name': 'repo'})):
            result, _, adapter = self.run_job({'id': 'unsafe%d' % i, 'project_directory': name})
            self.assertIn('rejected', result)
            self.assertEqual(adapter.calls, [])

    def test_existing_git_child_review_and_check_use_detached_child_commit(self):
        from tests.test_review_and_check import Reviewer
        self.register(); child = self.root / 'sleep'; init_repo(child)
        subprocess.run(['git', 'checkout', '-qb', 'timetrace/step'], cwd=child, check=True)
        (child / 'done.txt').write_text('child done')
        subprocess.run(['git', 'add', 'done.txt'], cwd=child, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'task'], cwd=child, check=True)
        subprocess.run(['git', 'checkout', '-q', 'main'], cwd=child, check=True)
        reviewer = Reviewer()
        result, _, _ = self.run_job({'kind': 'review_turn', 'project_directory': 'sleep', 'branch_name': 'timetrace/step'}, reviewer)
        self.assertEqual(result, 'job j1 → reviewed (pass)')
        self.assertIn('done.txt', reviewer.calls[0]['files'])
        self.assertFalse(Path(reviewer.calls[0]['cwd']).exists())
        self.db.save_check('root', 'output', [sys.executable, '-c', "from pathlib import Path; assert Path('done.txt').read_text() == 'child done'"])
        result, _, _ = self.run_job({'id': 'j2', 'kind': 'check', 'project_directory': 'sleep', 'branch_name': 'timetrace/step', 'check_name': 'output'})
        self.assertEqual(result, 'job j2 → check passed')
        self.assertEqual(git('status', '--porcelain', cwd=child), '')
        self.assertEqual(git('worktree', 'list', '--porcelain', cwd=child).count('worktree '), 1)
        self.assertFalse((self.root / '.git').exists())

    def test_recovery_marker_discovered_after_lock_blocks_before_child_creation(self):
        self.register()
        original = Agent._acquire_workspace
        def acquire(agent, path, deadline):
            lock = original(agent, path, deadline)
            self.db.mark_plan_started('p1', 'old-job', 'old-attempt')
            return lock
        with patch.object(Agent, '_acquire_workspace', acquire):
            result, _, adapter = self.run_job({'project_directory': 'new'})
        self.assertIn('checkpoint missing', result)
        self.assertFalse((self.root / 'new').exists())
        self.assertEqual(adapter.calls, [])

    def test_capability_in_inventory_request(self):
        sent = []
        client = CloudClient('https://example.invalid')
        client.request = lambda method, path, body, token: sent.append(body)
        client.update_inventory('t', [], [])
        self.assertEqual(sent[0].get('project_directories_version'), 1)
