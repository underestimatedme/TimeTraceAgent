"""Adapters raise can_enforce_zero_spend only from a billing verdict, expose the
verification details, and never hand billing variables to the tool process."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timetrace import billing
from timetrace.adapters import claude, codex
from timetrace.adapters.claude import ClaudeAdapter
from timetrace.adapters.codex import CodexAdapter
from timetrace.billing import StaticBilling
from timetrace.dispatch import adapter_dispatch_problem, adapter_zero_spend_verified
from timetrace.process import run_streaming


class AdapterCapabilityFromBillingTest(unittest.TestCase):
    def test_verified_billing_raises_zero_spend_and_unlocks_dispatch(self):
        for adapter in (ClaudeAdapter({}, billing=StaticBilling(True)), CodexAdapter({}, billing=StaticBilling(True))):
            with self.subTest(adapter=adapter.name):
                self.assertTrue(adapter.capabilities()["can_enforce_zero_spend"])
                self.assertTrue(adapter_zero_spend_verified(adapter))
                self.assertEqual(adapter_dispatch_problem(adapter), "")
                details = adapter.capability_details()
                self.assertEqual(details["unsupported_reason"], "")
                self.assertEqual(details["adapter_version"], adapter.adapter_version)

    def test_unverified_billing_keeps_gate_closed_with_reason(self):
        for adapter in (ClaudeAdapter({}, billing=StaticBilling(False, "not_logged_in")),
                        CodexAdapter({}, billing=StaticBilling(False, "api_key_fallback_in_env:OPENAI_API_KEY"))):
            with self.subTest(adapter=adapter.name):
                caps = adapter.capabilities()
                self.assertFalse(caps["can_enforce_zero_spend"])
                self.assertTrue(caps["can_dispatch"], "the implemented surface is still declared honestly")
                self.assertEqual(adapter_dispatch_problem(adapter), "billing_unverified")
                details = adapter.capability_details()
                self.assertIn(details["unsupported_reason"], ("not_logged_in", "api_key_fallback_in_env:OPENAI_API_KEY"))
                self.assertEqual(set(details), {"adapter_version", "verified_at", "unsupported_reason", "auth_method", "can_enforce_zero_spend"})

    def test_default_verifier_is_the_real_cached_check(self):
        # Constructed lazily: building an adapter must not shell out.
        with mock.patch.object(billing, "_run_status", side_effect=AssertionError("must not run at construction")):
            self.assertIsInstance(ClaudeAdapter({}).billing, billing.BillingVerifier)
            self.assertIsInstance(CodexAdapter({}).billing, billing.BillingVerifier)


class GateReverifiesTest(unittest.TestCase):
    def test_agent_gate_forces_a_fresh_verdict_before_spawn(self):
        from timetrace.agent import _capability_zero_spend

        calls = []

        class Recording:
            def __init__(self, verified):
                self.verified = verified

            def verdict(self, force=False):
                calls.append(force)
                return billing.BillingVerdict("claude", self.verified, "" if self.verified else "not_logged_in", "x", 1.0)
        good = ClaudeAdapter({}, billing=Recording(True))
        self.assertTrue(_capability_zero_spend(good, {}))
        self.assertIn(True, calls, "the gate must bypass the cache")
        bad = ClaudeAdapter({}, billing=Recording(False))
        self.assertFalse(_capability_zero_spend(bad, {}))

        class Broken:
            def verdict(self, force=False):
                raise RuntimeError("cli exploded")
        self.assertFalse(_capability_zero_spend(ClaudeAdapter({}, billing=Broken()), {}))


class DispatchedEnvironmentTest(unittest.TestCase):
    def test_run_streaming_drops_requested_keys(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TIMETRACE_BILL_TEST": "leak", "TIMETRACE_KEEP": "ok"}):
            code, lines = run_streaming([sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('TIMETRACE_')))"],
                                        d, str(Path(d) / "run.log"), drop_env=["TIMETRACE_BILL_TEST"], timeout=10)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["['TIMETRACE_KEEP']"])

    def test_claude_start_and_resume_strip_billing_variables(self):
        seen = []

        def fake_run(cmd, cwd, log_file, env=None, timeout=None, cancel_event=None, drop_env=None):
            seen.append(tuple(drop_env or ()))
            return 0, []
        adapter = ClaudeAdapter({}, billing=StaticBilling(True))
        with mock.patch.object(claude, "run_streaming", fake_run):
            adapter.start("p", "/tmp", "sess", "/tmp/timetrace-test.log")
            adapter.resume("p", "/tmp", "sess", "/tmp/timetrace-test.log")
        self.assertEqual(seen, [billing.billing_env_keys("claude")] * 2)
        self.assertIn("ANTHROPIC_API_KEY", seen[0])

    def test_codex_start_and_resume_strip_billing_variables(self):
        seen = []

        def fake_run(cmd, cwd, log_file, env=None, timeout=None, cancel_event=None, drop_env=None):
            seen.append(tuple(drop_env or ()))
            return 0, []
        adapter = CodexAdapter({}, billing=StaticBilling(True))
        with mock.patch.object(codex, "run_streaming", fake_run):
            adapter.start("p", "/tmp", "sess", "/tmp/timetrace-test.log")
            adapter.resume("p", "/tmp", "sess", "/tmp/timetrace-test.log")
        self.assertEqual(seen, [billing.billing_env_keys("codex")] * 2)
        self.assertIn("OPENAI_API_KEY", seen[0])


if __name__ == "__main__":
    unittest.main()
