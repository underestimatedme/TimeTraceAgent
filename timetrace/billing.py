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

The ACP agents follow the same rule. Gemini CLI is gated hermetically: an
allowlisted child environment, no .env or system settings file anywhere the
CLI could read one, exact user settings (personal Google login, AI credits
"never"), no billing keys in workspace settings, and the audited CLI version.
Cursor has no signal we can check, so it stays unverified and is never
dispatched.
"""
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# Environment variables through which each CLI could bill an API key or route to
# a metered endpoint. Removing them from the dispatched process closes the
# fallback even if the shell that started the runner had them.
BILLING_ENV_KEYS: Dict[str, Tuple[str, ...]] = {
    "claude": (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
    # Gemini CLI: an API key, Vertex AI, a service account or a Cloud project each moves a run off the personal
    # Google login onto metered or organisation billing; the base URLs reroute it to another endpoint.
    "gemini": (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID",
        "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL",
    ),
    "cursor": ("CURSOR_API_KEY",),
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


def _whole_json_object(output: str) -> Optional[dict]:
    """The output as one JSON object, or None: no text before or after it."""
    try:
        value = json.loads(output.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


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
        # `claude auth status` exits 1 when nobody is logged in and still prints its JSON. That exact answer (exit 1,
        # the whole output one JSON object, loggedIn strictly false) is a classification; any other non-success
        # exit stays "cannot verify". Neither can ever verify: only exit 0 reaches the checks below.
        if code == 1:
            signed_out = _whole_json_object(output)
            if signed_out is not None and signed_out.get("loggedIn") is False:
                return _unverified("claude", "not_logged_in", str(signed_out.get("authMethod") or ""), now)
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
    if code == 0 and "logged in using chatgpt" in text:
        return BillingVerdict(provider="codex", verified=True, reason="", auth_method="chatgpt", verified_at=now)
    if "api key" in text:
        return _unverified("codex", "auth_method_not_subscription:api_key", "api_key", now)
    if "not logged in" in text:
        return _unverified("codex", "not_logged_in", mode, now)
    if code != 0:
        # A failing status command that did not say "Not logged in" proves nothing about the login.
        return _unverified("codex", "auth_status_unavailable:exit_%d" % code, mode, now)
    return _unverified("codex", "auth_status_unparseable", mode, now)


# ---- Gemini CLI ---------------------------------------------------------------------------------------------------
# Gemini CLI has no read-only "auth status" command, and modelling how it resolves its configuration proved fragile
# (settings layers, .env discovery, path overrides, endpoint variables). The gate is therefore hermetic and allow-only:
# the child gets an allowlisted environment, and a run is verified only when the few files that decide its billing
# exist exactly as required and nothing else that could change them exists at all. Anything else is refused with a
# precise reason. Paths and keys follow Gemini CLI v0.60.0; no real Gemini CLI was run on this machine.
GEMINI_PERSONAL_LOGIN = "oauth-personal"
GEMINI_OVERAGE_OFF = "never"
GEMINI_AUDITED_MINOR = (0, 60)       # the CLI version these rules were written against: 0.60.x only
GEMINI_VERSION_TIMEOUT_SECONDS = 15.0
# The system settings file of each platform; its system-defaults.json sits next to it. Existence alone refuses.
GEMINI_SYSTEM_SETTINGS = {"darwin": "/Library/Application Support/GeminiCli/settings.json",
                          "linux": "/etc/gemini-cli/settings.json"}
# The only variables the Gemini child receives, from the run's one environment snapshot:
#   PATH          finding `node` (an npm-installed CLI is a node script) and the commands the model runs (git, ...)
#   HOME          where the CLI keeps its login and user settings - exactly the ones verified here
#   USER, LOGNAME the account name tools such as git fall back to
#   SHELL         the shell the CLI's command tool starts
#   LANG, LC_*    text encoding of the CLI's and the tools' output
#   TERM          output formatting of the tools the model runs
#   TMPDIR        the per-user temporary directory macOS assigns (node's os.tmpdir())
#   TZ            timestamps
# Everything else stays out by construction: API keys, Vertex / Cloud project settings, endpoint overrides such as
# CODE_ASSIST_ENDPOINT, GEMINI_CLI_HOME and the system-settings path variables, proxies and extra CA certificates (a
# network that needs them fails closed: the run cannot connect, so it cannot spend).
GEMINI_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")
GEMINI_ENV_ALLOWED_PREFIXES = ("LC_",)
# The top-level keys a workspace's .gemini/settings.json must not set (auth, billing, environment handling).
GEMINI_WORKSPACE_FORBIDDEN_KEYS = ("security", "billing", "advanced")
_GEMINI_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def gemini_child_env(snapshot: Mapping[str, str]) -> Dict[str, str]:
    """The Gemini child's whole environment: the allowlisted variables of the run's snapshot, nothing else."""
    return {key: value for key, value in snapshot.items()
            if key in GEMINI_ENV_ALLOWLIST or key.startswith(GEMINI_ENV_ALLOWED_PREFIXES)}


def gemini_home(env: Mapping[str, str]) -> Tuple[Optional[Path], str]:
    """The home the child will use: the snapshot's HOME (os.homedir() reads the same variable), or why not."""
    home = env.get("HOME")
    if not home:
        return None, "home_unset"
    if not os.path.isabs(home):
        return None, "home_not_absolute"
    return Path(home), ""


