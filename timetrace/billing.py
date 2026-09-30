"""Zero-additional-spend verification for the dispatch gate (R4 billing checks).

The product rule (total plan §Global Constraints): unattended execution and
automatic resume are allowed only when running a task cannot create new
charges. For the two coding CLIs that is true exactly when

1. the CLI is logged in through a flat-rate subscription (Claude.ai plan for
   Claude Code, ChatGPT plan for Codex), not an API key; and
2. no API-key billing path is reachable from the dispatched process: no
   provider key / token / alternative endpoint in the environment, no
   `apiKeyHelper` or key in Claude's settings, no stored key in Codex's auth file.

The check is read-only (`claude auth status`, `codex login status`); it never
starts a model run, so verifying costs nothing. Anything unknown stays
unverified with a machine-readable reason. Verdicts are cached briefly and
re-checked right before a spawn.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

# Environment variables through which each CLI could bill an API key or route to
# a metered endpoint. Removing them from the dispatched process closes the
# fallback even if the shell that started the runner had them.
BILLING_ENV_KEYS: Dict[str, Tuple[str, ...]] = {
    "claude": (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
}

STATUS_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class BillingVerdict:
    provider: str
    verified: bool
    reason: str            # "" when verified; otherwise a stable machine-readable code
    auth_method: str       # what the tool reported, for doctor output / logs
    verified_at: float     # epoch seconds of this check


def billing_env_keys(provider: str) -> Tuple[str, ...]:
    if provider in BILLING_ENV_KEYS:
        return BILLING_ENV_KEYS[provider]
    return tuple(key for keys in BILLING_ENV_KEYS.values() for key in keys)


def sanitized_env(provider: str, env: Mapping[str, str]) -> Dict[str, str]:
    """Copy of `env` without any variable that could switch the tool to API billing."""
    drop = set(billing_env_keys(provider))
    return {key: value for key, value in env.items() if key not in drop}


def _run_status(cmd: Sequence[str], timeout: float) -> Tuple[int, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _env_fallback(provider: str, env: Mapping[str, str]) -> str:
    for key in billing_env_keys(provider):
        if env.get(key):
            return "api_key_fallback_in_env:" + key
    return ""


def _unverified(provider: str, reason: str, auth_method: str, now: float) -> BillingVerdict:
    return BillingVerdict(provider=provider, verified=False, reason=reason, auth_method=auth_method, verified_at=now)


def verify_claude(cfg: Mapping[str, object], run: Callable[[Sequence[str], float], Tuple[int, str]] = _run_status,
                  env: Optional[Mapping[str, str]] = None, settings_path: Optional[str] = None,
                  now: Optional[float] = None) -> BillingVerdict:
    now = time.time() if now is None else now
    env = os.environ if env is None else env
    reason = _env_fallback("claude", env)
    if reason:
        return _unverified("claude", reason, "", now)

    settings_file = Path(settings_path or (Path.home() / ".claude" / "settings.json"))
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
    except (OSError, ValueError):
        return _unverified("claude", "settings_unreadable", "", now)
    if not isinstance(settings, dict):
        settings = {}
    if settings.get("apiKeyHelper"):
        return _unverified("claude", "api_key_helper_configured", "", now)
    settings_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    for key in billing_env_keys("claude"):
        if settings_env.get(key):
            return _unverified("claude", "api_key_fallback_in_settings:" + key, "", now)

    try:
        code, output = run([str(cfg.get("bin", "claude")), "auth", "status"], STATUS_TIMEOUT_SECONDS)
    except Exception as exc:  # missing binary, timeout, permission: all mean "cannot verify"
        return _unverified("claude", "auth_status_unavailable:" + type(exc).__name__, "", now)
    if code != 0:
        return _unverified("claude", "auth_status_unavailable:exit_%d" % code, "", now)
    try:
        status = json.loads(output[output.index("{"):]) if "{" in output else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        return _unverified("claude", "auth_status_unparseable", "", now)

    method = str(status.get("authMethod") or "")
    provider_kind = str(status.get("apiProvider") or "")
    plan = str(status.get("subscriptionType") or "")
    if status.get("loggedIn") is not True:
        return _unverified("claude", "not_logged_in", method, now)
    if method != "claude.ai":
        return _unverified("claude", "auth_method_not_subscription:" + (method or "unknown"), method, now)
    if provider_kind != "firstParty":
        return _unverified("claude", "api_provider_not_first_party:" + (provider_kind or "unknown"), method, now)
    if not plan:
        return _unverified("claude", "subscription_unknown", method, now)
    return BillingVerdict(provider="claude", verified=True, reason="", auth_method=method + "/" + plan, verified_at=now)


def verify_codex(cfg: Mapping[str, object], run: Callable[[Sequence[str], float], Tuple[int, str]] = _run_status,
                 env: Optional[Mapping[str, str]] = None, auth_path: Optional[str] = None,
                 now: Optional[float] = None) -> BillingVerdict:
    now = time.time() if now is None else now
    env = os.environ if env is None else env
    reason = _env_fallback("codex", env)
    if reason:
        return _unverified("codex", reason, "", now)

    auth_file = Path(auth_path or (Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "auth.json"))
    auth: Dict[str, object] = {}
    if auth_file.exists():
        try:
            loaded = json.loads(auth_file.read_text(encoding="utf-8"))
            auth = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return _unverified("codex", "auth_file_unreadable", "", now)
    mode = str(auth.get("auth_mode") or "")
    if mode and mode != "chatgpt":
        return _unverified("codex", "auth_method_not_subscription:" + mode, mode, now)
    if auth.get("OPENAI_API_KEY"):
        return _unverified("codex", "api_key_fallback_in_auth_file", mode, now)

    try:
        code, output = run([str(cfg.get("bin", "codex")), "login", "status"], STATUS_TIMEOUT_SECONDS)
    except Exception as exc:
        return _unverified("codex", "auth_status_unavailable:" + type(exc).__name__, mode, now)
    text = output.lower()
    if "logged in using chatgpt" in text:
        return BillingVerdict(provider="codex", verified=True, reason="", auth_method="chatgpt", verified_at=now)
    if "api key" in text:
        return _unverified("codex", "auth_method_not_subscription:api_key", "api_key", now)
    if "not logged in" in text or code != 0:
        return _unverified("codex", "not_logged_in", mode, now)
    return _unverified("codex", "auth_status_unparseable", mode, now)


class BillingVerifier:
    """Cached verdict. `ttl_seconds` keeps `capabilities()` cheap; the dispatch
    gate asks with `force=True` right before spawning."""

    def __init__(self, check: Callable[[float], BillingVerdict], ttl_seconds: float = 300.0,
                 clock: Callable[[], float] = time.time):
        self._check = check
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: Optional[BillingVerdict] = None

    def verdict(self, force: bool = False) -> BillingVerdict:
        now = self._clock()
        if force or self._cached is None or now - self._cached.verified_at >= self._ttl:
            self._cached = self._check(now)
        return self._cached


class StaticBilling:
    """Fixed verdict: unit tests, and explicit management-only deployments."""

    def __init__(self, verified: bool, reason: str = "", provider: str = "static"):
        self._verdict = BillingVerdict(provider=provider, verified=verified, reason="" if verified else (reason or "unverified"),
                                       auth_method="static", verified_at=0.0)

    def verdict(self, force: bool = False) -> BillingVerdict:
        return self._verdict


def claude_verifier(cfg: Mapping[str, object]) -> BillingVerifier:
    return BillingVerifier(lambda now: verify_claude(cfg, now=now))


def codex_verifier(cfg: Mapping[str, object]) -> BillingVerifier:
    return BillingVerifier(lambda now: verify_codex(cfg, now=now))
