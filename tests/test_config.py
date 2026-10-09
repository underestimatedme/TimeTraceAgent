import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, config


class HomePermissionsTest(unittest.TestCase):
    def test_data_directory_is_private_to_the_user(self):
        # Prompts, run logs and the outbox live here; on macOS every local
        # user is in group staff, which can traverse a default home.
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "timetrace"
            home.mkdir(mode=0o755)
            config.ensure_dirs(home)
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)


class DefaultsTest(unittest.TestCase):
    def test_output_tail_upload_defaults_on(self):
        self.assertIs(config.DEFAULTS["upload_output_tail"], True)


class ConfigValueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_set_writes_typed_values_and_keeps_other_keys(self):
        (self.home / "config.json").write_text(json.dumps({"claude": {"bin": "/x/claude"}}))
        config.set_value(self.home, "upload_output_tail", "false")
        config.set_value(self.home, "interval_sec", "45")
        doc = json.loads((self.home / "config.json").read_text())
        self.assertEqual(doc, {"claude": {"bin": "/x/claude"}, "upload_output_tail": False, "interval_sec": 45})
        self.assertEqual(stat.S_IMODE((self.home / "config.json").stat().st_mode), 0o600)
        self.assertIs(config.load(self.home)["upload_output_tail"], False)

    def test_booleans_accept_the_usual_spellings(self):
        for raw, expected in (("true", True), ("on", True), ("1", True), ("yes", True),
                              ("False", False), ("off", False), ("0", False), ("no", False)):
            with self.subTest(raw=raw):
                self.assertIs(config.parse_value("upload_output_tail", raw), expected)
        with self.assertRaises(ValueError):
            config.parse_value("upload_output_tail", "maybe")

    def test_unknown_nested_and_invalid_values_are_refused(self):
        for key, raw in (("nope", "1"), ("claude", "{}"), ("allowed_repos", "[]"),
                         ("interval_sec", "soon"), ("interval_sec", "-1"),
                         ("cloud_base_url", "http://evil.example/api")):
            with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                config.set_value(self.home, key, raw)
        self.assertFalse((self.home / "config.json").exists())

    def test_cloud_base_url_accepts_https(self):
        config.set_value(self.home, "cloud_base_url", "https://valley.example/timetrace/api/v1")
        self.assertEqual(config.load(self.home)["cloud_base_url"], "https://valley.example/timetrace/api/v1")


class ConfigCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"TIMETRACE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_get_shows_the_effective_value(self):
        code, out, _ = self.run_cli("config", "get", "upload_output_tail")
        self.assertEqual((code, out.strip()), (0, "true"))
        code, out, _ = self.run_cli("config", "get", "cloud_base_url")
        self.assertEqual(out.strip(), config.DEFAULTS["cloud_base_url"])

    def test_set_then_get(self):
        code, out, err = self.run_cli("config", "set", "upload_output_tail", "false")
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("config", "get", "upload_output_tail")
        self.assertEqual(out.strip(), "false")

    def test_list_shows_every_settable_key(self):
        code, out, _ = self.run_cli("config", "list")
        self.assertEqual(code, 0)
        for key in ("upload_output_tail", "cloud_base_url", "interval_sec"):
            self.assertIn(key, out)
        # the nested tool sections are not scalar keys; only their .bin is settable
        self.assertFalse(any(line.startswith("claude =") for line in out.splitlines()))
        self.assertIn("claude.bin = claude", out)
        self.assertIn("codex.bin = codex", out)

    def test_bad_key_exits_nonzero_with_a_message(self):
        code, _, err = self.run_cli("config", "set", "claude", "x")
        self.assertEqual(code, 2)
        self.assertIn("claude", err)
        code, _, err = self.run_cli("config", "get", "nope")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
