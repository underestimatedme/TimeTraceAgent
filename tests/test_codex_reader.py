"""The thread that reads `codex app-server` replies must survive its stream being closed under it."""
import threading
import unittest
from unittest.mock import patch

from timetrace.adapters import codex


class _Stdin:
    def write(self, _text):
        return 0

    def flush(self):
        pass

    def close(self):
        pass


class _BlockedStdout:
    """A pipe nobody writes to any more: readline waits like a blocking read until the stream is closed,
    then fails the way a closed file does."""

    def __init__(self):
        self.closed = False
        self._woken = threading.Event()

    def readline(self):
        self._woken.wait(5)
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        return ""

    def close(self):
        self.closed = True
        self._woken.set()


class _Process:
    def __init__(self):
        self.stdin = _Stdin()
        self.stdout = _BlockedStdout()

    def kill(self):
        pass

    def wait(self):
        return 0


class ReaderThreadTest(unittest.TestCase):
    def test_timeout_does_not_leave_a_reader_thread_crashing_on_the_closed_stream(self):
        # app_server_request gives the reader two seconds to finish after the kill, then closes the pipe. A
        # reader still waiting then used to die with `ValueError: I/O operation on closed file`, printed
        # from a thread nobody watches, next to the TimeoutError the caller already got.
        crashes = []
        with patch("timetrace.adapters.codex.subprocess.Popen", return_value=_Process()), \
             patch.object(threading, "excepthook", lambda args: crashes.append(args)):
            with self.assertRaises(TimeoutError):
                codex.app_server_request("/fake/codex", "account/rateLimits/read", timeout=0.1)
            for thread in threading.enumerate():
                if thread is not threading.current_thread() and thread.daemon:
                    thread.join(timeout=1)
        self.assertEqual([(c.exc_type.__name__, str(c.exc_value)) for c in crashes], [])


if __name__ == "__main__":
    unittest.main()
