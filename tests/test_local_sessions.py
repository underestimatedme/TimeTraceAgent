"""Local AI session import: parsers, mapping, incremental state, consent gate and agent wiring.

Every fixture here is synthetic (written by the tests); nothing is read from a real ~/.claude or ~/.codex."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from timetrace import local_sessions as ls
from timetrace import runner_state
from timetrace.agent import Agent
from timetrace.checkpoints import Checkpoint
from timetrace.cloud import CloudClient, CloudError
from timetrace.db import Database
from timetrace.models import RunResult


def write_lines(path: Path, lines, garbage=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        for raw in garbage:
            handle.write(raw + "\n")


def claude_user(sid, cwd, text, ts, **extra):
    line = {"type": "user", "sessionId": sid, "cwd": cwd, "gitBranch": "main", "timestamp": ts,
            "isSidechain": False, "message": {"role": "user", "content": text}}
    line.update(extra)
    return line


def claude_assistant(sid, cwd, mid, ts, usage=None, model="claude-model-a", content=None, **extra):
    line = {"type": "assistant", "sessionId": sid, "cwd": cwd, "gitBranch": "main", "timestamp": ts,
            "isSidechain": False,
            "message": {"id": mid, "role": "assistant", "model": model,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
                                           "cache_creation_input_tokens": 40},
                        "content": content or [{"type": "text", "text": "ok"}]}}
    line.update(extra)
    return line


def tool_result(sid, cwd, ts, with_marker=True):
    line = {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts, "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}
    if with_marker:
        line["toolUseResult"] = {"stdout": "done"}
    return line


def edit(name, path):
    return {"type": "tool_use", "id": "tu-" + name + path, "name": name, "input": {"file_path": path}}


def codex_meta(sid, cwd, branch="feature/x", **extra):
    payload = {"id": sid, "cwd": cwd, "git": {"branch": branch, "commit_hash": "abc"}, "originator": "test",
               "source": "cli"}
    payload.update(extra)
    return {"timestamp": "2026-11-16T09:00:00.000Z", "type": "session_meta", "payload": payload}


def codex_line(kind, payload, ts="2026-11-16T09:05:00.000Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def token_count(input_tokens, cached, output, ts="2026-11-16T09:10:00.000Z", cache_write=0):
    return codex_line("event_msg", {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output,
        "reasoning_output_tokens": 5, "cache_write_input_tokens": cache_write,
        "total_tokens": input_tokens + output}}}, ts)


def user_item(text, ts="2026-11-16T09:01:00.000Z"):
    return codex_line("event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "u", "content": [{"type": "text", "text": text}]}}, ts)


class Fixture(unittest.TestCase):
    """A TimeTrace home, a registered workspace, and fake Claude / Codex directories, all under one temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "tt-home"
        self.home.mkdir()
        self.claude = root / "claude-config"
        self.codex = root / "codex-home"
        self.workspace = root / "work" / "repo"
        (self.workspace / "src").mkdir(parents=True)
        self.outside = root / "elsewhere"
        self.outside.mkdir()
        self.db = Database(self.home / "timetrace.db")
        self.addCleanup(self.db.close)
        self.db.upsert_workspace("ws1", "repo", str(self.workspace), "main", kind="folder")
        self.cwd = str(self.workspace)

    def importer(self, **kwargs):
        return ls.Importer(self.home, claude_dir=self.claude, codex_dir=self.codex, **kwargs)

    def claude_session(self, sid, cwd=None, prompts=("fix the bug",), ts="2026-11-16T10:00:00.000Z", project="-p"):
        cwd = cwd or self.cwd
        lines = []
        for index, text in enumerate(prompts):
            lines.append(claude_user(sid, cwd, text, ts))
            lines.append(claude_assistant(sid, cwd, "msg-%s-%d" % (sid, index), ts))
        path = self.claude / "projects" / project / (sid + ".jsonl")
        write_lines(path, lines)
        return path

    def run_once(self, importer=None, **kwargs):
        sent = []
        kwargs.setdefault("upload", lambda batch: sent.append(list(batch)))
        stats = (importer or self.importer()).run(self.db, kwargs.pop("upload"), **kwargs)
        return stats, sent


