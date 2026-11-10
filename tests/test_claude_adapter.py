import json
import unittest
from pathlib import Path

from timetrace.adapters import claude
from timetrace.adapters.base import SAFETY_RULES

FIXTURES = Path(__file__).parent / "fixtures"


def load_lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class ParseStreamTest(unittest.TestCase):
    def test_success_run_yields_samples_and_session(self):
        res = claude.parse_stream(load_lines("claude_stream.jsonl"))
        self.assertTrue(res.ok)
        self.assertFalse(res.blocked)
        self.assertEqual(res.session_id, "d84c4ed6-6c86-42bf-ba75-21c6e635b3dc")
        self.assertEqual(res.output, "ACK")
        keys = {s.bucket_key: s for s in res.samples}
        self.assertEqual(set(keys), {"claude:five_hour", "claude:seven_day"})
        self.assertEqual(keys["claude:five_hour"].used_pct, 13.0)
        self.assertEqual(keys["claude:five_hour"].reset_at, 1788370200)
        self.assertEqual(keys["claude:five_hour"].window_mins, 300)
        self.assertTrue(keys["claude:five_hour"].is_representative)
        self.assertFalse(keys["claude:seven_day"].is_representative)
        self.assertEqual(keys["claude:seven_day"].used_pct, 3.0)
        self.assertEqual(res.reset_at, 1788370200)

    def test_rejected_status_marks_blocked(self):
        lines = load_lines("claude_stream.jsonl")
        lines = [l.replace('"status":"allowed"', '"status":"rejected"') for l in lines]
        res = claude.parse_stream(lines)
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)
        self.assertEqual(res.reset_at, 1788370200)

    def test_api_error_429_marks_blocked(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "API Error: 429", "session_id": "s", "api_error_status": 429}
        res = claude.parse_stream([json.dumps(result)])
        self.assertTrue(res.blocked)
        self.assertFalse(res.ok)

    def test_limit_text_with_error_marks_blocked(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "You've hit your limit · resets 9pm", "session_id": "s",
                  "api_error_status": None}
        res = claude.parse_stream([json.dumps(result)])
        self.assertTrue(res.blocked)

    def test_plain_error_is_failure_not_block(self):
        result = {"type": "result", "subtype": "error_max_turns", "is_error": True,
                  "result": "stopped", "session_id": "s", "api_error_status": None}
        res = claude.parse_stream([json.dumps(result)])
        self.assertFalse(res.blocked)
        self.assertFalse(res.ok)
        self.assertIn("error_max_turns", res.error)

    def test_garbage_lines_ignored(self):
        res = claude.parse_stream(["", "not json", "{broken"])
        self.assertFalse(res.ok)
        self.assertEqual(res.samples, [])


