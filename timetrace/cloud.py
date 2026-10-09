"""Small Valley HTTP client using only the Python standard library.

Transport rules: https only (plain http is accepted for loopback test
servers), certificate verification by the default SSL context, no redirects
(urllib would replay the Authorization header to the new location), a
timeout on every call and a bounded response size."""
import ipaddress
import json
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

MAX_RESPONSE_BYTES = 1024 * 1024
LOOPBACK_HOSTS = ("localhost",)


class CloudError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: int = 0):
        super().__init__(message)
        self.status = status
        self.code = code


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused: %s" % msg, headers, fp)


def no_redirect_opener() -> Callable:
    """urlopen-compatible callable that treats any 3xx as an error."""
    return urllib.request.build_opener(_RefuseRedirect()).open


def read_bounded(stream: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """At most `limit` bytes; anything longer is an error, not a truncation."""
    data = stream.read(limit + 1) if stream is not None else b""
    if len(data or b"") > limit:
        raise ValueError("response larger than %d bytes" % limit)
    return data or b""


def check_base_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url or "")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("cloud_base_url must be an absolute https URL: %r" % base_url)
    if parts.scheme == "https":
        return base_url
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host in LOOPBACK_HOSTS
        if loopback:
            return base_url
    raise ValueError("cloud_base_url must use https (plain http only for localhost): %r" % base_url)


def _segment(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


class CloudClient:
    def __init__(self, base_url: str, opener: Optional[Callable] = None, timeout: int = 30):
        self.base_url = check_base_url(base_url).rstrip("/")
        self.opener = opener or no_redirect_opener()
        self.timeout = timeout

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                token: Optional[str] = None, raw: Optional[bytes] = None,
                content_type: str = "application/json", timeout: Optional[int] = None) -> Any:
        payload = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
        request = urllib.request.Request(self.base_url + path, data=payload, method=method)
        request.add_header("Accept", "application/json")
        if payload is not None:
            request.add_header("Content-Type", content_type)
        if token:
            # Never replayed by urllib to a redirect target.
            request.add_unredirected_header("Authorization", "Bearer " + token)
        try:
            try:
                with self.opener(request, timeout=timeout or self.timeout) as response:
                    status, raw = response.status, read_bounded(response)
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, read_bounded(exc)
        except ValueError as exc:
            raise CloudError("oversized Valley response") from exc
        if status == 204:
            return None
        if 300 <= status < 400:
            raise CloudError("Valley redirect refused", status=status)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CloudError("invalid Valley response", status=status) from exc
        if not isinstance(envelope, dict):
            raise CloudError("invalid Valley response", status=status)
        if status >= 400 or envelope.get("code") != 0:
            raise CloudError(str(envelope.get("message") or "Valley request failed")[:300], status,
                             _int(envelope.get("code")))
        return envelope.get("data")

    def upload_job_file(self, token: str, job_id: str, name: str, data: bytes) -> Dict[str, Any]:
        """Uploads one file a run produced (multipart field `file`); Valley keeps
        it privately for the job's work item and answers with its id."""
        boundary = "timetrace-" + uuid.uuid4().hex
        filename = "".join(ch for ch in name if ch not in '"\\\r\n') or "file"
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        return self.request("POST", "/runner/jobs/%s/files" % _segment(job_id), token=token,
                            raw=head + data + tail, content_type="multipart/form-data; boundary=" + boundary,
                            timeout=max(self.timeout, 120))

    def create_device_authorization(self, name: str, platform: str, version: str) -> Dict[str, Any]:
        return self.request("POST", "/device-authorizations", {
            "device_name": name, "platform": platform, "client_version": version,
        })

    def poll_device_authorization(self, device_code: str) -> Dict[str, Any]:
        return self.request("POST", "/device-authorizations/token", {"device_code": device_code})

    def activate(self, device_code: str, activation_code: str) -> Dict[str, Any]:
        return self.request("POST", "/device-authorizations/activate", {
            "device_code": device_code, "activation_code": activation_code,
        })

    def create_phone_authorization(self, name: str, platform: str, version: str,
                                   previous_device_code: str = "") -> Dict[str, Any]:
        value = self.request("POST", "/iphone/device-authorizations", {
            "device_name": name, "platform": platform, "client_version": version,
            "previous_device_code": previous_device_code,
        })
        value["pairing_version"] = 2
        return value

    def activate_phone(self, device_code: str, phone_code: str) -> Dict[str, Any]:
        return self.request("POST", "/iphone/device-authorizations/activate", {
            "device_code": device_code, "phone_code": phone_code,
        })

    def revoke_phone(self, refresh_token: str) -> Dict[str, Any]:
        return self.request("POST", "/iphone/runner/revoke", {"refresh_token": refresh_token})

    def refresh(self, refresh_token: str, idempotency_key: str) -> Dict[str, Any]:
        return self.request("POST", "/runner-auth/refresh", {
            "refresh_token": refresh_token, "idempotency_key": idempotency_key,
        })

    def update_inventory(self, token: str, workspaces: list, tools: list,
                         max_parallel: Optional[int] = None,
                         max_parallel_per_tool: Optional[Dict[str, int]] = None,
                         extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """`extras`: the protocol-2 fields (protocol_version, agent_version,
        accepting_local, health, reset_credits)."""
        body = {"workspaces": workspaces, "tools": tools, "workflow_inputs_version": 1}
        for key in ("protocol_version", "agent_version", "accepting_local", "health", "reset_credits"):
            if extras and key in extras:
                body[key] = extras[key]
        if max_parallel is not None:
            body["max_parallel"] = int(max_parallel)
        if max_parallel_per_tool is not None:
            body["max_parallel_per_tool"] = {str(k): int(v) for k, v in max_parallel_per_tool.items()}
        return self.request("PUT", "/runner/inventory", body, token)

    def post_quota_samples(self, token: str, samples: list) -> Dict[str, Any]:
        """Send de-identified quota readings to Valley (dedup by sample_id).
        The payload carries only opaque pool/profile ids — never credentials."""
        return self.request("POST", "/runner/quota/samples", {"samples": samples}, token)

    def claim(self, token: str) -> Optional[Dict[str, Any]]:
        return self.request("POST", "/runner/jobs/claim", {"wait_seconds": 0}, token)

    def append_events(self, token: str, job_id: str, attempt_id: str, epoch: int, events: list) -> Dict[str, Any]:
        return self.request("POST", "/runner/attempts/%s/events" % _segment(attempt_id), {
            "job_id": job_id, "lease_epoch": epoch, "events": events,
        }, token)

    def renew(self, token: str, attempt_id: str, epoch: int) -> Dict[str, Any]:
        return self.request("POST", "/runner/attempts/%s/renew" % _segment(attempt_id), {
            "lease_epoch": epoch,
        }, token)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
