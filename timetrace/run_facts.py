"""运行事实: what the computer itself saw one run do, sent beside the AI's own report so a person can see where the two
disagree ("says it is done, but its last test command failed").

Contract: docs/superpowers/specs/2026-11-17-run-facts-design.md (TimeTrace repository).

Commands come from the tool's own event stream, never from anything the run wrote:
- Claude Code stream-json: a `Bash` tool_use and its tool_result. A non-zero exit comes back as `is_error` with the text
  "Exit code N"; any other error (a denied or invalid call) means the command did not run, so it is not counted.
- `codex exec --json`: `command_execution` items, with `exit_code` and `status` (`declined` did not run).
- The Codex App Server: `commandExecution` items, with `exitCode` and `status`.

Which commands ran tests is recognized from the command line against a fixed list of test runners, so it is shown as
"recognized as tests", never as every check the run made. A command's exit status is its test's outcome only when the
test runner is the last part of a plain `&&` / `;` chain; behind `|`, `||`, `&`, a group, `!` or a control keyword the
outcome is `unknown`, never a guess. Only counts and names from the fixed list leave the computer: no command text,
output or path.

A stream the runner did not see whole (a line that looked like JSON and did not decode) has no command facts at all,
never zeros: "no tests ran" is only ever said about a run whose every command the runner watched.
"""
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = 1
MAX_COUNT = 1000000
TOOLS_MAX = 8

# The names a test command is reported under. Valley accepts these and no others.
TEST_TOOLS = ("pytest", "unittest", "go test", "cargo test", "npm test", "pnpm test", "yarn test", "bun test",
              "vitest", "jest", "mocha", "ava", "playwright", "xcodebuild test", "swift test", "gradle test",
              "mvn test", "make test", "tox", "nox", "rspec", "phpunit", "ctest", "bats", "dotnet test",
              "deno test", "mix test")

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_STANDALONE = {"vitest": "vitest", "jest": "jest", "mocha": "mocha", "ava": "ava", "tox": "tox", "nox": "nox",
               "rspec": "rspec", "phpunit": "phpunit", "ctest": "ctest", "bats": "bats",
               "pytest": "pytest", "py.test": "pytest"}
_SUBCOMMAND = {"go": ("test", "go test"), "swift": ("test", "swift test"), "dotnet": ("test", "dotnet test"),
               "deno": ("test", "deno test"), "mix": ("test", "mix test"), "playwright": ("test", "playwright")}