class ClaudeParserTest(Fixture):
    def parse(self, lines, garbage=(), name="sess-1"):
        path = self.claude / "projects" / "-p" / (name + ".jsonl")
        write_lines(path, lines, garbage)
        return ls.parse_claude(str(path))

    def test_usage_is_counted_once_per_message_id(self):
        cwd = self.cwd
        usage = {"input_tokens": 3, "output_tokens": 7, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 9}
        summary = self.parse([
            claude_user("sess-1", cwd, "explain the parser", "2026-11-16T10:00:00Z"),
            # one reply split over three lines (one content block each), same id and usage
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:01Z", usage, content=[{"type": "thinking"}]),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:02Z", usage, content=[{"type": "text", "text": "a"}]),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:03Z", usage, content=[edit("Edit", "src/a.py")]),
            claude_assistant("sess-1", cwd, "m2", "2026-11-16T10:00:04Z", usage),
        ])
        self.assertEqual((summary.input_tokens, summary.output_tokens), (6, 14))
        self.assertEqual((summary.cache_read_tokens, summary.cache_write_tokens), (200, 18))

    def test_only_questions_the_person_asked_are_turns(self):
        cwd = self.cwd
        summary = self.parse([
            claude_user("sess-1", cwd, "first real question", "2026-11-16T10:00:00Z"),
            tool_result("sess-1", cwd, "2026-11-16T10:00:01Z"),
            tool_result("sess-1", cwd, "2026-11-16T10:00:02Z", with_marker=False),
            claude_user("sess-1", cwd, "injected context", "2026-11-16T10:00:03Z", isMeta=True),
            claude_user("sess-1", cwd, "sub-agent prompt", "2026-11-16T10:00:04Z", isSidechain=True),
            claude_user("sess-1", cwd, "<task-notification>done</task-notification>", "2026-11-16T10:00:05Z",
                        origin={"kind": "task-notification"}),
            claude_user("sess-1", cwd, "<system-reminder>x</system-reminder>", "2026-11-16T10:00:06Z"),
            claude_user("sess-1", cwd, "<command-name>/model</command-name>", "2026-11-16T10:00:07Z"),
            claude_user("sess-1", cwd, "summary of earlier work", "2026-11-16T10:00:08Z", isCompactSummary=True),
            claude_user("sess-1", cwd, "peer note", "2026-11-16T10:00:09Z", turnOrigin="peer"),
            claude_user("sess-1", cwd, [{"type": "text", "text": "second real question"}], "2026-11-16T10:00:10Z",
                        origin={"kind": "human"}),
            claude_user("sess-1", cwd, [{"type": "image", "source": {}}], "2026-11-16T10:00:11Z"),
        ])
        self.assertEqual(summary.turns, 3)

    def test_sidechain_replies_cost_tokens_but_are_not_turns(self):
        cwd = self.cwd
        summary = self.parse([
            claude_user("sess-1", cwd, "go", "2026-11-16T10:00:00Z"),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:01Z"),
            claude_assistant("sess-1", cwd, "m2", "2026-11-16T10:00:02Z", isSidechain=True),
        ])
        self.assertEqual(summary.turns, 1)
        self.assertEqual(summary.output_tokens, 40)

    def test_copied_lines_of_another_session_do_not_count(self):
        cwd = self.cwd
        summary = self.parse([
            claude_user("other", cwd, "question of the original session", "2026-11-15T08:00:00Z"),
            claude_assistant("other", cwd, "o1", "2026-11-15T08:00:01Z", model="claude-model-old"),
            {"type": "custom-title", "sessionId": "other", "customTitle": "Original"},
            claude_user("sess-1", cwd, "continued here", "2026-11-16T10:00:00Z"),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:01Z"),
        ])
        self.assertEqual(summary.session_id, "sess-1")
        self.assertEqual((summary.turns, summary.output_tokens), (1, 20))
        self.assertEqual(summary.model, "claude-model-a")
        self.assertEqual(ls.format_time(summary.started_at), "2026-11-16T10:00:00Z")

    def test_unknown_types_garbage_and_missing_fields_are_tolerated(self):
        cwd = self.cwd
        summary = self.parse([
            {"type": "brand-new-kind", "sessionId": "sess-1", "timestamp": "2026-11-16T09:59:00Z", "data": [1, 2]},
            {"type": "attachment", "sessionId": "sess-1"},
            {"type": "assistant", "sessionId": "sess-1", "timestamp": "2026-11-16T10:00:00Z"},  # no message
            {"type": "assistant", "sessionId": "sess-1", "message": {"id": "m9", "usage": "nonsense",
                                                                     "content": "not a list"}},
            {"type": "user", "sessionId": "sess-1", "cwd": cwd, "message": {"content": 42}},
            {"type": "user", "sessionId": "sess-1", "message": "not an object"},
            claude_user("sess-1", cwd, "a question", "not a time"),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:30:00+08:00",
                             usage={"input_tokens": -5, "output_tokens": "7", "cache_read_input_tokens": True}),
            [1, 2, 3],
        ], garbage=["{not json", "", "\x00\x01", '{"type": "user", "sessionId": "sess-1", "trunc'])
        self.assertEqual(summary.turns, 1)
        self.assertEqual(summary.tokens(), 0)
        self.assertEqual(ls.format_time(summary.started_at), "2026-11-16T02:30:00Z")
        self.assertEqual(ls.format_time(summary.ended_at), "2026-11-16T10:00:00Z")

    def test_model_branch_and_distinct_files(self):
        cwd = self.cwd
        summary = self.parse([
            claude_user("sess-1", cwd, "edit", "2026-11-16T10:00:00Z", gitBranch="first"),
            claude_assistant("sess-1", cwd, "m1", "2026-11-16T10:00:01Z", model="model-b",
                             content=[edit("Edit", "src/a.py"), edit("Write", cwd + "/src/a.py")]),
            claude_assistant("sess-1", cwd, "m2", "2026-11-16T10:00:02Z", model="model-a",
                             content=[edit("MultiEdit", "src/b.py"), edit("Read", "src/c.py")]),
            claude_assistant("sess-1", cwd, "m3", "2026-11-16T10:00:03Z", model="model-a",
                             content=[{"type": "tool_use", "name": "NotebookEdit",
                                       "input": {"notebook_path": "n.ipynb"}}]),
            claude_assistant("sess-1", cwd, "m4", "2026-11-16T10:00:04Z", model="<synthetic>", gitBranch="last"),
        ])
        self.assertEqual(summary.model, "model-a")
        self.assertEqual(summary.git_branch, "last")
        self.assertEqual(summary.files_touched, 3)

    def test_a_file_without_any_session_id_is_no_session(self):
        self.assertIsNone(self.parse([{"type": "summary", "summary": "x"}, "garbage"]))


