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

The ACP agents follow the same rule: Gemini CLI is verified from its own
settings and .env files (personal Google login, no key / Vertex / Cloud
project anywhere it would load one); Cursor has no signal we can check, so its
adapter stays unverified and is never dispatched.
"""
import json
import os
import re
import subprocess
import sys
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
# Gemini CLI has no read-only "auth status" command, so the verdict comes from the files it decides by (not run
# against a real Gemini CLI on this machine; paths and keys follow its settings code): the settings layers it merges
# and the .env file it loads. Only "Login with Google" on a personal account (`oauth-personal`), with no key, Vertex
# AI or Cloud project anywhere it would read one and no automatic use of paid AI credits, is zero-spend; every other
# login type (an API key, Vertex AI, Cloud Shell, an external provider) bills, or may bill, someone.
GEMINI_PERSONAL_LOGIN = "oauth-personal"
GEMINI_SYSTEM_SETTINGS = {"darwin": "/Library/Application Support/GeminiCli/settings.json",
                          "linux": "/etc/gemini-cli/settings.json"}
# billing.overageStrategy: what Gemini CLI does with paid AI credits once the plan's quota runs out on the Google
# login path. "always" spends them without asking; "ask" asks a person (nobody is there in an unattended ACP run,
# and what the CLI then does was not verified); "never" leaves them alone. Only "never", or no value in any layer
# (no opt-in to credits at all), is accepted; anything else, including a value this code does not know, is not.
GEMINI_OVERAGE_OFF = "never"
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*(?:=|:\s)\s*(.*)$")


def gemini_config_home(env: Mapping[str, str], home: Optional[str] = None) -> Tuple[Optional[Path], str]:
    """The directory Gemini CLI takes as its home (paths.ts homedir(): `GEMINI_CLI_HOME` when set, else the user's
    home) and so where its own settings and login live, or why that cannot be known. A relative override would be
    resolved against the run's working directory, which is not this process's: it is refused rather than guessed.
    The adapter pins the child's GEMINI_CLI_HOME to exactly this directory, so what was verified is what runs."""
    override = env.get("GEMINI_CLI_HOME")
    if override:
        if not os.path.isabs(override):
            return None, "gemini_cli_home_not_absolute"
        return Path(override), ""
    return (Path(home) if home else Path.home()), ""


def _home_env_file(home: Path) -> Optional[Path]:
    return next((p for p in (home / ".gemini" / ".env", home / ".env") if p.exists()), None)


def _walk_env_file(start: str) -> Optional[Path]:
    current = Path(os.path.abspath(start))
    while True:
        for candidate in (current / ".gemini" / ".env", current / ".env"):
            if candidate.exists():
                return candidate
        if current.parent == current:
            return None
        current = current.parent


def gemini_env_file(start: str, home: Optional[str] = None) -> Optional[Path]:
    """The one .env file Gemini CLI loads for a process started in `start` (its findEnvFile): walking up from there,
    the first `.gemini/.env` or `.env`; past the filesystem root, `<home>/.gemini/.env`, then `<home>/.env`. It sets
    only variables the process does not already have, so a key stripped from the environment comes back through it."""
    return _walk_env_file(start) or _home_env_file(Path(home) if home else Path.home())


def _gemini_env_files(start: Optional[str], homes: Sequence[Path]) -> List[Path]:
    """Every .env file a run could load: the one the walk up from `start` finds, else the home fallback of each home
    the CLI might use for it (its config home and the OS home, when they differ: not knowing which, both count)."""
    found = _walk_env_file(start) if start is not None else None
    if found is not None:
        return [found]
    files = [_home_env_file(home) for home in dict.fromkeys(homes)]
    return [path for path in files if path is not None]


def _dotenv_keys(path: Path) -> Dict[str, str]:
    """KEY=value pairs of a .env file (dotenv syntax, read generously: a value that only might be set counts)."""
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _DOTENV_LINE.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        value = match.group(2).strip()
        if value[:1] in ("'", '"', "`"):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split("#", 1)[0].strip()
        values[match.group(1)] = value
    return values


def _env_file_problem(path: Optional[Path]) -> str:
    if path is None:
        return ""
    try:
        values = _dotenv_keys(path)
    except OSError:
        return "env_file_unreadable"
    for key in billing_env_keys("gemini"):
        if values.get(key):
            return "api_key_fallback_in_env_file:" + key
    return ""


