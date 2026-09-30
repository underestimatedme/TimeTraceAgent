"""import_parse: a read-only AI run in an empty directory that turns a shared
conversation into a structured import proposal."""
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from timetrace import results
from timetrace.adapters import claude, codex
from timetrace.adapters.base import IMPORT_RULES
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.dispatch import coding_slot_lock, tool_slot_lock
from timetrace.models import RunResult

SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"
GOOD = {"is_task": True, "reason": "要做一个网站",
        "candidates": [{"project": {"match_id": None, "name": "官网", "type_id": "web", "tag_names": [],
                                    "confidence": 0.8},
                        "task": {"title": "做官网", "description": "首页 {草稿}"},
                        "pipeline": {"template_id": None, "stages": []}}]}


def lease():
    return datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()


class Cloud:
    def __init__(self, job):
        self.job = job
        self.events = []

    def claim(self, token):
        job = {"id": "imp1", "kind": "import_parse", "tool_profile_id": "claude-default", "provider": "claude",
               "prompt": "请解析这段对话", "import_id": "im_01-AB"}
        job.update(self.job)
        job = {k: v for k, v in job.items() if v is not None}
        return {"job": job, "attempt_id": "a1", "lease_epoch": 1, "lease_expires_at": lease()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": lease()}

    def post_quota_samples(self, token, samples):
        return {}


class Parser:
    def __init__(self, reply="```json\n%s\n```" % json.dumps(GOOD, ensure_ascii=False), result=None,
                 zero_spend=True, write=None):
        self.reply, self.result, self.zero_spend, self.write = reply, result, zero_spend, write
        self.calls = []

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": self.zero_spend}

    def parse_import(self, prompt, cwd, log_file, cancel_event=None):
        mode = stat.S_IMODE(os.stat(cwd).st_mode)
        self.calls.append({"prompt": prompt, "cwd": cwd, "files": sorted(os.listdir(cwd)), "mode": mode})
        if self.write:
            Path(cwd, self.write).write_text("x")
        if self.result is not None:
            return self.result
        return RunResult(exit_code=0, ok=True, output=self.reply, session_id="s")

    def start(self, *args, **kwargs):
        raise AssertionError("an import parse must not start a task run")

    resume = chat = review = start


class ImportParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.db = Database(self.d / "timetrace.db")
        self.home = self.d / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, parser=None, **job):
        self.cloud = Cloud(job)
        self.parser = parser or Parser()
        self.db.conn.execute("DELETE FROM remote_claim")
        self.db.conn.execute("DELETE FROM remote_outbox")
        agent = Agent(self.db, self.cloud, {"claude": self.parser}, self.home, lambda: "token",
                      prepare_workspace=self.no_worktree, max_parallel_per_tool={"claude": 1})
        outcome = agent.run_once()
        return outcome, (self.cloud.events[-1] if self.cloud.events else None)

    @staticmethod
    def no_worktree(*args, **kwargs):
        raise AssertionError("no worktree for an import parse")

    def test_runs_in_a_fresh_private_empty_directory_removed_afterwards(self):
        outcome, done = self.run_job()
        self.assertEqual(outcome, "job imp1 → parsed")
        call = self.parser.calls[0]
        self.assertEqual(call["cwd"], str((self.home / "imports" / "imp1").resolve()))
        self.assertEqual(call["files"], [])
        self.assertEqual(call["mode"], 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "imports").st_mode), 0o700)
        self.assertFalse(Path(call["cwd"]).exists())
        self.assertEqual(call["prompt"], "请解析这段对话")
        self.assertEqual([e["type"] for e in self.cloud.events], ["running", "completed"])
        self.assertEqual(done["import_result"], GOOD)
        self.assertEqual(done["message"], "completed")
        self.assertIn("```json", done["reply"])
        self.assertEqual(done["result_summary"], done["reply"][:1000])
        self.assertIn("output_tail", done)
        tool_slot_lock(self.home, "claude", 0).acquire().release()
        coding_slot_lock(self.home).acquire().release()

    def test_directory_is_removed_even_when_the_tool_writes_or_crashes(self):
        _, done = self.run_job(Parser(write="stray.txt"))
        self.assertFalse((self.home / "imports" / "imp1").exists())

        class Crash(Parser):
            def parse_import(self, prompt, cwd, log_file, cancel_event=None):
                self.calls.append(cwd)
                raise RuntimeError("boom")
        outcome, done = self.run_job(Crash())
        self.assertEqual(done["type"], "failed")
        self.assertFalse(Path(self.parser.calls[0]).exists())

    def test_a_leftover_directory_is_replaced_with_an_empty_one(self):
        stale = self.home / "imports" / "imp1"
        stale.mkdir(parents=True)
        (stale / "old.txt").write_text("old")
        self.run_job()
        self.assertEqual(self.parser.calls[0]["files"], [])

    def test_a_symlinked_imports_root_is_refused(self):
        self.home.mkdir()
        target = self.d / "elsewhere"
        target.mkdir()
        (self.home / "imports").symlink_to(target)
        outcome, done = self.run_job()
        self.assertEqual(self.parser.calls, [])
        self.assertEqual(done["type"], "failed")
        self.assertEqual(os.listdir(target), [])

    def test_workspace_id_is_ignored_and_not_required(self):
        outcome, _ = self.run_job(workspace_id="nope")
        self.assertEqual(outcome, "job imp1 → parsed")
        self.assertTrue(self.parser.calls[0]["cwd"].endswith("/imports/imp1"))

    def test_the_conversation_is_not_kept_in_the_local_database(self):
        self.run_job(prompt="私密对话内容 " + SECRET)
        row = self.db.get_remote_claim("imp1")
        self.assertEqual((row["prompt"], row["workspace_id"], row["state"]), ("", "", "reported"))

    def test_job_id_cannot_name_another_path(self):
        self.run_job(id="../../evil")
        self.assertEqual(Path(self.parser.calls[0]["cwd"]).parent, (self.home / "imports").resolve())

    def test_invalid_import_id_fails_before_any_spawn(self):
        for value in ("", "a/b", "x" * 65, "a b", 5, None):
            with self.subTest(value=value):
                outcome, done = self.run_job(import_id=value)
                self.assertEqual(self.parser.calls, [])
                self.assertEqual(done["type"], "failed")
                self.assertIn("rejected", outcome)

    def test_invalid_json_completes_without_import_result(self):
        outcome, done = self.run_job(Parser(reply="这不是任务。"))
        self.assertEqual(outcome, "job imp1 → parsed (invalid)")
        self.assertEqual(done["type"], "completed")
        self.assertNotIn("import_result", done)
        self.assertEqual(done["message"], "completed; " + results.INVALID_IMPORT_NOTE)
        self.assertEqual(results.INVALID_IMPORT_NOTE, "解析结果无效")

    def test_result_and_reply_are_redacted_and_bounded(self):
        value = dict(GOOD, reason="key " + SECRET)
        reply = "x" * 40000 + "\n```json\n" + json.dumps(value) + "\n```"
        _, done = self.run_job(Parser(reply=reply))
        self.assertNotIn(SECRET, json.dumps(self.cloud.events))
        self.assertNotIn(SECRET, done["import_result"]["reason"])
        self.assertLessEqual(len(done["reply"].encode("utf-8")), 32 * 1024)

    def test_quota_block_and_failure_carry_no_result(self):
        _, done = self.run_job(Parser(result=RunResult(exit_code=1, blocked=True, error="usage limit")))
        self.assertEqual(done["type"], "waiting_quota")
        self.assertNotIn("import_result", done)
        self.assertFalse((self.home / "imports" / "imp1").exists())
        _, done = self.run_job(Parser(result=RunResult(exit_code=2, ok=False, error="boom")))
        self.assertEqual(done["type"], "failed")
        self.assertNotIn("import_result", done)

    def test_zero_spend_gate_applies(self):
        outcome, _ = self.run_job(Parser(zero_spend=False))
        self.assertEqual(self.parser.calls, [])
        self.assertIn("blocked", outcome)

    def test_busy_tool_slot_defers(self):
        held = tool_slot_lock(self.home, "claude", 0).acquire()
        try:
            outcome, _ = self.run_job()
        finally:
            held.release()
        self.assertIn("deferred (tool busy: claude)", outcome)
        self.assertEqual(self.parser.calls, [])
        self.assertFalse((self.home / "imports" / "imp1").exists())

    def test_tool_without_import_support_is_rejected(self):
        class NoImport(Parser):
            parse_import = None
        outcome, done = self.run_job(NoImport())
        self.assertEqual(outcome, "job imp1 → rejected (import unsupported)")
        self.assertEqual(done["type"], "failed")

    def test_unknown_tool_is_rejected(self):
        outcome, done = self.run_job(provider="gemini")
        self.assertIn("rejected (tool unavailable)", outcome)


