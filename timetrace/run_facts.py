"""运行事实: what the computer itself saw one run do, sent beside the AI's own report so a person can see where the two
disagree ("says it is done, but the last test failed").

Contract: docs/superpowers/specs/2026-11-17-run-facts-design.md (TimeTrace repository).

Commands come from the tool's own event stream, never from anything the run wrote:
- Claude Code stream-json: a `Bash` tool_use and its tool_result. A non-zero exit comes back as `is_error` with the text
  "Exit code N"; any other error (a denied or invalid call) means the command did not run, so it is not counted.
- `codex exec --json`: `command_execution` items, with `exit_code` and `status` (`declined` did not run).
- The Codex App Server: `commandExecution` items, with `exitCode` and `status`.

Which commands were tests is recognized from the command line against a fixed list of test runners, so it is shown as
"recognized as tests", never as every check the run made. Only counts and names from that fixed list leave the
computer: no command text, output or path.

A stream the runner did not see whole (a command event it could not read) has no command facts at all, never zeros:
"no tests ran" is only ever said about a run whose every command the runner watched.
"""
import json
import re
import shlex
from typing import Any, Dict, List, Optional

VERSION = 1
MAX_COUNT = 1000000
TOOLS_MAX = 8

# The names a test command is reported under. Valley accepts these and no others.
TEST_TOOLS = ("pytest", "unittest", "go test", "cargo test", "npm test", "pnpm test", "yarn test", "bun test",
              "vitest", "jest", "mocha", "ava", "playwright", "xcodebuild test", "swift test", "gradle test",
              "mvn test", "make test", "tox", "nox", "rspec", "phpunit", "ctest", "bats", "dotnet test",
              "deno test", "mix test")

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_PLAIN_WRAPPERS = {"time", "nohup", "command", "exec", "caffeinate", "xvfb-run", "npx", "bunx", "pnpx"}
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}
_STANDALONE = {"vitest": "vitest", "jest": "jest", "mocha": "mocha", "ava": "ava", "tox": "tox", "nox": "nox",
               "rspec": "rspec", "phpunit": "phpunit", "ctest": "ctest", "bats": "bats",
               "pytest": "pytest", "py.test": "pytest"}
_SUBCOMMAND = {"go": ("test", "go test"), "swift": ("test", "swift test"), "dotnet": ("test", "dotnet test"),
               "deno": ("test", "deno test"), "mix": ("test", "mix test"), "playwright": ("test", "playwright")}
