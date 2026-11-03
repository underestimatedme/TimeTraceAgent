import copy
import io
import logging
import pickle
import time
import unittest
from datetime import datetime, timedelta, timezone

from timetrace.mcp_access import ENV_NAME, McpAccess, parse_claim, skip_note

CLOUD = "https://apis.atlaspaces.com/timetrace/api/v1"
LOCAL_CLOUD = "http://localhost:8080/timetrace/api/v1"
SENTINEL = "ttm_SENTINEL_" + "A1b2C3d4E5" * 4


def iso(delta=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat(timespec="seconds").replace("+00:00", "Z")


def claim(url=CLOUD + "/mcp", token=SENTINEL, expires=None):
    return {"job": {"id": "j1"}, "mcp": {"url": url, "token": token, "expires_at": expires or iso()}}


class ParsingTests(unittest.TestCase):
    def test_a_good_claim_gives_an_access_whose_environment_has_the_token(self):
        access = McpAccess.from_claim(claim(), cloud_base_url=CLOUD)
        self.assertIsNotNone(access)
        self.assertEqual(access.env(), {ENV_NAME: SENTINEL})
        self.assertEqual(access.url, CLOUD + "/mcp")
        self.assertGreater(access.expires_at, time.time())

    def test_what_is_not_a_good_claim_gives_nothing(self):
        bad = {
            "no mcp": {"job": {}},
            "mcp is null": {"mcp": None},
            "mcp is a string": {"mcp": "x"},
            "not https": claim(url="http://apis.atlaspaces.com/timetrace/api/v1/mcp"),
            "token without the prefix": claim(token="abc_" + "x" * 40),
            "token with a space": claim(token="ttm_" + "x" * 20 + " " + "y" * 5),
            "token too long": claim(token="ttm_" + "x" * 600),
            "expired": claim(expires=iso(-60)),
            "expiry that is not a time": claim(expires="soon"),
            "url that is not text": {"mcp": {"url": 7, "token": SENTINEL, "expires_at": iso()}},
            "token that is not text": {"mcp": {"url": CLOUD + "/mcp", "token": 7, "expires_at": iso()}},
            "no url": {"mcp": {"token": SENTINEL, "expires_at": iso()}},
        }
        for name, case in bad.items():
            self.assertIsNone(McpAccess.from_claim(case, cloud_base_url=CLOUD), name)

    def test_the_address_is_checked_by_its_parts_not_its_look(self):
        for name, url in {
            "user and password": "https://u:p@apis.atlaspaces.com/timetrace/api/v1/mcp",
            "a user only": "https://u@apis.atlaspaces.com/timetrace/api/v1/mcp",
            "query": CLOUD + "/mcp?token=x",
            "fragment": CLOUD + "/mcp#x",
            "another host": "https://evil.example.com/timetrace/api/v1/mcp",
            "a host that starts like ours": "https://apis.atlaspaces.com.evil.com/timetrace/api/v1/mcp",
            "another port": "https://apis.atlaspaces.com:8443/timetrace/api/v1/mcp",
            "the path does not end in /mcp": CLOUD + "/mcpx",
            "the path is a longer one": CLOUD + "/mcp/oauth",
            "a port that is not one": "https://apis.atlaspaces.com:99999/timetrace/api/v1/mcp",
            "not an address": "not a url",
        }.items():
            self.assertIsNone(McpAccess.from_claim(claim(url=url), cloud_base_url=CLOUD), name)

    def test_the_host_is_compared_without_regard_to_case_and_the_default_port(self):
        self.assertIsNotNone(McpAccess.from_claim(claim(url="https://APIS.atlaspaces.com:443/timetrace/api/v1/mcp"), cloud_base_url=CLOUD))

    def test_plain_http_only_for_a_local_valley_the_person_configured(self):
        local = "http://localhost:8080/timetrace/api/v1/mcp"
        self.assertIsNotNone(McpAccess.from_claim(claim(url=local), cloud_base_url=LOCAL_CLOUD))
        for host in ("localhost", "127.0.0.1", "[::1]"):
            cloud = "http://%s:8080/timetrace/api/v1" % host
            self.assertIsNotNone(McpAccess.from_claim(claim(url="http://%s:8080/timetrace/api/v1/mcp" % host), cloud_base_url=cloud), host)
        # the same address, when the Agent talks to the real cloud: refused, the claim does not get to say it is local
        self.assertIsNone(McpAccess.from_claim(claim(url=local), cloud_base_url=CLOUD))
        # an address that only looks local
        for url in ("http://localhost.evil.com:8080/timetrace/api/v1/mcp", "http://127.0.0.1.evil.com:8080/timetrace/api/v1/mcp",
                    "http://localhost:9090/timetrace/api/v1/mcp", "http://127.0.0.1:8080/timetrace/api/v1/mcp"):
            self.assertIsNone(McpAccess.from_claim(claim(url=url), cloud_base_url=LOCAL_CLOUD), url)

    def test_without_a_configured_cloud_address_nothing_is_believed(self):
        self.assertIsNone(McpAccess.from_claim(claim(), cloud_base_url=None))
        self.assertIsNone(McpAccess.from_claim(claim(), cloud_base_url=""))

    def test_the_reason_names_the_rule_and_never_a_value(self):
        for case in (claim(url="https://u:p@evil.example/mcp"), claim(token="nope" + SENTINEL), claim(url=CLOUD + "/mcp?x=" + SENTINEL),
                     claim(url="https://evil.example/mcp", token=SENTINEL)):
            access, reason = parse_claim(case, cloud_base_url=CLOUD)
            self.assertIsNone(access)
            self.assertTrue(reason)
            for secret in (SENTINEL, "evil.example", "u:p"):
                self.assertNotIn(secret, reason)
                self.assertNotIn(secret, skip_note(reason))


class TheTokenIsInNoTextTests(unittest.TestCase):
    def setUp(self):
        self.access = McpAccess.from_claim(claim(), cloud_base_url=CLOUD)

    def assertNoToken(self, value):
        self.assertNotIn(SENTINEL, value)
        self.assertNotIn(SENTINEL[4:], value)

    def test_repr_str_format_and_fstring(self):
        for text in (repr(self.access), str(self.access), "%s" % self.access, "%r" % self.access, format(self.access), "{}".format(self.access),
                     f"{self.access}", f"{self.access!r}", f"{self.access:>80}"):
            self.assertNoToken(text)
        self.assertIn("redacted", str(self.access))

    def test_a_copy_still_works_and_hides_the_token_too(self):
        for dup in (copy.copy(self.access), copy.deepcopy(self.access)):
            self.assertEqual(dup.env(), {ENV_NAME: SENTINEL})
            for text in (repr(dup), str(dup), format(dup), f"{dup}"):
                self.assertNoToken(text)

    def test_it_cannot_be_pickled(self):
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.assertRaises(TypeError) as caught:
                pickle.dumps(self.access, protocol)
            self.assertNoToken(str(caught.exception))

    def test_it_is_not_in_the_log_or_an_exception_when_the_claim_is_refused(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for case in (claim(url="http://evil.example/mcp"), {"mcp": {"url": CLOUD + "/mcp", "token": SENTINEL, "expires_at": object()}},
                     {"mcp": {"url": CLOUD + "/mcp", "token": SENTINEL}}):
            try:
                access, reason = parse_claim(case, cloud_base_url=CLOUD)
            except Exception as exc:  # a refusal is a return value, not an exception
                self.fail("raised %r" % exc)
            self.assertIsNone(access)
            self.assertNoToken(reason)
        self.assertNoToken(stream.getvalue())

    def test_the_access_is_frozen(self):
        with self.assertRaises(Exception):
            self.access.token = "ttm_other"


if __name__ == "__main__":
    unittest.main()
