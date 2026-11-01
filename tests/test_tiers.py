import base64
import json
import tempfile
import unittest
from pathlib import Path

from timetrace import tiers


def jwt_with(claims):
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "hdr." + seg + ".sig"


class TierTest(unittest.TestCase):
    def test_codex_plan_from_id_token(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "auth.json"
            p.write_text(json.dumps({"tokens": {"id_token": jwt_with({"https://api.openai.com/auth": {"chatgpt_plan_type": "Plus"}})}}))
            self.assertEqual(tiers.codex_plan_tier(p), "plus")

    def test_codex_missing_or_malformed_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(tiers.codex_plan_tier(Path(d) / "missing.json"))
            p = Path(d) / "auth.json"
            p.write_text(json.dumps({"tokens": {"id_token": "not-a-jwt"}}))
            self.assertIsNone(tiers.codex_plan_tier(p))
            p.write_text("{not json")
            self.assertIsNone(tiers.codex_plan_tier(p))

    def test_claude_subscription_type_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".credentials.json"
            p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", "subscriptionType": "max"}}))
            self.assertEqual(tiers.claude_plan_tier(p), "max")
            self.assertIsNone(tiers.claude_plan_tier(Path(d) / "nope.json", keychain=lambda: None))

    def test_claude_falls_back_to_keychain_when_file_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            secret = json.dumps({"claudeAiOauth": {"accessToken": "secret", "subscriptionType": "pro"}})
            self.assertEqual(tiers.claude_plan_tier(Path(d) / "nope.json", keychain=lambda: secret), "pro")
            self.assertIsNone(tiers.claude_plan_tier(Path(d) / "nope.json", keychain=lambda: "{broken"))

    def test_tier_strings_never_carry_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".credentials.json"
            p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-secret", "subscriptionType": " Max "}}))
            self.assertEqual(tiers.claude_plan_tier(p), "max")


class AccountKeyTest(unittest.TestCase):
    def test_codex_account_key_is_an_opaque_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "auth.json"
            p.write_text(json.dumps({"tokens": {"account_id": "aece85c3-d92e-4f53"}}))
            key = tiers.codex_account_key(p)
            self.assertEqual(len(key), 8)
            self.assertNotIn("aece", key)
            self.assertEqual(key, tiers.codex_account_key(p))
            self.assertIsNone(tiers.codex_account_key(Path(d) / "none.json"))

    def test_claude_account_key_from_claude_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".claude.json"
            p.write_text(json.dumps({"oauthAccount": {"accountUuid": "u-1", "emailAddress": "a@b.c"}}))
            self.assertEqual(len(tiers.claude_account_key(p)), 8)
            other = Path(d) / "other.json"
            other.write_text(json.dumps({"oauthAccount": {"accountUuid": "u-2"}}))
            self.assertNotEqual(tiers.claude_account_key(p), tiers.claude_account_key(other))


class ClaudeKeychainTest(unittest.TestCase):
    """The Keychain may hold several `Claude Code-credentials` items (one per account).
    The one Claude Code is logged in with is the item named after the login user."""

    GOOD = json.dumps({"claudeAiOauth": {"accessToken": "tok", "refreshToken": "ref", "expiresAt": 4102444800000, "subscriptionType": "max"}})
    STALE = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0, "subscriptionType": "pro"}})

    def fake_security(self, items, calls=None):
        """`items` maps account -> secret; a lookup without -a answers like Keychain does: the first item."""
        class Result:
            def __init__(self, code, out=""):
                self.returncode, self.stdout, self.stderr = code, out, ""

        def run(command, **kwargs):
            if calls is not None:
                calls.append(command)
            account = command[command.index("-a") + 1] if "-a" in command else None
            if account is None:
                return Result(0, next(iter(items.values()))) if items else Result(44)
            return Result(0, items[account]) if account in items else Result(44)
        return run

    def test_prefers_the_item_named_after_the_login_user_over_a_stale_first_item(self):
        import unittest.mock as mock
        items = {"root": self.STALE, "joeyserver": self.GOOD}  # `root` is what an unscoped lookup returns first
        with mock.patch.object(tiers.subprocess, "run", self.fake_security(items)), \
                mock.patch.object(tiers, "_login_name", lambda: "joeyserver"):
            oauth = tiers.claude_oauth(Path("/nonexistent"))
        self.assertEqual(oauth.get("accessToken"), "tok")
        self.assertEqual(oauth.get("subscriptionType"), "max")

    def test_falls_back_to_an_unscoped_lookup_when_no_item_has_the_user_name(self):
        import unittest.mock as mock
        with mock.patch.object(tiers.subprocess, "run", self.fake_security({"someone": self.GOOD})), \
                mock.patch.object(tiers, "_login_name", lambda: "joeyserver"):
            self.assertEqual(tiers.claude_oauth(Path("/nonexistent")).get("accessToken"), "tok")

    def test_when_every_item_is_blank_the_blank_one_still_gives_the_tier(self):
        import unittest.mock as mock
        with mock.patch.object(tiers.subprocess, "run", self.fake_security({"root": self.STALE})), \
                mock.patch.object(tiers, "_login_name", lambda: "joeyserver"):
            oauth = tiers.claude_oauth(Path("/nonexistent"))
        self.assertFalse(oauth.get("accessToken"))
        self.assertEqual(oauth.get("subscriptionType"), "pro")

    def test_asks_for_the_secret_only_with_the_user_scoped_command_first(self):
        import unittest.mock as mock
        calls = []
        with mock.patch.object(tiers.subprocess, "run", self.fake_security({"joeyserver": self.GOOD}, calls)), \
                mock.patch.object(tiers, "_login_name", lambda: "joeyserver"):
            tiers.claude_oauth(Path("/nonexistent"))
        self.assertEqual(calls[0][:6], ["security", "find-generic-password", "-s", tiers.CLAUDE_KEYCHAIN_SERVICE, "-a", "joeyserver"])
        self.assertEqual(len(calls), 1)  # a usable token stops the search
