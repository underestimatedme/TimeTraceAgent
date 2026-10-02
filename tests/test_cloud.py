import json
import unittest

from timetrace.cloud import CloudClient, CloudError


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = json.dumps(body).encode()

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CloudClientTest(unittest.TestCase):
    def test_envelope_and_bearer(self):
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return Response(200, {"code": 0, "message": "ok", "data": {"status": "pending"}})

        client = CloudClient("https://valley.example/timetrace/api/v1", opener=opener)
        data = client.request("POST", "/runner/jobs/claim", {"wait_seconds": 0}, token="secret")
        self.assertEqual(data["status"], "pending")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret")

    def test_nonzero_envelope_raises(self):
        client = CloudClient("https://v", opener=lambda req, timeout: Response(200, {"code": 40900, "message": "conflict"}))
        with self.assertRaises(CloudError):
            client.request("GET", "/x")

    def test_renew_uses_attempt_endpoint(self):
        seen = []
        client = CloudClient("https://v", opener=lambda req, timeout: (seen.append(req) or Response(200, {"code": 0, "data": {}})))
        client.renew("token", "a1", 3)
        self.assertTrue(seen[0].full_url.endswith("/runner/attempts/a1/renew"))

    def test_events_use_attempt_endpoint(self):
        seen = []
        client = CloudClient("https://v", opener=lambda req, timeout: (seen.append(req) or Response(200, {"code": 0, "data": {}})))
        client.append_events("token", "j1", "a1", 3, [{"seq": 1, "type": "running"}])
        self.assertTrue(seen[0].full_url.endswith("/runner/attempts/a1/events"))

    def test_quota_samples_endpoint_and_body(self):
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return Response(200, {"code": 0, "data": {"accepted": 1}})

        client = CloudClient("https://v", opener=opener)
        payload = [{"sample_id": "s1", "pool_id": "pool-1", "used_percent": 20.0}]
        client.post_quota_samples("secrettoken", payload)
        self.assertTrue(seen[0].full_url.endswith("/runner/quota/samples"))
        body = json.loads(seen[0].data.decode())
        self.assertEqual(body, {"samples": payload})
        # The sample body itself must not carry credentials or account emails.
        raw = seen[0].data.decode()
        for leak in ("secrettoken", "refresh", "@"):
            self.assertNotIn(leak, raw)


class InventoryProtocolTwoTest(unittest.TestCase):
    def test_inventory_body_matches_the_protocol_doc(self):
        seen = []
        client = CloudClient("https://v", opener=lambda req, timeout: (seen.append(req) or Response(200, {"code": 0, "data": {}})))
        health = {"claude_login": "ok", "codex_login": "expired", "disk_free_gb": 42.5,
                  "workspaces": [{"id": "ws1", "exists": True, "git": True, "clean": False}],
                  "sleep_prevention": "active", "checked_at": "2026-09-30T12:00:00Z"}
        credits = [{"pool_id": "pool-codex-ab", "tool_profile_id": "codex-default", "status": "unknown"}]
        client.update_inventory("t", [], [], 2, {"claude": 2, "codex": 2},
                                extras={"protocol_version": 2, "agent_version": "0.4.0", "accepting_local": True,
                                        "health": health, "reset_credits": credits, "ignored": 1})
        body = json.loads(seen[0].data.decode())
        self.assertEqual(seen[0].get_method(), "PUT")
        self.assertTrue(seen[0].full_url.endswith("/runner/inventory"))
        self.assertEqual(body["protocol_version"], 2)
        self.assertEqual(body["agent_version"], "0.4.0")
        self.assertIs(body["accepting_local"], True)
        self.assertEqual(body["health"], health)
        self.assertEqual(body["reset_credits"], credits)
        self.assertNotIn("ignored", body)
        self.assertEqual(body["max_parallel_per_tool"], {"claude": 2, "codex": 2})


class CloudTransportHardeningTest(unittest.TestCase):
    def test_refuses_plain_http_except_loopback(self):
        # Bearer and refresh tokens travel on every call.
        for url in ("http://valley.example/api", "ftp://x", "file:///etc/passwd", "valley.example", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CloudClient(url)
        for url in ("https://valley.example/api/v1", "http://127.0.0.1:8080/api", "http://localhost:9/api",
                    "http://[::1]:8080/api"):
            with self.subTest(url=url):
                CloudClient(url)

    def test_oversized_response_is_rejected_without_reading_it_all(self):
        class Huge:
            status = 200
            asked = []

            def read(self, n=-1):
                self.asked.append(n)
                return b"x" * (n if n and n > 0 else 50 * 1024 * 1024)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        huge = Huge()
        client = CloudClient("https://v", opener=lambda req, timeout: huge)
        with self.assertRaises(CloudError):
            client.request("GET", "/x")
        self.assertTrue(huge.asked and all(0 < n <= 1024 * 1024 + 1 for n in huge.asked), huge.asked)

    def test_path_parameters_from_the_server_are_escaped(self):
        seen = []
        client = CloudClient("https://v", opener=lambda req, timeout: (seen.append(req) or Response(200, {"code": 0, "data": {}})))
        client.renew("token", "../../device-authorizations?x=1", 3)
        self.assertTrue(seen[0].full_url.startswith("https://v/runner/attempts/"), seen[0].full_url)
        self.assertNotIn("/../", seen[0].full_url)
        self.assertNotIn("?", seen[0].full_url)

    def test_default_client_does_not_follow_redirects_with_the_token(self):
        import http.server
        import threading
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/api"):
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:%d/stolen" % self.server.server_port)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"code": 0, "data": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CloudClient("http://127.0.0.1:%d/api" % server.server_port, timeout=5)
            with self.assertRaises(CloudError):
                client.request("POST", "/runner/jobs/claim", {"wait_seconds": 0}, token="secret-token")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([h[0] for h in hits], ["/api/runner/jobs/claim"])


if __name__ == "__main__":
    unittest.main()
