import os
import sys
import tempfile
import time
import unittest
import threading
from pathlib import Path

from timetrace.process import run_streaming


class ProcessTest(unittest.TestCase):
    def test_already_cancelled_run_never_creates_a_process(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "spawned"
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)],
                              d, str(Path(d) / "run.log"), cancel_event=cancelled)
            self.assertFalse(marker.exists())

    def test_secret_named_environment_never_reaches_the_model_process(self):
        # The runner may be started from a shell holding deploy credentials;
        # the model's tool calls must not be able to read (and echo) them.
        from unittest.mock import patch
        leak = {"GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "a", "AWS_SESSION_TOKEN": "s",
                "DB_PASSWORD": "p", "STRIPE_API_KEY": "k", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "x",
                "GOOGLE_APPLICATION_CREDENTIALS": "/c.json", "NPM_TOKEN": "n"}
        keep = {"TIMETRACE_KEEP_ME": "1", "PATH": os.environ.get("PATH", ""), "SSH_AUTH_SOCK": "/tmp/agent"}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, dict(leak, **keep)):
            code, lines = run_streaming([sys.executable, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
                                        d, str(Path(d) / "run.log"), timeout=10)
        import json
        names = set(json.loads(lines[-1]))
        self.assertFalse(names & set(leak), names & set(leak))
        self.assertTrue(set(keep) <= names)

    def test_drains_stderr_without_deadlock(self):
        with tempfile.TemporaryDirectory() as d:
            log = str(Path(d) / "run.log")
            script = "import sys; sys.stderr.write('x'*200000); print('done')"
            code, lines = run_streaming([sys.executable, "-c", script], d, log, timeout=5)
            self.assertEqual(code, 0)
            self.assertEqual(lines, ["done"])
            self.assertGreater(os.path.getsize(log), 200000)

    def test_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            started = time.monotonic()
            code, _ = run_streaming(
                [sys.executable, "-c", "import time; time.sleep(30)"], d,
                str(Path(d) / "run.log"), timeout=0.2,
            )
            self.assertLess(time.monotonic() - started, 3)
            self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()


class DetachedChildTest(unittest.TestCase):
    def test_children_left_behind_by_a_normal_exit_are_killed(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "child.pid"
            code, _ = run_streaming(
                ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > '%s'; exit 0" % pidfile],
                d, str(Path(d) / "run.log"))
            self.assertEqual(code, 0)
            pid = int(pidfile.read_text())
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(.05)
            else:
                os.kill(pid, 9)
                self.fail("a detached child outlived the tool")


class TailTextTest(unittest.TestCase):
    def test_tail_text_keeps_last_bytes_and_replaces_invalid_utf8(self):
        from timetrace.process import tail_text
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_bytes(b"first line\n" + b"x" * 9000 + b"\nlast \xff line\n")
            tail = tail_text(p, limit=100)
            self.assertLessEqual(len(tail.encode("utf-8")), 8192)
            self.assertTrue(tail.endswith("last \ufffd line\n"))
            self.assertNotIn("first line", tail)
            self.assertEqual(tail_text(Path(d) / "missing", limit=100), "")

    def test_tail_text_returns_whole_small_file(self):
        from timetrace.process import tail_text
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_text("step 1\nstep 2\n")
            self.assertEqual(tail_text(p, limit=8000), "step 1\nstep 2\n")

    def test_tail_text_stays_within_the_byte_budget_after_replacement(self):
        from timetrace.process import tail_text
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_bytes(b"\xff" * 9000)          # every byte becomes a 3-byte U+FFFD
            tail = tail_text(p, limit=100)
            self.assertLessEqual(len(tail.encode("utf-8")), 100)

