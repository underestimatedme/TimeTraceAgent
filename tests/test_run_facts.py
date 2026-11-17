"""运行事实: the commands, tests and changes the computer itself saw one run make, carried by its completion event
beside the AI's own report.

Contract: docs/superpowers/specs/2026-11-17-run-facts-design.md (TimeTrace repository)."""
import io
import json
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from timetrace import run_facts, worktree
from timetrace.adapters import claude, codex
import queue

from timetrace.adapters.codex_interactive import UNREADABLE, CodexTurn, _read_messages
from timetrace.agent import Agent
from timetrace.claude_stream import StreamSession
from timetrace.models import RunResult
from tests.test_codex_interactive import Host
from tests.test_remote_jobs import Adapter, Base, ControlCloud
from tests.test_token_usage import lines, printed

SESSION = "5b0c1f0e-2f43-4b8e-9d55-0d7c3a1e9a10"


def bash(use_id, command, **extra):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": "Bash", "input": dict({"command": command}, **extra)}]}})


def outcome(use_id, is_error=False, content="ok"):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": use_id, "is_error": is_error, "content": content}]}})


def item(item_id, command, exit_code, status="completed"):
    return json.dumps({"type": "item.completed", "item": {"id": item_id, "type": "command_execution",
                                                          "command": command, "exit_code": exit_code,
                                                          "status": status}})


def counted(tool, output):
    commands = run_facts.Commands(tool)
    for line in output:
        commands.feed(line)
    return commands.total()


class TestToolTest(unittest.TestCase):
    def test_test_runners_behind_shells_wrappers_and_package_managers(self):
        cases = {
            "pytest -q": "pytest",
            "python3 -m pytest tests/test_x.py": "pytest",
            "python -m unittest discover": "unittest",
            ".venv/bin/pytest": "pytest",
            "uv run pytest": "pytest",
            "poetry run python -m pytest": "pytest",
            "cd web && npm test": "npm test",
            "npm --prefix web test": "npm test",
            "npm run test:unit -- --run": "npm test",
            "pnpm --filter web test": "pnpm test",
            "yarn test:e2e": "yarn test",
            "bun test": "bun test",
            "npx vitest run": "vitest",
            "CI=1 npx jest --ci": "jest",
            "go test ./...": "go test",
            "cargo +nightly test -p core": "cargo test",
            "cargo nextest run": "cargo test",
            "xcodebuild -scheme App test": "xcodebuild test",
            "swift test": "swift test",
            "./gradlew :app:testDebugUnitTest": "gradle test",
            "mvn -q verify": "mvn test",
            "make -C web test": "make test",
            "timeout 600 go test ./...": "go test",
            "env FOO=1 pytest": "pytest",
            "/bin/zsh -lc \"cd web && npm test\"": "npm test",
            "bash -c 'python3 -m pytest -x'": "pytest",
            "npx playwright test": "playwright",
            "git status && pytest | tail -5": "pytest",
            "dotnet test": "dotnet test",
            "env -u FOO pytest": "pytest",
            "env --chdir web npm test": "npm test",
            "uv run --project web pytest": "pytest",
            "uv run --with pytest-cov pytest": "pytest",
            "npx -p jest jest --ci": "jest",
            "python3 -Bm pytest": "pytest",
            "python3 -W ignore -m pytest": "pytest",
            "python3 .venv/bin/pytest -q": "pytest",
            "pytest>out.txt": "pytest",
            "pytest -q 2>&1 | tail -5": "pytest",
            "echo 'a;b' && go test ./...": "go test",
        }
        for command, tool in cases.items():
            self.assertEqual(run_facts.test_tool(command), tool, command)
            self.assertIn(tool, run_facts.TEST_TOOLS)

    def test_commands_that_only_mention_a_test_runner_are_not_tests(self):
        for command in ("grep -rn pytest .", "cat jest.config.js", "pip install pytest", "npm install vitest",
                        "npm run build", "go vet ./...", "cargo build", "make build", "git commit -m 'pytest'",
                        "echo npm test", "python3 -c 'import pytest'", "ls tests", "", "bash -c", "'unclosed",
                        # What follows `-c` code or a script is its arguments; a quoted or escaped `;` is a word.
                        'python3 -B -c "print(1)" -m pytest', "python3 script.py -m pytest", "bash script.sh -c pytest",
                        'echo ";" pytest', "echo a\\;pytest", "uv run --with pytest python script.py",
                        # A comment, a heredoc's body and a lookup run nothing.
                        "printf ok # ; pytest", "cat <<EOF\npytest\nEOF", "cat <<-'END'\n\tpytest\n\tEND",
                        "command -v pytest", "type pytest",
                        None, 42):
            self.assertIsNone(run_facts.test_tool(command), command)


