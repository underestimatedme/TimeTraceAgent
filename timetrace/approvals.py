"""Local permission rules for a Claude task run (protocol 2).

Claude Code asks the host (`can_use_tool`) before a tool call its own
permission mode does not settle. The runner answers from this table first,
without bothering the phone:

- deny: what SAFETY_RULES forbids — `git push`, mutating remotes or other
  branches, git config writes, writes outside the task's worktree or into
  its `.git` / CI configuration, and reading credential locations. A phone
  approval can never override a deny: it is evaluated before anything is
  asked.
- allow: reads and edits inside the worktree, and a short list of read-only
  commands (plus `git add` / `git commit`, which a task needs) given as one
  simple command without shell operators, git global options or paths
  outside the worktree.
- ask: everything else goes to the phone (`approval_requested`).
"""
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from timetrace.redact import redact
from timetrace.results import redact_deep

ALLOW, DENY, ASK = "allow", "deny", "ask"
INPUT_BYTES = 3500        # Valley accepts ≤ 4 KB of JSON input; keep headroom
SUMMARY_CHARS = 300
TOOL_CHARS = 64

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
# Tools with no effect outside the conversation itself.
INERT_TOOLS = ("TodoWrite", "TodoRead")
PATH_KEYS = ("file_path", "notebook_path", "path")

# Locations under the user's home that hold credentials; never read or written.
CREDENTIAL_PATHS = (
    ".ssh", ".aws", ".gnupg", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".kube", ".azure",
    ".docker/config.json", ".config/gh", ".config/gcloud", ".timetrace", ".claude/.credentials.json",
    ".claude.json", ".codex/auth.json", "Library/Keychains",
)
_CREDENTIAL_TEXT = re.compile(
    r"(?:^|[\s'\"=:/~])(?:\.ssh|\.aws|\.gnupg|\.netrc|\.git-credentials|\.npmrc|\.pypirc|\.kube|\.azure|"
    r"\.docker/config\.json|\.config/gh|\.config/gcloud|\.timetrace|\.claude/\.credentials|\.claude\.json|"
    r"\.codex/auth\.json|Library/Keychains)(?:$|[\s'\"/])"
    r"|\bsecurity\s+(?:find-(?:generic|internet)-password|dump-keychain|export)\b")
# CI configuration (SAFETY_RULES 1): never modified by a task.
CI_PATHS = re.compile(r"(?:^|/)(?:\.github/workflows/|\.gitlab-ci\.yml$|Jenkinsfile$|\.circleci/|"
                      r"azure-pipelines\.yml$|\.travis\.yml$|bitbucket-pipelines\.yml$)")
