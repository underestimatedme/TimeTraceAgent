"""Local check commands: registry, CLI, inventory names."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.db import Database
from tests.test_parallel import init_repo


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.d), "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_argv_is_stored_as_a_list(self):
        self.db.save_check("ws1", "test", ["npm", "test", "--", "a b"])
        self.assertEqual(self.db.get_check("ws1", "test"), ["npm", "test", "--", "a b"])
        self.assertIsNone(self.db.get_check("ws1", "nope"))
        self.assertIsNone(self.db.get_check("ws2", "test"))
        self.db.save_check("ws1", "test", ["make", "check"])
        self.assertEqual(self.db.get_check("ws1", "test"), ["make", "check"])
        self.assertEqual(self.db.list_checks("ws1"), [{"workspace_id": "ws1", "name": "test",
                                                       "argv": ["make", "check"]}])
        self.assertTrue(self.db.remove_check("ws1", "test"))
        self.assertFalse(self.db.remove_check("ws1", "test"))

    def test_removing_a_workspace_forgets_its_checks(self):
        self.db.save_check("ws1", "lint", ["ruff", "."])
        self.db.remove_workspace("ws1")
        self.assertEqual(self.db.list_checks(), [])

    def test_migration_adds_the_table_to_an_old_database(self):
        path = self.d / "old.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE remote_workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
                     " path TEXT NOT NULL UNIQUE, default_branch TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        conn.commit(); conn.close()
        db = Database(path)
        db.save_check("w", "t", ["true"])
        Database(path)  # idempotent
        self.assertEqual(Database(path).get_check("w", "t"), ["true"])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        cli.add_workspace(self.db, str(self.repo), workspace_id="ws1")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch("timetrace.cli._open", return_value=(self.d, {}, self.db)), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_list_remove(self):
        code, out, _ = self.run_cli("workspace", "check", "add", "ws1", "unit", "--", "python3", "-m", "unittest")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.db.get_check("ws1", "unit"), ["python3", "-m", "unittest"])
        # By name or path as well as id.
        self.run_cli("workspace", "check", "add", "repo", "lint", "--", "ruff", "check", ".")
        self.run_cli("workspace", "check", "add", str(self.repo), "build", "--", "make")
        code, out, _ = self.run_cli("workspace", "check", "list", "ws1")
        self.assertEqual(code, 0)
        self.assertIn("unit", out); self.assertIn("python3 -m unittest", out)
        self.assertIn("lint", out); self.assertIn("build", out)
        code, out, _ = self.run_cli("workspace", "check", "list")
        self.assertIn("unit", out)
        code, out, _ = self.run_cli("workspace", "check", "remove", "ws1", "lint")
        self.assertEqual(code, 0)
        self.assertIsNone(self.db.get_check("ws1", "lint"))
        code, _, err = self.run_cli("workspace", "check", "remove", "ws1", "lint")
        self.assertEqual(code, 1)

    def test_invalid_names_commands_and_workspaces_are_refused(self):
        for name in ("Unit", "a b", "", "x" * 41, "../x", "测试"):
            with self.subTest(name=name):
                code, _, err = self.run_cli("workspace", "check", "add", "ws1", name, "--", "true")
                self.assertEqual(code, 2)
        code, _, err = self.run_cli("workspace", "check", "add", "ws1", "empty", "--")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("workspace", "check", "add", "ws1", "nul", "--", "a\0b")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("workspace", "check", "add", "nope", "unit", "--", "true")
        self.assertEqual(code, 2)
        self.assertEqual(self.db.list_checks(), [])

    def test_inventory_carries_names_never_commands(self):
        self.db.save_check("ws1", "unit", ["secret-runner", "--token=abc"])
        self.db.save_check("ws1", "lint", ["ruff"])
        workspaces = cli._runner_workspaces(self.db)
        self.assertEqual(workspaces[0]["checks"], ["lint", "unit"])
        self.assertNotIn("secret-runner", json.dumps(workspaces))
        self.assertNotIn("ruff", json.dumps(workspaces))


if __name__ == "__main__":
    unittest.main()
