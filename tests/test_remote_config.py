import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from timetrace import config
from timetrace.remote_config import RemoteConfig, ConfigFailure, effective, validate_model

class RemoteConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        config.set_value(self.home,'codex.model','old-model')
        self.cfg=config.load(self.home);self.adapters={}
        self.validator=Mock()
        self.control=RemoteConfig(self.home,self.cfg,self.adapters,self.validator)
        self.agent=SimpleNamespace(max_parallel=4,max_parallel_per_tool={'codex':2,'claude':2},cloud=Mock(),access_token=lambda:'token',maintain=Mock(),log=Mock())
        self.op={'runner_id':'r1','revision':1,'status':'queued','before':effective(self.cfg,'codex'),'desired':{'provider':'codex','model':'new-model','parallel':1,'max_parallel':3}}
        self.agent.cloud.runner_config.return_value={'latest':self.op}
    def test_apply_persists_and_hot_swaps_runtime_and_recovers_after_restart(self):
        self.control.apply(self.agent,self.op)
        self.assertEqual(effective(config.load(self.home),'codex'),self.op['desired'])
        self.assertEqual(self.agent.max_parallel,3);self.assertEqual(self.agent.max_parallel_per_tool['codex'],1)
        self.assertIn('codex',self.adapters);self.assertEqual(self.cfg['codex']['model'],'new-model')
        self.control.apply(self.agent,self.op)
        self.validator.assert_called_once()
        self.assertEqual((self.home/'config.json').stat().st_mode&0o777,0o600)
    def test_busy_does_not_write_until_idle(self):
        self.assertTrue(self.control(self.agent,True));self.validator.assert_not_called()
        self.agent.cloud.ack_runner_config.assert_called_with('token',1,'waiting_idle')
        self.assertEqual(config.load(self.home)['codex']['model'],'old-model')
        self.control.next_poll=0
        self.assertFalse(self.control(self.agent,False));self.agent.maintain.assert_called_once_with(force=True)
        self.agent.cloud.ack_runner_config.assert_called_with('token',1,'applied')
    def test_invalid_model_acknowledges_failure_and_keeps_original(self):
        self.validator.side_effect=ConfigFailure('model_unavailable')
        self.assertFalse(self.control(self.agent))
        self.agent.cloud.ack_runner_config.assert_called_with('token',1,'failed','model_unavailable')
        self.assertEqual(config.load(self.home)['codex']['model'],'old-model')
    def test_local_edit_conflicts_and_does_not_get_overwritten(self):
        config.set_value(self.home,'max_parallel','6')
        with self.assertRaisesRegex(ConfigFailure,'local_conflict'):self.control.apply(self.agent,self.op)
        self.assertEqual(config.load(self.home)['max_parallel'],6)
    def test_lost_ack_retries_without_reapplying_or_revalidating(self):
        self.agent.cloud.ack_runner_config.side_effect=[OSError('network'),None]
        self.assertTrue(self.control(self.agent));self.control.next_poll=0
        self.assertFalse(self.control(self.agent));self.validator.assert_called_once()
        self.assertEqual(self.agent.maintain.call_count,2)
    def test_repairing_new_runner_resets_receipt_scope(self):
        self.op['revision']=10;self.control.apply(self.agent,self.op)
        self.op.update(runner_id='r2',revision=1,before=self.op['desired'].copy())
        self.op['desired']={**self.op['before'],'parallel':2}
        self.control.apply(self.agent,self.op)
        self.assertEqual(config.load_user(self.home)['_remote_configuration']['runner_id'],'r2')
    def test_bounded_payload_rejects_commands_and_invalid_parallel(self):
        for field,value in [('command','echo forbidden'),('parallel',True),('max_parallel',9),('model','a; echo b')]:
            op=copy.deepcopy(self.op);op['desired'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ConfigFailure,'invalid_config'):self.control.apply(self.agent,op)
        self.assertEqual(config.load(self.home)['codex']['model'],'old-model')
    def test_failed_atomic_write_keeps_runtime(self):
        with patch('timetrace.config._write_user',side_effect=OSError()):
            with self.assertRaisesRegex(ConfigFailure,'write_failed'):self.control.apply(self.agent,self.op)
        self.assertEqual(self.cfg['codex']['model'],'old-model');self.assertEqual(self.agent.max_parallel,4)
    def test_codex_catalog_validation_without_inference(self):
        adapter=SimpleNamespace(cfg={'bin':'codex'})
        with patch('timetrace.remote_config.identity',return_value=('account','old')),patch('timetrace.remote_config.read_catalog',return_value={'status':'ready','models':[{'id':'new-model'}]}) as rpc:
            validate_model(adapter,self.op['desired']);rpc.assert_called_once_with(adapter)
            with self.assertRaisesRegex(ConfigFailure,'model_unavailable'):validate_model(adapter,{**self.op['desired'],'model':'missing'})

    def test_catalog_failures_preserve_specific_diagnostics(self):
        adapter=SimpleNamespace(cfg={})
        for status,code in [('login_required','login_required'),('unsupported','unsupported_profile'),('error','model_catalog_unavailable')]:
            with self.subTest(status=status),patch('timetrace.remote_config.identity',return_value=('account','old')),patch('timetrace.remote_config.read_catalog',return_value={'status':status,'models':[]}):
                with self.assertRaisesRegex(ConfigFailure,code):validate_model(adapter,self.op['desired'])

    def test_real_worker_loop_drains_before_apply_and_can_grow_from_one_slot(self):
        import threading
        from timetrace.agent import Agent
        from timetrace.db import Database
        db=Database(self.home/'worker.db')
        self.addCleanup(db.close)
        release=threading.Event();finished=threading.Event();states=[];claims=[]
        cloud=Mock();cloud.runner_config.return_value={'latest':None}
        agent=Agent(db,cloud,self.adapters,self.home,lambda:'token',max_parallel=1)
        agent.maintain=Mock()
        def handle(claim):
            self.assertTrue(release.wait(2),'configuration interrupted or deadlocked worker')
            finished.set()
            return 'completed'
        agent.handle=handle
        def claim():
            claims.append(1)
            return {'job':{'id':'running-job'}} if len(claims)==1 else 'idle'
        agent._next_claim=claim
        def remote(a,busy):
            self.control.next_poll=0
            if busy:
                cloud.runner_config.return_value={'latest':self.op}
            holding=self.control(a,busy)
            if busy:
                states.append('waiting')
                self.assertEqual(config.load(self.home)['codex']['model'],'old-model')
                release.set()
            elif states:
                self.assertTrue(finished.is_set())
                self.assertEqual(a.max_parallel,3)
                states.append('applied');a.stop()
            return holding
        agent.remote_config=remote
        timer=threading.Timer(3,agent.stop);timer.start()
        try:agent.run_forever(interval=.01,log=lambda line:None)
        finally:release.set();timer.cancel()
        self.assertEqual(states[-1],'applied')
        self.assertEqual(self.validator.call_count,1)