_PACKAGE_MANAGERS = {"npm": "npm test", "pnpm": "pnpm test", "yarn": "yarn test", "bun": "bun test"}
# Package-manager options that take a value (`npm --prefix web test`).
_VALUE_OPTIONS = {"--prefix", "-C", "--dir", "--filter", "-F", "--workspace", "-w", "--cwd"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_OPERATORS = {";", "&", "&&", "|", "||", "(", ")", ";;", "|&"}
_GRADLE_TASK = re.compile(r"(?:^|:)(?:test\w*|check|connected\w*Test)$")
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc", ".org")
_DOC_NAMES = ("license", "licence", "changelog", "notice", "authors", "readme")


def _words(command: str) -> Optional[List[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _segments(words: List[str]) -> List[List[str]]:
    out, current = [], []
    for word in words:
        if word in _OPERATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(word)
    if current:
        out.append(current)
    return out


def _skip_options(words: List[str], start: int) -> int:
    i = start
    while i < len(words) and words[i].startswith("-"):
        i += 1 + (words[i] in _VALUE_OPTIONS and i + 1 < len(words))
    return i


def test_tool(command: Any, depth: int = 0) -> Optional[str]:
    """The TEST_TOOLS name of the test runner `command` starts (in any of its `&&` / `;` / `|` parts, behind a shell's
    `-c`, `env`, `npx`, `uv run` and similar), or None. A guess from the command line only."""
    if not isinstance(command, str) or depth > 3:
        return None
    words = _words(command)
    if not words:
        return None
    for segment in _segments(words):
        found = _segment_tool(segment, depth)
        if found:
            return found
    return None


def _segment_tool(words: List[str], depth: int) -> Optional[str]:
    while words:
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            return None
        name = words[0].rsplit("/", 1)[-1]
        if name in _SHELLS:
            for i, word in enumerate(words[1:], 1):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                    return test_tool(words[i + 1], depth + 1) if i + 1 < len(words) else None
            return None
        if name == "env":
            i = 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGNMENT.match(words[i])):
                i += 1
            words = words[i:]
            continue
        if name == "nice":
            i = 1
            while i < len(words) and words[i].startswith("-"):
                i += 1 + (words[i] == "-n")
            words = words[i:]
            continue
        if name == "timeout":
            words = words[_skip_options(words, 1) + 1:]
            continue
        if name in _PLAIN_WRAPPERS:
            words = words[_skip_options(words, 1):]
            continue
        if name in _RUNNERS:
            i = _skip_options(words, 1)
            if i < len(words) and words[i] == "run":
                words = words[_skip_options(words, i + 1):]
                continue
            return None
        return _tool_of(name, words)
    return None


def _tool_of(name: str, words: List[str]) -> Optional[str]:
    rest = words[1:]
    if name in _STANDALONE:
        return _STANDALONE[name]
    if re.fullmatch(r"python[0-9.]*", name):
        for i, word in enumerate(rest[:-1]):
            if word == "-m":
                return {"pytest": "pytest", "unittest": "unittest", "tox": "tox", "nox": "nox"}.get(rest[i + 1])
        return None
    if name in _SUBCOMMAND:
        sub, tool = _SUBCOMMAND[name]
        i = _skip_options(words, 1)
        return tool if i < len(words) and words[i] == sub else None
    if name == "cargo":
        i = 1
        while i < len(words) and (words[i].startswith("+") or words[i].startswith("-")):
            i += 1
        return "cargo test" if i < len(words) and words[i] in ("test", "nextest") else None
    if name in _PACKAGE_MANAGERS:
        i = _skip_options(words, 1)
        sub = words[i] if i < len(words) else ""
        if sub in ("test", "t", "tst") or (name in ("yarn", "pnpm") and sub.startswith("test")):
            return _PACKAGE_MANAGERS[name]
        if sub in ("run", "run-script"):
            j = _skip_options(words, i + 1)
            return _PACKAGE_MANAGERS[name] if j < len(words) and words[j].startswith("test") else None
        if sub in ("exec", "dlx", "x"):
            return _segment_tool(words[_skip_options(words, i + 1):], 0)
        return None
    if name == "xcodebuild":
        return "xcodebuild test" if any(w in ("test", "test-without-building") for w in rest) else None
    if name in ("gradle", "gradlew"):
        return "gradle test" if any(_GRADLE_TASK.search(w) for w in rest if not w.startswith("-")) else None
    if name in ("mvn", "mvnw"):
        return "mvn test" if any(w in ("test", "verify") for w in rest) else None
    if name == "make":
        return "make test" if any(w in ("test", "tests", "check") for w in rest if not w.startswith("-")) else None
    return None


def _json_line(line: Any) -> Optional[Dict[str, Any]]:
    value = json.loads(line.strip())
    return value if isinstance(value, dict) else None


class Commands:
    """One run's commands, counted as its stream is read. Feed it every line (`feed`, run_streaming's `on_line`) or
    every decoded message (`add`, the stream host's `on_message`) of one tool, then read `total()`."""

    def __init__(self, tool: str):
        self.tool = tool  # claude | codex_exec | codex_app_server
        self.ran = 0
        self.failed = 0
        self.tests_ran = 0
        self.tests_failed = 0
        self.last_test = ""
        self.tools: List[str] = []
        self.broken = False
        self.pending: Dict[str, Any] = {}
        self.counted = set()

    # ---- input ------------------------------------------------------------------------------------------------------

    def feed(self, line: str) -> None:
        markers = {"claude": ('"tool_use"', '"tool_result"'), "codex_exec": ('"command_execution"',)}.get(self.tool, ())
        if not isinstance(line, str) or not any(m in line for m in markers):
            return
        try:
            msg = _json_line(line)
        except ValueError:
            self.broken = True
            return
        if msg is not None:
            self.add(msg)

    def add(self, msg: Any) -> None:
        try:
            if not isinstance(msg, dict):
                return
            if self.tool == "claude":
                self._claude(msg)
            elif self.tool == "codex_exec":
                if msg.get("type") == "item.completed":
                    self._codex_item(msg.get("item"), "command_execution", "exit_code")
        except Exception:
            self.broken = True

    def app_server_item(self, item: Any) -> None:
        """An `item/completed` notification's item from the Codex App Server."""
        try:
            self._codex_item(item, "commandExecution", "exitCode")
        except Exception:
            self.broken = True

    def _claude(self, msg: Dict[str, Any]) -> None:
        message = msg.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if msg.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("name") == "Bash":
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if tool_input.get("run_in_background"):
                    continue  # it returns at once; how it ends is not in this result
                if isinstance(block.get("id"), str):
                    self.pending[block["id"]] = tool_input.get("command")
            elif msg.get("type") == "user" and block.get("type") == "tool_result":
                use_id = block.get("tool_use_id")
                if not isinstance(use_id, str) or use_id not in self.pending:
                    continue
                command = self.pending.pop(use_id)
                if not block.get("is_error"):
                    self._record(command, False)
                elif _claude_exit(block.get("content")):
                    self._record(command, True)
                # Any other error is a call that did not run (denied, invalid input): not a command.

    def _codex_item(self, item: Any, kind: str, exit_key: str) -> None:
        if not isinstance(item, dict) or item.get("type") != kind:
            return
        item_id = item.get("id")
        if item_id is not None:
            if item_id in self.counted:
                return
            self.counted.add(item_id)
        status = item.get("status")
        if status in ("declined", "in_progress", "inProgress"):
            return
        code = item.get(exit_key)
        failed = status == "failed" or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
        self._record(item.get("command"), failed)

    def _record(self, command: Any, failed: bool) -> None:
        self.ran += 1
        self.failed += bool(failed)
        tool = test_tool(command)
        if tool:
            self.tests_ran += 1
            self.tests_failed += bool(failed)
            self.last_test = "failed" if failed else "passed"
            if tool not in self.tools and len(self.tools) < TOOLS_MAX:
                self.tools.append(tool)

    # ---- output -----------------------------------------------------------------------------------------------------

    def total(self) -> Optional[Dict[str, Any]]:
        """{"commands": {ran, failed}, "tests": {ran, failed, last?, tools?}}, or None when the stream was not read
        whole."""
        if self.broken:
            return None
        tests: Dict[str, Any] = {"ran": min(self.tests_ran, MAX_COUNT), "failed": min(self.tests_failed, MAX_COUNT)}
        if self.tests_ran:
            tests["last"] = self.last_test
            tests["tools"] = list(self.tools)
        return {"commands": {"ran": min(self.ran, MAX_COUNT), "failed": min(self.failed, MAX_COUNT)}, "tests": tests}


def _claude_exit(content: Any) -> bool:
    """A Bash tool_result error that is the command's own failure: a non-zero exit or a timeout."""
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict)
                           and isinstance(part.get("text"), str))
    if not isinstance(content, str):
        return False
    text = content.lstrip()
    return bool(re.match(r"(?:Error: )?Exit code \d+", text)) or "timed out" in text[:200].lower()


def is_doc(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return (lower.endswith(_DOC_SUFFIXES) or lower.startswith(("docs/", "doc/"))
            or any(name == base or name.startswith(base + ".") for base in _DOC_NAMES))


def build(commands: Optional[Dict[str, Any]], changes: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The completion event's `run_facts`, or None when there is nothing the runner saw for itself."""
    if not commands and not changes:
        return None
    out: Dict[str, Any] = {"version": VERSION}
    if commands:
        out.update(commands)
    if changes:
        out["changes"] = changes
    return out