class CodexParserTest(Fixture):
    def parse(self, lines, garbage=(), name="rollout-2026-11-16T09-00-00-c0dex.jsonl"):
        path = self.codex / "sessions" / "2026" / "11" / "16" / name
        write_lines(path, lines, garbage)
        return ls.parse_codex(str(path))

    def test_cached_input_is_subtracted_and_the_last_total_wins(self):
        summary = self.parse([
            codex_meta("c0dex", self.cwd),
            codex_line("turn_context", {"model": "gpt-model", "cwd": self.cwd}, "2026-11-16T09:00:30Z"),
            user_item("add a test"),
            token_count(100, 60, 10, "2026-11-16T09:02:00Z"),
            token_count(1000, 600, 50, "2026-11-16T09:03:00Z", cache_write=7),
        ])
        self.assertEqual((summary.input_tokens, summary.cache_read_tokens), (400, 600))
        self.assertEqual((summary.output_tokens, summary.cache_write_tokens), (50, 7))
        self.assertEqual((summary.model, summary.git_branch, summary.turns), ("gpt-model", "feature/x", 1))
        self.assertEqual(ls.format_time(summary.ended_at), "2026-11-16T09:03:00Z")

    def test_older_user_message_events_and_injected_messages(self):
        summary = self.parse([
            codex_meta("c0dex", self.cwd),
            codex_line("event_msg", {"type": "user_message", "message": "<environment_context>x</environment_context>"}),
            codex_line("event_msg", {"type": "user_message", "message": "AGENTS rules", "kind": "user_instructions"}),
            codex_line("event_msg", {"type": "user_message", "message": "real question", "kind": "plain"}),
            codex_line("event_msg", {"type": "user_message", "message": "second", "images": []}),
            # the same questions as items in a rollout that writes both markers: not counted twice
            user_item("real question"),
            user_item("second"),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "real question"}]}),
        ])
        self.assertEqual(summary.turns, 2)

    def test_question_replies_the_tool_asked_for_are_not_turns(self):
        summary = self.parse([
            codex_meta("c0dex", self.cwd),
            user_item("<send_user_message_question_reply>yes</send_user_message_question_reply>"),
            user_item("please continue"),
        ])
        self.assertEqual(summary.turns, 1)

    def test_first_session_meta_is_the_rollouts_own(self):
        summary = self.parse([
            codex_meta("c0dex", self.cwd, branch="mine"),
            codex_meta("parent-thread", "/somewhere/else", branch="parent"),
            user_item("go"),
        ])
        self.assertEqual((summary.session_id, summary.cwd, summary.git_branch), ("c0dex", self.cwd, "mine"))

    def test_sub_agent_threads_are_skipped(self):
        self.assertIsNone(self.parse([
            codex_meta("c0dex", self.cwd, source={"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}),
            user_item("task from the parent"),
        ]))

    def test_file_changes_count_distinct_paths(self):
        change = {"type": "item_completed", "item": {"type": "FileChange", "id": "f", "changes": {
            self.cwd + "/a.py": {"type": "update", "unified_diff": ""}, self.cwd + "/b.py": {"type": "add"}}}}
        again = {"type": "patch_apply_end", "changes": {self.cwd + "/a.py": {"update": {}}}}
        patch_call = {"type": "custom_tool_call", "name": "exec",
                      "input": "*** Begin Patch\n*** Update File: z.py\n*** End Patch"}
        summary = self.parse([codex_meta("c0dex", self.cwd), user_item("go"), codex_line("event_msg", change),
                              codex_line("event_msg", again), codex_line("response_item", patch_call)])
        self.assertEqual(summary.files_touched, 2)

    def test_patch_bodies_are_the_fallback_without_change_events(self):
        body = "*** Begin Patch\n*** Update File: a.py\n@@\n*** Add File: %s/b.py\n*** Delete File: c.py\n*** End Patch"
        summary = self.parse([
            codex_meta("c0dex", self.cwd), user_item("go"),
            codex_line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": body % self.cwd}),
            codex_line("response_item", {"type": "function_call", "name": "shell",
                                         "arguments": json.dumps({"command": ["apply_patch", "*** Begin Patch\n"
                                                                              "*** Update File: a.py\n*** End Patch"]})}),
        ])
        self.assertEqual(summary.files_touched, 3)

    def test_usage_record_fallback_garbage_and_missing_meta(self):
        summary = self.parse([
            codex_meta("c0dex", self.cwd), "not json", {"type": "event_msg"}, {"type": "event_msg", "payload": []},
            codex_line("event_msg", {"type": "token_count", "info": None}),
            codex_line("token_usage_record", {"thread_token_usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                                     "output_tokens": 5}}),
            user_item("go"),
        ])
        self.assertEqual((summary.input_tokens, summary.cache_read_tokens, summary.output_tokens), (30, 20, 5))
        self.assertIsNone(self.parse([user_item("no meta"), token_count(1, 0, 1)], name="rollout-x.jsonl"))


class MappingTest(unittest.TestCase):
    def test_workspace_mapping_inside_outside_and_nested(self):
        workspaces = [("outer", "/w/proj"), ("inner", "/w/proj/sub")]
        self.assertEqual(ls.workspace_for("/w/proj", workspaces), "outer")
        self.assertEqual(ls.workspace_for("/w/proj/src/x", workspaces), "outer")
        self.assertEqual(ls.workspace_for("/w/proj/sub/deep", workspaces), "inner")
        self.assertIsNone(ls.workspace_for("/w/project2", workspaces))
        self.assertIsNone(ls.workspace_for("/w", workspaces))
        self.assertIsNone(ls.workspace_for("", workspaces))

    def test_session_key_and_times(self):
        self.assertEqual(ls.session_key("claude", "abc"),
                         __import__("hashlib").sha256(b"claude:abc").hexdigest())
        self.assertEqual(ls.format_time(ls.parse_time("2026-11-16T10:00:00.123456789Z")), "2026-11-16T10:00:00Z")
        self.assertEqual(ls.format_time(ls.parse_time("2026-11-16T10:00:00-0130")), "2026-11-16T11:30:00Z")
        for bad in (None, 5, "", "2026-13-01T00:00:00Z", "yesterday"):
            self.assertIsNone(ls.parse_time(bad))

    def test_default_roots_follow_the_environment(self):
        self.assertEqual(ls.claude_root({"CLAUDE_CONFIG_DIR": "/c"}), Path("/c"))
        self.assertEqual(ls.claude_root({}, Path("/u")), Path("/u/.claude"))
        self.assertEqual(ls.codex_root({"CODEX_HOME": "/x"}), Path("/x"))
        self.assertEqual(ls.codex_root({}, Path("/u")), Path("/u/.codex"))


class ImporterTest(Fixture):
    def test_uploads_contract_records_for_sessions_inside_a_workspace(self):
        self.claude_session("in-ws", cwd=self.cwd + "/src")
        self.claude_session("outside", cwd=str(self.outside))
        write_lines(self.codex / "sessions" / "2026" / "11" / "16" / "rollout-2026-11-16T09-00-00-cx1.jsonl",
                    [codex_meta("cx1", self.cwd), user_item("hello"), token_count(10, 4, 3)])
        stats, sent = self.run_once()
        records = [record for batch in sent for record in batch]
        self.assertEqual(sorted(r["provider"] for r in records), ["claude", "codex"])
        claude = next(r for r in records if r["provider"] == "claude")
        self.assertEqual(set(claude), {"provider", "session_key", "workspace_id", "title", "model", "git_branch",
                                       "started_at", "ended_at", "turns", "input_tokens", "output_tokens",
                                       "cache_read_tokens", "cache_write_tokens", "files_touched"})
        self.assertEqual(claude["session_key"], ls.session_key("claude", "in-ws"))
        self.assertEqual((claude["workspace_id"], claude["title"], claude["turns"]), ("ws1", "", 1))
        raw = json.dumps(records)
        for leak in ("in-ws", "cx1", str(self.workspace), "src"):
            self.assertNotIn(leak, raw)
        self.assertEqual((stats["uploaded"], stats["not_sent"]), (2, 1))

    def test_symlinked_paths_map_to_the_same_workspace(self):
        link = Path(self.tmp.name) / "link-to-repo"
        os.symlink(str(self.workspace), str(link))
        self.claude_session("via-link", cwd=str(link) + "/src")
        _, sent = self.run_once()
        self.assertEqual([r["workspace_id"] for batch in sent for r in batch], ["ws1"])
        # and a workspace registered through a link holds sessions recorded under the real path
        self.db.remove_workspace("ws1")
        self.db.upsert_workspace("ws2", "repo", str(link), "main", kind="folder")
        _, sent = self.run_once()
        self.assertEqual([r["workspace_id"] for batch in sent for r in batch], ["ws2"])

    def test_sessions_timetrace_ran_are_never_sent(self):
        self.claude_session("own-recorded")
        self.claude_session("own-outbox")
        self.claude_session("own-checkpoint")
        self.claude_session("own-run")
        self.claude_session("in-worktree", cwd=str(self.home / "worktrees" / "7"))
        self.claude_session("by-hand")
        self.db.record_provider_session("own-recorded")
        self.db.queue_remote_event("j", "a", 1, {"seq": 2, "type": "completed", "provider_session_id": "own-outbox"})
        self.db.conn.execute("DELETE FROM own_provider_session WHERE session_id='own-outbox'")  # the outbox alone
        self.db.save_checkpoint(Checkpoint(
            plan_id="p", job_id="j", attempt_id="a", tool_profile_id="claude-default", provider_session_id="own-checkpoint",
            canonical_workspace=self.cwd, git_head="h", dirty_paths_digest="d", last_output_offset=0,
            completed_criteria=[], side_effect_summary="", reason="r"))
        task = self.db.add_task("p", self.cwd)
        self.db.add_run(task, "claude", "own-run", "/log")
        _, sent = self.run_once()
        self.assertEqual([r["session_key"] for batch in sent for r in batch], [ls.session_key("claude", "by-hand")])

    def test_unchanged_files_are_not_read_again_and_grown_files_are_sent_again(self):
        path = self.claude_session("grows")
        importer = self.importer()
        _, sent = self.run_once(importer)
        self.assertEqual(len(sent), 1)
        with patch.dict(ls.PARSERS, {"claude": self.fail_parse, "codex": self.fail_parse}):
            stats, sent = self.run_once(importer)
        self.assertEqual((sent, stats["unchanged"]), ([], 1))
        with open(str(path), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude_user("grows", self.cwd, "and one more thing", "2026-11-16T11:00:00Z")) + "\n")
        _, sent = self.run_once(importer)
        self.assertEqual([(r["session_key"], r["turns"]) for batch in sent for r in batch],
                         [(ls.session_key("claude", "grows"), 2)])

    def fail_parse(self, path):
        raise AssertionError("an unchanged file was read again")

    def test_a_changed_file_with_the_same_record_is_not_sent_again(self):
        path = self.claude_session("same")
        self.run_once()
        with open(str(path), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "queue-operation", "sessionId": "same"}) + "\n")
        stats, sent = self.run_once()
        self.assertEqual((sent, stats["unchanged"]), ([], 1))

    def test_a_newly_registered_workspace_rereads_unsent_files(self):
        self.claude_session("later", cwd=str(self.outside))
        _, sent = self.run_once()
        self.assertEqual(sent, [])
        self.db.upsert_workspace("ws-later", "elsewhere", str(self.outside), "main", kind="folder")
        _, sent = self.run_once()
        self.assertEqual([r["workspace_id"] for batch in sent for r in batch], ["ws-later"])

    def test_consent_off_means_no_scan_and_no_upload(self):
        self.claude_session("s1")
        importer = self.importer()
        with patch.object(importer, "files", side_effect=AssertionError("scanned while off")):
            stats, sent = self.run_once(importer, enabled=lambda: False)
        self.assertEqual((stats["off"], sent), (True, []))
        self.assertEqual(self.db.local_session_files(), {})

    def test_switch_turned_off_mid_round_stops_before_the_next_batch(self):
        for index in range(5):
            self.claude_session("s%d" % index)
        state = {"on": True}
        sent = []

        def upload(batch):
            sent.append(batch)
            state["on"] = False

        stats, _ = self.run_once(self.importer(batch_size=2), upload=upload, enabled=lambda: state["on"])
        self.assertEqual((len(sent), stats["off"], stats["uploaded"]), (1, True, 2))

    def test_batches_hold_at_most_100_and_a_round_reads_at_most_200_files(self):
        for index in range(205):
            self.claude_session("b%03d" % index, project="-p%d" % (index % 3))
        stats, sent = self.run_once()
        self.assertEqual([len(batch) for batch in sent], [100, 100])
        self.assertEqual(stats["read"], 200)
        _, sent = self.run_once()
        self.assertEqual([len(batch) for batch in sent], [5])

    def test_an_upload_failure_leaves_the_state_for_a_retry(self):
        self.claude_session("retry-me")

        def offline(batch):
            raise CloudError("unavailable", status=503)

        stats, _ = self.run_once(upload=offline)
        self.assertEqual(stats["failed"], "CloudError")
        self.assertEqual([row["fingerprint"] for row in self.db.local_session_files().values()], [])
        _, sent = self.run_once()
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(row["fingerprint"] for row in self.db.local_session_files().values()))

    def test_a_record_valley_refuses_is_isolated_and_not_offered_again(self):
        for index in range(4):
            self.claude_session("r%d" % index)
        bad = ls.session_key("claude", "r2")
        accepted = []

        def upload(batch):
            if any(r["session_key"] == bad for r in batch):
                raise CloudError("field out of range", status=422)
            accepted.extend(r["session_key"] for r in batch)

        stats, _ = self.run_once(upload=upload)
        self.assertEqual((stats["uploaded"], stats["rejected"]), (3, 1))
        self.assertNotIn(bad, accepted)
        stats, sent = self.run_once()
        self.assertEqual(sent, [])

    def test_oversized_files_are_skipped(self):
        self.claude_session("big")
        stats, sent = self.run_once(self.importer(max_file_bytes=10))
        self.assertEqual((stats["too_large"], sent), (1, []))

    def test_busy_files_do_not_use_up_the_read_budget_for_older_sessions(self):
        old = self.claude_session("finished")
        os.utime(old, (100, 100))
        self.claude_session("active")
        importer = self.importer(max_files=1)
        stats, batches = self.run_once(importer, busy_since=lambda: 200)
        self.assertEqual((stats["read"], stats["deferred"]), (1, 1))
        self.assertEqual(batches[0][0]["session_key"], ls.session_key("claude", "finished"))
        stats, batches = self.run_once(importer)
        self.assertEqual(stats["uploaded"], 1)
        self.assertEqual(batches[0][0]["session_key"], ls.session_key("claude", "active"))

    def test_files_written_while_a_run_is_going_wait_for_it(self):
        self.claude_session("maybe-a-run")
        stats, sent = self.run_once(busy_since=lambda: time.time() - 60)
        self.assertEqual((stats["deferred"], sent), (1, []))
        _, sent = self.run_once(busy_since=lambda: None)
        self.assertEqual(len(sent), 1)

    def test_symlinks_and_credential_files_are_never_opened(self):
        secret_dir = Path(self.tmp.name) / "secret"
        secret_dir.mkdir()
        write_lines(secret_dir / "x.jsonl", [claude_user("linked", self.cwd, "q", "2026-11-16T10:00:00Z")])
        project = self.claude / "projects" / "-p"
        project.mkdir(parents=True)
        os.symlink(str(secret_dir / "x.jsonl"), str(project / "linked.jsonl"))
        os.symlink(str(secret_dir), str(self.claude / "projects" / "-linked-dir"))
        (self.claude / ".credentials.json").write_text("{}")
        (self.codex / "sessions").mkdir(parents=True)
        (self.codex / "auth.json").write_text("{}")
        opened = []
        real_open = open

        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=watch):
            _, sent = self.run_once()
        self.assertEqual(sent, [])
        self.assertFalse([p for p in opened if "secret" in p or p.endswith("auth.json") or "credentials" in p])

    def test_files_that_disappeared_are_forgotten(self):
        path = self.claude_session("gone")
        self.run_once()
        self.assertEqual(len(self.db.local_session_files()), 1)
        path.unlink()
        self.run_once()
        self.assertEqual(self.db.local_session_files(), {})

    def test_empty_sessions_are_not_sent(self):
        write_lines(self.claude / "projects" / "-p" / "empty.jsonl",
                    [{"type": "custom-title", "sessionId": "empty", "customTitle": "t"},
                     {"type": "user", "sessionId": "empty", "cwd": self.cwd, "isMeta": True,
                      "timestamp": "2026-11-16T10:00:00Z", "message": {"content": "x"}}])
        stats, sent = self.run_once()
        self.assertEqual((sent, stats["not_sent"]), ([], 1))


