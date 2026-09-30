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

