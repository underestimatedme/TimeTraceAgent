"""Pairing under Valley's rate limit.

Valley's gateway answers 429 with Retry-After (whole seconds) before the
pairing handlers run. The computer must keep the same device code, wait what
the server asked, never outlive the code's own expiry, and still stop at once
on an explicit refusal.

The fixture is a loopback HTTP server speaking the pairing endpoints, a fake
clock, and a stand-in for the Keychain. Nothing here reaches a real account.
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from timetrace import cli
from timetrace.cloud import CloudClient, CloudError

POLL = "/device-authorizations/token"
ACTIVATE = "/iphone/device-authorizations/activate"
CREATE = "/iphone/device-authorizations"
CREDENTIALS = {"access_token": "qa-access", "refresh_token": "qa-refresh", "expires_in": 900,
               "runner": {"id": "qa-runner", "name": "样例电脑"}}
WAITING_PHONE = {"status": "waiting_phone", "attempts_remaining": 3}


def ok(data):
    return 200, {"code": 0, "data": data}, {}


def limited(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return 429, {"code": 40000, "message": "rate limited"}, headers


def refused(status, message, code=None):
    return status, {"code": code or status * 100, "message": message}, {}


class FakeValley:
    """Scripted answers per path, in order; the last answer of a path repeats."""

    def __init__(self, script, clock):
        self.script = {path: list(answers) for path, answers in script.items()}
        self.clock = clock
        self.requests = []  # (seconds on the fake clock, path, body)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                path = self.path.replace("/timetrace/api/v1", "", 1)
                fake.requests.append((fake.clock[0], path, body))
                answers = fake.script[path]
                status, envelope, headers = answers.pop(0) if len(answers) > 1 else answers[0]
                raw = json.dumps(envelope).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/timetrace/api/v1" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def at(self, path):
        return [seconds for seconds, seen, _ in self.requests if seen == path]


class PairingRateLimitTest(unittest.TestCase):
    def pair(self, script, expires_in=300, interval=5, through_pair_computer=False):
        """Runs one pairing against the fixture on a fake clock starting at 0."""
        self.clock = [0.0]
        self.valley = FakeValley(script, self.clock)
        self.addCleanup(self.valley.close)
        self.saved, self.asked, self.out = [], [], []

        def ask(prompt):
            self.asked.append(prompt)
            return "0000"

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.clock[0] += seconds

        cloud = CloudClient(self.valley.url)
        auth = {"device_code": "qa-device", "user_code": "ABCD2345", "expires_in": expires_in,
                "interval": interval, "pairing_version": 2,
                "verification_uri": "timetrace://pair?code=ABCD2345&v=2"}
        out = lambda *a: self.out.append(" ".join(map(str, a)))
        with tempfile.TemporaryDirectory() as home, \
             patch("timetrace.cli.CredentialStore.save", new=lambda store, value: self.saved.append(value)), \
             patch("timetrace.cli.CredentialStore.load", return_value=None), \
             patch("timetrace.cli._cloud", return_value=cloud), \
             patch("timetrace.cli._adapters", return_value={}), \
             patch("timetrace.cli._runner_workspaces", return_value=[]), \
             patch("timetrace.cli._runner_tools", return_value=[]), \
             patch("timetrace.cli.CloudClient.update_inventory", return_value={}), \
             patch("timetrace.cli.Agent.report_quota", return_value=None), \
             patch("timetrace.cli.time.time", side_effect=lambda: self.clock[0]), \
             patch("timetrace.cli.time.sleep", side_effect=sleep):
            if through_pair_computer:
                return cli.pair_computer({}, None, Path(home), out=out, input_fn=ask)
            return cli._await_pairing(cloud, auth, {}, None, Path(home), out, input_fn=ask)

    def device_codes(self):
        return {body.get("device_code") for _, _, body in self.valley.requests}

    def test_client_reads_retry_after_from_a_rate_limited_answer(self):
        valley = FakeValley({POLL: [limited(7), limited(), limited("soon"), ok({"status": "pending"})]}, [0.0])
        self.addCleanup(valley.close)
        client = CloudClient(valley.url)
        seen = []
        for _ in range(3):
            with self.assertRaises(CloudError) as raised:
                client.poll_device_authorization("qa-device")
            self.assertEqual(raised.exception.status, 429)
            seen.append(raised.exception.retry_after)
        self.assertEqual(seen, [7, None, None])
        self.assertEqual(client.poll_device_authorization("qa-device"), {"status": "pending"})

    def test_waiting_then_rate_limited_then_confirmed_keeps_the_same_device_code(self):
        result = self.pair({
            POLL: [ok({"status": "pending"}), limited(7), ok(WAITING_PHONE)],
            ACTIVATE: [ok(CREDENTIALS)],
        })
        self.assertEqual(result, 0)
        self.assertEqual(self.device_codes(), {"qa-device"})
        # Normal interval, then exactly the announced wait before asking again.
        self.assertEqual(self.valley.at(POLL), [0, 5, 12])
        self.assertEqual(self.valley.at(ACTIVATE), [12])
        self.assertEqual(self.valley.at(CREATE), [])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(any("限流" in line and "7 秒" in line for line in self.out), self.out)

    def test_rate_limited_confirmation_retries_the_same_digits_after_the_wait(self):
        result = self.pair({
            POLL: [ok(WAITING_PHONE)],
            ACTIVATE: [limited(9), limited(4), ok(CREDENTIALS)],
        })
        self.assertEqual(result, 0)
        # 9 s as asked; then 5 s, because 4 s would be faster than the polling interval.
        self.assertEqual(self.valley.at(ACTIVATE), [0, 9, 14])
        activations = [body for _, path, body in self.valley.requests if path == ACTIVATE]
        self.assertEqual(activations, [{"device_code": "qa-device", "phone_code": "0000"}] * 3)
        self.assertEqual(len(self.asked), 1, "the digits are not asked for again")
        self.assertEqual(len(self.saved), 1)

    def test_no_retry_after_waits_one_polling_interval_and_a_short_one_never_polls_faster(self):
        result = self.pair({
            POLL: [limited(), limited(1), ok(WAITING_PHONE)],
            ACTIVATE: [ok(CREDENTIALS)],
        }, interval=5)
        self.assertEqual(result, 0)
        self.assertEqual(self.valley.at(POLL), [0, 5, 10])

    def test_still_limited_until_the_code_expires_stops_without_a_new_code(self):
        result = self.pair({
            CREATE: [ok({"device_code": "qa-device", "user_code": "ABCD2345", "expires_in": 150, "interval": 5,
                         "verification_uri": "timetrace://pair?code=ABCD2345&v=2"})],
            POLL: [limited(60)],
        }, through_pair_computer=True)
        self.assertEqual(result, 1)
        self.assertEqual(self.valley.at(CREATE), [0], "no second code is requested while limited")
        self.assertEqual(self.valley.at(POLL), [0, 60, 120])
        self.assertEqual(self.clock[0], 150, "the wait ends at the code's own expiry, not later")
        self.assertEqual(self.saved, [])
        self.assertTrue(any("过期" in line and "限流" in line for line in self.out), self.out)
        self.assertFalse(any("上一个二维码已过期" in line for line in self.out), self.out)

    def test_a_wait_longer_than_the_code_lives_ends_at_expiry(self):
        result = self.pair({POLL: [limited(3600)]}, expires_in=40)
        self.assertEqual(result, 1)
        self.assertEqual(self.valley.at(POLL), [0])
        self.assertEqual(self.clock[0], 40)

    def test_explicit_refusal_after_a_rate_limit_stops_at_once(self):
        result = self.pair({POLL: [limited(6), ok({"status": "cancelled"})]})
        self.assertEqual(result, 1)
        self.assertEqual(self.valley.at(POLL), [0, 6])
        self.assertEqual(self.valley.at(ACTIVATE), [])
        self.assertEqual(self.saved, [])
        self.assertTrue(any("锁定或取消" in line for line in self.out), self.out)

    def test_confirmation_refused_after_a_rate_limit_is_final(self):
        result = self.pair({
            POLL: [ok(WAITING_PHONE)],
            ACTIVATE: [limited(5), refused(409, "本次绑定已取消")],
        })
        self.assertEqual(result, 1)
        self.assertEqual(self.valley.at(ACTIVATE), [0, 5])
        self.assertEqual(self.saved, [])

    def test_wrong_digits_while_the_status_read_is_limited_asks_again_without_crashing(self):
        result = self.pair({
            POLL: [ok(WAITING_PHONE), limited(3), ok(WAITING_PHONE)],
            ACTIVATE: [refused(401, "数字不正确"), ok(CREDENTIALS)],
        })
        self.assertEqual(result, 0)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.device_codes(), {"qa-device"})

    def test_expiry_without_a_rate_limit_still_asks_for_a_new_code(self):
        self.assertIsNone(self.pair({POLL: [ok({"status": "pending"})]}, expires_in=12))
        self.assertIsNone(self.pair({POLL: [limited(4), ok({"status": "expired"})]}))


if __name__ == "__main__":
    unittest.main()