class OutcomeTest(unittest.TestCase):
    def test_the_exit_status_is_the_tests_own_only_at_the_end_of_a_plain_chain(self):
        own = ["pytest -q", "pytest -q 2>&1", "pytest &>log.txt", "cd web && npm test", "echo start; pytest",
               "# run them\npytest -q", "cat <<EOF > a.txt\nhi\nEOF\npytest -q", "/bin/zsh -lc \"cd web && npm test\"",
               "timeout 600 go test ./...", "uv run pytest"]
        masked = ["pytest || true", "true || pytest", "pytest | tail -5", "pytest &", "! pytest",
                  "if pytest; then echo ok; fi", "(cd web && npm test)", "pytest; echo done",
                  "/bin/zsh -lc \"pytest | tail\"", "{ pytest; }"]
        for command in own:
            self.assertEqual(run_facts.classify(command)[1], True, command)
        for command in masked:
            self.assertEqual(run_facts.classify(command)[1], False, command)

    def test_a_masked_outcome_is_unknown_never_passed(self):
        output = [bash("a", "python3 -B -m unittest missing_module || true"), outcome("a"),
                  bash("b", "pytest | tail -5"), outcome("b")]
        self.assertEqual(counted("claude", output)["tests"],
                         {"ran": 2, "failed": 0, "last": "unknown", "tools": ["unittest", "pytest"]})
        output += [bash("c", "pytest -q"), outcome("c", True, "Exit code 1")]
        self.assertEqual(counted("claude", output)["tests"],
                         {"ran": 3, "failed": 1, "last": "failed", "tools": ["unittest", "pytest"]})