class BuildCmdTest(unittest.TestCase):
    cfg = {"bin": "claude", "permission_mode": "acceptEdits", "model": None, "extra_args": [],
           "allowed_tools": ["Bash(git add:*)", "Bash(git commit:*)"]}

    def test_start_uses_session_id(self):
        cmd = claude.build_cmd(self.cfg, "do it", session_id="abc")
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash(git push*)")
        i = cmd.index("--allowedTools")
        self.assertEqual(cmd[i + 1:i + 3], ["Bash(git add:*)", "Bash(git commit:*)"])
        # The variadic --allowedTools must be closed by another option.
        self.assertTrue(cmd[i + 3].startswith("--"), cmd[i + 3])

    def test_no_allowed_tools_flag_when_empty(self):
        cmd = claude.build_cmd(dict(self.cfg, allowed_tools=[]), "x", session_id="abc")
        self.assertNotIn("--allowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], SAFETY_RULES)
        self.assertEqual(cmd[-1], "x")

    def test_resume_uses_resume_flag_and_model(self):
        cfg = dict(self.cfg, model="haiku", extra_args=["--effort", "low"])
        cmd = claude.build_cmd(cfg, "go on", session_id="abc", resume="abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertIn("--effort", cmd)


    def test_prompt_that_looks_like_an_option_stays_a_prompt(self):
        # A phone-supplied prompt is the last argv element. If it starts with
        # "-", Claude's option parser would read e.g. --settings={hooks...}
        # or --permission-mode=bypassPermissions as a flag.
        for prompt in ("--settings={\"hooks\":{}}", "-p", "--permission-mode=bypassPermissions"):
            with self.subTest(prompt=prompt):
                cmd = claude.build_cmd(self.cfg, prompt, session_id="abc")
                self.assertFalse(cmd[-1].startswith("-"))
                self.assertEqual(cmd[-1].strip(), prompt)
        self.assertEqual(claude.build_cmd(self.cfg, "normal", session_id="abc")[-1], "normal")

    def test_project_settings_from_the_worktree_are_not_loaded(self):
        # Worktree content (or a previous run's commit) could carry
        # .claude/settings.json with hooks or a broad allow list; -p mode skips
        # the trust dialog, so only the user's own settings are loaded.
        cmd = claude.build_cmd(self.cfg, "x", session_id="abc")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        cmd = claude.build_cmd(dict(self.cfg, setting_sources="user,project"), "x", session_id="abc")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user,project")


class StreamCmdTest(unittest.TestCase):
    cfg = {"bin": "claude", "permission_mode": "acceptEdits", "model": None, "extra_args": [],
           "allowed_tools": ["Bash(git add:*)"]}

    def test_task_run_is_bidirectional_with_host_permission_prompts(self):
        cmd = claude.build_stream_cmd(self.cfg, session_id="abc")
        self.assertEqual(cmd[:7], ["claude", "-p", "--input-format", "stream-json", "--output-format",
                                   "stream-json", "--verbose"])
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "host")
        self.assertEqual(cmd[cmd.index("--permission-prompt-tool") + 1], "stdio")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "abc")
        i = cmd.index("--disallowedTools")
        self.assertEqual(cmd[i + 1:i + 3], ["Bash(git push:*)", "Bash(git push*)"])
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], SAFETY_RULES)
        # The prompt is never an argv element (it goes over stdin).
        self.assertTrue(cmd[-1] == SAFETY_RULES or cmd[-2].startswith("--"))

    def test_resume_and_explicit_widening_are_kept(self):
        cmd = claude.build_stream_cmd(dict(self.cfg, permission_mode="bypassPermissions"), resume="s1")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")

    def test_folder_stream_run_keeps_restricted_mode_and_never_asks(self):
        cmd = claude.build_stream_folder_cmd(self.cfg, "/ws", session_id="s1")
        self.assertIn("--restricted", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertNotIn("--permission-prompt-tool", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/ws")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash")


class StreamAdapterTest(unittest.TestCase):
    """ClaudeAdapter + RunControl against the fake stream-json CLI."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.transcript = self.d / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, turns, on_event=None):
        import os, threading
        from unittest.mock import patch
        from timetrace.billing import StaticBilling
        from timetrace.remote_control import RunControl
        scenario = self.d / "s.json"
        scenario.write_text(json.dumps({"turns": turns}))
        fake = str(Path(__file__).with_name("fake_claude_stream.py"))
        adapter = claude.ClaudeAdapter({"bin": fake, "allowed_tools": []}, billing=StaticBilling(True),
                                       credentials=lambda: {})
        cancel = threading.Event()
        control = RunControl("j1", self.d / "home", cancel, root=str(self.d), user_home=str(self.d / "user"))
        stop = threading.Event()
        def pump():
            while not stop.is_set():
                for event in control.take_events():
                    if on_event:
                        on_event(control, event)
                stop.wait(.02)
        t = threading.Thread(target=pump, daemon=True); t.start()
        env = {"TT_FAKE_SCENARIO": str(scenario), "TT_FAKE_TRANSCRIPT": str(self.transcript)}
        try:
            with patch.dict(os.environ, env):
                result = adapter.start("build it", str(self.d), "11111111-2222-3333-4444-555555555555",
                                       str(self.d / "run.log"), cancel, control=control)
        finally:
            stop.set(); t.join(2)
        for event in control.take_events():
            if on_event:
                on_event(control, event)
        records = [json.loads(l) for l in self.transcript.read_text().splitlines()]
        return result, records

    def test_phone_approved_command_continues_the_run(self):
        seen = []
        def approve(control, event):
            seen.append(event)
            if event["type"] == "approval_requested":
                control.apply_lease({"approvals": [{"request_id": event["request_id"], "decision": "approve",
                                                    "remember": False}]})
        result, records = self.run_adapter([[{"permission": {"tool": "Bash", "input": {"command": "npm test"}}},
                                             {"permission": {"tool": "Bash", "input": {"command": "git push"}}},
                                             {"result": "all green"}]], approve)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "all green")
        argv = records[0]["argv"]
        self.assertNotIn("build it", argv)
        replies = [r["decision"]["reply"]["response"]["behavior"] for r in records if "decision" in r]
        self.assertEqual(replies, ["allow", "deny"])  # git push: local rule, never asked
        self.assertEqual([e["type"] for e in seen], ["approval_requested", "approval_resolved"])




class McpInjectionTest(unittest.TestCase):
    """A dispatched execution run is given TimeTrace's MCP server: a config file that names the token's variable,
    the token in the one process's environment, and nothing of it anywhere else."""

    TOKEN = "ttm_SENTINEL_" + "Z9y8X7w6V5" * 4
    URL = "https://apis.atlaspaces.com/timetrace/api/v1/mcp"

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.transcript = self.d / "transcript.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def access(self, token=None):
        from timetrace.mcp_access import McpAccess
        return McpAccess(self.URL, token or self.TOKEN, 4102444800.0)

    def adapter(self):
        from timetrace.billing import StaticBilling
        fake = str(Path(__file__).with_name("fake_claude_stream.py"))
        return claude.ClaudeAdapter({"bin": fake, "allowed_tools": []}, billing=StaticBilling(True), credentials=lambda: {})

    def run_fake(self, turns, mcp, resume=False, cancel=None, control=True, exit_code=0, log="run.log"):
        import os, threading
        from unittest.mock import patch
        from timetrace.remote_control import RunControl
        scenario = self.d / "s.json"
        scenario.write_text(json.dumps({"turns": turns, "exit_code": exit_code}))
        cancel = cancel or threading.Event()
        ctl = RunControl("j1", self.d / "home", cancel, root=str(self.d), user_home=str(self.d / "user")) if control else None
        before = dict(os.environ)
        env = {"TT_FAKE_SCENARIO": str(scenario), "TT_FAKE_TRANSCRIPT": str(self.transcript)}
        adapter = self.adapter()
        with patch.dict(os.environ, env):
            run = adapter.resume if resume else adapter.start
            result = run("build it", str(self.d), "11111111-2222-3333-4444-555555555555", str(self.d / log), cancel, control=ctl, mcp=mcp)
            during = dict(os.environ)  # (read while the patch is still in: after it, the environment is always restored)
        self.assertEqual(dict(before, **env), during, "the process's own environment changed")
        return result

    def transcript_records(self):
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]

    def leftovers(self):
        return [p for p in self.d.rglob("mcp-*")]

    def test_the_command_names_the_config_and_allows_the_tools_and_never_holds_the_token(self):
        cmd = claude.build_stream_cmd({"bin": "claude", "allowed_tools": ["Bash(npm test)"]}, session_id="abc", mcp_config="/x/mcp.json")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/x/mcp.json")
        allowed = cmd[cmd.index("--allowedTools") + 1:]
        self.assertIn("mcp__timetrace", allowed)
        self.assertIn("Bash(npm test)", allowed)
        self.assertNotIn("--strict-mcp-config", cmd)
        self.assertFalse([part for part in cmd if "ttm_" in part])
        plain = claude.build_cmd({"bin": "claude", "allowed_tools": []}, "do it", session_id="abc", mcp_config="/x/mcp.json")
        self.assertEqual(plain[plain.index("--mcp-config") + 1], "/x/mcp.json")
        self.assertEqual(plain[plain.index("--allowedTools") + 1], "mcp__timetrace")
        self.assertNotIn("--strict-mcp-config", plain)

    def test_the_config_is_followed_by_an_option_so_it_can_never_swallow_the_prompt(self):
        # `--mcp-config <configs...>` and `--allowedTools <tools...>` take every plain word after them: a real Claude Code read
        # the prompt that came right after the config path as a second config file and refused to start (found with the real
        # `claude`, which the fake stream cannot catch). Whatever follows a path of ours must be an option.
        for cfg in ({"bin": "claude"}, {"bin": "claude", "allowed_tools": ["Bash(npm test)"], "model": "opus", "setting_sources": ""},
                    {"bin": "claude", "extra_args": ["--max-turns", "3"]}):
            for cmd in (claude.build_stream_cmd(cfg, session_id="abc", mcp_config="/x/mcp.json"),
                        claude.build_cmd(cfg, "do the task", session_id="abc", mcp_config="/x/mcp.json")):
                at = cmd.index("--mcp-config")
                self.assertTrue(all(word.startswith("-") for word in cmd[at + 2:at + 3]), cmd[at:at + 3])
                self.assertEqual(len(cmd) > at + 2, True)
                tools = cmd.index("--allowedTools")
                words = []
                for word in cmd[tools + 1:]:
                    if word.startswith("-"):
                        break
                    words.append(word)
                self.assertNotIn("do the task", words)
            self.assertEqual(claude.build_cmd(cfg, "do the task", session_id="abc", mcp_config="/x/mcp.json")[-1], "do the task")

    def test_only_the_directory_write_mcp_config_made_is_ever_removed(self):
        victim = self.d / "victim"
        victim.mkdir()
        (victim / "mcp.json").write_text("{}")
        (victim / "keep.txt").write_text("x")
        claude.remove_mcp_config(str(victim / "mcp.json"))
        claude.remove_mcp_config(str(victim / "mcp-x.json"))
        self.assertTrue((victim / "keep.txt").exists())
        path = claude.write_mcp_config(str(self.d), self.access())
        claude.remove_mcp_config(path)
        self.assertFalse(Path(path).parent.exists())
        claude.remove_mcp_config(None)

    def test_without_it_the_command_is_what_it_was(self):
        cfg = {"bin": "claude", "allowed_tools": ["Bash(npm test)"], "model": "opus"}
        for builder, args in ((claude.build_stream_cmd, (cfg,)), (claude.build_cmd, (cfg, "do it"))):
            plain = builder(*args, session_id="abc")
            self.assertNotIn("--mcp-config", plain)
            self.assertNotIn("mcp__timetrace", plain)
            self.assertEqual(plain, builder(*args, session_id="abc", mcp_config=None))

    def test_the_other_kinds_of_run_are_untouched(self):
        cfg = {"bin": "claude"}
        self.assertNotIn("--mcp-config", claude.build_chat_cmd(cfg, "x", session_id="a"))
        self.assertIn("--strict-mcp-config", claude.build_chat_cmd(cfg, "x", session_id="a"))
        for cmd in (claude.build_folder_cmd(cfg, "x", "/w", session_id="a"), claude.build_stream_folder_cmd(cfg, "/w", session_id="a")):
            self.assertNotIn("--mcp-config", cmd)
            self.assertIn("--strict-mcp-config", cmd)

    def test_the_config_file_is_private_from_the_start_and_holds_no_token(self):
        path = claude.write_mcp_config(str(self.d), self.access())
        import os, stat
        self.addCleanup(claude.remove_mcp_config, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        document = json.loads(Path(path).read_text())
        self.assertEqual(document, {"mcpServers": {"timetrace": {"type": "http", "url": self.URL,
                                                                   "headers": {"Authorization": "Bearer ${TIMETRACE_MCP_TOKEN}"}}}})
        self.assertNotIn(self.TOKEN, Path(path).read_text())

    def test_a_directory_wider_than_private_is_never_the_place_the_file_lives_in(self):
        import os, stat
        wide = self.d / "wide"
        wide.mkdir(mode=0o755)
        os.chmod(wide, 0o755)
        path = claude.write_mcp_config(str(wide), self.access())
        self.addCleanup(claude.remove_mcp_config, path)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertNotEqual(os.path.dirname(path), str(wide))

    def test_two_runs_do_not_share_a_file(self):
        import os
        a, b = claude.write_mcp_config(str(self.d), self.access()), claude.write_mcp_config(str(self.d), self.access())
        self.addCleanup(claude.remove_mcp_config, a)
        self.addCleanup(claude.remove_mcp_config, b)
        self.assertNotEqual(a, b)
        self.assertNotEqual(os.path.dirname(a), os.path.dirname(b))

    def test_a_run_has_the_token_in_its_environment_and_the_file_it_reads_and_nothing_remains(self):
        result = self.run_fake([[{"result": "done"}]], self.access())
        self.assertTrue(result.ok, result.error)
        argv_record, mcp_record = self.transcript_records()[:2]
        self.assertNotIn(self.TOKEN, json.dumps(argv_record))
        self.assertEqual(mcp_record["mcp"]["token_in_env"], self.TOKEN)
        config = mcp_record["mcp"]["config"]
        self.assertEqual((config["mode"], config["dir_mode"]), (0o600, 0o700))
        self.assertEqual(config["json"]["mcpServers"]["timetrace"]["headers"]["Authorization"], "Bearer ${TIMETRACE_MCP_TOKEN}")
        self.assertIn("mcp__timetrace", argv_record["argv"])
        self.assertEqual(self.leftovers(), [])
        self.assertNotIn(self.TOKEN, (self.d / "run.log").read_text())

    def test_a_run_without_it_has_neither(self):
        self.run_fake([[{"result": "done"}]], None)
        records = self.transcript_records()
        self.assertNotIn("--mcp-config", records[0]["argv"])
        self.assertIsNone(records[1]["mcp"]["token_in_env"])

    def test_the_file_is_deleted_when_the_run_fails_is_cancelled_or_raises(self):
        import threading
        from unittest.mock import patch
        self.run_fake([[{"result": "boom", "is_error": True, "subtype": "error"}]], self.access(), exit_code=3)
        self.assertEqual(self.leftovers(), [], "after a failed run")
        from timetrace.dispatch import DispatchDenied
        before_start = threading.Event()
        before_start.set()
        with self.assertRaises(DispatchDenied):  # cancelled before the process was started
            self.run_fake([[{"sleep": 30}]], self.access(), cancel=before_start)
        self.assertEqual(self.leftovers(), [], "after a run cancelled before it started")
        mid_run = threading.Event()
        timer = threading.Timer(1.0, mid_run.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.run_fake([[{"sleep": 30}]], self.access(), cancel=mid_run)
        self.assertTrue(mid_run.is_set())
        self.assertEqual(self.leftovers(), [], "after a run cancelled while it ran")
        adapter = self.adapter()
        with patch.object(claude.ClaudeAdapter, "_run_stream", side_effect=RuntimeError("the adapter broke")):
            with self.assertRaises(RuntimeError):
                adapter.start("x", str(self.d), "s", str(self.d / "run.log"), None, control=object(), mcp=self.access())
        self.assertEqual(self.leftovers(), [], "after an exception inside the run")

    def test_a_continuation_gets_its_own_new_token_and_file(self):
        first = self.access()
        self.run_fake([[{"result": "one"}]], first)
        again = self.access("ttm_SENTINEL_" + "Q1w2E3r4T5" * 4)
        self.run_fake([[{"result": "two"}]], again, resume=True)
        seen = [r["mcp"]["token_in_env"] for r in self.transcript_records() if "mcp" in r]
        self.assertEqual(seen, [first.token, again.token])
        resumed = [r["argv"] for r in self.transcript_records() if "argv" in r][1]
        self.assertIn("--resume", resumed)
        self.assertEqual(self.leftovers(), [])

    def test_a_continuation_with_a_new_token_leaves_neither_the_new_nor_the_old_one_in_the_log_or_the_result(self):
        old = self.TOKEN
        new = "ttm_SENTINEL_" + "Qq1Ww2Ee3R" * 4
        result = self.run_fake([[{"raw": "lastrun" + old}, {"echo_env": "TIMETRACE_MCP_TOKEN"}, {"raw": "diagnosticprefix" + new + "suffix"},
                                 {"stderr": "diagnosticprefix" + old}, {"result": "old " + old + " new " + new}]], self.access(new), resume=True)
        self.assertTrue(result.ok, result.error)
        text = (self.d / "run.log").read_text() + (result.output or "") + (result.error or "")
        for token in (old, new):
            self.assertNotIn(token, text)
            self.assertNotIn(token[4:], text)
        self.assertIn("REDACTED", text)
        self.assertEqual(self.leftovers(), [])

    def test_a_config_that_cannot_be_written_says_nothing_of_the_token(self):
        adapter = self.adapter()
        with self.assertRaises(Exception) as caught:
            adapter.start("x", str(self.d), "s", str(self.d / "no-such-dir" / "run.log"), None, control=None, mcp=self.access())
        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertNotIn(self.TOKEN[4:], repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