_PACKAGE_MANAGERS = {"npm": "npm test", "pnpm": "pnpm test", "yarn": "yarn test", "bun": "bun test"}
_PYTHON_MODULES = {"pytest": "pytest", "unittest": "unittest", "tox": "tox", "nox": "nox"}
# Options that take the next word as their value, per program (`npm --prefix web test`, `uv run --project web pytest`).
_VALUE_OPTIONS = {
    "package": {"--prefix", "-C", "--dir", "--filter", "-F", "--workspace", "-w", "--cwd"},
    "env": {"-u", "--unset", "-C", "--chdir", "-P", "-S", "--split-string"},
    "npx": {"-p", "--package", "-c", "--call"},
    "runner": {"--project", "--directory", "--with", "--with-requirements", "--with-editable", "--python", "-p",
               "--package", "--extra", "--group", "--env-file", "--index", "--index-url", "-C", "-P", "--env", "-e"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "exec": {"-a"},
    "caffeinate": {"-t", "-w"},
    "xvfb-run": {"-s", "--server-args", "-n", "--server-num", "-e", "--error-file", "-f", "--auth-file", "-p",
                 "--xauth-protocol"},
    "nice": {"-n", "--adjustment"},
}
# Wrappers that run the rest of the line and pass its exit status on.
_PLAIN_WRAPPERS = {"time": "", "nohup": "", "exec": "exec", "caffeinate": "caffeinate", "xvfb-run": "xvfb-run",
                   "npx": "npx", "bunx": "npx", "pnpx": "npx"}
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}
# Arguments with which a test runner lists, collects, dry-runs or prints help or its version instead of running tests.
_NOT_RUNNING = {
    "pytest": {"--collect-only", "--co", "--version", "-V", "--help", "-h", "--fixtures", "--markers", "--setup-plan"},
    "unittest": {"--help", "-h"},
    "jest": {"--listTests", "--version", "-v", "--help", "-h", "--showConfig"},
    "vitest": {"--version", "-v", "--help", "-h", "list"},
    "mocha": {"--version", "-V", "--help", "-h", "--dry-run", "--list-files"},
    "ava": {"--version", "--help"},
    "go test": {"-list", "-c", "-n", "-h", "-help"},
    "cargo test": {"--no-run", "--list", "--help", "-h"},
    "npm test": {"--help", "-h"}, "pnpm test": {"--help", "-h"}, "yarn test": {"--help", "-h"}, "bun test": {"--help"},
    "playwright": {"--list", "--help", "-h"},
    "xcodebuild test": {"-showBuildSettings", "-showTestPlans", "-list", "-help", "-version", "-showdestinations",
                        "-showsdks", "-dry-run"},
    "swift test": {"--list-tests", "-l", "--help", "-h", "--version"},
    "gradle test": {"--dry-run", "-m", "--help", "-h", "--version", "-v"},
    "mvn test": {"--help", "-h", "--version", "-v"},
    "make test": {"-n", "--dry-run", "--just-print", "--recon", "-q", "--question", "--help", "-h"},
    "tox": {"--help", "-h", "--version", "-l", "--listenvs", "--listenvs-all", "-a", "--showconfig", "--notest"},
    "nox": {"--help", "-h", "--version", "-l", "--list", "--list-sessions"},
    "rspec": {"--dry-run", "--help", "-h", "--version", "-v"},
    "phpunit": {"--list-tests", "--list-suites", "--list-groups", "--help", "-h", "--version"},
    "ctest": {"-N", "--show-only", "--help", "--version"},
    "bats": {"--count", "-c", "--help", "-h", "--version", "-v"},
    "dotnet test": {"--list-tests", "-t", "--help", "-h"},
    "deno test": {"--no-run", "--help", "-h"},
    "mix test": {"--help"},
}


# Arguments that list, collect, dry-run or skip tests for whichever runner they reach (also through `npm test -- ...`).
_NEVER_RUNNING = {"--collect-only", "--co", "--listTests", "--list-tests", "--list", "-list", "--no-run", "--dry-run",
                  "--just-print", "--showConfig", "--setup-plan", "--list-files", "--list-suites", "--list-groups",
                  "--show-only", "--notest", "-DskipTests", "-Dmaven.test.skip", "-DskipITs"}


def _runs_tests(tool: str, words: List[str]) -> bool:
    """False when an argument makes the runner list, collect, dry-run, skip or print help instead of running tests
    (make's combined short flags such as -nk count too)."""
    blocked = _NOT_RUNNING.get(tool, set()) | _NEVER_RUNNING
    for word in words[1:]:  # past `--` too: `cargo test -- --list` lists
        name, _, value = word.partition("=")
        if name in blocked and not (name.startswith("-D") and value.lower() == "false"):
            return False
        if tool == "make test" and word.startswith("-") and not word.startswith("--") and ("n" in word[1:] or "q" in word[1:]):
            return False
    return True


