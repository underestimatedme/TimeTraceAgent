import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from timetrace.app_preview import collect

class AppPreviewTest(unittest.TestCase):
    def test_only_committed_regular_browser_files_are_bundled(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            def git(*args):return subprocess.check_output(['git','-C',d,*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.name','Test');git('config','user.email','test@example.test')
            (p/'index.html').write_text('<script src="app.js"></script>')
            (p/'app.js').write_text('document.body.dataset.ok="yes"')
            (p/'private.json').write_text('{"secret":true}');(p/'.env').write_text('secret');(p/'link.js').symlink_to('/etc/hosts')
            git('add','.');git('commit','-m','initial');sha=git('rev-parse','HEAD')
            (p/'app.js').write_text('uncommitted');(p/'extra.js').write_text('not tracked')
            body,err=collect(d,sha);self.assertEqual(err,'')
            b=json.loads(body);self.assertEqual(b['commit'],sha)
            self.assertEqual({f['path'] for f in b['files']},{'index.html','app.js'})
            self.assertEqual(base64.b64decode(b['files'][0]['data']).decode(),'document.body.dataset.ok="yes"')
            (p/'app.js').write_bytes(b'x'*1400001);git('add','app.js');git('commit','-m','large')
            body,err=collect(d,git('rev-parse','HEAD'));self.assertIsNone(body);self.assertIn('1.4 MB',err)
