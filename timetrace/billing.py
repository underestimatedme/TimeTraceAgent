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

The ACP agents follow the same rule. Gemini CLI is gated hermetically and
allow-only, without running anything: an allowlisted child environment, the
audited CLI version read from its package.json, exact user settings (personal
Google login, AI credits "never", no other key), a personal cached login, and
no .env, workspace Gemini or agent configuration, user extension / command /
skill / policy / agent directory or system configuration anywhere.
Cursor has no signal we can check, so it stays unverified and is never
dispatched.
"""
import json
import os
import re
import shutil
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
# Gemini CLI has no read-only "auth status" command, and modelling how it resolves its configuration proved fragile.
# The gate is hermetic and allow-only: the child gets an allowlisted environment, nothing is executed to verify (no
# `--version`: it loads settings and runs migrations and cleanup before parsing its arguments), and a run is
# verified only when the few files that decide its billing exist exactly as required and no other Gemini or agent
# configuration exists anywhere the CLI could load it. Anything else is refused with a precise reason. Paths are
# those of Gemini CLI v0.60.0 (packages/core/src/config/storage.ts); no real Gemini CLI was run on this machine.
GEMINI_PACKAGE = "@google/gemini-cli"
GEMINI_AUDITED_VERSION = re.compile(r"0\.60\.\d+")   # audited on 0.60.0; its patch releases are accepted
# Storage.getSystemConfigDir(): system settings, system defaults and system policies live here. Existence refuses.
GEMINI_SYSTEM_DIRS = ("/Library/Application Support/GeminiCli", "/etc/gemini-cli")
# ~/.gemini/settings.json must be exactly this document: personal Google login, paid AI credits off. No other key
# (mcpServers, tools, hooks, extensions, context, advanced, model, ui, ...) is allowed, not even cosmetic ones.
GEMINI_USER_SETTINGS = {"security": {"auth": {"selectedType": "oauth-personal"}}, "billing": {"overageStrategy": "never"}}
# What v0.60.0 loads from ~/.gemini besides settings: user commands, skills, policies and agents (Storage), and
# extensions. Existence refuses (extensions: a non-empty directory).
GEMINI_USER_DIRS = ("commands", "skills", "policies", "agents")
# Cached Google login (Storage.getOAuthCredsPath()). code_assist/oauth2.ts isAdcCredentials(): an object without a
# string `type`, or with type "authorized_user", is set on a plain OAuth2 client (the personal login); any other type
# goes through GoogleAuth.fromJSON() (service account, external account, ...).
GEMINI_PERSONAL_CREDENTIAL_TYPES = (None, "authorized_user")
# The only variables the Gemini child receives, from the run's one environment snapshot:
#   PATH          finding `node` (the CLI is a node script) and the commands the model runs (git, ...)
#   HOME          where the CLI keeps its login and user settings - exactly the ones verified here
#   USER, LOGNAME the account name tools such as git fall back to
#   SHELL         the shell the CLI's command tool starts
#   LANG, LC_*    text encoding of the CLI's and the tools' output
#   TERM          output formatting of the tools the model runs
#   TMPDIR        the per-user temporary directory macOS assigns (node's os.tmpdir())
#   TZ            timestamps
# Everything else stays out by construction: API keys, Vertex / Cloud project settings, endpoint overrides such as
# CODE_ASSIST_ENDPOINT, GEMINI_CLI_HOME and the system-settings path variables, GOOGLE_APPLICATION_CREDENTIALS,
# proxies and extra CA certificates (a network that needs them fails closed: the run cannot connect, cannot spend).
GEMINI_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")
GEMINI_ENV_ALLOWED_PREFIXES = ("LC_",)


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


def _reject_constant(name: str) -> object:
    raise ValueError("not strict JSON: " + name)


def _strict_json(path: Path) -> object:
    """The file as strict JSON (no NaN / Infinity); raises OSError / ValueError otherwise."""
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _shown(path: Path, home: Path) -> str:
    """A path for a reason string, with the home shown as ~ (reasons reach doctor output and run errors)."""
    text, prefix = str(path), str(home)
    return "~" + text[len(prefix):] if text == prefix or text.startswith(prefix + os.sep) else text


def gemini_resolve(binary: str, env: Mapping[str, str]) -> Tuple[Optional[str], str]:
    """(the binary the child will be started with, "") or (None, why its version is not the audited one). Read
    statically, nothing is run: the binary found on the snapshot's PATH (or the absolute `bin`), its real path, and
    the nearest package.json above it, which must be @google/gemini-cli at version 0.60.x."""
    found = binary if os.path.isabs(binary) else shutil.which(binary, path=env.get("PATH", ""))
    if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
        return None, "gemini_version_unknown:not_found"
    current = Path(os.path.realpath(found)).parent
    while True:
        manifest = current / "package.json"
        if manifest.is_file():
            try:
                package = _strict_json(manifest)
            except (OSError, ValueError):
                return None, "gemini_version_unknown:unreadable_package_json"
            if not isinstance(package, dict) or package.get("name") != GEMINI_PACKAGE:
                return None, "gemini_version_unknown:not_gemini_cli"
            version = package.get("version")
            if not isinstance(version, str) or not GEMINI_AUDITED_VERSION.fullmatch(version):
                return None, "gemini_version_unaudited:%s" % (version if isinstance(version, str) and
                                                               re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", version) else "?")
            return found, ""
        if current.parent == current:
            return None, "gemini_version_unknown:no_package_json"
        current = current.parent


def _ancestors(start: str) -> List[Path]:
    """`start` and every directory above it, for the path as given and for its real path (so /tmp and /private/tmp
    are both covered)."""
    chains: List[Path] = []
    for begin in (Path(os.path.abspath(start)), Path(os.path.realpath(start))):
        current = begin
        while True:
            chains.append(current)
            if current.parent == current:
                break
            current = current.parent
    return list(dict.fromkeys(chains))


def _tree_problem(start: str, home: Path) -> str:
    """No .env file, no Gemini configuration other than the user's own ~/.gemini, and no agent configuration
    (.agents: skills v0.60.0 loads) in `start` or any directory above it. Existence alone refuses."""
    own = os.path.realpath(home / ".gemini")
    for directory in _ancestors(start):
        for name in (".env", ".agents"):
            if (directory / name).exists():
                return "gemini_dotenv_present:%s" % _shown(directory / name, home) if name == ".env" else \
                    "gemini_agents_config_present:%s" % _shown(directory / name, home)
        gemini = directory / ".gemini"
        if gemini.exists() and os.path.realpath(gemini) != own:
            return "gemini_workspace_config_present:%s" % _shown(gemini, home)
    return ""


def _user_problem(home: Path) -> str:
    """What ~ itself must look like: ~/.gemini/settings.json exactly GEMINI_USER_SETTINGS, a personal cached login,
    no .env, extension, command, skill, policy or agent configuration, no ~/.env or ~/.agents."""
    gemini = home / ".gemini"
    for path in (home / ".env", home / ".agents", gemini / ".env"):
        if path.exists():
            kind = "gemini_agents_config_present" if path.name == ".agents" else "gemini_dotenv_present"
            return "%s:%s" % (kind, _shown(path, home))
    for name in GEMINI_USER_DIRS:
        if (gemini / name).exists():
            return "gemini_user_config_present:%s" % _shown(gemini / name, home)
    extensions = gemini / "extensions"
    if extensions.exists() and (not extensions.is_dir() or any(extensions.iterdir())):
        return "gemini_user_config_present:%s" % _shown(extensions, home)
    reason = _settings_problem(gemini / "settings.json")
    if reason:
        return reason
    return _credentials_problem(gemini / "oauth_creds.json")


def _settings_problem(path: Path) -> str:
    if not path.exists():
        return "settings_missing"
    try:
        settings = _strict_json(path)
    except (OSError, ValueError):
        return "settings_unreadable"
    if not isinstance(settings, dict):
        return "settings_unreadable"
    for key in settings:
        if key not in GEMINI_USER_SETTINGS:
            return "settings_key_not_allowed:%s" % _key(key)
    security = settings.get("security")
    auth = security.get("auth") if isinstance(security, dict) else None
    if not isinstance(auth, dict) or "selectedType" not in auth:
        return "auth_type_unset"
    if auth["selectedType"] != "oauth-personal":
        return "auth_method_not_subscription:%s" % _key(auth["selectedType"])
    if security != GEMINI_USER_SETTINGS["security"]:
        extra = sorted(set(security) - {"auth"}) or sorted(set(auth) - {"selectedType"})
        prefix = "security." if set(security) - {"auth"} else "security.auth."
        return "settings_key_not_allowed:%s%s" % (prefix, _key(extra[0]))
    billing = settings.get("billing")
    if not isinstance(billing, dict) or "overageStrategy" not in billing:
        return "billing_overage_unset"
    if billing["overageStrategy"] != "never":
        return "billing_overage_enabled:%s" % _key(billing["overageStrategy"])
    if billing != GEMINI_USER_SETTINGS["billing"]:
        return "settings_key_not_allowed:billing.%s" % _key(sorted(set(billing) - {"overageStrategy"})[0])
    return ""


def _credentials_problem(path: Path) -> str:
    """The cached login must be a personal one. Only its `type` is read; no token value is ever kept or shown."""
    if not path.exists():
        return "gemini_credentials_missing"
    try:
        credentials = _strict_json(path)
    except (OSError, ValueError):
        return "gemini_credentials_not_personal:unreadable"
    if not isinstance(credentials, dict):
        return "gemini_credentials_not_personal:unreadable"
    kind = credentials.get("type")
    if kind not in GEMINI_PERSONAL_CREDENTIAL_TYPES:
        return "gemini_credentials_not_personal:%s" % _key(kind)
    return ""


def _key(value: object) -> str:
    """A settings key or value as it may appear in a reason: short, plain, never file content beyond that."""
    text = value if isinstance(value, str) else json.dumps(value)
    return text if re.fullmatch(r"[0-9A-Za-z_.$-]{1,40}", text) else "?"


def gemini_billing_problem(env: Mapping[str, str], cwd: Optional[str] = None, binary: str = "gemini",
                           extra_args: Sequence[object] = ()) -> Tuple[str, Optional[str]]:
    """(why a Gemini run is not verified zero-spend, "" when every allow condition holds; the binary to start).
    `env` is the run's one environment snapshot (the child gets its allowlisted part). Without `cwd` the home and
    every directory above it are checked (the verifier); with it, the run's directory and every one above it."""
    home, reason = gemini_home(env)
    if home is None:
        return reason, None
    if extra_args:
        return "gemini_extra_args_unsupported", None
    try:
        for directory in GEMINI_SYSTEM_DIRS:
            if os.path.exists(directory):
                return "gemini_system_config_present:%s" % directory, None
        reason = _tree_problem(cwd if cwd is not None else str(home), home) or _user_problem(home)
        if reason:
            return reason, None
        resolved, reason = gemini_resolve(binary, env)
    except OSError as exc:   # a directory that cannot be listed or read: nothing can be shown to be absent
        return "gemini_config_unreadable:%s" % type(exc).__name__, None
    return reason, resolved


def verify_gemini(cfg: Mapping[str, object], env: Optional[Mapping[str, str]] = None,
                  now: Optional[float] = None) -> BillingVerdict:
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    reason, _ = gemini_billing_problem(env, None, str(cfg.get("bin") or "gemini"), cfg.get("extra_args") or ())
    if reason:
        return _unverified("gemini", reason, "", now)
    return BillingVerdict(provider="gemini", verified=True, reason="", auth_method="oauth-personal", verified_at=now)


def gemini_workspace_problem(cwd: str, env: Mapping[str, str], binary: str = "gemini",
                             extra_args: Sequence[object] = ()) -> str:
    """The whole check again for one run, right before it is spawned, from the run's directory. "" to go ahead."""
    if not isinstance(env, Mapping):
        raise TypeError("env must be the run's environment snapshot")
    return gemini_billing_problem(env, cwd, binary, extra_args)[0]


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