# Words after which a command's exit status is not the test's own.
_CONTROL = {"!", "if", "then", "elif", "else", "while", "until", "do", "{"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GRADLE_TASK = re.compile(r"(?:^|:)(?:test\w*|check|connected\w*Test)$")
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc", ".org")
_DOC_NAMES = ("license", "licence", "changelog", "notice", "authors", "readme")
# Operators after which the last part's exit status is the whole command's.
_PLAIN_OPERATORS = {"&&", ";", "\n"}


# ---- reading a command line -------------------------------------------------------------------------------------------

def _lex(command: str) -> Optional[Tuple[List[List[str]], Set[str]]]:
    """The command's simple commands, each as its words with quotes removed, and every operator it uses (`&&`, `||`,
    `;`, `|`, `&`, `(`, `)`, newline). Comments, heredoc bodies and redirections (`>out`, `2>&1`, `&>log`) are not
    words. None when a quote is never closed."""
    segments: List[List[str]] = [[]]
    # A part that is only a redirection (`; < /dev/null`) is still a command: its exit status can be the last one.
    redirected: List[bool] = [False]
    seen: Set[str] = set()
    word: List[str] = []
    # drop_next: the next word is a redirection's target (`> out`, `< in`, `<<< text`), not part of the command.
    state = {"has_word": False, "drop_next": False, "has_word_quoted": False}
    heredocs: List[Tuple[str, bool]] = []
    quote, i, n = "", 0, len(command)

    def end_word():
        if state["has_word"] or word:
            if state["drop_next"]:
                state["drop_next"] = False
            else:
                segments[-1].append("".join(word))
        del word[:]
        state["has_word"] = state["has_word_quoted"] = False

    def operator(op):
        end_word()
        state["drop_next"] = False
        seen.add(op)
        if segments[-1] or redirected[-1]:
            segments.append([])
            redirected.append(False)

    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                word.append(ch)
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch == "\\" and i + 1 < n and command[i + 1] in '"\\$`':
                i += 1
                word.append(command[i])
            else:
                word.append(ch)
        elif ch in ("'", '"'):
            quote, state["has_word"], state["has_word_quoted"] = ch, True, True
        elif ch == "\\":
            if i + 1 < n and command[i + 1] != "\n":
                word.append(command[i + 1])
                state["has_word"] = True
            i += 2
            continue
        elif ch == "#" and not word and not state["has_word"]:
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif ch == "\n":
            operator("\n")
            i += 1
            if heredocs:
                i = _skip_heredocs(command, i, heredocs)
                heredocs = []
            continue
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            end_word()
            redirected[-1] = True  # a part that is only a heredoc is still a command with its own exit status
            i += 2
            strip = command.startswith("-", i)
            i += strip
            while i < n and command[i] in " \t":
                i += 1
            start = i
            while i < n and not command[i].isspace() and command[i] not in ";&|()<>":
                i += 1
            delimiter = command[start:i].replace("'", "").replace('"', "").replace("\\", "")
            if delimiter:
                heredocs.append((delimiter, strip))
            continue
        elif ch in "<>" or command.startswith("&>", i):
            if word and not state["has_word_quoted"] and "".join(word).isdigit():
                del word[:]  # the file descriptor of `2>/dev/null`
                state["has_word"] = False
            end_word()
            redirected[-1] = True
            i += 2 if ch == "&" else 1
            while i < n and command[i] in "<>":
                i += 1
            if i < n and command[i] == "&":  # 2>&1, <&3, >&-
                i += 1
                while i < n and (command[i].isdigit() or command[i] == "-"):
                    i += 1
            else:
                state["drop_next"] = True
            continue
        elif ch in ";&|()":
            op = command[i:i + 2] if command[i:i + 2] in ("&&", "||", ";;", "|&") else ch
            operator(op)
            i += len(op)
            continue
        elif ch.isspace():
            end_word()
        else:
            word.append(ch)
            state["has_word"] = True
        i += 1
    if quote:
        return None
    end_word()
    return [segment for segment, r in zip(segments, redirected) if segment or r], seen


def _skip_heredocs(command: str, i: int, heredocs: List[Tuple[str, bool]]) -> int:
    """Past the bodies of the heredocs started on the line that just ended (each up to its delimiter line)."""
    for delimiter, strip in heredocs:
        while i < len(command):
            end = command.find("\n", i)
            line = command[i:] if end < 0 else command[i:end]
            i = len(command) if end < 0 else end + 1
            if (line.lstrip("\t") if strip else line) == delimiter:
                break
    return i


def _skip_options(words: List[str], start: int, takes_value=frozenset()) -> int:
    """The index of the first word from `start` that is not an option (nor the value of one in `takes_value`)."""
    i = start
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        i += 1 + (words[i] in takes_value and i + 1 < len(words))
    return i


def classify(command: Any, depth: int = 0) -> Optional[Tuple[str, bool]]:
    """(TEST_TOOLS name, whether the command's exit status is that test's own) for a command that starts a test runner
    (in any of its parts, behind a shell's `-c`, `env`, `npx`, `uv run` and similar), or None. A guess from the command
    line only. The exit status is the test's own only when the runner is the last part of a plain `&&` / `;` chain."""
    if not isinstance(command, str) or depth > 3:
        return None
    lexed = _lex(command)
    if not lexed:
        return None
    segments, seen = lexed
    plain = seen <= _PLAIN_OPERATORS
    for k, segment in enumerate(segments):
        found = _segment_tool(segment, depth)
        if found:
            tool, own = found
            return tool, own and plain and k == len(segments) - 1
    return None


def test_tool(command: Any) -> Optional[str]:
    """The TEST_TOOLS name of the test runner `command` starts, or None (see classify)."""
    found = classify(command)
    return found[0] if found else None


def _segment_tool(words: List[str], depth: int) -> Optional[Tuple[str, bool]]:
    own = True
    while words:
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            return None
        name = words[0].rsplit("/", 1)[-1]
        if words[0] in _CONTROL:
            words, own = words[1:], False
            continue
        if name in _SHELLS:
            i = 1
            while i < len(words):
                word = words[i]
                takes_name = word[:1] in "-+" and not word.startswith("--") and word[1:].endswith("o")
                if word == "--noexec" or (takes_name and words[i + 1:i + 2] == ["noexec"]):
                    return None
                if takes_name:
                    i += 2  # -o pipefail, -euo pipefail, +o posix: the option's name
                    continue
                if word.startswith("--") or word.startswith("+"):
                    i += 1
                    continue
                if not word.startswith("-"):
                    return None  # a script file, not `-c`
                if "n" in word[1:]:
                    return None  # -n reads the script without running it
                if "c" in word[1:]:
                    found = classify(words[i + 1], depth + 1) if i + 1 < len(words) else None
                    return (found[0], found[1] and own) if found else None
                i += 1
            return None
        if name == "command":
            i = _skip_options(words, 1)
            if any("v" in w[1:] or "V" in w[1:] for w in words[1:i]):
                return None  # asks where a command is (-v, -V, -pv); runs nothing
            words = words[i:]
            continue
        if name == "env":
            i = 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGNMENT.match(words[i])):
                i += 1 + (words[i] in _VALUE_OPTIONS["env"] and i + 1 < len(words))
            words = words[i:]
            continue
        if name == "nice":
            words = words[_skip_options(words, 1, _VALUE_OPTIONS["nice"]):]
            continue
        if name == "timeout":
            words = words[_skip_options(words, 1, _VALUE_OPTIONS["timeout"]) + 1:]  # and the duration
            continue
        if name in _PLAIN_WRAPPERS:
            words = words[_skip_options(words, 1, _VALUE_OPTIONS.get(_PLAIN_WRAPPERS[name], frozenset())):]
            continue
        if name in _RUNNERS:
            i = _skip_options(words, 1, _VALUE_OPTIONS["runner"])
            if i < len(words) and words[i] == "run":
                words = words[_skip_options(words, i + 1, _VALUE_OPTIONS["runner"]):]
                continue
            return None
        tool = _tool_of(name, words, depth)
        if isinstance(tool, tuple):
            return tool[0], tool[1] and own
        return (tool, own) if tool and _runs_tests(tool, words) else None
    return None


