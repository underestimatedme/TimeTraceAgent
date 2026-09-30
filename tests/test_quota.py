import unittest

from timetrace.quota import (
    Window,
    availability,
    default_capabilities,
    make_window,
    merge_capabilities,
    payload_from_reading,
    sample_payload,
)


class QuotaTest(unittest.TestCase):
    def test_unknown_and_multi_window(self):
        self.assertEqual(availability([], 200), "unknown")
        # A window that has already expired is unknown, never treated as full.
        self.assertEqual(availability([Window(100, 100, 50, 150)], 200), "unknown")
        # Weekly exhausted dominates a fresh, available short window.
        windows = [Window(0, 300, 190, 250), Window(100, 900, 190, 250)]
        self.assertEqual(availability(windows, 200), "blocked")

    def test_all_fresh_available(self):
        windows = [Window(20, 300, 190, 250), Window(40, 900, 190, 250)]
        self.assertEqual(availability(windows, 200), "available")

    def test_nil_reading_is_unknown(self):
        windows = [Window(20, 300, 190, 250), Window(None, 900, 190, 250)]
        self.assertEqual(availability(windows, 200), "unknown")

    def test_partial_freshness_is_unknown(self):
        # One fresh, one stale -> not all applicable windows are fresh.
        windows = [Window(10, 300, 190, 250), Window(10, 300, 10, 150)]
        self.assertEqual(availability(windows, 200), "unknown")


