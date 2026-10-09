import subprocess
import tempfile
import unittest
from pathlib import Path
from timetrace import worktree
from timetrace.pipeline_inputs import prepare_pipeline_inputs

class PipelineInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name)/'repo';self.repo.mkdir();self.home=Path(self.tmp.name)/'agent'
        self.git('init','-q','-b','main');self.git('config','user.name','Test');self.git('config','user.email','test@example.test')
        (self.repo/'common').write_text('base\n');self.git('add','.');self.git('commit','-qm','base');self.base=self.git('rev-parse','HEAD')
    def git(self,*args,cwd=None):
        return subprocess.check_output(['git',*args],cwd=cwd or self.repo,stderr=subprocess.STDOUT,text=True).strip()
    def change(self,branch,file,text):
        self.git('checkout','-qb',branch,self.base);(self.repo/file).write_text(text);self.git('add',file);self.git('commit','-qm',branch)
        sha=self.git('rev-parse','HEAD');self.git('checkout','-q','main');return sha
    def output(self,n=1):return worktree.ensure(str(self.repo),n,self.home)[0]
    def test_sequential_changes_reach_next_step(self):
        sha=self.change('first','feature','first stage');path=self.output()
        head=prepare_pipeline_inputs(str(self.repo),path,[sha]);self.assertEqual(head,sha);self.assertEqual((Path(path)/'feature').read_text(),'first stage')
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)
    def test_parallel_inputs_merge_before_test(self):
        a=self.change('one','one','a');b=self.change('two','two','b');path=self.output()
        head=prepare_pipeline_inputs(str(self.repo),path,[b,a])
        self.assertEqual((Path(path)/'one').read_text(),'a');self.assertEqual((Path(path)/'two').read_text(),'b')
        for sha in [a,b]:self.git('merge-base','--is-ancestor',sha,head,cwd=path)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)
    def test_conflict_stops_without_touching_main(self):
        a=self.change('one','common','one\n');b=self.change('two','common','two\n');path=self.output()
        with self.assertRaises(ValueError):prepare_pipeline_inputs(str(self.repo),path,[a,b])
        self.assertEqual((self.repo/'common').read_text(),'base\n');self.assertEqual(self.git('rev-parse','HEAD'),self.base)
    def test_rejects_main_missing_objects_and_option_refs(self):
        path=self.output()
        for commits in [['--upload-pack=bad'],['f'*40],['HEAD'],['a'*40]*31]:
            with self.assertRaises(ValueError):prepare_pipeline_inputs(str(self.repo),path,commits)
        with self.assertRaises(ValueError):prepare_pipeline_inputs(str(self.repo),str(self.repo),[self.base])
    def test_agent_conflict_does_not_start_ai(self):
        from tests.test_agent import FakeCloud, Adapter
        from timetrace.agent import Agent
        from timetrace.db import Database
        a=self.change('one','common','one\n');b=self.change('two','common','two\n')
        class Cloud(FakeCloud):
            def claim(inner,token):
                claim=super().claim(token);claim['job'].update(stage_iteration=1,input_commit=a,input_commits=[a,b]);return claim
        self.home.mkdir(exist_ok=True)
        db=Database(self.home/'state.sqlite');self.addCleanup(db.close)
        db.upsert_workspace('ws1','repo',str(self.repo),'main')
        cloud,adapter=Cloud(),Adapter()
        Agent(db,cloud,{'codex':adapter},self.home,lambda:'test-token').run_once()
        self.assertFalse(hasattr(adapter,'args'));self.assertEqual(cloud.events[-1]['type'],'failed')
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def _workflow_success(self, edit):
        from tests.test_agent import FakeCloud, Adapter
        from timetrace.agent import Agent
        from timetrace.db import Database
        from timetrace.models import RunResult
        case = self
        class Cloud(FakeCloud):
            def claim(inner, token):
                claim = super().claim(token)
                claim['job'].update(stage_iteration=1, input_commit=case.base, input_commits=[case.base])
                return claim
        class Writer(Adapter):
            def start(inner, prompt, cwd, session_id, log_file, cancel_event=None):
                inner.cwd = cwd
                name = 'common' if edit == 'tracked' else 'implemented-feature.txt'
                (Path(cwd) / name).write_text('implementation\n')
                if edit == 'committed':
                    case.git('add', name, cwd=cwd); case.git('commit', '-qm', 'feature', cwd=cwd)
                    out = Path(cwd) / '.timetrace/out'; out.mkdir(parents=True, exist_ok=True)
                    (out / 'result.json').write_text('{"artifacts":[]}')
                return RunResult(ok=True)
        self.home.mkdir(exist_ok=True)
        db = Database(self.home / 'state.sqlite'); self.addCleanup(db.close)
        db.upsert_workspace('ws1', 'repo', str(self.repo), 'main')
        cloud, adapter = Cloud(), Writer()
        Agent(db, cloud, {'codex': adapter}, self.home, lambda: 'test-token').run_once()
        return cloud.events[-1], adapter.cwd

    def test_workflow_rejects_uncommitted_tracked_implementation(self):
        event, path = self._workflow_success('tracked')
        self.assertEqual(event['type'], 'failed')
        self.assertIn('提交', event['message'])
        self.assertNotIn('output_commit', event)
        self.assertEqual((Path(path) / 'common').read_text(), 'implementation\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)

    def test_workflow_untracked_files_are_named_not_fatal(self):
        # Leftover scratch the model could not delete (a denied `rm`) must not
        # fail a step whose work is committed; the user sees what was left out.
        event, path = self._workflow_success('untracked')
        self.assertEqual(event['type'], 'completed')
        self.assertEqual(event['output_commit'], self.base)
        self.assertIn('implemented-feature.txt', event['message'])
        self.assertIn('未纳入产出', event['message'])
        self.assertTrue((Path(path) / 'implemented-feature.txt').exists())

    def test_workflow_committed_output_allows_only_result_scratch(self):
        event, path = self._workflow_success('committed')
        self.assertEqual(event['type'], 'completed')
        self.assertEqual(event['output_commit'], self.git('rev-parse', 'HEAD', cwd=path))
        self.assertIn('implemented-feature.txt', self.git('ls-tree', '--name-only', event['output_commit'], cwd=path))
