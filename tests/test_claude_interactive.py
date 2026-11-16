import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from tests.test_codex_interactive import Host
from timetrace.adapters.claude import ClaudeAdapter
from timetrace.billing import StaticBilling


class ClaudeInteractiveTest(unittest.TestCase):
    def test_matches_native_echo_to_each_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "claude"
            fake.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fake_claude_interactive.py").read_text())
            fake.chmod(0o700)
            host = Host()
            expected = str(uuid.uuid4())
            adapter = ClaudeAdapter({"bin": str(fake), "timeout_seconds": 7}, billing=StaticBilling(True, "test"))
            result = adapter.work_session("initial", tmp, expected, str(root / "run.log"), threading.Event(),
                                          control=host, resuming=True, read_only=True)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(host.session_id, expected)
            self.assertEqual(result.session_id, expected)
            self.assertEqual(host.replies, ["Public reply"])
            self.assertEqual(host.delivery, ["applied", ("control-1", "uncertain")])


if __name__ == "__main__":
    unittest.main()