class WindowParseTest(unittest.TestCase):
    def test_rejects_out_of_range(self):
        for bad in (-1, 101, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                make_window(bad, None, 100, 200)

    def test_expires_not_after_reset(self):
        # A freshness horizon cannot outlive a trusted reset boundary.
        w = make_window(50, 150, 100, 300)
        self.assertEqual(w.expires_at, 150)

    def test_requires_positive_window(self):
        with self.assertRaises(ValueError):
            make_window(50, None, 200, 200)


class CapabilityTest(unittest.TestCase):
    def test_defaults_all_false(self):
        caps = default_capabilities()
        self.assertEqual(set(caps.values()), {False})
        self.assertFalse(caps["can_enforce_zero_spend"])

    def test_merge_only_known_keys(self):
        caps = merge_capabilities({"can_record": True, "bogus": True})
        self.assertTrue(caps["can_record"])
        self.assertNotIn("bogus", caps)
        # A manual/source claim cannot grant the billing-safety capability.
        self.assertFalse(caps["can_enforce_zero_spend"])


class SamplePayloadTest(unittest.TestCase):
    def test_payload_carries_no_secrets(self):
        w = make_window(30, None, 190, 250)
        payload = sample_payload("sid", "pool-1", "cli-a", "short", "codex", w,
                                 source="runner", confidence="exact")
        self.assertEqual(payload["pool_id"], "pool-1")
        self.assertEqual(payload["used_percent"], 30.0)
        # RFC3339 timestamps, opaque ids only; no tokens/emails/env.
        allowed = {"sample_id", "pool_id", "profile_id", "scope", "kind",
                   "used_percent", "reset_at", "observed_at", "expires_at",
                   "source", "confidence", "limit_id", "window_mins", "pool_authoritative"}
        self.assertEqual(set(payload), allowed)
        self.assertTrue(payload["observed_at"].endswith("Z"))


class ReadingPayloadTest(unittest.TestCase):
    def test_sample_dedup_identity_includes_pool_profile_and_window_duration(self):
        payloads = [payload_from_reading("codex:" + "long_limit_" * 12 + ":primary", "codex", 6,
                    None, minutes, pool, profile, 1000)
                    for pool, profile, minutes in (("pool-a", "profile-a", 300),
                                                   ("pool-b", "profile-a", 300),
                                                   ("pool-a", "profile-b", 300),
                                                   ("pool-a", "profile-a", 10080))]
        self.assertEqual(len({p["sample_id"] for p in payloads}), 4)
        self.assertTrue(all(len(p["sample_id"]) <= 80 for p in payloads))

    def test_colliding_primary_limits_preserve_identity(self):
        import json
        from pathlib import Path
        from timetrace.adapters.codex import parse_rate_limits
        readings = parse_rate_limits(json.loads((Path(__file__).parent / "fixtures/codex_ratelimits.json").read_text()))
        payloads = [payload_from_reading(s.bucket_key, s.tool, s.used_pct, s.reset_at,
                    s.window_mins, "account-pool", "custom-profile", 1788300000) for s in readings]
        identities = {(p.get("limit_id"), p["scope"], p.get("window_mins")) for p in payloads}
        # Slot names become duration scopes so the phone can label the windows.
        self.assertEqual(identities, {("codex:codex", "short", 300),
                                     ("codex:codex", "weekly", 10080),
                                     ("codex:base_model_inference", "weekly", 10080)})
        self.assertTrue(all(p.get("pool_authoritative") is False for p in payloads))

    def test_distinct_subsecond_readings_are_not_tied(self):
        # Two readings in the same second must not collapse to the same
        # observed_at / sample_id (else the server's latest-wins reduce ties).
        p1 = payload_from_reading("codex:weekly", "codex", 100.0, None, 10080, "pool", "prof", 1789438863.502)
        p2 = payload_from_reading("codex:weekly", "codex", 12.0, None, 10080, "pool", "prof", 1789438863.778)
        self.assertNotEqual(p1["observed_at"], p2["observed_at"])
        self.assertNotEqual(p1["sample_id"], p2["sample_id"])
        self.assertRegex(p1["observed_at"], r"\.\d{3}Z$")


class AdapterCapabilityTest(unittest.TestCase):
    def test_adapters_declare_conservative_capabilities(self):
        from timetrace.adapters.claude import ClaudeAdapter
        from timetrace.adapters.codex import CodexAdapter

        from timetrace.billing import StaticBilling

        unverified = StaticBilling(False, "not_logged_in")
        for adapter in (ClaudeAdapter({}, billing=unverified), CodexAdapter({}, billing=unverified)):
            caps = adapter.capabilities()
            self.assertEqual(set(caps), set(default_capabilities()))
            self.assertTrue(caps["can_record"])
            # Billing safety is never declared by the adapter itself; it comes
            # only from a billing verdict (subscription login, no API-key path).
            self.assertFalse(caps["can_enforce_zero_spend"])
            self.assertTrue(adapter.adapter_version)

        # No local login: nothing to read. (Injected so the test never sees the developer's real login.)
        self.assertFalse(ClaudeAdapter({}, billing=unverified, credentials=lambda: {}).capabilities()["can_read_quota"])
        self.assertTrue(CodexAdapter({}, billing=unverified).capabilities()["can_read_quota"])


if __name__ == "__main__":
    unittest.main()


class SemanticScopeTest(unittest.TestCase):
    def test_codex_slot_names_become_duration_scopes(self):
        # Codex reports slots ("primary"/"secondary"); the phone needs a meaning.
        weekly = payload_from_reading("codex:codex:primary", "codex", 11, 2000, 10080, "pool-codex", "codex-default", 1000)
        self.assertEqual(weekly["scope"], "weekly")
        short = payload_from_reading("codex:codex:primary", "codex", 11, 2000, 300, "pool-codex", "codex-default", 1000)
        self.assertEqual(short["scope"], "short")
        secondary = payload_from_reading("codex:codex:secondary", "codex", 11, 2000, 10080, "pool-codex", "codex-default", 1000)
        self.assertEqual(secondary["scope"], "weekly")

    def test_named_scopes_are_kept(self):
        five = payload_from_reading("claude:five_hour", "claude", 41, 2000, 300, "pool-claude", "claude-default", 1000)
        self.assertEqual(five["scope"], "five_hour")
        unknown_duration = payload_from_reading("codex:codex:primary", "codex", 11, 2000, None, "pool-codex", "codex-default", 1000)
        self.assertEqual(unknown_duration["scope"], "primary")

