import unittest
from timetrace.routing import identity, problem

class Adapter:
    name = 'codex'
    cfg = {'model': 'explicit-test-model'}
    def routing_account_key(self): return 'a'*64

class RoutingTests(unittest.TestCase):
    def test_exact_model_and_account_are_required(self):
        a=Adapter()
        j={'routing_required':True,'routing_account':'account-codex-'+'a'*64,'routing_model':'explicit-test-model','tool_profile_id':'codex-default'}
        self.assertEqual(problem(a,j),'')
        self.assertEqual(problem(a,dict(j,routing_model='other')),'routing_model_changed')
        self.assertEqual(problem(a,dict(j,routing_account='other')),'routing_account_changed')
        self.assertEqual(problem(a,dict(j,tool_profile_id='other')),'routing_profile_changed')
        self.assertEqual(problem(a,{}),'')
    def test_default_or_unknown_identity_cannot_satisfy_policy(self):
        a=Adapter(); a.cfg={}
        self.assertEqual(identity(a),('account-codex-'+'a'*64,''))
        a.routing_account_key=lambda:None
        self.assertEqual(identity(a),('',''))

    def test_extra_arguments_cannot_override_reported_model(self):
        a = Adapter()
        for args in (["--model", "other"], ["--fallback-model=other"], ["-c", "model=other"]):
            a.cfg = {"model": "explicit-test-model", "extra_args": args}
            self.assertEqual(identity(a), ("account-codex-" + "a" * 64, ""))