class ExtractTest(unittest.TestCase):
    def test_fenced_block_is_preferred_over_later_bare_objects(self):
        text = "看：\n```json\n{\"a\": 1}\n```\n另外 {\"b\": 2}"
        self.assertEqual(results.extract_import(text), {"a": 1})

    def test_last_fenced_dict_wins_and_non_dict_fences_are_skipped(self):
        text = "```json\n{\"a\": 1}\n```\n```json\n{\"a\": 2}\n```\n```json\n[1, 2]\n```"
        self.assertEqual(results.extract_import(text), {"a": 2})

    def test_bare_object(self):
        self.assertEqual(results.extract_import('结果：{"is_task": false, "reason": "闲聊"} 完'),
                         {"is_task": False, "reason": "闲聊"})

    def test_braces_inside_strings_do_not_confuse(self):
        value = {"t": "a } b { c", "n": {"x": "}}{{"}}
        self.assertEqual(results.extract_import("前言 " + json.dumps(value, ensure_ascii=False) + " 后记"), value)

    def test_last_top_level_object_not_a_nested_one(self):
        text = '{"first": 1} 然后 {"outer": {"inner": 2}} 结束'
        self.assertEqual(results.extract_import(text), {"outer": {"inner": 2}})

    def test_stray_brace_before_the_object(self):
        self.assertEqual(results.extract_import('用 { 包住，最终：{"a": 1}'), {"a": 1})

    def test_non_dict_and_garbage_are_invalid(self):
        for text in (None, "", "no json", "```json\n[1,2]\n```", "[1, 2]", "{bad json}", '"str"',
                     "{" * 5000, '{"a": ' * 3000):
            with self.subTest(text=(text or "")[:20]):
                self.assertIsNone(results.extract_import(text))

    def test_oversize_is_invalid(self):
        big = {"d": "x" * (64 * 1024)}
        self.assertIsNone(results.extract_import("```json\n" + json.dumps(big) + "\n```"))
        self.assertIsNone(results.extract_import(json.dumps(big)))
        fits = {"d": "x" * (60 * 1024)}
        self.assertEqual(results.extract_import(json.dumps(fits)), fits)

    def test_all_strings_are_redacted_recursively(self):
        value = {"k": [SECRET, {"deep": "token " + SECRET}], "n": 1, "b": None}
        found = results.extract_import(json.dumps(value))
        self.assertNotIn(SECRET, json.dumps(found))
        self.assertEqual((found["n"], found["b"]), (1, None))