def _strip_json_comments(text: str) -> str:
    """Gemini CLI reads settings.json with // and /* */ comments allowed (JSONC); strings are kept as they are."""
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_string = ch != '"'
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _gemini_settings(path: Path) -> Dict[str, object]:
    """The settings document; raises OSError / ValueError when it cannot be read as one JSON object."""
    value = json.loads(_strip_json_comments(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("settings is not an object")
    return value


def _section(settings: Mapping[str, object], *keys: str) -> Mapping[str, object]:
    for key in keys:
        settings = settings.get(key) if isinstance(settings.get(key), dict) else {}
    return settings


# One leaf each of the settings that decide billing, read from one document (None: this layer does not set it). The
# login type moved from a top-level `selectedAuthType` (older layout) to security.auth.selectedType.
_GEMINI_KEYS: Dict[str, Callable[[Mapping[str, object]], object]] = {
    "selectedType": lambda s: (_section(s, "security", "auth").get("selectedType")
                               if "selectedType" in _section(s, "security", "auth") else s.get("selectedAuthType")),
    "enforcedType": lambda s: _section(s, "security", "auth").get("enforcedType"),
    "useExternal": lambda s: _section(s, "security", "auth").get("useExternal"),
    "overageStrategy": lambda s: _section(s, "billing").get("overageStrategy"),
}


def _gemini_layers(config_home: Path, env: Mapping[str, str], cwd: Optional[str],
                   system_settings_path: Optional[str], system_defaults_path: Optional[str]
                   ) -> Tuple[List[Tuple[str, Mapping[str, object]]], str]:
    """The settings documents Gemini CLI merges, lowest precedence first: the system defaults, the user's (in its
    config home), the workspace's (the run's directory, repository content) and the system override; or why they
    cannot all be read. Only the user's file is required: it is where a login is recorded."""
    system = Path(system_settings_path or env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
                  or GEMINI_SYSTEM_SETTINGS.get(sys.platform, GEMINI_SYSTEM_SETTINGS["linux"]))
    defaults = Path(system_defaults_path or env.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH")
                    or system.parent / "system-defaults.json")
    user = config_home / ".gemini" / "settings.json"
    workspace = Path(os.path.abspath(cwd)) / ".gemini" / "settings.json" if cwd is not None else None
    layers: List[Tuple[str, Mapping[str, object]]] = []
    for label, path in (("system_defaults", defaults), ("user", user),
                        ("workspace", workspace if workspace != user else None), ("system", system)):
        if path is None:
            continue
        if not path.exists():
            if label == "user":
                return [], "settings_missing"
            continue
        try:
            layers.append((label, _gemini_settings(path)))
        except (OSError, ValueError):
            return [], "settings_unreadable" if label == "user" else label + "_settings_unreadable"
    return layers, ""


def _effective(layers: Sequence[Tuple[str, Mapping[str, object]]], key: str) -> Tuple[str, object]:
    """(layer, value) of one setting after the merge: the highest-precedence layer that sets it wins."""
    found: Tuple[str, object] = ("", None)
    for label, settings in layers:
        value = _GEMINI_KEYS[key](settings)
        if value is not None and value != "":
            found = (label, value)
    return found


def _overridden(label: str, detail: str, user_reason: str) -> str:
    return user_reason + detail if label == "user" else "%s_settings_override:%s" % (label, detail)


def gemini_billing_problem(env: Mapping[str, str], home: Optional[str] = None, cwd: Optional[str] = None,
                           system_settings_path: Optional[str] = None,
                           system_defaults_path: Optional[str] = None) -> Tuple[str, str]:
    """(why a Gemini run could bill, the effective login type); the reason is "" when it cannot. Without `cwd` it
    judges what every run shares (the verifier); with it, also what that run's directory adds (the .env file found
    from there, the workspace's settings), right before a spawn."""
    reason = _env_fallback("gemini", env)
    if reason:
        return reason, ""
    config_home, reason = gemini_config_home(env, home)
    if config_home is None:
        return reason, ""
    os_home = Path(home) if home else Path.home()
    for env_file in _gemini_env_files(cwd, [config_home, os_home]):
        reason = _env_file_problem(env_file)
        if reason:
            return reason, ""
    layers, reason = _gemini_layers(config_home, env, cwd, system_settings_path, system_defaults_path)
    if reason:
        return reason, ""
    label, selected = _effective(layers, "selectedType")
    selected = str(selected) if selected is not None else ""
    if _effective(layers, "useExternal")[1] is True:
        return "external_auth_configured", selected
    enforced = _effective(layers, "enforcedType")[1]
    if enforced is not None and enforced != GEMINI_PERSONAL_LOGIN:
        return "auth_type_enforced:%s" % enforced, selected
    if not selected:
        return "auth_type_unset", ""
    if selected != GEMINI_PERSONAL_LOGIN:
        return _overridden(label, selected, "auth_method_not_subscription:"), selected
    label, overage = _effective(layers, "overageStrategy")
    if overage is not None and overage != GEMINI_OVERAGE_OFF:
        if label == "user":
            return "billing_overage_enabled:%s" % overage, selected
        return "%s_settings_override:billing.overageStrategy=%s" % (label, overage), selected
    return "", selected


def verify_gemini(cfg: Mapping[str, object], env: Optional[Mapping[str, str]] = None, home: Optional[str] = None,
                  system_settings_path: Optional[str] = None, system_defaults_path: Optional[str] = None,
                  now: Optional[float] = None) -> BillingVerdict:
    now = time.time() if now is None else now
    env = os.environ if env is None else env
    reason, selected = gemini_billing_problem(env, home, None, system_settings_path, system_defaults_path)
    if reason:
        return _unverified("gemini", reason, selected, now)
    return BillingVerdict(provider="gemini", verified=True, reason="", auth_method=selected, verified_at=now)


def gemini_workspace_problem(cwd: str, home: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                             system_settings_path: Optional[str] = None,
                             system_defaults_path: Optional[str] = None) -> str:
    """The whole check again for one run, right before it is spawned, now with what its directory adds: the .env
    file Gemini CLI would load from there (a project's `.env` often holds a GEMINI_API_KEY for the app itself) and
    the workspace's .gemini/settings.json, repository content merged over the user's settings. "" to go ahead."""
    env = os.environ if env is None else env
    return gemini_billing_problem(env, home, cwd, system_settings_path, system_defaults_path)[0]


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
