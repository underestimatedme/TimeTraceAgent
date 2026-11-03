import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from timetrace.runtime_preview import manifest, snapshot, RuntimePreviews

class RuntimePreviewTests(unittest.TestCase):
    def test_manifest_boundary(self):
        for value in ({}, {'version':1,'runtime':'python','command':['sh','evil'],'port':8080},{'version':1,'runtime':'python','command':['python','server.py'],'port':22}):
            with self.assertRaises(ValueError): manifest(json.dumps(value))
        self.assertEqual(manifest('{"version":1,"runtime":"python","command":["python","server.py"],"port":8080}')['port'],8080)

    def test_committed_snapshot_excludes_private_and_symlink(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as target:
            root=Path(root);(root/'.keji').mkdir();(root/'.keji/preview.json').write_text('{"version":1,"runtime":"python","command":["python","server.py"],"port":8080}')
            (root/'server.py').write_text('print(1)');(root/'.env').write_text('PRIVATE=secret');(root/'link').symlink_to('/etc/passwd')
            self.git(root,'init');self.git(root,'add','.');self.git(root,'-c','user.name=QA','-c','user.email=qa@localhost','commit','-m','fixture')
            sha=self.git(root,'rev-parse','HEAD').strip();(root/'untracked.txt').write_text('private')
            snapshot(root,sha,Path(target));self.assertFalse((Path(target)/'.env').exists());self.assertFalse((Path(target)/'link').exists());self.assertFalse((Path(target)/'untracked.txt').exists())

    @staticmethod
    def git(root,*args): return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL).decode()

    @unittest.skipUnless(os.environ.get('KEJI_LIVE_DOCKER')=='1','explicit real Docker acceptance')
    def test_real_backend_isolation_mutation_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'.keji').mkdir();(root/'.keji/preview.json').write_text('{"version":1,"runtime":"python","command":["python","server.py"],"port":8080}')
            (root/'server.py').write_text('''from http.server import BaseHTTPRequestHandler,HTTPServer
import json
count=0
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'count':count}).encode())
 def do_POST(self):
  global count
  count+=2;self.do_GET()
HTTPServer(('127.0.0.1',8080),H).serve_forever()
''')
            self.git(root,'init');self.git(root,'add','.');self.git(root,'-c','user.name=QA','-c','user.email=qa@localhost','commit','-m','real backend fixture')
            agent=SimpleNamespace(db=SimpleNamespace(get_workspace=lambda _: {'path':str(root)}),_stop=threading.Event())
            runtime=RuntimePreviews(agent);key='acceptance-test';sha=self.git(root,'rev-parse','HEAD').strip()
            try:
                runtime.launch({'id':key,'workspace_id':'qa','commit':sha})
                get=runtime.http(key,{'method':'GET','path':'/api/count'});self.assertEqual(json.loads(base64.b64decode(get['body']))['count'],0)
                post=runtime.http(key,{'method':'POST','path':'/api/count','body':'e30=','content_type':'application/json'});self.assertEqual(json.loads(base64.b64decode(post['body']))['count'],2)
                state=json.loads(subprocess.check_output(['docker','inspect','keji-preview-'+key]))[0]
                self.assertEqual(state['HostConfig']['NetworkMode'],'none');self.assertTrue(state['HostConfig']['ReadonlyRootfs']);self.assertFalse(state['NetworkSettings']['Ports']);self.assertEqual(state['Config']['User'],'65534:65534')
            finally: runtime.stop(key)
            self.assertNotEqual(subprocess.run(['docker','inspect','keji-preview-'+key],capture_output=True).returncode,0)