def _python_tool(words: List[str]) -> Optional[str]:
    """`python -m pytest`, `python -Bm pytest`; never when `-c` runs code or a script comes first (what follows is its
    arguments, not Python's options)."""
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--" or not word.startswith("-") or word == "-":
            name = word.rsplit("/", 1)[-1] if word not in ("--", "-") else ""
            return _STANDALONE.get(name) if name in ("pytest", "py.test") else None
        if word.startswith("--"):
            i += 1
            continue
        flags = word[1:]
        for n, flag in enumerate(flags):
            if flag == "c":
                return None
            if flag == "m":
                module = flags[n + 1:] or (words[i + 1] if i + 1 < len(words) else "")
                return _PYTHON_MODULES.get(module)
            if flag in "WXQ":
                if n == len(flags) - 1:
                    i += 1  # its value is the next word
                break
        i += 1
    return None


def _tool_of(name: str, words: List[str], depth: int):
    rest = words[1:]
    if name in _STANDALONE:
        return _STANDALONE[name]
    if re.fullmatch(r"python[0-9.]*", name):
        return _python_tool(words)
    if name in _SUBCOMMAND:
        sub, tool = _SUBCOMMAND[name]
        i = _skip_options(words, 1)
        return tool if i < len(words) and words[i] == sub else None
    if name == "cargo":
        i = 1
        while i < len(words) and (words[i].startswith("+") or words[i].startswith("-")):
            i += 1
        if i < len(words) and words[i] == "nextest":
            j = _skip_options(words, i + 1)
            return "cargo test" if j < len(words) and words[j] == "run" else None  # `nextest list` lists
        return "cargo test" if i < len(words) and words[i] == "test" else None
    if name in _PACKAGE_MANAGERS:
        i = _skip_options(words, 1, _VALUE_OPTIONS["package"])
        sub = words[i] if i < len(words) else ""
        if sub in ("test", "t", "tst") or (name in ("yarn", "pnpm") and sub.startswith("test")):
            return _PACKAGE_MANAGERS[name]
        if sub in ("run", "run-script"):
            j = _skip_options(words, i + 1, _VALUE_OPTIONS["package"])
            return _PACKAGE_MANAGERS[name] if j < len(words) and words[j].startswith("test") else None
        if sub in ("exec", "dlx", "x"):
            return _segment_tool(words[_skip_options(words, i + 1, _VALUE_OPTIONS["package"]):], depth)
        return None
    if name == "xcodebuild":
        return "xcodebuild test" if any(w in ("test", "test-without-building") for w in rest) else None
    if name in ("gradle", "gradlew"):
        tasks, skip = [], False
        for w in rest:
            if skip:
                skip = False
            elif w in ("-x", "--exclude-task"):
                skip = True  # its value is a task left out, not run
            elif not w.startswith("-"):
                tasks.append(w)
        return "gradle test" if any(_GRADLE_TASK.search(w) for w in tasks) else None
    if name in ("mvn", "mvnw"):
        return "mvn test" if any(w in ("test", "verify") for w in rest) else None
    if name == "make":
        return "make test" if any(w in ("test", "tests", "check") for w in rest if not w.startswith("-")) else None
    return None