class ClaudeCommandsTest(unittest.TestCase):
    def test_real_claude_code_stream(self):
        # Claude Code 2.1.325: `python3 -c 'print(1)'`, then `python3 -c 'import sys; sys.exit(3)'`.
        self.assertEqual(counted("claude", lines("claude_stream_commands.jsonl")),
                         {"commands": {"ran": 2, "failed": 1}, "tests": {"ran": 0, "failed": 0}})

    def test_tests_their_last_outcome_and_tools(self):
        output = [bash("a", "pytest -q"), outcome("a", True, "Exit code 1\nFAILED test_x"),
                  bash("b", "ls"), outcome("b"),
                  bash("c", "cd web && npm test"), outcome("c"),
                  bash("d", "pytest -q"), outcome("d")]
        self.assertEqual(counted("claude", output), {
            "commands": {"ran": 4, "failed": 1},
            "tests": {"ran": 3, "failed": 1, "last": "passed", "tools": ["pytest", "npm test"]}})
        output.append(bash("e", "pytest -q"))
        output.append(outcome("e", True, [{"type": "text", "text": "Error: Exit code 2"}]))
        self.assertEqual(counted("claude", output)["tests"]["last"], "failed")

    def test_a_call_that_did_not_run_or_has_no_result_is_not_a_command(self):
        output = [bash("a", "pytest"), outcome("a", True, "Permission to use Bash has been denied."),
                  bash("b", "npm test", run_in_background=True), outcome("b", False, "Command running in background"),
                  bash("c", "go test ./..."),  # never answered: the run ended first
                  outcome("zzz"),              # an answer to nothing seen
                  bash("d", "sleep 999"), outcome("d", True, "Command timed out after 2m 0.0s")]
        self.assertEqual(counted("claude", output), {"commands": {"ran": 1, "failed": 1}, "tests": {"ran": 0, "failed": 0}})

    def test_the_stream_reader_tells_of_a_line_it_could_not_read(self):
        # Claude's stream session decodes each line before any watcher sees it; one it cannot decode must still mean
        # "not seen whole", never zero commands.
        commands = run_facts.Commands("claude")
        session = StreamSession(["claude"], ".", "/dev/null", "go", on_message=commands.add, on_unreadable=commands.feed)
        output = [bash("a", "pytest"), '{"type":"user","message":{"content":[{"type":"tool_result", broken',
                  json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "s"})]
        session._read_stdout(io.StringIO("\n".join(output) + "\n"), io.StringIO())
        self.assertIsNone(commands.total())
        fine = run_facts.Commands("claude")
        session = StreamSession(["claude"], ".", "/dev/null", "go", on_message=fine.add, on_unreadable=fine.feed)
        session._read_stdout(io.StringIO("\n".join([bash("a", "pytest"), outcome("a"), "a plain-text warning"]) + "\n"),
                             io.StringIO())
        self.assertEqual(fine.total()["tests"], {"ran": 1, "failed": 0, "last": "passed", "tools": ["pytest"]})

    def test_subagent_commands_count_and_other_tools_do_not(self):
        nested = json.loads(bash("a", "pytest"))
        nested["parent_tool_use_id"] = "toolu_parent"
        read = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "x"}}]}})
        self.assertEqual(counted("claude", [json.dumps(nested), outcome("a"), read, outcome("r")])["tests"]["ran"], 1)

    def test_a_command_line_it_could_not_read_means_no_facts_rather_than_zeros(self):
        output = [bash("a", "pytest"), '{"type":"user","message":{"content":[{"type":"tool_result", broken']
        self.assertIsNone(counted("claude", output))
        # Cut before any marker of what it was: still not read whole.
        self.assertIsNone(counted("claude", [bash("a", "pytest"), '{"type":"user","message":']))
        self.assertIsNone(counted("codex_exec", [item("1", "pytest", 0), '{"type":"item.comp']))
        self.assertIsNotNone(counted("claude", [bash("a", "pytest"), outcome("a"), "warning: not json"]))
        commands = run_facts.Commands("claude")
        commands.add({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                                                    "input": []}]}})
        self.assertIsNotNone(commands.total())  # an odd shape is skipped, never a crash

    def test_every_claude_run_path_reports_commands(self):
        fixture = lines("claude_stream_commands.jsonl")
        sid = str(uuid.uuid4())

        def run(call, output):
            def fake(cmd, *args, on_line=None, on_message=None, **kwargs):
                return printed(output, on_line, on_message)
            with patch.object(claude, "run_streaming", fake), patch.object(claude, "run_stream", fake):
                return call(claude.ClaudeAdapter({"bin": "claude"}, billing=object(), credentials=lambda: {}))

        expected = {"commands": {"ran": 2, "failed": 1}, "tests": {"ran": 0, "failed": 0}}
        with tempfile.TemporaryDirectory() as d:
            log = str(Path(d) / "run.log")
            for name, call in {"start": lambda a: a.start("go", d, sid, log),
                               "stream resume": lambda a: a.resume("go", d, sid, log, control=object()),
                               "folder": lambda a: a.start_folder("go", d, d, sid, log)}.items():
                self.assertEqual(run(call, fixture).commands, expected, name)
            res = run(lambda a: a.work_session("go", d, SESSION, log, threading.Event(), control=object(),
                                               resuming=False, read_only=False), fixture)
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.commands, expected)