def _shown(path: Path, home: Path) -> str:
    """A path for a reason string, with the home shown as ~ (reasons reach doctor output and run errors)."""
    text = str(path)
    return "~" + text[len(str(home)):] if text == str(home) or text.startswith(str(home) + os.sep) else text


def _dotenv_candidates(start: Optional[str], home: Path) -> List[Path]:
    """Every .env file Gemini CLI could load: each `.gemini/.env` and `.env` from the run directory up to the
    filesystem root, and the home's two. Their content is never read: existence alone refuses."""
    files: List[Path] = []
    if start is not None:
        current = Path(os.path.abspath(start))
        while True:
            files += [current / ".gemini" / ".env", current / ".env"]
            if current.parent == current:
                break
            current = current.parent
    files += [home / ".gemini" / ".env", home / ".env"]
    return list(dict.fromkeys(files))


def _system_files(system_settings_path: Optional[str], system_defaults_path: Optional[str]) -> List[Path]:
    settings = [Path(p) for p in GEMINI_SYSTEM_SETTINGS.values()]
    if system_settings_path:
        settings.append(Path(system_settings_path))
    files = settings + [p.parent / "system-defaults.json" for p in settings]
    if system_defaults_path:
        files.append(Path(system_defaults_path))
    return list(dict.fromkeys(files))