# ---- counting a run's commands ----------------------------------------------------------------------------------------

class Commands:
    """One run's commands, counted as its stream is read. Feed it every line (`feed`, run_streaming's `on_line`, or a
    stream reader's `on_unreadable`), every decoded message (`add`, the stream host's `on_message`) or every App Server
    item (`app_server_item`) of one tool, then read `total()`."""

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
        """One raw stdout line. A line that looks like JSON and does not decode means the stream was not read whole."""
        if not isinstance(line, str) or not line.lstrip().startswith("{"):
            return
        try:
            msg = json.loads(line.strip())
        except ValueError:
            self.broken = True
            return
        self.add(msg)

    def unreadable(self, *_: Any) -> None:
        """A line of this run's stream could not be read: its commands are not known whole."""
        self.broken = True

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
        found = classify(command)
        if found:
            tool, own = found
            self.tests_ran += 1
            if own:
                self.tests_failed += bool(failed)
                self.last_test = "failed" if failed else "passed"
            else:
                self.last_test = "unknown"
            if tool not in self.tools and len(self.tools) < TOOLS_MAX:
                self.tools.append(tool)

    # ---- output -----------------------------------------------------------------------------------------------------

    def total(self) -> Optional[Dict[str, Any]]:
        """{"commands": {ran, failed}, "tests": {ran, failed, last?, tools?}}, or None when the stream was not read
        whole. tests.failed counts only test commands whose exit status was their own; last is passed, failed or
        unknown."""
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