class CodexCommandsTest(unittest.TestCase):
    def test_real_codex_exec_stream(self):
        # Codex 0.165.0: the same two commands, run as `/bin/zsh -lc "..."`.
        self.assertEqual(counted("codex_exec", lines("codex_exec_commands.jsonl")),
                         {"commands": {"ran": 2, "failed": 1}, "tests": {"ran": 0, "failed": 0}})

    def test_status_and_exit_code(self):
        output = [item("1", "/bin/zsh -lc 'pytest -q'", 1, "failed"), item("2", "/bin/zsh -lc 'pytest -q'", 0),
                  item("3", "/bin/zsh -lc 'rm -rf x'", None, "declined"), item("4", "/bin/zsh -lc 'go test ./...'", 2),
                  item("4", "/bin/zsh -lc 'go test ./...'", 2),  # the same item again counts once
                  json.dumps({"type": "item.started", "item": {"id": "5", "type": "command_execution",
                                                               "command": "pytest", "status": "in_progress"}})]
        self.assertEqual(counted("codex_exec", output), {
            "commands": {"ran": 3, "failed": 2},
            "tests": {"ran": 3, "failed": 2, "last": "failed", "tools": ["pytest", "go test"]}})

    def test_adapter_reports_commands(self):
        fixture = lines("codex_exec_commands.jsonl")
        with tempfile.TemporaryDirectory() as d, \
                patch.object(codex, "run_streaming", lambda *a, on_line=None, **k: printed(fixture, on_line)), \
                patch.object(codex, "thread_model", lambda *a, **k: ""):
            res = codex.CodexAdapter({"bin": "codex"}).start("go", d, "", str(Path(d) / "run.log"))
        self.assertEqual(res.commands, {"commands": {"ran": 2, "failed": 1}, "tests": {"ran": 0, "failed": 0}})

    def test_app_server_line_it_could_not_read_means_no_facts(self):
        incoming = queue.SimpleQueue()
        _read_messages(io.StringIO('{"method":"item/completed","params":{"item":{"type":"commandExecution"\nnot json\n'),
                       incoming)
        self.assertIs(incoming.get_nowait(), UNREADABLE)
        self.assertIsNone(incoming.get_nowait())  # the plain-text line is skipped; then the end
        turn = CodexTurn(["codex"], ".", Host(), threading.Event(), 30, False)
        turn.thread_id, turn.turn_id = "th", "tu"
        turn.incoming.put(UNREADABLE)
        self.assertTrue(turn._receive())
        turn._complete({"status": "completed", "items": []})
        self.assertIsNone(turn.commands.total())

    def test_app_server_turn_counts_its_own_command_items(self):
        turn = CodexTurn(["codex"], ".", Host(), threading.Event(), 30, False)
        turn.thread_id, turn.turn_id = "th", "tu"

        def done(item_id, command, code, status="completed", turn_id="tu", thread="th"):
            turn._notification("item/completed", {"threadId": thread, "turnId": turn_id, "item": {
                "id": item_id, "type": "commandExecution", "command": command, "exitCode": code, "status": status}})
        done("1", "/bin/zsh -lc 'npm test'", 1, "failed")
        done("2", "/bin/zsh -lc 'npm test'", 0)
        done("3", "/bin/zsh -lc 'npm test'", 0, turn_id="other")   # another turn's
        done("4", "/bin/zsh -lc 'npm test'", 0, thread="other")    # another thread's
        turn._complete({"status": "completed", "items": [
            {"id": "2", "type": "commandExecution", "command": "npm test", "exitCode": 0, "status": "completed"}]})
        self.assertEqual(turn.commands.total(), {
            "commands": {"ran": 2, "failed": 1},
            "tests": {"ran": 2, "failed": 1, "last": "passed", "tools": ["npm test"]}})


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


