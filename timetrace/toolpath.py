"""Where `claude` / `codex` (and the `node` an npm-installed CLI needs) live.

launchd starts the Runner with a fixed PATH, not the user's shell, so a tool
installed through npm/nvm, Volta, bun, pnpm, mise or asdf is invisible to it.
`setup`, `agent install` and `agent doctor` therefore resolve each tool the
way the user's login shell does (`$SHELL -lic 'command -v claude'`), fall back
to scanning the usual install locations, and record the absolute path as
`<tool>.bin`; the LaunchAgent PATH then gains the tool's directory and the
directory of `node`.
"""
import glob
import os
import plistlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

LAUNCH_AGENT_LABEL = "com.atlaspaces.timetrace.agent"
# The LaunchAgent PATH before any tool directory is added. ~/.local/bin (claude's
# native installer) is appended per user.
SYSTEM_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin")
# The Codex desktop app ships its own `codex`.
APP_DIRS = ("/Applications/Codex.app/Contents/Resources", "/Applications/Codex.app/Contents/MacOS")
SHELL_TIMEOUT_SECONDS = 8.0
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")


def is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def default_path_dirs(user_home: Path) -> List[str]:
    return list(SYSTEM_DIRS) + [str(Path(user_home) / ".local" / "bin")]


def join_path(dirs: Iterable[str]) -> str:
    seen, out = set(), []
    for d in dirs:
        d = str(d).rstrip("/") or "/"
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def login_shell_which(name: str, shell: Optional[str] = None, timeout: float = SHELL_TIMEOUT_SECONDS,
                      run: Callable = subprocess.run) -> Optional[str]:
    """Absolute path of `name` as the user's interactive login shell sees it
    (nvm, mise, Volta … are set up in the rc files). None when not found, the
    shell failed or took longer than `timeout`."""
    if not _SAFE_NAME.match(name):
        return None
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    try:
        proc = run([shell, "-lic", "command -v %s" % name], capture_output=True, text=True,
                   timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("alias ") and "=" in line:  # e.g. alias claude=~/.claude/local/claude
            line = line.split("=", 1)[1].strip().strip("'\"").split(" ")[0]
            line = os.path.expanduser(line)
        if line.startswith("/") and is_executable(line):
            return line
    return None


def _version_key(path: str):
    """Newest first: `latest` / `current`, then numeric versions descending."""
    name = Path(path).parent.name if Path(path).name == "bin" else Path(path).name
    if name in ("latest", "current", "default"):
        return (0, ())
    numbers = tuple(-int(n) for n in re.findall(r"\d+", name))
    return (1, numbers)


def _versioned(pattern: str) -> List[str]:
    return sorted((p for p in glob.glob(pattern) if os.path.isdir(p)), key=_version_key)


def npm_global_bin(user_home: Path, shell_which: Optional[Callable[[str], Optional[str]]] = None,
                   run: Callable = subprocess.run) -> Optional[str]:
    """`$(npm config get prefix)/bin`, with npm (and the node next to it) found
    like any other tool. None when npm is not installed."""
    shell_which = shell_which or login_shell_which
    npm = shell_which("npm") or _scan("npm", _static_dirs(user_home))
    if not npm:
        return None
    env = dict(os.environ)
    env["PATH"] = join_path([os.path.dirname(npm), os.path.dirname(os.path.realpath(npm))]
                            + env.get("PATH", "").split(":"))
    try:
        proc = run([npm, "config", "get", "prefix"], capture_output=True, text=True,
                   timeout=SHELL_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    prefix = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not prefix or not prefix[-1].startswith("/"):
        return None
    return str(Path(prefix[-1]) / "bin")


def _static_dirs(user_home: Path) -> List[str]:
    h = str(user_home)
    dirs = [h + "/.local/bin", h + "/.claude/local"]
    dirs += list(SYSTEM_DIRS[:2])
    dirs += [h + "/.volta/bin", h + "/.bun/bin", h + "/.npm-global/bin",
             h + "/Library/pnpm", h + "/.local/share/pnpm"]
    dirs += _versioned(h + "/.local/share/mise/installs/*/*/bin")
    dirs += [h + "/.local/share/mise/shims"]
    dirs += _versioned(h + "/.asdf/installs/*/*/bin")
    dirs += [h + "/.asdf/shims"]
    dirs += _versioned(h + "/.nvm/versions/node/*/bin")
    dirs += list(APP_DIRS)
    dirs += list(SYSTEM_DIRS[2:])
    return dirs


def searched_places(user_home: Path, npm_bin: Optional[str] = None) -> List[str]:
    """candidate_dirs as the user reads it: ~ for HOME, globs unexpanded."""
    places = ["~/.local/bin", "~/.claude/local"] + list(SYSTEM_DIRS[:2]) + [
        "~/.volta/bin", "~/.bun/bin", "~/.npm-global/bin"]
    if npm_bin:
        places.append(_tilde(npm_bin, user_home) + "（npm config get prefix）")
    places += ["~/Library/pnpm", "~/.local/share/pnpm", "~/.local/share/mise/installs/*/*/bin",
               "~/.local/share/mise/shims", "~/.asdf/installs/*/*/bin", "~/.asdf/shims",
               "~/.nvm/versions/node/*/bin"] + list(APP_DIRS) + list(SYSTEM_DIRS[2:])
    return places


def candidate_dirs(user_home: Path, npm_bin: Optional[str] = None) -> List[str]:
    """The places scanned when the login shell does not know a tool, in order."""
    dirs = _static_dirs(Path(user_home))
    if npm_bin:
        dirs.insert(7, npm_bin)
    return join_path(dirs).split(":")


def _scan(name: str, dirs: Sequence[str]) -> Optional[str]:
    for d in dirs:
        path = os.path.join(d, name)
        if is_executable(path):
            return path
    return None


def needs_node(path: str) -> bool:
    """True when the tool is a script run through node (`#!/usr/bin/env node`)."""
    try:
        with open(os.path.realpath(path), "rb") as fh:
            head = fh.read(256)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return False
    return b"node" in head.split(b"\n", 1)[0]


@dataclass
class Resolution:
    name: str
    path: Optional[str] = None
    source: str = ""          # "login_shell" | "scan" | "config"
    searched: List[str] = field(default_factory=list)
    needs_node: bool = False
    node: Optional[str] = None

    @property
    def dirs(self) -> List[str]:
        """Directories the LaunchAgent PATH needs for this tool: node's first."""
        out = []
        if self.node:
            out.append(os.path.dirname(self.node))
        if self.path:
            out.append(os.path.dirname(self.path))
        return out


def resolve_node(tool_path: Optional[str], user_home: Path, shell_which=None,
                 npm_bin: Optional[str] = None) -> Optional[str]:
    """The node an npm-installed tool runs with: the one installed next to it
    (nvm, mise, asdf keep node in the same bin directory), else the login
    shell's, else the first in the usual places."""
    if tool_path:
        for d in (os.path.dirname(tool_path), os.path.dirname(os.path.realpath(tool_path))):
            if is_executable(os.path.join(d, "node")):
                return os.path.join(d, "node")
    return (shell_which or login_shell_which)("node") or _scan("node", candidate_dirs(user_home, npm_bin))


def resolve_tool(name: str, user_home: Path, shell_which=None, npm_bin: Optional[str] = None,
                 configured: Optional[str] = None) -> Resolution:
    """Find one tool. `configured` is a `<tool>.bin` the user set explicitly:
    it is used as is (an absolute path) or looked up by that name."""
    shell_which = shell_which or login_shell_which
    shell = os.environ.get("SHELL") or "/bin/zsh"
    lookup = configured if configured and not os.path.isabs(configured) else name
    res = Resolution(name=name)
    if configured and os.path.isabs(configured):
        res.searched = ["%s.bin = %s" % (name, configured)]
        if is_executable(configured):
            res.path, res.source = configured, "config"
    else:
        res.searched = ["登录 shell（%s -lic 'command -v %s'）" % (shell, lookup)]
        found = shell_which(lookup)
        if found:
            res.path, res.source = found, "login_shell"
        else:
            dirs = candidate_dirs(user_home, npm_bin)
            res.searched += searched_places(user_home, npm_bin)
            found = _scan(lookup, dirs)
            if found:
                res.path, res.source = found, "scan"
    if res.path:
        res.needs_node = needs_node(res.path)
        res.node = resolve_node(res.path, user_home, shell_which, npm_bin)
    return res


def _tilde(path: str, user_home: Path) -> str:
    home = str(user_home)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def launch_path(user_home: Path, resolutions: Iterable[Resolution]) -> str:
    """LaunchAgent PATH: node's directory first (an npm tool's `env node` must
    find the right one), the defaults, then each tool's own directory."""
    resolutions = list(resolutions)
    nodes = [os.path.dirname(r.node) for r in resolutions if r.node and r.needs_node]
    nodes += [os.path.dirname(r.node) for r in resolutions if r.node and not r.needs_node]
    tools = [os.path.dirname(r.path) for r in resolutions if r.path]
    return join_path(nodes[:1] + default_path_dirs(user_home) + tools + nodes[1:])


def plist_path(user_home: Path) -> Path:
    return Path(user_home) / "Library" / "LaunchAgents" / (LAUNCH_AGENT_LABEL + ".plist")


def installed_launch_path(user_home: Path) -> Optional[str]:
    """PATH of the installed LaunchAgent, None when it is not installed."""
    path = plist_path(user_home)
    try:
        doc = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    env = doc.get("EnvironmentVariables") or {}
    return str(env.get("PATH") or "")


def runner_visibility(path: str, needs_node_: bool, path_value: str):
    """Would a process with PATH=`path_value` (the LaunchAgent's) see the tool
    at `path`, and the node it needs? (ok, problem code)."""
    if not is_executable(path):
        return False, "binary_missing"
    dirs = [d.rstrip("/") for d in path_value.split(":") if d]
    if os.path.dirname(path).rstrip("/") not in dirs:
        return False, "not_on_path"
    if needs_node_ and not shutil.which("node", path=path_value):
        return False, "node_not_on_path"
    return True, ""


def launch_agent_state(uid: Optional[int] = None, run: Callable = subprocess.run) -> Dict[str, object]:
    """launchd's view of the Runner: {"loaded": bool, "running": bool, "pid": int|None}."""
    uid = os.getuid() if uid is None else uid
    try:
        proc = run(["launchctl", "print", "gui/%d/%s" % (uid, LAUNCH_AGENT_LABEL)],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"loaded": False, "running": False, "pid": None}
    if proc.returncode != 0:
        return {"loaded": False, "running": False, "pid": None}
    text = proc.stdout or ""
    state = re.search(r"^\s*state = (\S+)", text, re.M)
    pid = re.search(r"^\s*pid = (\d+)", text, re.M)
    return {"loaded": True, "running": bool(state and state.group(1) == "running"),
            "pid": int(pid.group(1)) if pid else None}


def extend_runtime_path(binaries: Iterable[str], environ=os.environ) -> None:
    """Runner start-up: make sure the directory of every absolute `<tool>.bin`
    (and the node installed next to it) is on PATH, even when the LaunchAgent
    was installed before the tool was."""
    current = environ.get("PATH", "")
    parts = current.split(":") if current else []
    extra: List[str] = []
    for binary in binaries:
        if not binary or not os.path.isabs(binary):
            continue
        d, real = os.path.dirname(binary), os.path.dirname(os.path.realpath(binary))
        node_dir = d if is_executable(os.path.join(d, "node")) else (
            real if is_executable(os.path.join(real, "node")) else None)
        if node_dir and node_dir not in parts and node_dir not in extra:
            extra.insert(0, node_dir)
        if d not in parts and d not in extra:
            extra.append(d)
    if extra:
        environ["PATH"] = join_path(extra + parts)
