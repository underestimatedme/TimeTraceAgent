import json
import unittest

from timetrace.credentials import CredentialStore, SessionManager


class Result:
    def __init__(self, code=0, stdout=""):
        self.returncode, self.stdout = code, stdout


class CredentialTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