# A command is "simple" when none of these appear: no chaining, pipes,
# redirection, substitution or background jobs.
SHELL_OPERATORS = re.compile(r"[;&|<>`\n\r]|\$\(")
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n\r`]|\$\(|\)")

READ_ONLY = ("ls", "cat", "head", "tail", "wc", "pwd", "grep", "rg", "find", "file", "stat", "which")
GIT_READ_ONLY = ("status", "diff", "log", "show", "rev-parse", "ls-files", "blame")
GIT_COMMIT = ("add", "commit")
# Options that make an otherwise read-only command write or execute.
FIND_ACTIONS = ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
RISKY_OPTIONS = ("--output", "--ext-diff", "--pre", "--exec")

DENY_PREFIX = "timetrace 安全规则禁止这个操作："


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str = ""


def _home(home: Optional[str]) -> str:
    return os.path.realpath(os.path.expanduser(home or "~"))


def _resolve(path: str, root: str) -> str:
    text = os.path.expanduser(str(path))
    if not os.path.isabs(text):
        text = os.path.join(root, text)
    return os.path.realpath(text)


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _credential(path: str, home: str) -> bool:
    for rel in CREDENTIAL_PATHS:
        if _inside(path, os.path.realpath(os.path.join(home, rel))):
            return True
    return False


def _paths(tool_input: Dict[str, Any]) -> List[str]:
    found = []
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _file_tool(tool: str, tool_input: Dict[str, Any], root: str, home: str) -> Verdict:
    paths = _paths(tool_input) or ([root] if tool in READ_TOOLS else [])
    if not paths:
        return Verdict(ASK)
    writes = tool in WRITE_TOOLS
    outside = False
    for raw in paths:
        path = _resolve(raw, root)
        inside = _inside(path, root)
        # The worktree itself lives under ~/.timetrace: only paths outside it
        # can reach a credential location.
        if not inside and (_credential(path, home) or _CREDENTIAL_TEXT.search(_scrub(raw, root))):
            return Verdict(DENY, "访问凭据目录")
        if writes:
            if not inside:
                return Verdict(DENY, "写入工作副本以外的路径")
            rel = os.path.relpath(path, root)
            if rel == ".git" or rel.startswith(".git" + os.sep) or "/.git/" in "/" + rel + "/":
                return Verdict(DENY, "修改 .git 元数据")
            if CI_PATHS.search(rel.replace(os.sep, "/")):
                return Verdict(DENY, "修改 CI 配置")
        outside = outside or not inside
    return Verdict(ASK if outside else ALLOW)


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _strip_env(tokens: List[str]) -> List[str]:
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    return tokens[i:]


def _git_parts(tokens: List[str]):
    """(global options, subcommand, args) of a git invocation."""
    i, options = 1, []
    while i < len(tokens) and tokens[i].startswith("-"):
        options.append(tokens[i])
        if tokens[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env") and i + 1 < len(tokens):
            options.append(tokens[i + 1])
            i += 1
        i += 1
    sub = tokens[i] if i < len(tokens) else ""
    return options, sub, tokens[i + 1:]


def _git_denied(args_tokens: List[str]) -> str:
    _, sub, args = _git_parts(args_tokens)
    flags = set(a for a in args if a.startswith("-"))
    positional = [a for a in args if not a.startswith("-")]
    if sub == "push":
        return "git push"
    if sub == "remote" and positional and positional[0] in ("add", "set-url", "remove", "rm", "rename",
                                                            "set-head", "set-branches", "prune"):
        return "修改远程仓库配置"
    if sub == "config" and not flags & {"--get", "--get-all", "--get-regexp", "--list", "-l"}:
        return "修改 git 配置"
    if sub == "branch" and (flags & {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force"}
                            or (positional and not flags & {"--list", "-l", "--contains", "--merged",
                                                            "--no-merged", "--points-at"})):
        return "创建、改名或删除分支"
    if sub == "switch" or (sub == "checkout" and flags & {"-b", "-B", "--orphan"}):
        return "切换或创建分支"
    if sub in ("worktree", "update-ref", "symbolic-ref", "filter-branch", "replace"):
        return "修改分支或仓库结构"
    return ""


def _scrub(text: str, root: str) -> str:
    """`text` with the worktree's own absolute path replaced by ".", so the
    credential pattern (which matches `.timetrace/`) sees only other paths."""
    return str(text).replace(root + os.sep, "./").replace(root, ".")


def _bash(command: str, root: str, home: str) -> Verdict:
    if not isinstance(command, str) or not command.strip():
        return Verdict(ASK)
    if _CREDENTIAL_TEXT.search(_scrub(command, root)):
        return Verdict(DENY, "访问凭据")
    segments = [s for s in SEGMENT_SPLIT.split(command) if s.strip()]
    for segment in segments:
        tokens = _strip_env(_tokens(segment))
        if tokens and tokens[0] in ("sudo", "doas"):
            return Verdict(DENY, "以管理员身份运行")
        if tokens and os.path.basename(tokens[0]) == "git":
            reason = _git_denied(tokens)
            if reason:
                return Verdict(DENY, reason)
    if SHELL_OPERATORS.search(command):
        return Verdict(ASK)
    tokens = _strip_env(_tokens(command))
    if not tokens:
        return Verdict(ASK)
    program = tokens[0]
    args = tokens[1:]
    if any(a.startswith(RISKY_OPTIONS) for a in args):
        return Verdict(ASK)
    if program == "git":
        options, sub, rest = _git_parts(tokens)
        if options or sub not in GIT_READ_ONLY + GIT_COMMIT:
            return Verdict(ASK)
        args = rest
    elif program not in READ_ONLY:
        return Verdict(ASK)
    elif program == "find" and any(a in FIND_ACTIONS for a in args):
        return Verdict(ASK)
    for arg in args:
        if arg.startswith("-") or not re.search(r"[/~]|^\.\.?$", arg):
            continue
        path = _resolve(arg, root)
        if _inside(path, root):
            continue
        if _credential(path, home):
            return Verdict(DENY, "访问凭据目录")
        return Verdict(ASK)
    return Verdict(ALLOW)


def evaluate(tool: str, tool_input: Any, root: str, home: Optional[str] = None) -> Verdict:
    """What the runner answers for one permission prompt in the worktree `root`."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    root = os.path.realpath(root)
    home_dir = _home(home)
    if tool == "Bash":
        return _bash(tool_input.get("command") or "", root, home_dir)
    if tool in WRITE_TOOLS or tool in READ_TOOLS:
        return _file_tool(tool, tool_input, root, home_dir)
    if tool in INERT_TOOLS:
        return Verdict(ALLOW)
    for value in tool_input.values():
        if isinstance(value, str) and _CREDENTIAL_TEXT.search(value):
            return Verdict(DENY, "访问凭据")
    return Verdict(ASK)


def remember_key(tool: str, tool_input: Any) -> Optional[str]:
    """What "本任务内同类都允许" remembers: the tool plus, for a shell command,
    its first two words. A compound command is never remembered or matched."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or SHELL_OPERATORS.search(command):
            return None
        tokens = _strip_env(_tokens(command))
        if not tokens:
            return None
        return "Bash:" + " ".join(tokens[:2])
    return str(tool)[:TOOL_CHARS] or None


def summary(tool: str, tool_input: Any) -> str:
    """One line for the phone (redacted, ≤ 300 characters)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    paths = _paths(tool_input)
    if tool == "Bash":
        text = "运行 " + str(tool_input.get("command") or "")
    elif tool in WRITE_TOOLS:
        text = "修改文件 " + (paths[0] if paths else "")
    elif tool in READ_TOOLS:
        text = "读取 " + (paths[0] if paths else str(tool_input.get("pattern") or ""))
    elif tool == "WebFetch":
        text = "访问网页 " + str(tool_input.get("url") or "")
    elif tool == "WebSearch":
        text = "搜索网页：" + str(tool_input.get("query") or "")
    elif str(tool).startswith("mcp__"):
        text = "使用工具 " + str(tool)
    else:
        text = "使用 " + str(tool)
    text = " ".join(redact(text).split())
    return (text[:SUMMARY_CHARS - 1] + "…") if len(text) > SUMMARY_CHARS else (text or "使用 " + str(tool))


def _size(value: Any) -> int:
    # Valley bounds the raw JSON it receives, which cloud.py encodes with
    # json.dumps defaults (ASCII escapes).
    return len(json.dumps(value))


def safe_input(tool_input: Any, limit: int = INPUT_BYTES) -> Any:
    """The tool input as it may leave this computer: every string redacted,
    long strings cut, the whole at most `limit` bytes of JSON."""
    value = redact_deep(tool_input if isinstance(tool_input, (dict, list)) else {})
    if _size(value) <= limit:
        return value
    for cut in (1000, 300, 80):
        def shorten(v):
            if isinstance(v, str):
                return v if len(v) <= cut else v[:cut] + "…"
            if isinstance(v, dict):
                return {str(k)[:80]: shorten(x) for k, x in list(v.items())[:40]}
            if isinstance(v, list):
                return [shorten(x) for x in v[:40]]
            return v
        smaller = shorten(value)
        if _size(smaller) <= limit:
            return smaller
    return {"truncated": True}


def tool_label(tool: Any) -> str:
    return (str(tool or "") or "unknown")[:TOOL_CHARS]
