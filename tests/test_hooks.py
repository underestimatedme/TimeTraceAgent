import json
import tempfile
import unittest
from pathlib import Path

from timetrace import hooks
from timetrace.db import Database
from timetrace.models import PENDING, RunResult


class ExtractValidateTest(unittest.TestCase):
    def test_extract_array_surrounded_by_prose(self):
        text = 'Sure! Here you go:\n[{"prompt": "a"}, {"prompt": "b", "tool": "codex"}]\nDone.'
        self.assertEqual(len(hooks.extract_tasks(text)), 2)

    def test_extract_skips_non_array_brackets(self):
        text = 'see [1] above ... {"x": [1,2]} ... [{"prompt": "real"}]'
        items = hooks.extract_tasks(text)
        self.assertEqual(items, [1])  # first valid array wins; validate() drops it later
        self.assertEqual(hooks.validate(items, 5), [])

    def test_extract_raises_without_array(self):
        with self.assertRaises(ValueError):
            hooks.extract_tasks("no tasks here")

    def test_validate_normalises_and_caps(self):
        items = [{"prompt": " a "}, {"prompt": ""}, "junk", {"prompt": "b", "tool": "gemini",
                                                            "any_tool": 1},
                 {"prompt": "c", "tool": "codex"}, {"prompt": "d"}]
        out = hooks.validate(items, 3)
        self.assertEqual(out, [
            {"prompt": "a", "tool": None, "any_tool": False},
            {"prompt": "b", "tool": None, "any_tool": True},
            {"prompt": "c", "tool": "codex", "any_tool": False},
        ])


class FakeAdapter:
    def __init__(self, res):
        self.res = res
        self.calls = []

    def resume(self, prompt, cwd, session_id, log_file):
        self.calls.append((prompt, cwd, session_id))
        return self.res


class HookFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.cfg = {"hook_max_tasks": 2}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _parent(self, generation=0):
        tid = self.db.add_task("parent", "/repo", tool="claude", on_success="what next?",
                               generation=generation)
        self.db.update_task(tid, session_id="sid", worktree="/wt", state="done")
        return self.db.get_task(tid)

    def test_run_hook_writes_inbox_then_ingest_creates_children(self):
        parent = self._parent()
        ad = FakeAdapter(RunResult(ok=True, output='[{"prompt":"t1"},{"prompt":"t2","tool":"codex"},{"prompt":"t3"}]'))
        path = hooks.run_hook(self.db, ad, parent, self.home, self.cfg, now=100, log_file="/dev/null")
        self.assertTrue(path and path.exists())
        self.assertIn("what next?", ad.calls[0][0])
        self.assertIn("ONE JSON array", ad.calls[0][0])
        self.assertEqual(ad.calls[0][1:], ("/wt", "sid"))
        data = json.loads(path.read_text())
        self.assertEqual(data["parent_id"], parent["id"])
        self.assertEqual(len(data["tasks"]), 2)  # capped by hook_max_tasks
        self.assertEqual(len(self.db.list_events(type_="hook_generated")), 1)

        added = hooks.ingest(self.db, self.home, self.cfg, now=200)
        self.assertEqual(added, 2)
        kids = [t for t in self.db.list_tasks() if t["parent_id"] == parent["id"]]
        self.assertEqual(len(kids), 2)
        for k in kids:
            self.assertEqual(k["generation"], 1)
            self.assertEqual(k["depends_on"], parent["id"])
            self.assertEqual(k["repo"], "/repo")
            self.assertEqual(k["state"], PENDING)
        self.assertEqual(kids[1]["tool"], "codex")
        self.assertFalse(path.exists())
        self.assertTrue((self.home / "inbox" / "done" / path.name).exists())

    def test_generation_one_parent_is_rejected_at_hook_time(self):
        parent = self._parent(generation=1)
        ad = FakeAdapter(RunResult(ok=True, output='[{"prompt":"t1"}]'))
        self.assertIsNone(hooks.run_hook(self.db, ad, parent, self.home, self.cfg, 1, "/dev/null"))
        self.assertEqual(ad.calls, [])
        self.assertIn("depth", self.db.list_events(type_="hook_rejected")[0]["payload"]["reason"])

    def test_ingest_rejects_generation_one_parent(self):
        parent = self._parent(generation=1)
        inbox = self.home / "inbox"
        inbox.mkdir()
        (inbox / "x.json").write_text(json.dumps({"parent_id": parent["id"],
                                                  "tasks": [{"prompt": "t"}]}))
        self.assertEqual(hooks.ingest(self.db, self.home, self.cfg, now=5), 0)
        self.assertTrue((inbox / "rejected" / "x.json").exists())
        self.assertEqual(len(self.db.list_tasks()), 0)

    def test_ingest_rejects_garbage_and_missing_parent(self):
        inbox = self.home / "inbox"
        inbox.mkdir()
        (inbox / "a.json").write_text("not json")
        (inbox / "b.json").write_text(json.dumps({"parent_id": 42, "tasks": [{"prompt": "t"}]}))
        self.assertEqual(hooks.ingest(self.db, self.home, self.cfg, now=5), 0)
        self.assertEqual(len(self.db.list_events(type_="hook_rejected")), 2)

    def test_hook_output_without_json_is_rejected(self):
        parent = self._parent()
        ad = FakeAdapter(RunResult(ok=True, output="I could not think of anything."))
        self.assertIsNone(hooks.run_hook(self.db, ad, parent, self.home, self.cfg, 1, "/dev/null"))
        ev = self.db.list_events(type_="hook_rejected")[0]
        self.assertIn("no JSON array", ev["payload"]["reason"])

    def test_blocked_hook_run_is_rejected_not_crashed(self):
        parent = self._parent()
        ad = FakeAdapter(RunResult(blocked=True, error="limit"))
        self.assertIsNone(hooks.run_hook(self.db, ad, parent, self.home, self.cfg, 1, "/dev/null"))
        self.assertTrue(self.db.list_events(type_="hook_rejected")[0]["payload"]["blocked"])


if __name__ == "__main__":
    unittest.main()