class ReviewRegressionTest(Fixture):
    """Blockers found in review of the first version (each test fails on it)."""

    # 2. consent is checked before every request, also inside a 422 split
    def test_switch_turned_off_during_a_split_makes_no_further_request(self):
        for index in range(3):
            self.claude_session("split%d" % index)
        state = {"on": True}
        requests = []

        def upload(batch):
            requests.append(len(batch))
            state["on"] = False
            raise CloudError("field out of range", status=422)

        stats, _ = self.run_once(upload=upload, enabled=lambda: state["on"])
        self.assertEqual(requests, [3])
        self.assertTrue(stats["off"])
        self.assertEqual([row for row in self.db.local_session_files().values() if row["fingerprint"]], [])

    # 3. the run state is current when a file is decided and when it is sent
    def test_a_run_that_ends_while_the_round_reads_is_not_imported(self):
        self.claude_session("by-hand", ts="2026-11-16T10:00:00.000Z")
        self.claude_session("folder-run", ts="2026-11-16T10:00:00.000Z")
        real = ls.PARSERS["claude"]

        def parse(path):
            # the round has started; now a folder-workspace run records its provider session id and ends
            self.db.record_provider_session("folder-run")
            return real(path)

        with patch.dict(ls.PARSERS, {"claude": parse}):
            _, sent = self.run_once()
        self.assertEqual([r["session_key"] for batch in sent for r in batch], [ls.session_key("claude", "by-hand")])

    def test_a_run_recorded_between_two_requests_is_not_sent(self):
        self.claude_session("first")
        self.claude_session("second")
        requests = []

        def upload(batch):
            requests.append([r["session_key"] for r in batch])
            self.db.record_provider_session("second")
            self.db.record_provider_session("first")

        self.run_once(self.importer(batch_size=1), upload=upload)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(self.db.local_session_files()), 2)  # the other one is settled unsent

    # 4. linked scan roots never lead outside ~/.claude and ~/.codex
    def outside_rollout(self):
        outside = Path(self.tmp.name) / "outside-sessions"
        write_lines(outside / "2026" / "11" / "16" / "rollout-2026-11-16T09-00-00-cx9.jsonl",
                    [codex_meta("cx9", self.cwd), user_item("hello"), token_count(10, 4, 3)])
        return outside

    def outside_project(self):
        outside = Path(self.tmp.name) / "outside-projects"
        write_lines(outside / "-p" / "linked.jsonl", [claude_user("linked", self.cwd, "q", "2026-11-16T10:00:00Z"),
                                                    claude_assistant("linked", self.cwd, "m", "2026-11-16T10:00:01Z")])
        return outside

    def assert_nothing_read(self, outside):
        opened = []
        real_open = open

        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=watch):
            stats, sent = self.run_once()
        self.assertEqual((sent, stats["read"]), ([], 0))
        self.assertEqual([p for p in opened if p.endswith(".jsonl")], [])

    def test_linked_codex_sessions_directory_is_not_read(self):
        outside = self.outside_rollout()
        self.codex.mkdir()
        os.symlink(str(outside), str(self.codex / "sessions"))
        self.assert_nothing_read(outside)

    def test_linked_codex_home_is_not_read(self):
        outside = Path(self.tmp.name) / "outside-codex"
        outside.mkdir()
        os.symlink(str(self.outside_rollout()), str(outside / "sessions"))
        os.symlink(str(outside), str(self.codex))
        self.assert_nothing_read(outside)

    def test_linked_claude_projects_and_claude_dir_are_not_read(self):
        outside = self.outside_project()
        self.claude.mkdir()
        os.symlink(str(outside), str(self.claude / "projects"))
        self.assert_nothing_read(outside)
        os.unlink(str(self.claude / "projects"))
        self.claude.rmdir()
        real_dir = Path(self.tmp.name) / "real-claude"
        real_dir.mkdir()
        os.symlink(str(outside), str(real_dir / "projects"))
        os.symlink(str(real_dir), str(self.claude))
        self.assert_nothing_read(outside)

    def test_linked_directories_inside_codex_sessions_are_skipped(self):
        outside = self.outside_rollout()
        (self.codex / "sessions" / "2026").mkdir(parents=True)
        os.symlink(str(outside / "2026" / "11"), str(self.codex / "sessions" / "2026" / "11"))
        self.assert_nothing_read(outside)


