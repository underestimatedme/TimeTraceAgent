"""Boundary fixtures are protocol examples; actual Mac/iPhone acceptance is separate."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from timetrace import cli
from timetrace.cloud import CloudClient, CloudError

class PhonePairingTest(unittest.TestCase):
    def test_phone_digits_are_entered_on_computer_and_retried_unchanged(self):
        seen=[]
        class Cloud:
            def poll_device_authorization(self, code):
                return {"status":"waiting_phone", "expires_at":"2026-10-08T01:10:00Z", "server_now":"2026-10-08T01:05:00Z", "attempts_remaining":3}
            def activate_phone(self, code, digits):
                seen.append(digits)
                if len(seen)==1: raise CloudError("request failed",status=503)
                return {"access_token":"qa-access","refresh_token":"qa-refresh","expires_in":900,"runner":{"id":"qa-runner","name":"样例电脑"}}
            def update_inventory(self,*a,**kw): return {}
        saved=[];questions=[];clock=[1000.0]
        def ask(prompt): questions.append(prompt);return "0000"
        with tempfile.TemporaryDirectory() as d, patch('builtins.input',side_effect=ask), \
             patch('timetrace.cli.CredentialStore.save',new=lambda self,value:saved.append(value)), \
             patch('timetrace.cli._adapters',return_value={}), \
             patch('timetrace.cli._runner_workspaces',return_value=[]), \
             patch('timetrace.cli._runner_tools',return_value=[]), \
             patch('timetrace.cli.Agent.report_quota',return_value=None), \
             patch('timetrace.cli.time.time',side_effect=lambda:clock[0]), \
             patch('timetrace.cli.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            result=cli._await_pairing(Cloud(),{"device_code":"qa-device","expires_in":300,"interval":1,"pairing_version":2},{},None,Path(d),lambda *a:None)
        self.assertEqual(result,0)
        self.assertEqual(seen,['0000','0000'])
        self.assertEqual(len(questions),1)
        self.assertEqual(len(saved),1)

    def test_failure_to_revoke_retains_persisted_credentials(self):
        deleted=[];out=io.StringIO()
        cloud=CloudClient('http://127.0.0.1:5217/timetrace/api/v1')
        with patch('timetrace.cli._open',return_value=(None,{},None)), \
             patch('timetrace.cli._cloud',return_value=cloud), \
             patch('timetrace.cli.CredentialStore.load',return_value={'refresh_token':'qa-refresh'}), \
             patch('timetrace.cli.CredentialStore.delete',new=lambda self:deleted.append(True)), \
             patch('timetrace.cli.SessionManager.token',return_value='qa-access'), \
             patch('timetrace.cloud.CloudClient.request',side_effect=CloudError('request failed',status=503)), \
             redirect_stdout(out):
            result=cli.cmd_cloud_logout(None)
        self.assertEqual(result,1)
        self.assertEqual(deleted,[])
        self.assertNotIn('已在刻迹账号中解绑',out.getvalue())

    def test_client_new_pairing_paths_and_phone_code_stays_string(self):
        calls=[]
        def request(client,method,path,body=None,token=None): calls.append((method,path,body));return {}
        with patch('timetrace.cloud.CloudClient.request',new=request):
            client=CloudClient('http://127.0.0.1:5217/timetrace/api/v1')
            client.create_phone_authorization('Mac','darwin','i09-qa','old-secret')
            client.activate_phone('secret','0000')
            client.revoke_phone('refresh-secret')
        self.assertEqual(calls,[('POST','/iphone/device-authorizations',{'device_name':'Mac','platform':'darwin','client_version':'i09-qa','previous_device_code':'old-secret'}),('POST','/iphone/device-authorizations/activate',{'device_code':'secret','phone_code':'0000'}),('POST','/iphone/runner/revoke',{'refresh_token':'refresh-secret'})])

    def test_long_computer_display_name_does_not_break_reverse_pair_qr(self):
        from urllib.parse import quote
        name='电脑名称很长' * 10
        link='timetrace://pair?code=ABCD2345&name='+quote(name)+'&platform=darwin&exp=1790000600&v=2'
        class Cloud:
            def create_phone_authorization(self,*a):
                return {'device_code':'qa-device-secret','user_code':'ABCD2345','verification_uri':link,'expires_in':300,'pairing_version':2}
            def poll_device_authorization(self,*a): return {'status':'expired'}
        out=[]
        with patch('timetrace.cli._cloud',return_value=Cloud()):
            result=cli.pair_computer({},None,None,out=lambda *a:out.append(' '.join(map(str,a))),max_rounds=1)
        self.assertEqual(result,1)
        self.assertIn('v=2','\n'.join(out))
        self.assertNotIn('qa-device-secret','\n'.join(out))
