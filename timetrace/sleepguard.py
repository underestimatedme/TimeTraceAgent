"""Keep the Mac awake while a job runs: `caffeinate -i -w <agent pid>`.

-i prevents idle sleep only (closing the lid still sleeps the Mac); -w ends
caffeinate by itself when the agent exits, so a crashed agent never keeps
the computer awake. Started when the first job starts, stopped when the
last one ends (config `prevent_sleep`, default true)."""
import os
import subprocess
import threading
from typing import Callable, Optional

CAFFEINATE = "/usr/bin/caffeinate"


class SleepGuard:
    def __init__(self, pid: Optional[int] = None, binary: str = CAFFEINATE, popen: Callable = subprocess.Popen):
        self.pid = pid or os.getpid()
        self.binary, self._popen = binary, popen
        self._proc = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            try:
                self._proc = self._popen([self.binary, "-i", "-w", str(self.pid)], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def active(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
