"""Zero-additional-spend verification (R4 billing checks).

An adapter may raise `can_enforce_zero_spend` only when the tool is logged in
through a flat-rate subscription AND no API-key billing path can be reached
from the dispatched process. Everything else stays unverified with a reason.
"""
import json
import os
import tempfile
import unittest

from timetrace import billing


CLAUDE_OK = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                        "subscriptionType": "team", "email": "someone@example.com"})


def runner(outputs):
    """Fake subprocess runner: maps the first two argv words to (code, stdout)."""
    calls = []

    def run(cmd, timeout):
        calls.append(list(cmd))
        code, out = outputs.get(" ".join(cmd[1:3]), (1, ""))
        return code, out
    run.calls = calls
    return run


class SanitizedEnvTest(unittest.TestCase):
    def test_drops_only_billing_keys_for_the_provider(self):
        env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
               "CLAUDE_CODE_USE_BEDROCK": "1", "OPENAI_API_KEY": "o", "HOME": "/h"}
        claude_env = billing.sanitized_env("claude", env)
        self.assertEqual(set(claude_env), {"PATH", "HOME", "OPENAI_API_KEY"})
        codex_env = billing.sanitized_env("codex", env)
        self.assertNotIn("OPENAI_API_KEY", codex_env)
        self.assertIn("ANTHROPIC_API_KEY", codex_env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k", "input mapping is not mutated")

    def test_unknown_provider_drops_every_known_billing_key(self):
        env = {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "o", "PATH": "/bin"}
        self.assertEqual(set(billing.sanitized_env("other", env)), {"PATH"})


class ClaudeVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, output=CLAUDE_OK, code=0, env=None, settings=None):
        if settings is not None:
            with open(self.settings, "w", encoding="utf-8") as fh:
                json.dump(settings, fh)
        run = runner({"auth status": (code, output)})
        return billing.verify_claude({"bin": "claude"}, run=run, env=env or {}, settings_path=self.settings, now=1000.0)

    def test_subscription_login_without_api_key_path_is_verified(self):
        verdict = self.verify()
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.reason, "")
        self.assertEqual(verdict.auth_method, "claude.ai/team")
        self.assertEqual(verdict.verified_at, 1000.0)
        self.assertEqual(verdict.provider, "claude")

    def test_api_key_in_environment_blocks_even_when_logged_in(self):
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            with self.subTest(key=key):
                verdict = self.verify(env={key: "x"})
                self.assertFalse(verdict.verified)
                self.assertEqual(verdict.reason, "api_key_fallback_in_env:" + key)

    def test_api_key_helper_or_env_in_settings_blocks(self):
        self.assertEqual(self.verify(settings={"apiKeyHelper": "/bin/echo"}).reason, "api_key_helper_configured")
        self.assertEqual(self.verify(settings={"env": {"ANTHROPIC_API_KEY": "x"}}).reason,
                         "api_key_fallback_in_settings:ANTHROPIC_API_KEY")
        self.assertTrue(self.verify(settings={"model": "opus", "env": {"FOO": "1"}}).verified)

    def test_not_logged_in_or_api_key_login_is_unverified(self):
        self.assertEqual(self.verify(json.dumps({"loggedIn": False})).reason, "not_logged_in")
        api = json.dumps({"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"})
        self.assertEqual(self.verify(api).reason, "auth_method_not_subscription:apiKey")
        third = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock", "subscriptionType": "max"})
        self.assertEqual(self.verify(third).reason, "api_provider_not_first_party:bedrock")
        no_plan = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})
        self.assertEqual(self.verify(no_plan).reason, "subscription_unknown")

    def test_cli_failure_or_garbage_is_unverified_not_an_exception(self):
        self.assertEqual(self.verify("", code=1).reason, "auth_status_unavailable:exit_1")
        self.assertEqual(self.verify("not json").reason, "auth_status_unparseable")

        def boom(cmd, timeout):
            raise OSError("missing binary")
        verdict = billing.verify_claude({"bin": "claude"}, run=boom, env={}, settings_path=self.settings, now=1.0)
        self.assertFalse(verdict.verified)
        self.assertTrue(verdict.reason.startswith("auth_status_unavailable:"))

    def test_status_command_is_invoked_with_configured_binary(self):
        run = runner({"auth status": (0, CLAUDE_OK)})
        billing.verify_claude({"bin": "/opt/claude"}, run=run, env={}, settings_path=self.settings, now=1.0)
        self.assertEqual(run.calls, [["/opt/claude", "auth", "status"]])


class CodexVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = os.path.join(self.tmp.name, "auth.json")

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, output="Logged in using ChatGPT\n", code=0, env=None, auth=None):
        if auth is not None:
            with open(self.auth, "w", encoding="utf-8") as fh:
                json.dump(auth, fh)
        run = runner({"login status": (code, output)})
        return billing.verify_codex({"bin": "codex"}, run=run, env=env or {}, auth_path=self.auth, now=2000.0)

    def test_chatgpt_login_without_api_key_is_verified(self):
        verdict = self.verify(auth={"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}})
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.auth_method, "chatgpt")
        self.assertEqual(verdict.provider, "codex")
        self.assertEqual(verdict.verified_at, 2000.0)

    def test_api_key_login_or_key_in_auth_file_blocks(self):
        self.assertEqual(self.verify("Logged in using an API key\n").reason, "auth_method_not_subscription:api_key")
        self.assertEqual(self.verify(auth={"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-live"}).reason,
                         "api_key_fallback_in_auth_file")
        self.assertEqual(self.verify(auth={"auth_mode": "apikey"}).reason, "auth_method_not_subscription:apikey")

    def test_environment_key_blocks(self):
        self.assertEqual(self.verify(env={"OPENAI_API_KEY": "x"}).reason, "api_key_fallback_in_env:OPENAI_API_KEY")

    def test_not_logged_in_and_cli_failure(self):
        self.assertEqual(self.verify("Not logged in\n", code=1).reason, "not_logged_in")
        self.assertEqual(self.verify("something else", code=0).reason, "auth_status_unparseable")


class VerifierCacheTest(unittest.TestCase):
    def test_caches_within_ttl_and_reverifies_after(self):
        clock = [100.0]
        calls = []

        def check(now):
            calls.append(now)
            return billing.BillingVerdict(provider="claude", verified=True, reason="", auth_method="claude.ai/team", verified_at=now)
        verifier = billing.BillingVerifier(check, ttl_seconds=60, clock=lambda: clock[0])
        self.assertTrue(verifier.verdict().verified)
        self.assertTrue(verifier.verdict().verified)
        self.assertEqual(calls, [100.0])
        clock[0] = 161.0
        verifier.verdict()
        self.assertEqual(calls, [100.0, 161.0])
        verifier.verdict(force=True)
        self.assertEqual(len(calls), 3)

    def test_static_verdict_is_for_tests_and_management_mode(self):
        self.assertFalse(billing.StaticBilling(False, "management_only").verdict().verified)
        self.assertEqual(billing.StaticBilling(False, "management_only").verdict().reason, "management_only")
        self.assertTrue(billing.StaticBilling(True).verdict().verified)


if __name__ == "__main__":
    unittest.main()
