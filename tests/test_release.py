import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from timetrace import release, worktree
from timetrace.db import Database


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "app.txt").write_text("new")
        for args in (("init", "-q"), ("config", "user.email", "qa@example.invalid"),
                     ("config", "user.name", "QA"), ("add", "."), ("commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        self.db = Database(self.root / "db.sqlite")
        self.db.upsert_workspace("repo", "Release QA", str(self.repo), "HEAD")
        self.workspace = self.db.get_workspace("repo")
        self.state = self.root / "deployed.txt"
        self.state.write_text("previous")
        state = self.state
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(state.read_bytes())
            def log_message(self, *args):
                pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.script = self.root / "trusted.py"
        self.script.write_text('''import os, pathlib, sys, urllib.request
step, path, url, target = sys.argv[1:]
p = pathlib.Path(path)
if step == 'release-prepare':
    assert pathlib.Path('app.txt').read_text() == 'new'
elif step == 'release-deploy':
    p.write_text(target)
elif step == 'release-rollback':
    p.write_text('previous')
else:
    expected = 'new' if step == 'release-health' else 'previous'
    assert urllib.request.urlopen(url).read().decode() == expected
print(step, 'PASS', os.environ['TIMETRACE_RELEASE_COMMIT'])
''')
        self.commands("new")

    def commands(self, target):
        for name in release.NAMES:
            self.db.save_check("repo", name, [sys.executable, "-I", str(self.script), name,
                                             str(self.state), "http://127.0.0.1:%d/" % self.http.server_port, target])

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.db.conn.close()
        self.tmp.cleanup()

    def prepare(self):
        return release.prepare(self.root, self.db, self.workspace, "HEAD", {})

    def test_real_release_health_and_idempotent_replay(self):
        prepared = self.prepare()
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(self.state.read_text(), "previous")
        with self.assertRaises(ValueError):
            release.execute(self.root, prepared["id"], "wrong-approval", {})
        done = release.execute(self.root, prepared["id"], prepared["authorization"], {})
        self.assertEqual(done["state"], "healthy")
        self.assertEqual(self.state.read_text(), "new")
        self.assertEqual(len(done["steps"]), 3)
        self.assertEqual(release.execute(self.root, done["id"], done["authorization"], {}), done)

    def test_failed_online_health_rolls_back_and_verifies_previous(self):
        self.commands("broken")
        p = self.prepare()
        done = release.execute(self.root, p["id"], p["authorization"], {})
        self.assertEqual(done["state"], "rolled_back")
        self.assertEqual(self.state.read_text(), "previous")
        self.assertEqual([s["name"] for s in done["steps"]], list(release.NAMES))
        self.assertNotEqual(done["steps"][2]["exit_code"], 0)
        self.assertEqual(done["steps"][-1]["exit_code"], 0)

    def test_unknown_side_effect_is_never_replayed(self):
        p = self.prepare()
        path = self.root / "releases" / p["id"] / "receipt.json"
        p["state"] = "release-deploy:running"
        path.write_text(json.dumps(p))
        with self.assertRaisesRegex(ValueError, "禁止重放"):
            release.execute(self.root, p["id"], p["authorization"], {})
        self.assertEqual(self.state.read_text(), "previous")

    def test_changed_checker_or_commit_requires_new_authorization(self):
        p = self.prepare()
        self.script.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "已变化"):
            release.execute(self.root, p["id"], p["authorization"], {})
        self.assertEqual(self.state.read_text(), "previous")

    def test_changed_tracked_source_is_not_deployed(self):
        p = self.prepare()
        (Path(p["contract"]["checkout"]) / "app.txt").write_text("unreviewed")
        with self.assertRaisesRegex(ValueError, "源码改动"):
            release.execute(self.root, p["id"], p["authorization"], {})
        self.assertEqual(self.state.read_text(), "previous")

    def test_prepared_build_changes_require_new_approval(self):
        p = self.prepare()
        (Path(p["contract"]["checkout"]) / "untracked-build.js").write_text("different artifact")
        with self.assertRaisesRegex(ValueError, "构建产物已变化"):
            release.execute(self.root, p["id"], p["authorization"], {})
        self.assertEqual(self.state.read_text(), "previous")
