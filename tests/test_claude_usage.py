import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.error import HTTPError

from timetrace.adapters import claude_usage


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ClaudeUsageTest(unittest.TestCase):
    def creds(self, d, expires_ms=4102444800000):
        p = Path(d) / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-secret", "expiresAt": expires_ms,
                                                   "subscriptionType": "max"}}))
        return p

    def test_parses_windows_into_samples(self):
        body = {"five_hour": {"utilization": 12.5, "resets_at": "2026-09-25T10:00:00Z"},
                "seven_day": {"utilization": 40, "resets_at": "2026-09-27T14:00:00Z"}}
        seen = {}

        def opener(request, timeout=0):
            seen["auth"] = request.get_header("Authorization")
            seen["beta"] = request.get_header("Anthropic-beta")
            return FakeResponse(json.dumps(body).encode())

        with tempfile.TemporaryDirectory() as d:
            samples = claude_usage.read_usage(self.creds(d), opener=opener, now=1000, keychain=lambda: None)
        self.assertEqual(seen["auth"], "Bearer tok-secret")
        self.assertEqual(seen["beta"], "oauth-2025-04-20")
        by_key = {s.bucket_key: s for s in samples}
        self.assertEqual(by_key["claude:five_hour"].used_pct, 12.5)
        self.assertEqual(by_key["claude:five_hour"].window_mins, 300)
        self.assertEqual(by_key["claude:seven_day"].window_mins, 10080)
        self.assertEqual(by_key["claude:seven_day"].reset_at, 1790517600)
        self.assertTrue(all(s.source == "live" for s in samples))
        self.assertTrue(by_key["claude:five_hour"].is_representative)

    def test_oversized_body_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            samples = claude_usage.read_usage(self.creds(d), opener=lambda r, timeout=0: FakeResponse(
                b'{"five_hour": {"utilization": 1}' + b" " * (2 * 1024 * 1024) + b"}"),
                                              now=1000, keychain=lambda: None)
        self.assertIsNone(samples)

    def test_default_opener_refuses_redirects_and_keeps_the_token_unredirected(self):
        seen = {}

        def opener(request, timeout=0):
            seen["request"] = request
            return FakeResponse(b"{}")

        with unittest.mock.patch("timetrace.adapters.claude_usage.no_redirect_opener", return_value=opener):
            with tempfile.TemporaryDirectory() as d:
                claude_usage.read_usage(self.creds(d), now=1000, keychain=lambda: None)
        request = seen["request"]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.unredirected_hdrs.get("Authorization"), "Bearer tok-secret")

    def test_token_from_keychain_when_file_is_absent(self):
        secret = json.dumps({"claudeAiOauth": {"accessToken": "kc-secret", "expiresAt": 4102444800000}})
        seen = {}

        def opener(request, timeout=0):
            seen["auth"] = request.get_header("Authorization")
            return FakeResponse(json.dumps({"five_hour": {"utilization": 1, "resets_at": None}}).encode())

        with tempfile.TemporaryDirectory() as d:
            samples = claude_usage.read_usage(Path(d) / "none.json", opener=opener, now=0, keychain=lambda: secret)
        self.assertEqual(seen["auth"], "Bearer kc-secret")
        self.assertEqual(samples[0].used_pct, 1.0)
        self.assertIsNone(samples[0].reset_at)

    def test_missing_credentials_or_401_yields_none_without_leaking(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(claude_usage.read_usage(Path(d) / "none.json", opener=lambda *a, **k: None, now=0, keychain=lambda: None))

            def unauthorized(request, timeout=0):
                raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}"))

            self.assertIsNone(claude_usage.read_usage(self.creds(d), opener=unauthorized, now=0, keychain=lambda: None))

            def boom(request, timeout=0):
                raise OSError("network down")

            self.assertIsNone(claude_usage.read_usage(self.creds(d), opener=boom, now=0, keychain=lambda: None))

    def test_expired_token_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            called = []
            self.assertIsNone(claude_usage.read_usage(self.creds(d, expires_ms=1),
                                                      opener=lambda r, timeout=0: called.append(1), now=10, keychain=lambda: None))
            self.assertEqual(called, [])


class ClaudeAdapterQuotaTest(unittest.TestCase):
    def test_adapter_read_limits_goes_through_the_usage_endpoint(self):
        from timetrace.adapters.claude import ClaudeAdapter
        from timetrace.billing import StaticBilling
        body = {"five_hour": {"utilization": 41.0, "resets_at": "2026-09-25T01:20:00.128092+00:00"},
                "seven_day": {"utilization": 60.0, "resets_at": "2026-09-25T20:00:00.128114+00:00"}}
        adapter = ClaudeAdapter({}, billing=StaticBilling(True, "test"),
                                credentials=lambda: {"accessToken": "tok", "expiresAt": 4102444800000})
        with unittest.mock.patch("timetrace.adapters.claude_usage.no_redirect_opener",
                                 lambda: (lambda request, timeout=0: FakeResponse(json.dumps(body).encode()))):
            samples = adapter.read_limits()
        self.assertEqual([(s.bucket_key, s.used_pct) for s in samples],
                         [("claude:five_hour", 41.0), ("claude:seven_day", 60.0)])
        self.assertTrue(adapter.capabilities()["can_read_quota"])

    def test_utilization_is_always_a_percent(self):
        # The live endpoint reports percent (41.0, 60.0 observed on 2026-09-25); a
        # fraction-scaling heuristic would turn 0.5% into 50% and 1.0 into 1%.
        self.assertEqual(claude_usage._percent(0.5), 0.5)
        self.assertEqual(claude_usage._percent(1.0), 1.0)
        self.assertEqual(claude_usage._percent(100), 100.0)
        self.assertIsNone(claude_usage._percent("n/a"))


class EpochParsingTest(unittest.TestCase):
    def test_tolerates_long_fractions_and_compact_offsets(self):
        self.assertEqual(claude_usage._epoch("2026-09-25T01:20:00.128092+00:00"), 1790299200)
        self.assertEqual(claude_usage._epoch("2026-09-25T01:20:00.1280921+00:00"), 1790299200)  # 7 fractional digits
        self.assertEqual(claude_usage._epoch("2026-09-25T01:20:00+0000"), 1790299200)           # compact offset
        self.assertEqual(claude_usage._epoch("2026-09-25T01:20:00Z"), 1790299200)
        self.assertEqual(claude_usage._epoch(1790299200), 1790299200)
        self.assertIsNone(claude_usage._epoch("soon"))
        self.assertIsNone(claude_usage._epoch(None))

