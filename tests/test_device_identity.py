import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json
import io
from timetrace import cli, credentials

class DeviceIdentityTest(unittest.TestCase):
    def test_cli_pairing_carries_stable_identity_to_the_real_http_boundary(self):
        requests=[]
        class Reply:
            status=201
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,*args): return b'{"code":0,"data":{"device_code":"test-code","user_code":"ABCDEFGH","verification_uri":"timetrace://pair?code=ABCDEFGH&v=2","expires_in":300}}'
        def opener(req,**kw): requests.append(json.loads(req.data));return Reply()
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'TIMETRACE_HOME':d}), patch('timetrace.cloud.no_redirect_opener',return_value=opener), patch('timetrace.cli._await_pairing',return_value=0),patch('timetrace.cli.CredentialStore.load',return_value=None):
            cfg={'cloud_base_url':'http://127.0.0.1:9999/timetrace/api/v1'}
            with patch('timetrace.cli.platform.node',return_value='Mac'):
                self.assertEqual(cli.pair_computer(cfg,None,Path(d),out=lambda *a:None),0)
            with patch('timetrace.cli.platform.node',return_value='Renamed Mac'):
                self.assertEqual(cli.pair_computer(cfg,None,Path(d),out=lambda *a:None),0)
        self.assertIn('device_id',requests[0])
        self.assertRegex(requests[0]['device_id'],r'^[0-9a-f]{64}$')
        self.assertEqual(requests[0]['device_id'],requests[1]['device_id'])

    def test_authenticated_inventory_also_carries_the_stable_identity(self):
        from timetrace.cloud import CloudClient
        payloads=[]
        class Reply(io.BytesIO):
            status=200
        def opener(req,**kw): payloads.append(json.loads(req.data));return Reply(b'{"code":0,"data":{"updated":true}}')
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'TIMETRACE_HOME':d}),patch('timetrace.cloud.no_redirect_opener',return_value=opener):
            cloud=cli._cloud({'cloud_base_url':'http://localhost:9999'})
            cloud.update_inventory('test-access',[],[])
            cloud.create_phone_authorization('Mac','darwin','0.5.1')
        self.assertIn('device_id',payloads[0])
        self.assertEqual(payloads[0]['device_id'],payloads[1]['device_id'])

    def test_identity_survives_unavailable_hardware_and_is_owner_only(self):
        import os
        with tempfile.TemporaryDirectory() as d, patch('timetrace.credentials._hardware_identity',return_value=None):
            home=Path(d);first=credentials.device_identity(home)
            self.assertEqual(credentials.device_identity(home),first)
            self.assertEqual(os.stat(home/'device-id').st_mode & 0o777,0o600)
            self.assertEqual(os.stat(home/'device-id.lock').st_mode & 0o777,0o600)
        self.assertRegex(first,r'^[0-9a-f]{64}$')

    def test_hardware_identity_distinguishes_same_named_computers_and_survives_reinstall(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b,tempfile.TemporaryDirectory() as c:
            with patch('timetrace.credentials._hardware_identity',return_value='00112233-4455-6677-8899-aabbccddeeff'):
                first=credentials.device_identity(Path(a));reinstalled=credentials.device_identity(Path(b))
            with patch('timetrace.credentials._hardware_identity',return_value='10112233-4455-6677-8899-aabbccddeeff'):
                different=credentials.device_identity(Path(c))
            self.assertEqual(first,reinstalled)
            self.assertNotEqual(first,different)
            self.assertNotIn('00112233',first)
            with patch('timetrace.credentials._hardware_identity',return_value=None):
                self.assertEqual(credentials.device_identity(Path(a)),first)

    def test_concurrent_initialization_has_one_persistent_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as d,patch('timetrace.credentials._hardware_identity',return_value=None),ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:credentials.device_identity(Path(d)),range(16)))
            self.assertEqual(len(set(values)),1)
            self.assertEqual((Path(d)/'device-id').read_text().strip(),values[0])

    def test_real_ioreg_plist_is_parsed_and_raw_hardware_id_is_not_uploaded(self):
        import plistlib
        import subprocess
        fixture=plistlib.dumps([{'IOPlatformUUID':'00112233-4455-6677-8899-AABBCCDDEEFF'}])
        with patch('timetrace.credentials.subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=fixture)):
            self.assertEqual(credentials._hardware_identity(),'00112233-4455-6677-8899-aabbccddeeff')

    def test_corrupt_persistent_identity_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'device-id';path.write_text('corrupted')
            with self.assertRaises(RuntimeError): credentials.device_identity(Path(d))
            self.assertEqual(path.read_text(),'corrupted')

    def test_upgrade_pairing_carries_old_runner_hint_without_refreshing_secrets(self):
        payloads=[]
        class Reply(io.BytesIO):
            status=201
        def opener(req,**kw):
            payloads.append(json.loads(req.data))
            return Reply(b'{"code":0,"data":{"device_code":"test-code","user_code":"ABCDEFGH","verification_uri":"timetrace://pair?code=ABCDEFGH&v=2","expires_in":300}}')
        existing={'runner':{'id':'legacy-runner','name':'Mac'},'refresh_token':'private-old-credential'}
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'TIMETRACE_HOME':d}),patch('timetrace.cloud.no_redirect_opener',return_value=opener),patch('timetrace.cli.CredentialStore.load',return_value=existing),patch('timetrace.cli._await_pairing',return_value=0):
            cli.pair_computer({'cloud_base_url':'http://localhost:9999'},None,Path(d),out=lambda *a:None)
        self.assertEqual(payloads[0].get('previous_runner_id'),'legacy-runner')
        self.assertNotIn('private-old-credential',json.dumps(payloads))
