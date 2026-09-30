"""Runner credentials stored in the macOS login Keychain."""
import json
import ctypes
import time
import uuid
from typing import Any, Callable, Dict, Optional


class CredentialStore:
    SERVICE = "com.atlaspaces.timetrace.runner"

    def __init__(self, runner: Optional[Callable] = None):
        self.runner = runner

    def save(self, credentials: Dict[str, Any]) -> None:
        # Access tokens are deliberately memory-only. Only the rotating refresh
        # credential and non-secret runner metadata survive process restarts.
        persisted = {key: value for key, value in credentials.items()
                     if key not in ("access_token", "expires_at")}
        secret = json.dumps(persisted, separators=(",", ":"))
        if self.runner is not None:
            self.runner(["security", "add-generic-password", "-U", "-s", self.SERVICE,
                         "-a", "default", "-w"], check=True, capture_output=True,
                        text=True, input=secret)
        else:
            _NativeKeychain(self.SERVICE).save("default", secret)

    def load(self, account: str = "default") -> Optional[Dict[str, Any]]:
        if self.runner is None:
            raw = _NativeKeychain(self.SERVICE).load(account)
            return json.loads(raw) if raw is not None else None
        result = self.runner([
            "security", "find-generic-password", "-s", self.SERVICE, "-a", account, "-w",
        ], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)

    def delete(self, account: str = "default") -> None:
        if self.runner is None:
            _NativeKeychain(self.SERVICE).delete(account)
            return
        self.runner([
            "security", "delete-generic-password", "-s", self.SERVICE, "-a", account,
        ], check=False, capture_output=True, text=True)


class _NativeKeychain:
    """Minimal Security.framework bridge; secrets never appear in process argv."""
    def __init__(self, service: str):
        self.service = service.encode()
        self.api = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    def _find(self, account: str):
        data_len = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        account_bytes = account.encode()
        status = self.api.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(account_bytes), account_bytes,
            ctypes.byref(data_len), ctypes.byref(data), ctypes.byref(item))
        return status, data_len, data, item

    def load(self, account: str) -> Optional[str]:
        status, data_len, data, item = self._find(account)
        if status != 0:
            return None
        try:
            return ctypes.string_at(data, data_len.value).decode("utf-8")
        finally:
            self.api.SecKeychainItemFreeContent(None, data)
            self.core.CFRelease(item)

    def save(self, account: str, secret: str) -> None:
        status, data_len, data, item = self._find(account)
        secret_bytes = secret.encode()
        if status == 0:
            self.api.SecKeychainItemFreeContent(None, data)
            try:
                status = self.api.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret_bytes), secret_bytes)
            finally:
                self.core.CFRelease(item)
        else:
            account_bytes = account.encode()
            status = self.api.SecKeychainAddGenericPassword(
                None, len(self.service), self.service, len(account_bytes), account_bytes,
                len(secret_bytes), secret_bytes, None)
        if status != 0:
            raise RuntimeError("Keychain write failed (%d)" % status)

    def delete(self, account: str) -> None:
        status, data_len, data, item = self._find(account)
        if status != 0:
            return
        self.api.SecKeychainItemFreeContent(None, data)
        try:
            self.api.SecKeychainItemDelete(item)
        finally:
            self.core.CFRelease(item)


class SessionManager:
    def __init__(self, store: CredentialStore, cloud: Any, clock: Callable[[], float] = time.time):
        self.store, self.cloud, self.clock = store, cloud, clock
        self.credentials = store.load()
        self._access_token = str((self.credentials or {}).get("access_token") or "")
        self._expires_at = int((self.credentials or {}).get("expires_at") or 0)

    def token(self) -> str:
        if not self.credentials:
            raise RuntimeError("computer is not paired; run `timetrace cloud login`")
        if not self._access_token or self._expires_at <= int(self.clock()) + 30:
            request_key = str(self.credentials.get("pending_refresh_key") or uuid.uuid4())
            self.credentials["pending_refresh_key"] = request_key
            self.store.save(self.credentials)
            refreshed = self.cloud.refresh(self.credentials["refresh_token"], request_key)
            self._access_token = str(refreshed["access_token"])
            self._expires_at = int(self.clock()) + int(refreshed.get("expires_in") or 900)
            self.store.save(refreshed)
            self.credentials = refreshed
        return self._access_token
