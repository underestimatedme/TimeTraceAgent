import tempfile
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from timetrace.dispatch import (
    DispatchGate,
    FileLock,
    LockBusy,
    adapter_zero_spend_verified,
    coding_slot_lock,
    deny_reason,
    workspace_lock,
)


class DispatchGateTest(unittest.TestCase):
    def test_all_clear(self):
        gate = DispatchGate(cancelled=False, lease_valid=True, runner_online=True,
                            dependencies_ready=True, zero_spend_verified=True)
        self.assertIsNone(deny_reason(gate))

    def test_no_billing_guarantee(self):
        gate = DispatchGate(False, True, True, True, False)
        self.assertEqual(deny_reason(gate), "billing_unverified")

    def test_cancel_wins(self):
        # Cancellation dominates every other signal.
        gate = DispatchGate(True, True, True, True, True)
        self.assertEqual(deny_reason(gate), "cancelled")

    def test_cancel_beats_expired_lease(self):
        gate = DispatchGate(True, False, True, True, True)
        self.assertEqual(deny_reason(gate), "cancelled")

    def test_ordered_reasons(self):
        self.assertEqual(deny_reason(DispatchGate(False, False, True, True, True)), "lease_expired")
        self.assertEqual(deny_reason(DispatchGate(False, True, False, True, True)), "runner_offline")
        self.assertEqual(deny_reason(DispatchGate(False, True, True, False, True)), "dependencies_pending")

    def test_only_boolean_true_verifies_zero_spend(self):
        class Adapter:
            def __init__(self, value):
                self.value = value

            def capabilities(self):
                return {"can_enforce_zero_spend": self.value}

        self.assertTrue(adapter_zero_spend_verified(Adapter(True)))
        for value in ("false", "true", 1, [True]):
            with self.subTest(value=value):
                self.assertFalse(adapter_zero_spend_verified(Adapter(value)))


class FileLockTest(unittest.TestCase):
    def test_agent_and_scheduler_processes_share_slot_even_after_owner_crash(self):
        worker = str(Path(__file__).with_name("process_lock_worker.py"))
        for role, rival in (("agent", "scheduler"), ("scheduler", "agent")):
            with self.subTest(owner=role), tempfile.TemporaryDirectory() as d:
                home = Path(d)
                repo = home / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-qb", "main", str(repo)], check=True)
                owner = subprocess.Popen([sys.executable, worker, d, role, "hold"], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    ready = owner.stdout.readline()
                    self.assertTrue(ready.startswith("ready "), ready)
                    child_pid = int(ready.split()[1])
                    for crashed in (False, True):
                        if crashed:
                            owner.kill()
                            owner.wait(timeout=5)
                            os.kill(child_pid, 0)
                            writes = home / "child-writes"
                            size = writes.stat().st_size if writes.exists() else 0
                            until = time.monotonic() + 2
                            while (not writes.exists() or writes.stat().st_size <= size) and time.monotonic() < until:
                                time.sleep(.01)
                            self.assertGreater(writes.stat().st_size, size, "orphan must actually still write")
                        peer = subprocess.run([sys.executable, worker, d, rival, "probe"], capture_output=True,
                                              text=True, check=True, timeout=5)
                        self.assertIn("deferred (runner busy)", peer.stdout)
                        self.assertNotIn("spawned", peer.stdout)
                finally:
                    try:
                        os.killpg(owner.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    owner.wait(timeout=5)
                    owner.stdout.close()
                    owner.stderr.close()

    def test_crashed_owner_with_live_orphan_keeps_fence(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "owner.lock")
            script = '''
import subprocess, sys, time
from timetrace.dispatch import FileLock
lock = FileLock(sys.argv[1]).acquire()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
'''
            owner = subprocess.Popen([sys.executable, "-c", script, path], stdout=subprocess.PIPE,
                                     text=True, start_new_session=True)
            try:
                child_pid = int(owner.stdout.readline())
                owner.kill()
                owner.wait(timeout=5)
                os.kill(child_pid, 0)  # the orphan still exists after flock owner died
                probe = subprocess.run([sys.executable, "-c", '''
import sys
from timetrace.dispatch import FileLock, LockBusy
try:
    lock = FileLock(sys.argv[1]).acquire()
except LockBusy:
    print("blocked")
else:
    lock.release()
    print("acquired")
''', path], text=True, capture_output=True, check=True, timeout=5)
                self.assertEqual(probe.stdout.strip(), "blocked")
            finally:
                os.killpg(owner.pid, signal.SIGKILL)
                owner.wait(timeout=5)
                owner.stdout.close()

    def test_separate_process_observes_workspace_alias_lock(self):
        with tempfile.TemporaryDirectory() as d:
            home, repo = Path(d) / "home", Path(d) / "repo"
            repo.mkdir()
            alias = Path(d) / "alias"
            alias.symlink_to(repo, target_is_directory=True)
            held = workspace_lock(home, str(repo)).acquire()
            try:
                probe = subprocess.run([sys.executable, "-c", '''
import sys
from timetrace.dispatch import workspace_lock, LockBusy
try:
    workspace_lock(sys.argv[1], sys.argv[2]).acquire()
except LockBusy:
    print("blocked")
''', str(home), str(alias)], text=True, capture_output=True, check=True, timeout=5)
                self.assertEqual(probe.stdout.strip(), "blocked")
            finally:
                held.release()

    def test_second_acquire_is_denied(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "x.lock")
            first = FileLock(path).acquire()
            try:
                with self.assertRaises(LockBusy):
                    FileLock(path).acquire()
            finally:
                first.release()
            # After release the lock is free again.
            again = FileLock(path).acquire()
            again.release()

    def test_workspace_lock_is_per_canonical_path(self):
        with tempfile.TemporaryDirectory() as home:
            with tempfile.TemporaryDirectory() as ws1, tempfile.TemporaryDirectory() as ws2:
                a = workspace_lock(home, ws1).acquire()
                try:
                    # A different workspace is not blocked by the first lock.
                    b = workspace_lock(home, ws2).acquire()
                    b.release()
                    # The same workspace is blocked.
                    with self.assertRaises(LockBusy):
                        workspace_lock(home, ws1).acquire()
                finally:
                    a.release()

    def test_coding_slot_is_singleton(self):
        with tempfile.TemporaryDirectory() as home:
            slot = coding_slot_lock(home).acquire()
            try:
                with self.assertRaises(LockBusy):
                    coding_slot_lock(home).acquire()
            finally:
                slot.release()


if __name__ == "__main__":
    unittest.main()
