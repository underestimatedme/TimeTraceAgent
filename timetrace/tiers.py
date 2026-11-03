"""Subscription tier read from the tools' own local logins.

Only the tier string leaves this module. Tokens are parsed for one claim and
discarded; nothing here logs, returns, or raises with credential material.
"""
import base64
import getpass
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _jwt_claims(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _parse(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _load(path: Path) -> Dict[str, Any]:
    try:
        return _parse(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text[:40] or None


def keychain_secret(service: str = CLAUDE_KEYCHAIN_SERVICE, account: Optional[str] = None) -> Optional[str]:
    """macOS login-keychain item for `service` (and `account`, when given), or None.
    Without an account Keychain answers with whichever item matches first, which is
    not necessarily the one in use. The secret is handed straight to the caller's
    parser and never logged."""
    command = ["security", "find-generic-password", "-s", service] + (["-a", account] if account else []) + ["-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _login_name() -> Optional[str]:
    try:
        return getpass.getuser() or None
    except (KeyError, OSError):
        return None


def _has_token(secret: str) -> bool:
    oauth = _parse(secret).get("claudeAiOauth")
    return isinstance(oauth, dict) and bool(oauth.get("accessToken"))


def claude_keychain(service: str = CLAUDE_KEYCHAIN_SERVICE) -> Optional[str]:
    """The Claude Code login item. Claude Code files it under the login user's name, but
    other items with the same service can exist (a stale one left by a tool run as root, say)
    and an unscoped lookup may return one of those with empty tokens. So: the user's own
    item first, then any item; the first one that carries an access token wins, and a blank
    one is returned only when nothing better exists (it still names the plan)."""
    blank: Optional[str] = None
    user = _login_name()
    for account in ((user, None) if user else (None,)):
        secret = keychain_secret(service, account)
        if not secret:
            continue
        if _has_token(secret):
            return secret
        blank = blank or secret
    return blank


def claude_oauth(credentials_path: Path, keychain: Callable[[], Optional[str]] = claude_keychain) -> Dict[str, Any]:
    """The `claudeAiOauth` object from the credentials file, else from the
    Keychain item Claude Code uses on macOS. Empty dict when neither exists."""
    oauth = _load(credentials_path).get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = _parse(keychain()).get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else {}


def codex_plan_tier(auth_path: Path) -> Optional[str]:
    """`chatgpt_plan_type` from the ID token in ~/.codex/auth.json."""
    tokens = _load(auth_path).get("tokens") or {}
    claims = _jwt_claims(str(tokens.get("id_token") or "")) if isinstance(tokens, dict) else {}
    auth = claims.get("https://api.openai.com/auth") or {}
    return _clean(auth.get("chatgpt_plan_type")) if isinstance(auth, dict) else None


def claude_plan_tier(credentials_path: Path, keychain: Callable[[], Optional[str]] = claude_keychain) -> Optional[str]:
    """`subscriptionType` from Claude Code's credentials (file, then Keychain)."""
    return _clean(claude_oauth(credentials_path, keychain).get("subscriptionType"))


def _digest(value: Any, length: int = 8) -> Optional[str]:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length] if text else None


def codex_account_key(auth_path: Path, digest_length: int = 8) -> Optional[str]:
    """Opaque 8-hex digest of the ChatGPT account the Codex login uses. Two
    computers on one account share a quota pool; different accounts do not."""
    tokens = _load(auth_path).get("tokens") or {}
    if not isinstance(tokens, dict):
        return None
    account = tokens.get("account_id")
    if not account:
        claims = _jwt_claims(str(tokens.get("id_token") or ""))
        auth = claims.get("https://api.openai.com/auth") or {}
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return _digest(account, digest_length)


def claude_account_key(claude_json: Path, digest_length: int = 8) -> Optional[str]:
    """Opaque digest of the Claude account (`oauthAccount.accountUuid` in ~/.claude.json)."""
    account = _load(claude_json).get("oauthAccount") or {}
    return _digest(account.get("accountUuid"), digest_length) if isinstance(account, dict) else None