class ImportArgvTest(unittest.TestCase):
    def test_claude_import_is_a_plan_mode_chat_without_mcp_in_a_new_session(self):
        seen = {}

        def fake(cmd, cwd, log_file, **kwargs):
            seen["cmd"], seen["cwd"] = cmd, cwd
            return 0, ['{"type":"result","subtype":"success","result":"{}","session_id":"s"}']
        cfg = {"bin": "claude", "allowed_tools": ["Bash(rm:*)"], "extra_args": ["--dangerously-skip-permissions"],
               "permission_mode": "bypassPermissions"}
        with tempfile.TemporaryDirectory() as d, patch.object(claude, "run_streaming", fake):
            res = claude.ClaudeAdapter(cfg, billing=object(), credentials=lambda: {}).parse_import(
                "-p conversation", d, str(Path(d) / "log"))
        cmd = seen["cmd"]
        self.assertTrue(res.ok)
        self.assertEqual(seen["cwd"], d)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], IMPORT_RULES)
        for widening in ("--allowedTools", "--dangerously-skip-permissions", "bypassPermissions", "--add-dir"):
            self.assertNotIn(widening, cmd)
        self.assertEqual(cmd[-1], " -p conversation")

    def test_codex_import_is_read_only_new_thread_outside_git(self):
        seen = {}

        def fake(cmd, cwd, log_file, **kwargs):
            seen["cmd"] = cmd
            return 0, ['{"type":"thread.started","thread_id":"t"}', '{"type":"turn.completed"}']
        cfg = {"bin": "codex", "sandbox": "danger-full-access", "extra_args": ["--yolo"]}
        with tempfile.TemporaryDirectory() as d, patch.object(codex, "run_streaming", fake):
            codex.CodexAdapter(cfg, billing=object()).parse_import("conversation", d, str(Path(d) / "log"))
        cmd = seen["cmd"]
        self.assertNotIn("resume", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("-C") + 1], d)
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertNotIn("--yolo", cmd)
        self.assertNotIn("danger-full-access", " ".join(cmd))
        self.assertTrue(cmd[-1].startswith(IMPORT_RULES))


if __name__ == "__main__":
    unittest.main()