class SecondReviewRegressionTest(Fixture):
    """Blockers found in the second review (each test fails on 1e3496a)."""

    def test_a_run_that_ends_between_the_two_reads_of_the_final_check_is_not_sent(self):
        """The run is going when the last check before the request starts, records its provider session id and stops
        counting as running in between the check's two reads (a real thread, held at a barrier)."""
        self.claude_session("folder-run")
        agent = Agent(self.db, object(), {}, self.home, lambda: "token", local_sessions=self.importer())
        agent._local_session_import = agent._local_session_known = True
        agent.cloud = type("Cloud", (), {})()
        uploads = []
        agent.cloud.post_local_sessions = lambda token, sessions: uploads.append(sessions)
        cancel = threading.Event()
        state = {"phase": "decide"}
        first_read, finished = threading.Event(), threading.Event()

        def run_ends():
            first_read.wait(5)
            run_db = Database(self.db.path)
            run_db.record_provider_session("folder-run")  # the run reports its session...
            run_db.close()
            agent._untrack(cancel)                         # ...and then stops counting as running
            finished.set()

        runner = threading.Thread(target=run_ends)
        runner.start()
        self.addCleanup(runner.join, 5)
        real_payload = ls.Importer._payload

        def decided(*args):
            payload = real_payload(*args)
            # the file is decided while nothing runs; then the run starts (it resumed that session)
            agent._track(cancel)
            agent._running_since[id(cancel)] = time.time() - 60
            state["phase"] = "final"
            return payload

        def at_first_read_of_final_check():
            if state["phase"] == "final":
                state["phase"] = "done"
                first_read.set()
                self.assertTrue(finished.wait(5))

        real_busy, real_own = agent._busy_since, Database.own_provider_session_ids

        def busy():
            value = real_busy()
            at_first_read_of_final_check()
            return value

        def own(db):
            value = real_own(db)
            at_first_read_of_final_check()
            return value

        agent._busy_since = busy
        with patch.object(ls.Importer, "_payload", staticmethod(decided)), \
                patch.object(Database, "own_provider_session_ids", own):
            stats = agent.import_local_sessions()
        self.assertEqual(state["phase"], "done")
        self.assertEqual((uploads, stats.get("uploaded")), ([], 0))