class ChangesTest(unittest.TestCase):
    def repo(self, d):
        git(d, "init", "-q")
        git(d, "config", "user.email", "t@example.com")
        git(d, "config", "user.name", "t")
        (Path(d) / "a.py").write_text("x = 1\n")
        git(d, "add", ".")
        git(d, "commit", "-qm", "base")
        return git(d, "rev-parse", "HEAD")

    def test_counts_files_non_doc_files_and_lines_without_paths(self):
        with tempfile.TemporaryDirectory() as d:
            base = self.repo(d)
            (Path(d) / "a.py").write_text("x = 2\ny = 3\n")
            (Path(d) / "docs").mkdir()
            (Path(d) / "docs" / "guide.txt").write_text("hello\n")
            (Path(d) / "README.md").write_text("# r\n")
            (Path(d) / "logo.png").write_bytes(b"\x89PNG\x00\x01")
            git(d, "add", ".")
            git(d, "commit", "-qm", "work")
            head = git(d, "rev-parse", "HEAD")
            self.assertEqual(worktree.diff_numbers(d, base, head),
                             {"files": 4, "non_doc_files": 2, "insertions": 4, "deletions": 1})
            self.assertIsNone(worktree.diff_numbers(d, "nothex", head))
            self.assertIsNone(worktree.diff_numbers(d, "0" * 40, head))

    def test_doc_paths(self):
        for path in ("README.md", "docs/a.py", "CHANGELOG", "LICENSE.txt", "notes.rst", "Doc/x.MD"):
            self.assertTrue(run_facts.is_doc(path), path)
        for path in ("src/readme_parser.py", "web/src/App.tsx", "documents.py"):
            self.assertFalse(run_facts.is_doc(path), path)

    def test_attach_run_facts(self):
        commands = {"commands": {"ran": 3, "failed": 0},
                    "tests": {"ran": 1, "failed": 0, "last": "passed", "tools": ["pytest"]}}
        with tempfile.TemporaryDirectory() as d:
            base = self.repo(d)
            (Path(d) / "b.py").write_text("print(1)\n")
            git(d, "add", ".")
            git(d, "commit", "-qm", "work")
            head = git(d, "rev-parse", "HEAD")
            event = {"input_head": base, "output_commit": head}
            Agent._attach_run_facts(event, RunResult(ok=True, commands=commands), d, ["new.txt"], False)
            self.assertEqual(event["run_facts"], dict(commands, version=1, changes={
                "files": 1, "non_doc_files": 1, "insertions": 1, "deletions": 0, "left_out": 1}))

            # Resumed: the commands are only those after it resumed; the changes are still the whole delivery.
            event = {"input_head": base, "output_commit": head}
            Agent._attach_run_facts(event, RunResult(ok=True, commands=commands), d, [], True)
            self.assertTrue(event["run_facts"]["resumed"])
            self.assertEqual(event["run_facts"]["changes"]["files"], 1)
            event = {"input_head": base, "output_commit": head}
            Agent._attach_run_facts(event, RunResult(ok=True), d, [], True)  # no commands: nothing is partial
            self.assertNotIn("resumed", event["run_facts"])
            # Changes from a checkpoint's commit would be only part of the delivery: none.
            event = {"input_head": base, "output_commit": head}
            Agent._attach_run_facts(event, RunResult(ok=True, commands=commands), d, [], True, changes_partial=True)
            self.assertNotIn("changes", event["run_facts"])

            event = {}  # no commits (a folder run): commands only
            Agent._attach_run_facts(event, RunResult(ok=True, commands=commands), d, [], False)
            self.assertEqual(event["run_facts"], dict(commands, version=1))

            event = {}
            Agent._attach_run_facts(event, RunResult(ok=True), d, [], False)  # nothing seen: no facts
            self.assertNotIn("run_facts", event)

            with patch.object(worktree, "diff_numbers", side_effect=RuntimeError("boom")):
                event = {"input_head": base, "output_commit": head}
                Agent._attach_run_facts(event, RunResult(ok=True, commands=commands), d, [], False)
                self.assertNotIn("run_facts", event)


class Finishes(Adapter):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        return self.result


class TerminalEventRunFactsTest(Base):
    def test_only_a_completion_carries_run_facts(self):
        commands = {"commands": {"ran": 1, "failed": 1}, "tests": {"ran": 1, "failed": 1, "last": "failed",
                                                                    "tools": ["pytest"]}}
        cases = [
            (RunResult(exit_code=0, ok=True, output="done", session_id="s", commands=commands), "awaiting_review",
             dict(commands, version=1)),
            (RunResult(exit_code=0, ok=True, output="done", session_id="s"), "awaiting_review", None),
            (RunResult(exit_code=1, error="boom", session_id="s", commands=commands), "failed", None),
        ]
        for n, (result, state, expected) in enumerate(cases):
            job = "j%d" % n
            cloud = ControlCloud([{"id": job, "plan_id": "p%d" % n}])
            self.assertEqual(self.agent(cloud, Finishes(result)).run_once(), "job %s → %s" % (job, state))
            event = self.events(cloud, job)[-1]
            if expected is None:
                self.assertNotIn("run_facts", event)
            else:
                self.assertEqual(event["run_facts"], expected)


if __name__ == "__main__":
    unittest.main()
