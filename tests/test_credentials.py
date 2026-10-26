import json
import unittest
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from timetrace.credentials import CredentialStore, SessionManager


class Result:
    def __init__(self, code=0, stdout=""):
        self.returncode, self.stdout = code, stdout


class CredentialTest(unittest.TestCase):
    def test_parallel_workers_rotate_refresh_token_only_once(self):
        store, cloud = MemoryStore(), RotatingCloud()
        manager = SessionManager(store, cloud, clock=lambda: 100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: manager.token(), range(24)))
        self.assertEqual(tokens, ["access-1"] * 24)
        self.assertEqual(len(cloud.calls), 1)

    def test_managers_reload_a_credential_rotated_by_another_process(self):
        store, cloud = MemoryStore(), RotatingCloud()
        first, second = SessionManager(store, cloud), SessionManager(store, cloud)
        self.assertEqual(first.token(), "access-1")
        self.assertEqual(second.token(), "access-2")
        self.assertEqual(first.token(), "access-3")
        self.assertEqual([t for t, _ in cloud.calls], ["refresh-0", "refresh-1", "refresh-2"])

    def test_failed_refresh_reuses_persisted_idempotency_key_and_keeps_credentials(self):
        store, cloud = MemoryStore(), RotatingCloud()
        manager = SessionManager(store, cloud)
        original = cloud.refresh
        def fail(token, key):
            cloud.calls.append((token, key))
            raise TimeoutError("lost response")
        cloud.refresh = fail
        with self.assertRaises(TimeoutError): manager.token()
        key = store.value["pending_refresh_key"]
        self.assertEqual(store.value["refresh_token"], "refresh-0")
        cloud.calls.clear()
        cloud.refresh = original
        manager.token()
        self.assertEqual(cloud.calls[0], ("refresh-0", key))

    def test_access_rejection_preserves_refresh_and_explicit_logout_removes_access(self):
        store, cloud = MemoryStore(), RotatingCloud()
        manager = SessionManager(store, cloud)
        manager.token()
        manager.invalidate_access()
        self.assertEqual(store.value["refresh_token"], "refresh-1")
        self.assertEqual(manager.token(), "access-2")
        store.value = None
        with self.assertRaisesRegex(RuntimeError, "not paired"): manager.token()

    def test_keychain_commands_do_not_put_secret_in_account_name(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return Result()

        CredentialStore(run).save({"access_token": "access", "refresh_token": "refresh", "runner": {"id": "r1"}})
        command, kwargs = calls[0]
        self.assertEqual(command[0], "security")
        self.assertEqual(command[command.index("-a") + 1], "default")
        self.assertNotIn("refresh", " ".join(command))
        self.assertIn("refresh", kwargs["input"])
        self.assertNotIn("access", kwargs["input"])

    def test_session_manager_refreshes_expiring_access_token(self):
        class Store:
            def __init__(self):
                self.value = {"access_token": "old", "refresh_token": "r", "expires_at": 100}
            def load(self): return self.value
            def save(self, value): self.value = value
        class Cloud:
            def refresh(self, token, request_key):
                self.token = token
                self.request_key = request_key
                return {"access_token": "new", "refresh_token": "r2", "expires_in": 900}
        store, cloud = Store(), Cloud()
        manager = SessionManager(store, cloud, clock=lambda: 100)
        self.assertEqual(manager.token(), "new")
        self.assertEqual(cloud.token, "r")
        self.assertEqual(store.value["refresh_token"], "r2")


class MemoryStore:
    def __init__(self):
        self.value = {"refresh_token": "refresh-0"}
        self.lock = threading.RLock()
    def load(self): return copy.deepcopy(self.value)
    def save(self, value): self.value = copy.deepcopy(value)
    def session_lock(self): return self.lock


class RotatingCloud:
    def __init__(self): self.calls = []
    def refresh(self, token, key):
        if token != "refresh-%d" % len(self.calls): raise AssertionError("stale refresh token")
        time.sleep(.005)
        self.calls.append((token, key))
        return {"refresh_token": "refresh-%d" % len(self.calls), "access_token": "access-%d" % len(self.calls), "expires_in": 900}


if __name__ == "__main__":
    unittest.main()
