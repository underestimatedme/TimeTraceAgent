import json
import unittest

from timetrace.redact import redact

# Synthetic values in the shape of real credentials. None of them is live.
AWS_ID = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ALIYUN_ID = "LTAI" + "5tQ8xYzAbCdEfGhIjKlM"
GH_CLASSIC = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GH_OAUTH = "gho_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
GH_FINE = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
OPENAI = "sk-proj-" + "Abc123Def456Ghi789Jkl012Mno345Pqr678"
ANTHROPIC = "sk-ant-api03-" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCdEf"
ANTHROPIC_OAUTH = "sk-ant-oat01-" + "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"
SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
GOOGLE = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
       "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
       "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y\n"
       "Z5a6B7c8D9e0F1g2H3i4J5k6L7m8N9o0P1q2R3s4T5u6V7w8X9y0Z1a2B3c4D5e6\n"
       "-----END RSA PRIVATE KEY-----")

ORDINARY_GIT = """\
commit 9fceb02d0ae598e95dc970b74767f19372d61af8
Merge: 1a2b3c4 5d6e7f8
Author: Alex Chen <alex@example.com>
Date:   Thu Sep 25 10:00:00 2026 +0800

    tokenizer: handle unicode input
    secret sauce: none

diff --git a/src/auth/token_store.py b/src/auth/token_store.py
index 83db48f..bf269f4 100644
--- a/src/auth/token_store.py
+++ b/src/auth/token_store.py
@@ -10,7 +10,9 @@ class TokenStore:
-    def get_token(self):
+    def get_token(self, refresh=False):
+        token = self._load_token()
+        password: Optional[str] = None
+        if password is None:
+        max_tokens = 1024
+        api_key = os.environ.get("API_KEY")
+        token == other_token
         return self.token
 3 files changed, 12 insertions(+), 4 deletions(-)
sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
uuid 123e4567-e89b-12d3-a456-426614174000
remote: git@github.com:org/repo.git  https://github.com/org/repo/pull/12
Bearer tokens are rotated hourly.
{"type":"result","usage":{"input_tokens":1234,"output_tokens":56,"cache_read_input_tokens":0}}
PWD=/Users/alex/src/project
password: ${DB_PASSWORD}
token: <your token here>
"""


class RedactShapesTest(unittest.TestCase):
    def assertMasked(self, secret, text=None):
        text = text if text is not None else "value %s here" % secret
        out = redact(text)
        self.assertNotIn(secret, out)
        self.assertIn("REDACTED", out)
        return out

    def test_provider_token_shapes(self):
        for secret in (AWS_ID, ALIYUN_ID, GH_CLASSIC, GH_OAUTH, GH_FINE, OPENAI, ANTHROPIC,
                       ANTHROPIC_OAUTH, SLACK, GOOGLE, JWT):
            with self.subTest(secret=secret[:12]):
                out = self.assertMasked(secret)
                self.assertTrue(out.startswith("value "), out)
                self.assertTrue(out.endswith(" here"), out)

    def test_pem_private_key_block(self):
        out = self.assertMasked(PEM, "key:\n%s\nafter" % PEM)
        self.assertIn("after", out)
        self.assertNotIn("MIIEow", out)

    def test_pem_block_cut_by_the_tail_is_still_masked(self):
        begin_only = PEM.rsplit("\n", 1)[0]  # the tail ended before END
        self.assertNotIn("MIIEow", redact("x\n" + begin_only))
        end_only = PEM.split("\n", 1)[1]  # the tail started after BEGIN
        out = redact(end_only + "\nafter")
        self.assertNotIn("Z5a6B7c8", out)
        self.assertIn("after", out)

    def test_pem_inside_json_escaped_output(self):
        escaped = json.dumps({"content": PEM})
        self.assertNotIn("MIIEow", redact(escaped))

    def test_secret_named_assignments(self):
        cases = [
            "export AWS_SECRET_ACCESS_KEY=%s" % AWS_SECRET,
            "aws_secret_access_key = %s" % AWS_SECRET,
            'DB_PASSWORD="hunter2-hunter2"',
            "password: correct-horse-9",
            '{"access_token": "abc123def456ghi789"}',
            '"refresh_token":"r-0123456789abcdef"',
            "AccessKeySecret: %s" % AWS_SECRET,
            "client_secret='s3cr3t-value-xyz'",
            "+GITHUB_TOKEN=abcdef0123456789",
            "apiKey: 'k-1234567890'",
        ]
        for line in cases:
            with self.subTest(line=line):
                out = redact(line)
                self.assertIn("REDACTED", out)
                for fragment in ("hunter2", "correct-horse", "abc123def456", "r-0123", AWS_SECRET,
                                 "s3cr3t", "abcdef0123456789", "k-1234567890"):
                    self.assertNotIn(fragment, out)

    def test_assignment_keeps_the_key_name(self):
        self.assertEqual(redact("API_KEY=abc123def456"), "API_KEY=[REDACTED]")
        self.assertEqual(redact('"token": "abc123def456"'), '"token": "[REDACTED]"')

    def test_json_escaped_assignment(self):
        out = redact(json.dumps({"text": "API_KEY=abc123def456\nNEXT=1"}))
        self.assertNotIn("abc123def456", out)
        self.assertIn("NEXT=1", out)
        out = redact(json.dumps({"text": json.dumps({"token": "abc123def456"})}))
        self.assertNotIn("abc123def456", out)

    def test_bearer_and_basic_authorization(self):
        out = self.assertMasked("abcdefghijklmnop0123456789", "Authorization: Bearer abcdefghijklmnop0123456789")
        self.assertIn("Bearer", out)
        self.assertMasked("dXNlcjpwYXNzd29yZA==", "Authorization: Basic dXNlcjpwYXNzd29yZA==")

    def test_credentials_in_urls(self):
        out = redact("remote: https://alex:s3cretPass@github.com/org/repo.git")
        self.assertNotIn("s3cretPass", out)
        self.assertIn("github.com/org/repo.git", out)
        out = redact("DATABASE_URL=postgres://app:pa55word@db:5432/app")
        self.assertNotIn("pa55word", out)


class RedactNoFalsePositiveTest(unittest.TestCase):
    def test_ordinary_git_output_and_diffs_are_untouched(self):
        self.assertEqual(redact(ORDINARY_GIT), ORDINARY_GIT)

    def test_empty_and_none_like(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")

    def test_plain_prose_and_numbers(self):
        text = "Ran 257 tests in 284.925s\nOK\ntimetrace/7 → awaiting_review\ncoverage: 91%\n"
        self.assertEqual(redact(text), text)

    def test_idempotent(self):
        once = redact("API_KEY=abc123def456 and %s" % GH_CLASSIC)
        self.assertEqual(redact(once), once)


if __name__ == "__main__":
    unittest.main()
