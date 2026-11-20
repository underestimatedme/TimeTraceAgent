"""The install commands in the docs name the release this source is: a pinned tag left behind installs an old Agent."""
import re
import unittest
from pathlib import Path

from timetrace import __version__

ROOT = Path(__file__).resolve().parents[1]
PINNED = re.compile(r"TimeTraceAgent\.git@v(\d+\.\d+\.\d+)")


class InstallDocsTest(unittest.TestCase):
    def test_pipx_commands_install_the_current_release(self):
        for name in ("README.md", "docs/getting-started.md"):
            tags = PINNED.findall((ROOT / name).read_text(encoding="utf-8"))
            self.assertTrue(tags, "%s no longer shows a pipx install from a release tag" % name)
            self.assertEqual(set(tags), {__version__}, "%s: update the pinned tag when the version changes" % name)

    def test_getting_started_names_the_current_release_and_its_notes_exist(self):
        text = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
        self.assertIn("（当前为 [%s](releases/%s.md)）" % (__version__, __version__), text)
        self.assertTrue((ROOT / "docs/releases" / ("%s.md" % __version__)).is_file())


if __name__ == "__main__":
    unittest.main()