class Lifecycle(Fixture):
    """Drives the real Agent.run_once lifecycle with a tool that leaves a session file in the run's directory."""

    def lifecycle_agent(self, job, adapter):
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.cwd, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit",
                        "--allow-empty", "-qm", "initial"], cwd=self.cwd, check=True)
        self.db.upsert_workspace("ws1", "repo", self.cwd, "main", kind="git")
        test = self

        class Cloud:
            uploads = []

            def claim(self, token):
                return {"job": dict(job), "attempt_id": "a1", "lease_epoch": 1,
                        "lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

            def append_events(self, token, job_id, attempt_id, epoch, events):
                pass

            def renew(self, token, attempt_id, epoch):
                return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

            def post_quota_samples(self, token, samples):
                return {}

            def post_local_sessions(self, token, sessions):
                Cloud.uploads.append(sessions)

        agent = Agent(self.db, Cloud(), {job["provider"]: adapter}, self.home, lambda: "token",
                      prepare_workspace=lambda *args: (test.cwd, "main"), local_sessions=self.importer())
        agent._note_local_session_import({"local_session_import": True})
        agent._local_sessions_due = False
        self.addCleanup(self.join_round, agent)
        return agent, Cloud

    @staticmethod
    def join_round(agent):
        thread = agent._local_sessions_thread
        if thread is not None:
            thread.join(10)

    class Tool:
        """A tool that, like the real CLIs, leaves its session file in the run's directory and chooses its own id."""

        def __init__(self, fixture, sid, **result):
            self.fixture, self.sid, self.result = fixture, sid, result

        def capabilities(self):
            return {"can_record": True, "can_read_quota": False, "can_dispatch": True, "can_resume": True,
                    "can_enforce_zero_spend": True}

        def chat(self, prompt, cwd, session_id, log_file, cancel_event=None):
            self.fixture.claude_session(self.sid, cwd=cwd)
            return RunResult(session_id=self.sid, **self.result)

        def start(self, prompt, cwd, session_id, log_file, cancel_event=None, **kwargs):
            write_lines(self.fixture.codex / "sessions" / "2026" / "11" / "16" /
                        ("rollout-2026-11-16T09-00-00-%s.jsonl" % self.sid),
                        [codex_meta(self.sid, cwd), user_item("do it"), token_count(10, 4, 3)])
            return RunResult(session_id=self.sid, **self.result)

    def assert_run_not_imported(self, agent, cloud, sid):
        self.join_round(agent)  # the upkeep after the run may already have started a round of its own
        self.assertIn(sid, self.db.own_provider_session_ids())
        stats = agent.import_local_sessions()
        self.assertEqual((cloud.uploads, stats["uploaded"]), ([], 0))
        rows = list(self.db.local_session_files().values())
        self.assertEqual([(row["session_key"], row["fingerprint"]) for row in rows], [("", "")])  # settled, not sent


class ThirdReviewRegressionTest(Lifecycle):
    """Blockers found in the third review (each test fails on 83fcb2e)."""

    SOURCE = ("/Users/alice/Acme Corp/customer secret.csv", "alice@acme.example", "ghp_" + "A1b2C3d4E5" * 4,
              "secret.txt中的内容", ".env", "Payroll 2026", "Renamed by hand ~/private/notes.md")

    # 1. no conversation text, not even a title
    def test_no_conversation_text_leaves_not_even_a_title(self):
        cwd = self.cwd
        write_lines(self.claude / "projects" / "-p" / "talky.jsonl", [
            claude_user("talky", cwd, "edit %s and mail %s, token %s" % self.SOURCE[:3], "2026-11-16T10:00:00Z"),
            claude_assistant("talky", cwd, "m1", "2026-11-16T10:00:01Z"),
            claude_user("talky", cwd, "%s / %s / %s" % self.SOURCE[3:6], "2026-11-16T10:00:02Z"),
            {"type": "custom-title", "sessionId": "talky", "customTitle": self.SOURCE[6]},
        ])
        write_lines(self.codex / "sessions" / "2026" / "11" / "16" / "rollout-2026-11-16T09-00-00-cxt.jsonl",
                    [codex_meta("cxt", cwd), user_item(" ".join(self.SOURCE)), token_count(10, 4, 3)])
        _, sent = self.run_once()
        records = [record for batch in sent for record in batch]
        self.assertEqual(len(records), 2)
        self.assertEqual([record["title"] for record in records], ["", ""])
        self.assertEqual(sorted(record["turns"] for record in records), [1, 2])
        raw = json.dumps(sent, ensure_ascii=False)
        for text in self.SOURCE + ("Acme", "alice", "customer", "secret", "Payroll", "private", "edit", "Renamed"):
            self.assertNotIn(text, raw)

    # 2. every run's provider session is a TimeTrace run, whatever the outcome
    def test_a_failed_chat_turn_is_not_imported(self):
        job = {"id": "c1", "kind": "chat_turn", "workspace_id": "ws1", "tool_profile_id": "claude-default",
               "provider": "claude", "prompt": "explain"}
        agent, cloud = self.lifecycle_agent(job, self.Tool(self, "chat-failed", exit_code=2, ok=False, error="boom"))
        self.assertEqual(agent.run_once(), "job c1 → failed")
        self.assert_run_not_imported(agent, cloud, "chat-failed")

    def test_a_quota_blocked_chat_turn_is_not_imported(self):
        job = {"id": "c1", "kind": "chat_turn", "workspace_id": "ws1", "tool_profile_id": "claude-default",
               "provider": "claude", "prompt": "explain"}
        agent, cloud = self.lifecycle_agent(job, self.Tool(self, "chat-quota", exit_code=1, blocked=True,
                                                           error="You've hit your limit"))
        self.assertEqual(agent.run_once(), "job c1 → waiting_quota")
        self.assert_run_not_imported(agent, cloud, "chat-quota")

    def test_a_failed_task_run_is_not_imported(self):
        job = {"id": "j1", "workspace_id": "ws1", "tool_profile_id": "codex-default", "provider": "codex",
               "prompt": "do it"}
        agent, cloud = self.lifecycle_agent(job, self.Tool(self, "codex-thread-failed", exit_code=1, ok=False,
                                                           error="tests failed"))
        self.assertEqual(agent.run_once(), "job j1 → failed")
        self.assert_run_not_imported(agent, cloud, "codex-thread-failed")

    def test_ids_are_recorded_before_the_job_stops_counting_as_running(self):
        agent = Agent(self.db, object(), {}, self.home, lambda: "token")
        cancel = threading.Event()
        agent._track(cancel)
        agent._note_run_session(cancel, RunResult(session_id="s-1"), "s-2", None, {"not": "a result"})
        seen = {}
        real_record = self.db.record_provider_session

        def record(sid, now=None):
            seen.setdefault("running_at_record", agent._busy_since() is not None)
            real_record(sid, now)

        with patch.object(self.db, "record_provider_session", record):
            agent._untrack(cancel)
        self.assertTrue(seen["running_at_record"])
        self.assertIsNone(agent._busy_since())
        self.assertTrue({"s-1", "s-2"} <= self.db.own_provider_session_ids())


class FourthReviewRegressionTest(Lifecycle):
    """The run's session id must survive the cut of a long tool output (fails on fc07d30)."""

    FILLER = 10_050  # more than process.MAX_CAPTURED_LINES lines between thread.started and the end

    def fake_codex(self, mode, thread_id):
        """A stand-in `codex` binary: thread.started, then FILLER events, then how the turn ended."""
        script = Path(self.tmp.name) / ("codex-%s" % mode)
        script.write_text("\n".join([
            "#!" + sys.executable,
            "import json, sys",
            "if 'app-server' in sys.argv: sys.exit(1)",
            "print(json.dumps({'type': 'thread.started', 'thread_id': %r}))" % thread_id,
            "for i in range(%d):" % self.FILLER,
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'step %d' % i}}))",
            {"success": "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}}))\n"
                        "print(json.dumps({'type': 'turn.completed'}))\nsys.exit(0)",
             "failed": "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'boom'}}))\nsys.exit(1)",
             "quota": "print(json.dumps({'type': 'error', 'message': \"You've hit your usage limit\"}))\nsys.exit(1)",
             }[mode],
        ]) + "\n")
        script.chmod(0o755)
        return str(script)

    def codex_adapter(self, binary):
        from timetrace.adapters import codex

        class Codex(codex.CodexAdapter):
            def capabilities(self):
                return {"can_record": True, "can_read_quota": False, "can_dispatch": True, "can_resume": True,
                        "can_enforce_zero_spend": True}

            def read_limits(self):
                return []

        return Codex({"bin": binary}, billing=object())

    def check_long_new_chat(self, mode, outcome):
        thread_id = "thread-long-" + mode
        write_lines(self.codex / "sessions" / "2026" / "11" / "16" / ("rollout-2026-11-16T09-00-00-%s.jsonl" % thread_id),
                    [codex_meta(thread_id, self.cwd), user_item("explain"), token_count(10, 4, 3)])
        job = {"id": "c1", "kind": "chat_turn", "workspace_id": "ws1", "tool_profile_id": "codex-default",
               "provider": "codex", "prompt": "explain"}
        agent, cloud = self.lifecycle_agent(job, self.codex_adapter(self.fake_codex(mode, thread_id)))
        self.assertEqual(agent.run_once(), "job c1 → " + outcome)
        self.assert_run_not_imported(agent, cloud, thread_id)

    def test_a_long_successful_new_chat_is_not_imported(self):
        self.check_long_new_chat("success", "replied")

    def test_a_long_failed_new_chat_is_not_imported(self):
        self.check_long_new_chat("failed", "failed")

    def test_a_long_quota_blocked_new_chat_is_not_imported(self):
        self.check_long_new_chat("quota", "waiting_quota")

    def test_the_watch_sees_lines_the_returned_buffer_dropped(self):
        from timetrace.adapters import codex
        from timetrace.process import MAX_CAPTURED_LINES, run_streaming
        watch = codex.ThreadWatch()
        log = str(Path(self.tmp.name) / "run.log")
        code, lines = run_streaming([self.fake_codex("success", "t-first")], self.tmp.name, log, on_line=watch)
        self.assertEqual((code, len(lines)), (0, MAX_CAPTURED_LINES))
        self.assertIsNone(codex.parse_exec(lines).session_id)  # the cut dropped thread.started...
        self.assertEqual(watch.thread_id, "t-first")            # ...the watch kept it

    def test_claude_runs_know_their_session_whatever_the_output_keeps(self):
        from timetrace.adapters import claude
        seen = {}

        def long_output(cmd, cwd, log_file, **kwargs):
            seen["sid"] = cmd[cmd.index("--session-id") + 1]
            return 1, ['{"type": "assistant", "message": {}}'] * 10_000  # no line carries the session id

        adapter = claude.ClaudeAdapter({"bin": "claude"}, billing=object(), credentials=lambda: {})
        with patch.object(claude, "run_streaming", long_output):
            for run in (lambda: adapter.chat("q", self.cwd, "", "/dev/null"),
                        lambda: adapter.review("q", self.cwd, "/dev/null"),
                        lambda: adapter.plan("q", self.cwd, "/dev/null"),
                        lambda: adapter.parse_import("q", self.cwd, "/dev/null")):
                self.assertEqual(run().session_id, seen["sid"])

    def test_an_id_whose_write_failed_stays_excluded_and_is_written_later(self):
        self.claude_session("write-failed")
        agent = Agent(self.db, type("Cloud", (), {})(), {}, self.home, lambda: "token", local_sessions=self.importer())
        agent.cloud.post_local_sessions = lambda token, sessions: self.fail("a run was uploaded")
        agent._local_session_import = agent._local_session_known = True
        cancel = threading.Event()
        agent._track(cancel)
        agent._note_run_session(cancel, "write-failed")
        with patch.object(self.db, "record_provider_session", side_effect=OSError("disk busy")):
            agent._untrack(cancel)
        self.assertEqual(agent._pending_run_sessions(), {"write-failed"})
        self.assertNotIn("write-failed", self.db.own_provider_session_ids())
        stats = agent.import_local_sessions()          # still excluded while only held in memory
        self.assertEqual((stats["uploaded"], stats["not_sent"]), (0, 1))
        agent.maintain(now=1000)                       # the next upkeep writes it
        self.assertEqual(agent._pending_run_sessions(), set())
        self.assertIn("write-failed", self.db.own_provider_session_ids())