def _read_json_object(path: Path) -> Optional[Dict[str, object]]:
    """The file as one strict JSON object, or None (unreadable, not JSON, not an object)."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _user_settings_problem(home: Path) -> str:
    """~/.gemini/settings.json must exist, be strict JSON and say exactly: security.auth.selectedType ==
    "oauth-personal" (the layout v0.60.0 reads; the old top-level selectedAuthType does not count) with no other
    auth setting, and billing.overageStrategy == "never"."""
    path = home / ".gemini" / "settings.json"
    if not path.exists():
        return "settings_missing"
    settings = _read_json_object(path)
    if settings is None:
        return "settings_unreadable"
    security = settings.get("security") if isinstance(settings.get("security"), dict) else {}
    auth = security.get("auth") if isinstance(security.get("auth"), dict) else {}
    selected = auth.get("selectedType")
    if selected is None:
        return "auth_type_unset"
    if selected != GEMINI_PERSONAL_LOGIN:
        return "auth_method_not_subscription:%s" % selected
    extra = sorted(key for key in auth if key != "selectedType")
    if extra:
        return "auth_override_present:security.auth.%s" % extra[0]
    billing = settings.get("billing") if isinstance(settings.get("billing"), dict) else {}
    if "overageStrategy" not in billing:
        return "billing_overage_unset"
    if billing["overageStrategy"] != GEMINI_OVERAGE_OFF:
        return "billing_overage_enabled:%s" % billing["overageStrategy"]
    return ""


def _workspace_settings_problem(cwd: str, home: Path) -> str:
    """Each .gemini/settings.json from the run directory up to its repository root (the first directory holding
    .git), never above the home (whose file is the user's): none may set security, billing or advanced."""
    current = Path(os.path.abspath(cwd))
    while current != home:
        path = current / ".gemini" / "settings.json"
        if path.exists():
            settings = _read_json_object(path)
            if settings is None:
                return "workspace_settings_unreadable"
            for key in GEMINI_WORKSPACE_FORBIDDEN_KEYS:
                if key in settings:
                    return "workspace_settings_override:%s" % key
        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent
    return ""


def _version_problem(binary: str, env: Mapping[str, str], cwd: str, run: Callable = subprocess.run) -> str:
    """`<bin> --version` (it prints the version and exits: no model call), with the child's hermetic environment;
    only a plain 0.60.x is accepted."""
    try:
        proc = run([binary, "--version"], capture_output=True, text=True, timeout=GEMINI_VERSION_TIMEOUT_SECONDS,
                   stdin=subprocess.DEVNULL, env=gemini_child_env(env), cwd=cwd)
    except Exception as exc:   # missing binary, timeout, permission: all mean "not the audited CLI"
        return "gemini_version_unaudited:" + type(exc).__name__
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    match = _GEMINI_VERSION.fullmatch(lines[-1]) if proc.returncode == 0 and lines else None
    if match is None:
        return "gemini_version_unaudited:unparsable"
    if (int(match.group(1)), int(match.group(2))) != GEMINI_AUDITED_MINOR:
        return "gemini_version_unaudited:%s.%s.%s" % match.groups()
    return ""


def gemini_billing_problem(env: Mapping[str, str], cwd: Optional[str] = None, binary: str = "gemini",
                           system_settings_path: Optional[str] = None, system_defaults_path: Optional[str] = None,
                           run: Callable = subprocess.run) -> str:
    """Why a Gemini run is not verified zero-spend, "" when every allow condition holds. `env` is the run's one
    environment snapshot (the child gets its allowlisted part); without `cwd` it judges what every run shares (the
    verifier), with it also the run's own directory (right before the spawn)."""
    home, reason = gemini_home(env)
    if home is None:
        return reason
    for path in _system_files(system_settings_path, system_defaults_path):
        if path.exists():
            return "gemini_system_settings_present:%s" % path
    for path in _dotenv_candidates(cwd, home):
        if path.exists():
            return "gemini_dotenv_present:%s" % _shown(path, home)
    reason = _user_settings_problem(home)
    if reason:
        return reason
    if cwd is not None:
        reason = _workspace_settings_problem(cwd, home)
        if reason:
            return reason
    return _version_problem(binary, env, cwd if cwd is not None else str(home), run)


def verify_gemini(cfg: Mapping[str, object], env: Optional[Mapping[str, str]] = None,
                  system_settings_path: Optional[str] = None, system_defaults_path: Optional[str] = None,
                  now: Optional[float] = None, run: Callable = subprocess.run) -> BillingVerdict:
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    reason = gemini_billing_problem(env, None, str(cfg.get("bin") or "gemini"), system_settings_path,
                                    system_defaults_path, run)
    if reason:
        return _unverified("gemini", reason, "", now)
    return BillingVerdict(provider="gemini", verified=True, reason="", auth_method=GEMINI_PERSONAL_LOGIN,
                          verified_at=now)


def gemini_workspace_problem(cwd: str, env: Mapping[str, str], binary: str = "gemini",
                             system_settings_path: Optional[str] = None,
                             system_defaults_path: Optional[str] = None, run: Callable = subprocess.run) -> str:
    """The whole check again for one run, right before it is spawned, against the snapshot whose allowlisted part
    the child gets, now with the run's directory: every .env on the way up from there and its workspace settings.
    "" to go ahead."""
    if not isinstance(env, Mapping):
        raise TypeError("env must be the run's environment snapshot")
    return gemini_billing_problem(env, cwd, binary, system_settings_path, system_defaults_path, run)


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


def gemini_verifier(cfg: Mapping[str, object]) -> BillingVerifier:
    return BillingVerifier(lambda now: verify_gemini(cfg, now=now))
