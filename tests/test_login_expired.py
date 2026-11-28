"""An expired pairing login (Valley refuses the refresh credential with 401) versus a refresh that merely failed.

Fake cloud, in-memory credential store and temporary database only: no Keychain, network or Agent process."""
import copy
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.agent import LOGIN_EXPIRED_LOG, LOGIN_RESTORED_LOG, Agent
from timetrace.cloud import CloudError, LoginExpired
from timetrace.credentials import REFUSED_RETRY_FIRST_SECONDS, REFUSED_RETRY_MAX_SECONDS, SessionManager
from timetrace.db import Database

# What Valley's /runner-auth/refresh answers for an expired, unknown or revoked refresh credential.
REFUSED = CloudError("authentication required", 401, 40100)


class MemoryStore:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.lock = threading.RLock()
        self.deleted = False

    def load(self, account="default"):
        return copy.deepcopy(self.value)

    def save(self, value):
        persisted = {k: v for k, v in value.items() if k not in ("access_token", "expires_at")}
        self.value = copy.deepcopy(persisted)

    def delete(self, account="default"):
        self.deleted, self.value = True, None

    def session_lock(self):
        return self.lock


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Cloud:
    """refresh: tokens in `refused` get REFUSED; `failure`, when set, is raised for every other one."""
    def __init__(self, refused=(), failure=None):
        self.refused, self.failure = set(refused), failure
        self.refreshes, self.inventories, self.claims = [], 0, 0
        self.timeout = 30

    def refresh(self, refresh_token, request_key):
        self.refreshes.append(refresh_token)
        if refresh_token in self.refused:
            raise REFUSED
        if self.failure is not None:
            raise self.failure
        n = len(self.refreshes)
        return {"access_token": "access-%d" % n, "refresh_token": "rotated-%d" % n, "expires_in": 900,
                "runner": {"id": "r1", "name": "Mac"}}

    def update_inventory(self, token, *args, **kwargs):
        self.inventories += 1
        return {}

    def claim(self, token):
        self.claims += 1
        return None

    def post_quota_samples(self, token, samples):
        return {"accepted": len(samples)}


PAIRED = {"refresh_token": "dead", "runner": {"id": "r1", "name": "Mac"}}


class SessionManagerRefusalTest(unittest.TestCase):
    def test_a_refused_refresh_is_login_expired_keeps_the_credential_and_backs_off(self):
        store, cloud, clock = MemoryStore(PAIRED), Cloud(refused={"dead"}), Clock()
        manager = SessionManager(store, cloud, clock=clock)
        with self.assertRaises(LoginExpired) as caught:
            manager.token()
        self.assertEqual((caught.exception.status, caught.exception.code), (401, 40100))
        self.assertIsInstance(caught.exception, CloudError)  # existing `except CloudError` / status 401 paths still apply
        # Kept: `timetrace cloud login` reuses the runner id stored with it.
        self.assertFalse(store.deleted)
        self.assertEqual((store.value["refresh_token"], store.value["runner"]["id"]), ("dead", "r1"))
        # Every caller fails at once, without asking Valley again, until the delay is over.
        for _ in range(20):
            clock.now += 10
            with self.assertRaises(LoginExpired):
                manager.token()
        self.assertEqual(cloud.refreshes, ["dead"])
        # Then once more, and the delay doubles up to the cap.
        asked_at = [1000.0]
        delay = REFUSED_RETRY_FIRST_SECONDS
        while delay <= REFUSED_RETRY_MAX_SECONDS * 2:
            clock.now = asked_at[-1] + min(delay, REFUSED_RETRY_MAX_SECONDS) - 1
            with self.assertRaises(LoginExpired):
                manager.token()
            self.assertEqual(len(cloud.refreshes), len(asked_at), "asked before the delay was over")
            clock.now += 1
            with self.assertRaises(LoginExpired):
                manager.token()
            asked_at.append(clock.now)
            self.assertEqual(len(cloud.refreshes), len(asked_at))
            delay *= 2
        gaps = [b - a for a, b in zip(asked_at, asked_at[1:])]
        self.assertEqual(gaps[:4], [300, 600, 1200, 2400])
        self.assertEqual(set(gaps[4:]), {REFUSED_RETRY_MAX_SECONDS})

    def test_a_new_login_is_used_at_once(self):
        store, cloud, clock = MemoryStore(PAIRED), Cloud(refused={"dead"}), Clock()
        manager = SessionManager(store, cloud, clock=clock)
        with self.assertRaises(LoginExpired):
            manager.token()
        store.value = {"refresh_token": "fresh", "runner": {"id": "r1", "name": "Mac"}}  # `timetrace cloud login`
        self.assertEqual(manager.token(), "access-2")
        self.assertEqual(cloud.refreshes, ["dead", "fresh"])
        self.assertEqual(store.value["refresh_token"], "rotated-2")

    def test_transient_failures_are_not_an_expired_login_and_are_retried_at_once(self):
        failures = (TimeoutError("lost response"), urllib.error.URLError("offline"),
                    CloudError("登录暂时无法续期，请稍后重试", 503, 50300), CloudError("request failed", 500, 50000),
                    # A 401 without Valley's envelope (a proxy or captive portal) says nothing about the credential.
                    CloudError("invalid Valley response", 401))
        for failure in failures:
            with self.subTest(failure=failure):
                store, cloud = MemoryStore(PAIRED), Cloud(failure=failure)
                manager = SessionManager(store, cloud, clock=Clock())
                for attempt in range(3):
                    with self.assertRaises(type(failure)) as caught:
                        manager.token()
                    self.assertNotIsInstance(caught.exception, LoginExpired)
                self.assertEqual(len(cloud.refreshes), 3)  # no backoff: the next call asks again
                self.assertEqual(store.value["refresh_token"], "dead")
                cloud.failure = None
                self.assertEqual(manager.token(), "access-4")


class DaemonLoginExpiredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = Database(self.home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.lines = []

    def agent(self, store, cloud, clock, **kwargs):
        # Wired as `timetrace agent run` wires it: protocol 2 (an inventory heartbeat every round), the session
        # manager's token, and a rejected access token only invalidates it.
        manager = SessionManager(store, cloud, clock=clock)
        return Agent(self.db, cloud, {}, self.home, manager.token, inventory=lambda: ([], [], 1, None),
                     log=self.lines.append, on_revoked=manager.invalidate_access, report_protocol=True, **kwargs)

    def logged(self, text):
        return [line for line in self.lines if text in line]

    def test_refused_refresh_is_logged_once_and_backs_off_until_cloud_login(self):
        store, cloud, clock = MemoryStore(PAIRED), Cloud(refused={"dead"}), Clock()
        agent = self.agent(store, cloud, clock)
        for minute in range(10):  # the loop's rounds: upkeep, then a claim, once a minute
            clock.now = 1000.0 + 60 * minute
            agent.maintain(now=clock.now)
            self.assertEqual(agent.run_once(), "login_expired")
        self.assertEqual(self.logged("电脑登录已过期"), [LOGIN_EXPIRED_LOG])
        self.assertIn("timetrace cloud login", LOGIN_EXPIRED_LOG)
        self.assertEqual(self.logged("inventory push failed"), [])
        self.assertEqual(self.logged("授权失败"), [])
        # 20 token requests in 10 minutes; Valley was asked at minute 0 and minute 5 only.
        self.assertEqual(cloud.refreshes, ["dead", "dead"])
        self.assertEqual((cloud.inventories, cloud.claims), (0, 0))
        self.assertFalse(store.deleted)
        self.assertEqual(store.value["runner"]["id"], "r1")

        # `timetrace cloud login` stores a new credential: the same daemon resumes at its next round.
        store.value = {"refresh_token": "fresh", "runner": {"id": "r1", "name": "Mac"}}
        clock.now += 60
        agent.maintain(now=clock.now)
        self.assertEqual(agent.run_once(), "idle")
        self.assertEqual((cloud.inventories, cloud.claims), (1, 1))
        self.assertEqual(self.logged("电脑登录已恢复"), [LOGIN_RESTORED_LOG])
        self.assertEqual(self.logged("电脑登录已过期"), [LOGIN_EXPIRED_LOG])

    def test_transient_refresh_failure_is_not_reported_as_expired(self):
        store, clock = MemoryStore(PAIRED), Clock()
        cloud = Cloud(failure=CloudError("登录暂时无法续期，请稍后重试", 503, 50300))
        agent = self.agent(store, cloud, clock)
        for minute in range(3):
            clock.now = 1000.0 + 60 * minute
            agent.maintain(now=clock.now)
            with self.assertRaises(CloudError):  # the claim loop's ordinary error backoff takes it
                agent.run_once()
        self.assertEqual(self.logged("电脑登录已过期"), [])
        self.assertEqual(len(self.logged("inventory push failed: CloudError")), 3)
        self.assertEqual(len(cloud.refreshes), 6)  # asked every time: the credential may still be good
        cloud.failure = urllib.error.URLError("offline")
        agent.maintain(now=clock.now + 60)
        with self.assertRaises(urllib.error.URLError):
            agent.run_once()
        self.assertEqual(self.logged("电脑登录已过期"), [])
        cloud.failure = None
        agent.maintain(now=clock.now + 120)
        self.assertEqual(agent.run_once(), "idle")
        self.assertEqual(self.logged("电脑登录已恢复"), [])  # it never expired

    def test_rejected_access_token_keeps_the_old_message(self):
        """A claim refused with a still-refreshable credential is not an expired login."""
        class AccessRefused(Cloud):
            def claim(self, token):
                raise CloudError("authentication required", 401, 40100)

        store, cloud, clock = MemoryStore(PAIRED), AccessRefused(), Clock()
        agent = self.agent(store, cloud, clock)
        self.assertEqual(agent.run_once(), "revoked")
        self.assertEqual(len(self.logged("授权失败")), 1)
        self.assertEqual(self.logged("电脑登录已过期"), [])

    def test_both_loops_wait_a_minute_per_round_and_log_once(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                self.lines.clear()
                store, cloud, clock = MemoryStore(PAIRED), Cloud(refused={"dead"}), Clock()
                agent = self.agent(store, cloud, clock, max_parallel=2 if parallel else 1)
                waits = []
                stop, wake = agent._stop, agent._claim_wake

                def wait(timeout=None):
                    waits.append(timeout)
                    if len(waits) == 3:
                        stop.set()
                    return stop.is_set()
                stop.wait = wait
                wake.wait = lambda timeout=None: wait(timeout)
                with patch("timetrace.runtime_preview.RuntimePreviews.start"):
                    agent.run_forever(interval=5)
                self.assertEqual(waits, [60, 60, 60])
                self.assertEqual(self.logged("电脑登录已过期"), [LOGIN_EXPIRED_LOG])
                self.assertEqual(self.logged("inventory push failed"), [])
                self.assertEqual(cloud.refreshes, ["dead"])


class DoctorPairingTest(unittest.TestCase):
    def status(self, credentials, cloud):
        store = MemoryStore(credentials) if credentials is not None else None
        with patch("timetrace.cli.CredentialStore", new=lambda: store if store is not None else MemoryStore(None)), \
             patch("timetrace.cli._cloud", return_value=cloud):
            line, paired = cli._pairing_status({"cloud_base_url": "https://valley.test"})
        return line, paired, store

    def test_refused_refresh_says_login_expired_and_keeps_the_credential(self):
        cloud = Cloud(refused={"dead"})
        line, paired, store = self.status(PAIRED, cloud)
        self.assertFalse(paired)
        self.assertTrue(line.startswith("登录已过期或已在手机上解绑 → 运行 timetrace cloud login 重新绑定"), line)
        self.assertEqual(cloud.refreshes, ["dead"])
        self.assertEqual(cloud.timeout, cli.DOCTOR_CLOUD_TIMEOUT_SECONDS)
        self.assertFalse(store.deleted)
        self.assertEqual(store.value["refresh_token"], "dead")

    def test_network_and_server_errors_are_not_called_expired(self):
        for failure, shown in ((urllib.error.URLError("offline"), "没能连上服务器确认登录（URLError）"),
                               (TimeoutError("timed out"), "没能连上服务器确认登录（TimeoutError）"),
                               (CloudError("登录暂时无法续期，请稍后重试", 503, 50300), "没能向服务器确认登录（HTTP 503）"),
                               (CloudError("request failed", 500, 50000), "没能向服务器确认登录（HTTP 500）")):
            with self.subTest(failure=failure):
                line, paired, store = self.status(PAIRED, Cloud(failure=failure))
                self.assertTrue(paired)
                self.assertTrue(line.startswith("已完成；"), line)
                self.assertIn(shown, line)
                self.assertIn("不代表登录已过期", line)
                self.assertNotIn("登录已过期或", line)
                self.assertEqual(store.value["refresh_token"], "dead")

    def test_a_working_login_is_confirmed_and_the_rotation_is_stored(self):
        cloud = Cloud()
        line, paired, store = self.status(PAIRED, cloud)
        self.assertEqual((line, paired), ("已完成，服务器已确认登录有效", True))
        self.assertEqual(store.value["refresh_token"], "rotated-1")  # a running Runner reloads it

    def test_unpaired_and_incomplete_records_do_not_ask_valley(self):
        cloud = Cloud()
        self.assertEqual(self.status(None, cloud)[:2], ("未完成 → 运行 timetrace cloud login", False))
        line, paired, _ = self.status({"runner": {"id": "r1"}}, cloud)
        self.assertFalse(paired)
        self.assertIn("timetrace cloud login", line)
        self.assertEqual(cloud.refreshes, [])


if __name__ == "__main__":
    unittest.main()