class CloudLocalSessionsTest(unittest.TestCase):
    class Response:
        def __init__(self, status, body):
            self.status, self.body = status, json.dumps(body).encode()

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_endpoint_body_and_batch_bounds(self):
        seen = []
        client = CloudClient("https://v", opener=lambda req, timeout: (
            seen.append(req) or self.Response(200, {"code": 0, "data": {"accepted": 1}})))
        self.assertEqual(client.post_local_sessions("tok", [{"provider": "claude"}]), {"accepted": 1})
        self.assertTrue(seen[0].full_url.endswith("/runner/local-sessions"))
        self.assertEqual(seen[0].get_method(), "POST")
        self.assertEqual(json.loads(seen[0].data.decode()), {"sessions": [{"provider": "claude"}]})
        for size in (0, 101):
            with self.assertRaises(ValueError):
                client.post_local_sessions("tok", [{}] * size)

    def test_refusal_reason_is_read_from_the_envelope(self):
        for body in ({"code": "local_session_import_off", "message": "off"},
                     {"code": 40300, "message": "off", "data": {"code": "local_session_import_off"}},
                     {"code": 40300, "message": "off", "data": {"reason_code": "local_session_import_off"}},
                     {"code": 40300, "message": "off", "details": {"code": "local_session_import_off"}}):
            client = CloudClient("https://v", opener=lambda req, timeout, body=body: self.Response(403, body))
            with self.subTest(body=body), self.assertRaises(CloudError) as caught:
                client.post_local_sessions("tok", [{}])
            self.assertEqual((caught.exception.status, caught.exception.reason), (403, ls.OFF_CODE))
        client = CloudClient("https://v", opener=lambda req, timeout: self.Response(409, {"code": 40900, "message": "x"}))
        with self.assertRaises(CloudError) as caught:
            client.post_local_sessions("tok", [{}])
        self.assertEqual((caught.exception.code, caught.exception.reason), (40900, ""))


class AgentLocalSessionsTest(Fixture):
    def setUp(self):
        super().setUp()
        self.claude_session("by-hand")
        self.uploads, self.inventories, self.quota = [], [], []
        self.switch = {"local_session_import": True}
        test = self

        class Cloud:
            def update_inventory(self, token, *args, **kwargs):
                test.inventories.append(args)
                return dict(test.switch) if test.switch is not None else None

            def post_local_sessions(self, token, sessions):
                test.uploads.append(list(sessions))
                return {"accepted": len(sessions)}

            def post_quota_samples(self, token, samples):
                test.quota.append(samples)
                return {"accepted": len(samples)}

        class QuotaAdapter:
            def capabilities(self):
                return {"can_read_quota": True}

            def read_limits(self):
                from timetrace.models import Sample
                return [Sample(bucket_key="claude:claude:five_hour", tool="claude", used_pct=10, reset_at=None,
                               window_mins=300)]

        self.cloud = Cloud()
        self.agent = Agent(self.db, self.cloud, {"claude": QuotaAdapter()}, self.home, lambda: "token",
                           report_protocol=True, inventory=lambda: ([], []), local_sessions=self.importer())

    def turn_on(self):
        """As if an inventory answer had said on (the round it schedules is left to the test)."""
        self.agent._note_local_session_import({"local_session_import": True})
        self.agent._local_sessions_due = False

    def settle(self):
        thread = self.agent._local_sessions_thread
        if thread is not None:
            thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_import_runs_only_after_valley_says_it_is_on(self):
        self.agent.maintain(now=1000)  # quota due, switch not known yet: nothing scanned; the inventory turns it on
        self.settle()
        self.assertEqual(self.uploads, [])
        self.agent.maintain(now=1005)  # the next upkeep starts the first round right away
        self.settle()
        self.assertEqual([len(batch) for batch in self.uploads], [1])
        self.assertEqual(runner_state.load(self.home)["local_sessions"], "on")
        self.assertEqual(runner_state.load(self.home)["local_sessions_uploaded"], 1)

    def test_absent_or_false_switch_means_off(self):
        for answer in (None, {}, {"local_session_import": False}, {"local_session_import": "true"}):
            with self.subTest(answer=answer):
                self.switch = answer
                agent = Agent(self.db, self.cloud, {}, self.home, lambda: "token", report_protocol=True,
                              inventory=lambda: ([], []), local_sessions=self.importer())
                with patch.object(ls.Importer, "run", side_effect=AssertionError("scanned while off")):
                    for now in (1000, 1005, 1400):
                        agent.maintain(now=now)
                    self.assertIsNone(agent._local_sessions_thread)
                self.assertEqual(self.uploads, [])

    def test_a_403_off_answer_stops_until_the_inventory_says_on_again(self):
        def refused(token, sessions):
            raise CloudError("off", status=403, code=40300, reason=ls.OFF_CODE)

        self.cloud.post_local_sessions = refused
        self.turn_on()
        stats = self.agent.import_local_sessions()
        self.assertTrue(stats["off"])
        self.assertFalse(self.agent._local_session_import)
        self.assertEqual(self.db.local_session_files(), {})  # nothing recorded as uploaded
        with patch.object(ls.Importer, "run", side_effect=AssertionError("scanned while off")):
            self.agent._upkeep(5000, True)  # quota due; the inventory answer below turns it back on
            self.agent._upkeep(5005, False)  # ... but Valley saying on again is no new switch: no round right away
        self.assertTrue(self.agent._local_session_import)
        self.assertFalse(self.agent._local_sessions_due)
        self.assertIsNone(self.agent._local_sessions_thread)
        self.cloud.post_local_sessions = lambda token, sessions: self.uploads.append(sessions)
        self.agent._upkeep(5300, False)  # the quota timer's next round
        self.settle()
        self.assertEqual(len(self.uploads), 1)

    def test_scanner_failures_never_reach_the_heartbeat_or_the_quota_upload(self):
        self.turn_on()
        with patch.object(ls.Importer, "run", side_effect=RuntimeError("parser exploded")):
            self.agent.maintain(now=1000)
            self.settle()
            self.agent.maintain(now=1031)
            self.settle()
        self.assertEqual(len(self.inventories), 2)
        self.assertEqual(len(self.quota), 1)
        self.assertEqual(runner_state.load(self.home)["local_sessions_failed"], "RuntimeError")

    def test_upload_errors_never_reach_the_heartbeat(self):
        def broken(token, sessions):
            raise OSError("network down")

        self.cloud.post_local_sessions = broken
        self.turn_on()
        self.agent.maintain(now=1000)
        self.settle()
        self.agent.maintain(now=1031)
        self.assertEqual(len(self.inventories), 2)
        self.assertEqual(self.db.local_session_files(), {})

    def test_the_round_runs_on_its_own_thread_with_its_own_connection(self):
        seen = {}
        original = ls.Importer.run

        def spy(importer, db, *args, **kwargs):
            seen["thread"] = threading.get_ident()
            seen["db"] = db
            return original(importer, db, *args, **kwargs)

        self.turn_on()
        with patch.object(ls.Importer, "run", spy):
            self.agent.maintain(now=1000)
            self.settle()
        self.assertNotEqual(seen["thread"], threading.get_ident())
        self.assertIsNot(seen["db"], self.db)
        self.assertEqual(len(self.uploads), 1)

    def test_a_running_job_defers_what_was_written_during_it(self):
        self.turn_on()
        cancel = threading.Event()
        self.agent._track(cancel)
        self.agent._running_since[id(cancel)] = time.time() - 60
        stats = self.agent.import_local_sessions()
        self.assertEqual((stats["deferred"], self.uploads), (1, []))
        self.agent._untrack(cancel)
        self.agent.import_local_sessions()
        self.assertEqual(len(self.uploads), 1)


class DoctorLineTest(unittest.TestCase):
    def test_line_reflects_the_runner_state(self):
        from timetrace.cli import _local_sessions_line
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIn("未开启", _local_sessions_line(home))
            runner_state.record(home, local_sessions="on")
            self.assertIn("已开启", _local_sessions_line(home))
            runner_state.record(home, local_sessions_deferred=2)
            self.assertIn("2 个会话文件", _local_sessions_line(home))
            self.assertNotIn("还没有需要上传", _local_sessions_line(home))
            runner_state.record(home, local_sessions_uploaded_at=1793290701, local_sessions_uploaded=3)
            self.assertIn("3 段", _local_sessions_line(home))
            runner_state.record(home, local_sessions_failed="CloudError")
            self.assertIn("CloudError", _local_sessions_line(home))


if __name__ == "__main__":
    unittest.main()
