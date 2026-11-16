"""AI 用量统计: the tokens a dispatched run used, parsed from Claude / Codex output and carried by its terminal event.

Contract: docs/superpowers/specs/2026-11-16-token-usage-design.md (TimeTrace repository)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from timetrace import usage
from timetrace.adapters import claude, codex
from timetrace.adapters.codex_interactive import CodexTurn
from timetrace.billing import StaticBilling
from timetrace.models import RunResult
import tests.test_chat_turn as chat_turn
from tests.test_chat_turn import ChatAdapter, ChatCloud
from tests.test_codex_interactive import Host
from tests.test_remote_jobs import Adapter, Base, ControlCloud, WaitUntilCancelled

FIXTURES = Path(__file__).parent / "fixtures"
THREAD = "01a167a9-1103-7440-8bd4-d7f0e0a6ba5e"
ZERO = (0, 0, 0, 0)  # a new thread's running totals (timetrace.usage.ZERO)


def lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def tokens(model, i, o, r, w):
    return {"model": model, "input_tokens": i, "output_tokens": o, "cache_read_tokens": r, "cache_write_tokens": w}


def result_line(reported, **extra):
    msg = {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "s"}
    if reported is not None:
        msg["usage"] = reported
    msg.update(extra)
    return json.dumps(msg)


class UsageShapeTest(unittest.TestCase):
    def test_counts_take_only_finite_non_negative_numbers_and_are_bounded(self):
        for bad in (None, "12", -3, True, float("nan"), float("inf"), [1], {"n": 1}):
            self.assertEqual(usage.count(bad), 0, bad)
        self.assertEqual(usage.count(12.9), 12)
        self.assertEqual(usage.count(10 ** 15), usage.MAX_COUNT)

    def test_model_name_is_capped_at_64_characters_and_bytes(self):
        self.assertEqual(usage.model_name("  claude-sonnet-5-5 \n"), "claude-sonnet-5-5")
        self.assertEqual(usage.model_name("m" * 100), "m" * 64)
        self.assertLessEqual(len(usage.model_name("模" * 40).encode("utf-8")), 64)
        self.assertEqual(usage.model_name(42), "")
        # A lone surrogate (JSON "\\ud800") cannot be UTF-8: dropped, never an error.
        self.assertEqual(usage.model_name(json.loads('"gpt-\\ud800x"')), "gpt-x")
        self.assertEqual(usage.make(json.loads('"\\udfff"'), 5)["model"], "")

    def test_nothing_reported_is_no_usage_rather_than_zeros(self):
        self.assertIsNone(usage.make("claude-sonnet-5-5"))
        self.assertIsNone(usage.clean({"model": "x", "input_tokens": 0}))
        self.assertIsNone(usage.clean("lots"))
        self.assertEqual(usage.clean({"model": "x" * 80, "input_tokens": 10 ** 13, "output_tokens": "9"}),
                         tokens("x" * 64, usage.MAX_COUNT, 0, 0, 0))

    def test_merge_adds_two_runs_and_keeps_the_later_model(self):
        self.assertEqual(usage.merge(tokens("a", 1, 2, 3, 4), tokens("b", 10, 20, 30, 40)), tokens("b", 11, 22, 33, 44))
        self.assertEqual(usage.merge(tokens("a", 1, 2, 3, 4), tokens("", 1, 0, 0, 0)), tokens("a", 2, 2, 3, 4))
        self.assertEqual(usage.merge(None, tokens("b", 1, 0, 0, 0)), tokens("b", 1, 0, 0, 0))
        self.assertEqual(usage.merge(tokens("a", 1, 0, 0, 0), "garbage"), tokens("a", 1, 0, 0, 0))
        self.assertIsNone(usage.merge(None, None))


class ClaudeUsageTest(unittest.TestCase):
    def test_real_shaped_fixture(self):
        res = claude.parse_stream(lines("claude_stream.jsonl"))
        self.assertTrue(res.ok)
        # No modelUsage and no model on the assistant message: the model the session started with.
        self.assertEqual(res.usage, tokens("claude-haiku-4-5-20251001", 3, 5, 0, 0))

    def test_every_result_of_a_stream_session_is_added_and_the_model_is_the_largest_in_model_usage(self):
        res = claude.parse_stream(lines("claude_stream_usage.jsonl"))
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "second answer")
        self.assertEqual(res.usage, tokens("claude-sonnet-5-5", 12 + 6, 1020 + 310, 146806 + 165044, 18238 + 1210))

    def test_model_falls_back_to_the_last_real_assistant_message(self):
        assistant = {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}}
        synthetic = {"type": "assistant", "message": {"model": "<synthetic>", "content": []}}
        reported = {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 40}
        res = claude.parse_stream([json.dumps(assistant), json.dumps(synthetic),
                                   result_line(reported, modelUsage={})])
        self.assertEqual(res.usage, tokens("claude-opus-5-5", 7, 2, 100, 40))
        res = claude.parse_stream([result_line(reported)])
        self.assertEqual(res.usage, tokens("", 7, 2, 100, 40))

    def test_missing_zero_or_garbage_usage_is_none_and_never_changes_the_outcome(self):
        zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        for reported in (None, zero, "lots", [1, 2], {"input_tokens": "3", "output_tokens": -1, "cache_read_input_tokens": True}):
            res = claude.parse_stream([result_line(reported, modelUsage="garbage")])
            self.assertIsNone(res.usage, reported)
            self.assertTrue(res.ok)
            self.assertIsNone(res.error)
        res = claude.parse_stream(["[1, 2]", "{broken", "", result_line({"output_tokens": 4}, modelUsage={"m": "x", "": {}})])
        self.assertEqual(res.usage, tokens("", 0, 4, 0, 0))

    def test_a_lone_surrogate_in_a_model_name_never_fails_a_run(self):
        reported = {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        for extra in ({"modelUsage": json.loads('{"claude-\\ud800x": {"inputTokens": 7}}')}, {}):
            stream = ['{"type": "assistant", "message": {"model": "claude-\\udfffy", "content": []}}',
                      result_line(reported, **extra)]
            res = claude.parse_stream(stream)
            self.assertTrue(res.ok)
            self.assertIn(res.usage["model"], ("claude-x", "claude-y"))
            json.dumps(res.usage).encode("utf-8")

    def run_adapter(self, call, result_lines):
        """Each Claude run path (print mode, stream-json input, work session) through its real result handling."""
        seen = {}

        def fake(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            return 0, result_lines(cmd)
        with patch.object(claude, "run_streaming", fake), patch.object(claude, "run_stream", fake):
            adapter = claude.ClaudeAdapter({"bin": "claude"}, billing=object(), credentials=lambda: {})
            return call(adapter), seen["cmd"]

    def test_every_dispatched_run_path_reports_usage(self):
        fixture = lines("claude_stream_usage.jsonl")
        expected = tokens("claude-sonnet-5-5", 18, 1330, 311850, 19448)
        sid = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as d:
            log = str(Path(d) / "run.log")
            paths = {
                "start": lambda a: a.start("go", d, sid, log),
                "resume": lambda a: a.resume("go", d, sid, log),
                "stream start": lambda a: a.start("go", d, sid, log, control=object()),
                "stream resume": lambda a: a.resume("go", d, sid, log, control=object()),
                "folder": lambda a: a.start_folder("go", d, d, sid, log),
                "folder stream": lambda a: a.resume_folder("go", d, d, sid, log, control=object()),
                "chat": lambda a: a.chat("q", d, "", log),
                "review": lambda a: a.review("q", d, log),
                "plan": lambda a: a.plan("q", d, log),
                "import": lambda a: a.parse_import("q", d, log),
            }
            for name, call in paths.items():
                res, _ = self.run_adapter(call, lambda cmd: fixture)
                self.assertEqual(res.usage, expected, name)

            def confirmed(cmd):
                # A work session must confirm the session it was given.
                return [line.replace("5b0c1f0e-2f43-4b8e-9d55-0d7c3a1e9a10", sid) for line in fixture]
            res, _ = self.run_adapter(lambda a: a.work_session("go", d, sid, log, threading.Event(), control=object(),
                                                               resuming=False, read_only=True), confirmed)
            self.assertTrue(res.ok, res.error)
            self.assertEqual(res.usage, expected)


class CodexUsageTest(unittest.TestCase):
    def test_real_shaped_fixtures(self):
        res = codex.parse_exec(lines("codex_exec_success.jsonl"))
        self.assertTrue(res.ok)
        self.assertEqual(res.usage, tokens("", 1200, 40, 0, 0))
        # Cached input is inside input_tokens: taken out and counted as cache reads.
        res = codex.parse_exec(lines("codex_exec_cached.jsonl"), model="gpt-6.1-sol")
        self.assertEqual(res.usage, tokens("gpt-6.1-sol", 16419 - 12160, 1110, 12160, 0))
        self.assertIsNone(codex.parse_exec(lines("codex_exec_blocked.jsonl")).usage)

    def test_cache_writes_and_a_model_named_by_an_event(self):
        events = [{"type": "thread.started", "thread_id": THREAD, "model": "gpt-from-event"},
                  {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 300,
                                                       "cache_write_input_tokens": 120, "output_tokens": 45}}]
        res = codex.parse_exec([json.dumps(e) for e in events], model="configured")
        self.assertEqual(res.usage, tokens("gpt-from-event", 600, 45, 300, 120))

    def test_several_turns_report_running_totals_so_the_last_one_counts(self):
        events = [{"type": "thread.started", "thread_id": THREAD},
                  {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}},
                  {"type": "turn.completed", "usage": {"input_tokens": 2500, "cached_input_tokens": 1400, "output_tokens": 90}}]
        res = codex.parse_exec([json.dumps(e) for e in events])
        self.assertEqual(res.usage, tokens("", 1100, 90, 1400, 0))

    def test_a_resumed_thread_reports_only_what_this_run_added(self):
        res = codex.parse_exec(lines("codex_exec_cached.jsonl"), start=(16000, 12000, 0, 1000))
        self.assertEqual(res.usage, tokens("", 419 - 160, 110, 160, 0))
        # Saved totals unknown: no usage rather than the whole thread's.
        res = codex.parse_exec(lines("codex_exec_cached.jsonl"), start=None)
        self.assertIsNone(res.usage)
        self.assertTrue(res.ok)

    def test_totals_that_do_not_fit_together_are_no_usage_never_a_clamped_guess(self):
        def run(*totals, start=ZERO):
            events = [{"type": "turn.completed", "usage": t} for t in totals]
            return codex.parse_exec([json.dumps(e) for e in events], start=start)
        first = {"input_tokens": 2000, "cached_input_tokens": 1000, "output_tokens": 90}
        # Ended below where it began.
        self.assertIsNone(run(first, start=(2100, 1000, 0, 90)).usage)
        # A running total that went down.
        self.assertIsNone(run(first, {"input_tokens": 1500, "cached_input_tokens": 1000, "output_tokens": 95}).usage)
        # More cached than input; one total not whole among good ones.
        self.assertIsNone(run({"input_tokens": 10, "cached_input_tokens": 20, "output_tokens": 1}).usage)
        self.assertIsNone(run(first, {"input_tokens": 2600, "cached_input_tokens": 1.5, "output_tokens": 95}).usage)
        self.assertIsNone(run({"input_tokens": 2000, "output_tokens": 90}).usage)

    def test_a_total_below_the_starting_totals_at_any_point_is_no_usage(self):
        """Baseline (2000 incl. cached, 1000 cached, 0, 90), then 1000/400/50 and 2500/1400/100: the first update is
        below where the run began, so no share of it can be told (not input 100, output 10, cache read 400)."""
        events = [{"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}},
                  {"type": "turn.completed", "usage": {"input_tokens": 2500, "cached_input_tokens": 1400, "output_tokens": 100}}]
        res = codex.parse_exec([json.dumps(e) for e in events], start=(2000, 1000, 0, 90))
        self.assertTrue(res.ok)
        self.assertIsNone(res.usage)
        # Below in one field only (output) is enough.
        res = codex.parse_exec([json.dumps(events[1])], start=(2000, 1000, 0, 101))
        self.assertIsNone(res.usage)
        self.assertEqual(codex.parse_exec([json.dumps(events[1])], start=(2000, 1000, 0, 90)).usage,
                         tokens("", 100, 10, 400, 0))

    def test_the_last_turn_completed_without_a_whole_total_is_no_usage(self):
        """900/300/45, then a turn.completed with no usage: the run's end total is unknown (not 945)."""
        first = {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 300, "output_tokens": 45}}
        for last in ({"type": "turn.completed"}, {"type": "turn.completed", "usage": None},
                     {"type": "turn.completed", "usage": {"input_tokens": 950}}):
            res = codex.parse_exec([json.dumps(first), json.dumps(last)])
            self.assertTrue(res.ok, last)
            self.assertIsNone(res.usage, last)
        self.assertEqual(codex.parse_exec([json.dumps(first)]).usage, tokens("", 600, 45, 300, 0))

    def test_missing_or_garbage_usage_is_none_and_never_fails_the_run(self):
        for reported in ("lots", None, {"input_tokens": "1200", "output_tokens": -5}, {}):
            event = {"type": "turn.completed"}
            if reported is not None:
                event["usage"] = reported
            res = codex.parse_exec([json.dumps({"type": "thread.started", "thread_id": THREAD}), json.dumps(event), "[1]"])
            self.assertTrue(res.ok, reported)
            self.assertIsNone(res.usage, reported)

    def test_a_lone_surrogate_in_a_model_name_never_fails_a_run(self):
        events = ['{"type": "thread.started", "thread_id": "%s", "model": "gpt-\\ud800x"}' % THREAD,
                  json.dumps({"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 0,
                                                                 "output_tokens": 1}})]
        res = codex.parse_exec(events)
        self.assertTrue(res.ok)
        self.assertEqual(res.usage["model"], "gpt-x")
        json.dumps(res.usage).encode("utf-8")


def write_rollout(home, thread, records, day="2026/11/16", meta_id=None, end=b""):
    folder = Path(home) / "sessions" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("rollout-2026-11-16T09-00-00-%s.jsonl" % thread)
    with open(path, "wb") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": meta_id or thread}}).encode() + b"\n")
        for record in records:
            fh.write((record if isinstance(record, bytes) else json.dumps(record).encode()) + b"\n")
        fh.write(end)
    return path


def token_count(inputs, cached, output):
    total = {"input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": output, "total_tokens": inputs + output}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": total,
                                                                            "last_token_usage": total}}}


TURN = {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}


class CodexThreadTotalsTest(unittest.TestCase):
    def totals(self, home, thread=THREAD):
        return codex.thread_totals(thread, Path(home))

    def test_last_recorded_totals_of_the_thread(self):
        with tempfile.TemporaryDirectory() as home:
            write_rollout(home, THREAD, [TURN, token_count(100, 50, 5), token_count(16000, 12000, 1000),
                                         {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                                         {"type": "response_item", "payload": {}}])
            self.assertEqual(self.totals(home), (16000, 12000, 0, 1000))
            # The usual order: a usage record, then the token_count that states the same total.
            record = {"type": "token_usage_record", "payload": {"thread_token_usage": {
                "input_tokens": 17000, "cached_input_tokens": 12500, "output_tokens": 1100}}}
            write_rollout(home, THREAD, [TURN, record, token_count(17000, 12500, 1100)])
            self.assertEqual(self.totals(home), (17000, 12500, 0, 1100))
            # Read whole, nothing but the session's own record: no turn yet, so nothing used yet.
            write_rollout(home, THREAD, [])
            self.assertEqual(self.totals(home), ZERO)

    def test_a_damaged_or_ambiguous_rollout_is_unknown_never_an_older_total(self):
        half = b'{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20'
        cases = {
            # The reviewer's case: total 1000 complete, the newer total 2000 still half written.
            "last line half written": dict(records=[TURN, token_count(1000, 400, 50)], end=half),
            "last line not yet terminated": dict(records=[TURN, token_count(1000, 400, 50)],
                                                              end=json.dumps(token_count(2000, 900, 80)).encode()),
            "garbled line after the latest total": dict(records=[TURN, token_count(1000, 400, 50), half]),
            "garbled total": dict(records=[TURN, {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": "1000", "cached_input_tokens": 0, "output_tokens": 1}}}}]),
            "token_count info not an object": dict(records=[TURN, {"type": "event_msg", "payload": {
                "type": "token_count", "info": [1]}}]),
            "turns but no token_count": dict(records=[TURN, {"type": "response_item", "payload": {}}]),
            "a usage record but no token_count": dict(records=[{"type": "token_usage_record", "payload": {
                "thread_token_usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}}}]),
            "a later usage record that disagrees": dict(records=[TURN, token_count(1000, 400, 50), {
                "type": "token_usage_record", "payload": {"thread_token_usage": {
                    "input_tokens": 2000, "cached_input_tokens": 900, "output_tokens": 80}}}]),
            "another thread's session_meta": dict(records=[TURN, token_count(1000, 400, 50)],
                                                  meta_id="01a167a9-1103-7440-8bd4-d7f0e0a6ba00"),
            "a non-object line": dict(records=[TURN, token_count(1000, 400, 50), b"[1, 2]"]),
        }
        for name, case in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as home:
                write_rollout(home, THREAD, **case)
                self.assertIsNone(self.totals(home))

    def test_the_session_meta_must_be_a_complete_first_line(self):
        with tempfile.TemporaryDirectory() as home:
            path = write_rollout(home, THREAD, [TURN, token_count(1000, 400, 50)])
            self.assertEqual(self.totals(home), (1000, 400, 0, 50))
            with patch.object(codex, "ROLLOUT_HEAD_BYTES", 10):
                self.assertIsNone(self.totals(home))
            path.write_bytes(b"{garbled\n" + path.read_bytes().split(b"\n", 1)[1])
            self.assertIsNone(self.totals(home))

    def test_a_large_file_needs_its_latest_total_within_the_bytes_read(self):
        filler = [{"type": "response_item", "payload": {"x": "y" * 100}}] * 50
        with tempfile.TemporaryDirectory() as home, patch.object(codex, "ROLLOUT_TAIL_BYTES", 1000):
            write_rollout(home, THREAD, [TURN, token_count(100, 50, 5)] + filler)
            self.assertIsNone(self.totals(home))
            write_rollout(home, THREAD, [TURN] + filler + [token_count(100, 50, 5)])
            self.assertEqual(self.totals(home), (100, 50, 0, 5))
            write_rollout(home, THREAD, [TURN] + filler)  # cut from the middle: no turn-free proof of zero
            self.assertIsNone(self.totals(home))

    def test_only_a_regular_file_inside_sessions_reached_without_links(self):
        other = "01a167a9-1103-7440-8bd4-d7f0e0a6ba5f"
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(self.totals(home))  # no sessions directory
            for bad in ("tid-1", "*", "../x", None, THREAD + "*"):
                self.assertIsNone(codex.thread_totals(bad, Path(home)))
            outside = Path(home) / "outside"
            target = write_rollout(outside, THREAD, [TURN, token_count(1000, 400, 50)])
            self.assertEqual(self.totals(outside), (1000, 400, 0, 50))
            # A linked rollout file.
            leaf = Path(home) / "leaf"
            linked = leaf / "sessions" / "2026" / "11" / "16" / target.name
            linked.parent.mkdir(parents=True)
            linked.symlink_to(target)
            self.assertIsNone(self.totals(leaf))
            # A linked day directory (the file itself is no link): outside the sessions directory.
            day = Path(home) / "day"
            (day / "sessions" / "2026" / "11").mkdir(parents=True)
            (day / "sessions" / "2026" / "11" / "16").symlink_to(target.parent)
            self.assertIsNone(self.totals(day))
            # A linked sessions directory.
            whole = Path(home) / "whole"
            whole.mkdir()
            (whole / "sessions").symlink_to(outside / "sessions")
            self.assertIsNone(self.totals(whole))
            # Two rollouts claiming the same thread.
            write_rollout(outside, THREAD, [TURN, token_count(1000, 400, 50)], day="2026/11/17")
            self.assertIsNone(self.totals(outside))
            write_rollout(home, other, [])
            self.assertEqual(self.totals(home, other), ZERO)

    def test_adapter_resume_and_chat_subtract_the_saved_totals(self):
        with tempfile.TemporaryDirectory() as home:
            write_rollout(home, THREAD, [TURN, token_count(16000, 12000, 1000)])
            seen = []

            def fake(cmd, cwd, log, **kwargs):
                seen.append(cmd)
                return 0, lines("codex_exec_cached.jsonl")
            adapter = codex.CodexAdapter({"bin": "codex", "model": "gpt-6.1-sol"}, billing=StaticBilling(True))
            log = str(Path(home) / "run.log")
            with patch.dict(os.environ, {"CODEX_HOME": home}), patch.object(codex, "run_streaming", fake), \
                    patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                added = tokens("gpt-6.1-sol", 419 - 160, 110, 160, 0)
                self.assertEqual(adapter.resume("go", home, THREAD, log).usage, added)
                self.assertEqual(adapter.resume_folder("go", home, home, THREAD, log).usage, added)
                self.assertEqual(adapter.chat("q", home, THREAD, log).usage, added)
                whole = tokens("gpt-6.1-sol", 16419 - 12160, 1110, 12160, 0)
                for run in (lambda: adapter.start("go", home, "s", log), lambda: adapter.chat("q", home, "", log),
                            lambda: adapter.review("q", home, log), lambda: adapter.start_folder("go", home, home, "s", log)):
                    self.assertEqual(run().usage, whole)
                # A thread whose rollout is not found: no usage, the run itself unaffected.
                missing = adapter.resume("go", home, "01a167a9-1103-7440-8bd4-d7f0e0a6ba00", log)
                self.assertTrue(missing.ok)
                self.assertIsNone(missing.usage)
                with patch.object(codex, "thread_totals", side_effect=RuntimeError("boom")):
                    self.assertTrue(adapter.resume("go", home, THREAD, log).ok)
            self.assertEqual(seen[0][:4], ["codex", "exec", "resume", THREAD])

    def test_a_half_written_newer_total_is_never_subtracted_as_the_older_one(self):
        """Old total 1000 complete, the newer total 2000 half written when the run starts; the resumed run ends at
        2100. Only 100 were used: anything but no usage (1100 from the older total) would be a guess."""
        half = b'{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20'
        with tempfile.TemporaryDirectory() as home:
            write_rollout(home, THREAD, [TURN, token_count(1000, 0, 10)], end=half)
            ended = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 2100, "cached_input_tokens": 0,
                                                                    "output_tokens": 30}})
            adapter = codex.CodexAdapter({"bin": "codex"}, billing=StaticBilling(True))
            with patch.dict(os.environ, {"CODEX_HOME": home}), \
                    patch.object(codex, "run_streaming", lambda *a, **k: (0, [ended])), \
                    patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                res = adapter.resume("go", home, THREAD, str(Path(home) / "run.log"))
            self.assertTrue(res.ok)
            self.assertIsNone(res.usage)


class CodexAppServerUsageTest(unittest.TestCase):
    def fake(self, root):
        fake = root / "codex"
        fake.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fake_codex_interactive.py").read_text())
        fake.chmod(0o700)
        return str(fake)

    def test_turn_usage_is_its_last_totals_minus_the_replayed_ones(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            adapter = codex.CodexAdapter({"bin": self.fake(root), "timeout_seconds": 3}, billing=StaticBilling(True, "test"))
            result = adapter.work_session("initial", tmp, "old-thread", str(root / "run.log"), threading.Event(),
                                          control=Host(), resuming=True, read_only=False)
            self.assertTrue(result.ok, result.error)
            # Replayed (earlier turn): 1000 in / 600 cached / 0 written / 50 out; this turn ended at 2600 / 1500 / 10 / 130.
            self.assertEqual(result.usage, tokens("gpt-fake-codex", (2600 - 1000) - (1500 - 600), 80, 900, 10))

    def test_interrupted_turn_without_a_model_call_reports_none(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            root = Path(tmp)
            cancel = threading.Event()
            host = Host()
            host.attach_interactive = lambda _: cancel.set()
            adapter = codex.CodexAdapter({"bin": self.fake(root), "timeout_seconds": 3}, billing=StaticBilling(True, "test"))
            result = adapter.work_session("initial", tmp, "old-thread", str(root / "run.log"), cancel,
                                          control=host, resuming=True, read_only=False)
            self.assertFalse(result.ok)
            self.assertIsNone(result.usage)

    @staticmethod
    def turn(saved=ZERO):
        t = CodexTurn([], "/", None, threading.Event(), 3, False)
        t.thread_id, t.turn_id, t.model, t.saved_totals = "th", "tu", "gpt-x", saved
        return t

    @staticmethod
    def update(turn, turn_id, inputs, cached, output):
        turn._notification("thread/tokenUsage/updated", {"threadId": "th", "turnId": turn_id, "tokenUsage": {
            "total": {"inputTokens": inputs, "cachedInputTokens": cached, "cacheWriteInputTokens": 0,
                      "outputTokens": output}}})

    def test_starting_totals(self):
        new = self.turn()
        self.update(new, "tu", 900, 300, 40)
        self.assertEqual(new.turn_usage(), tokens("gpt-x", 600, 40, 300, 0))
        # No replay: the rollout's saved totals are the starting point.
        resumed = self.turn((500, 200, 0, 30))
        self.update(resumed, "tu", 900, 300, 40)
        self.assertEqual(resumed.turn_usage(), tokens("gpt-x", 300, 10, 100, 0))
        # A replay that agrees with the rollout, or one alone when the rollout could not be read.
        for saved in ((500, 200, 0, 30), None):
            replayed = self.turn(saved)
            self.update(replayed, "earlier", 500, 200, 30)
            self.update(replayed, "tu", 900, 300, 40)
            self.assertEqual(replayed.turn_usage(), tokens("gpt-x", 300, 10, 100, 0))
        unknown = self.turn(None)
        self.update(unknown, "tu", 900, 300, 40)
        self.assertIsNone(unknown.turn_usage())

    def test_replayed_totals_that_do_not_fit_together_are_no_usage(self):
        def check(name, saved, *updates):
            turn = self.turn(saved)
            for update in updates:
                self.update(turn, *update)
            self.assertIsNone(turn.turn_usage(), name)
        check("two different replays", None, ("earlier", 500, 200, 30), ("older", 400, 200, 30), ("tu", 900, 300, 40))
        check("a replay that disagrees with the rollout", (500, 200, 0, 30), ("earlier", 600, 200, 30), ("tu", 900, 300, 40))
        check("a replay for a new thread", ZERO, ("earlier", 500, 200, 30), ("tu", 900, 300, 40))
        check("an earlier turn's totals after this turn's", None, ("tu", 900, 300, 40), ("earlier", 500, 200, 30))
        check("this turn's totals going down", ZERO, ("tu", 900, 300, 40), ("tu", 800, 300, 40))
        check("ending below the replay", None, ("earlier", 1000, 200, 30), ("tu", 900, 300, 40))
        # Baseline (2000, 1000, 0, 90), updates 1000/400/50 then 2500/1400/100: the first is below the baseline.
        check("an update below the rollout's totals", (2000, 1000, 0, 90), ("tu", 1000, 400, 50), ("tu", 2500, 1400, 100))
        check("an update below the replayed totals", None, ("earlier", 2000, 1000, 90), ("tu", 1000, 400, 50),
              ("tu", 2500, 1400, 100))
        check("one field below the baseline", (2000, 1000, 0, 101), ("tu", 2500, 1400, 100))
        for garbage in ("x", {"total": [1]}, {"total": {"inputTokens": "9", "cachedInputTokens": 0, "outputTokens": 1}}):
            turn = self.turn()
            self.update(turn, "tu", 900, 300, 40)
            turn._notification("thread/tokenUsage/updated", {"threadId": "th", "turnId": "tu", "tokenUsage": garbage})
            self.assertIsNone(turn.turn_usage(), garbage)
        other = self.turn()
        other._notification("thread/tokenUsage/updated", {"threadId": "other", "turnId": "tu", "tokenUsage": {
            "total": {"inputTokens": 9, "cachedInputTokens": 0, "outputTokens": 1}}})
        self.update(other, "tu", 900, 300, 40)
        self.assertEqual(other.turn_usage(), tokens("gpt-x", 600, 40, 300, 0))  # another thread's is not this one's


USED = tokens("claude-sonnet-5-5", 18, 1330, 311850, 19448)


class Finishes(Adapter):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        return self.result


class TerminalEventUsageTest(Base):
    def test_completed_and_failed_carry_usage_only_when_present(self):
        cases = [
            (RunResult(exit_code=0, ok=True, output="done", session_id="s", usage=USED), "awaiting_review", "completed", USED),
            (RunResult(exit_code=0, ok=True, output="done", session_id="s"), "awaiting_review", "completed", None),
            (RunResult(exit_code=1, error="boom", session_id="s", usage=USED), "failed", "failed", USED),
            (RunResult(exit_code=1, error="boom", session_id="s"), "failed", "failed", None),
            (RunResult(exit_code=0, ok=True, output="done", session_id="s", usage={"model": "x", "input_tokens": "9"}),
             "awaiting_review", "completed", None),
        ]
        for n, (result, outcome, kind, expected) in enumerate(cases):
            job = "j%d" % n
            cloud = ControlCloud([{"id": job, "plan_id": "p%d" % n}])
            self.assertEqual(self.agent(cloud, Finishes(result)).run_once(), "job %s → %s" % (job, outcome))
            event = self.events(cloud, job)[-1]
            self.assertEqual(event["type"], kind)
            if expected is None:
                self.assertNotIn("usage", event)
            else:
                self.assertEqual(event["usage"], expected)

    def test_a_quota_pause_carries_what_the_run_used_before_it(self):
        result = RunResult(exit_code=1, blocked=True, error="usage limit", session_id="s", usage=USED)
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}])
        self.assertEqual(self.agent(cloud, Finishes(result)).run_once(), "job j1 → waiting_quota")
        self.assertEqual(self.events(cloud)[-1]["type"], "waiting_quota")
        self.assertEqual(self.events(cloud)[-1]["usage"], USED)
        cloud = ControlCloud([{"id": "j2", "plan_id": "p2"}])
        blocked = RunResult(exit_code=1, blocked=True, error="usage limit", session_id="s")
        self.agent(cloud, Finishes(blocked)).run_once()
        self.assertNotIn("usage", self.events(cloud, "j2")[-1])

    def test_a_cancelled_run_carries_what_it_used(self):
        class Used(WaitUntilCancelled):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                result = super().start(prompt, cwd, session_id, log_file, cancel_event)
                result.usage = USED
                return result
        adapter = Used()
        extra = {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}], lambda: extra)
        agent = self.agent(cloud, adapter)
        out = []
        worker = threading.Thread(target=lambda: out.append(agent.run_once()))
        worker.start()
        self.assertTrue(adapter.entered.wait(10))
        extra["desired_action"] = "cancel"
        worker.join(15)
        self.assertEqual(out, ["job j1 → cancelled"])
        last = self.events(cloud)[-1]
        self.assertEqual(last["type"], "cancelled")
        self.assertEqual(last["usage"], USED)

    def test_model_name_and_counts_are_bounded_on_the_way_out(self):
        result = RunResult(exit_code=0, ok=True, output="done", session_id="s",
                           usage=dict(USED, model="m" * 200, output_tokens=10 ** 14))
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, Finishes(result))
        agent.run_once()
        sent = self.events(cloud)[-1]["usage"]
        self.assertEqual(sent["model"], "m" * 64)
        self.assertEqual(sent["output_tokens"], usage.MAX_COUNT)
        # Whatever sets it, an event leaves this computer only with a usage in the contract's shape.
        self.assertNotIn("usage", agent._outbound({"type": "completed", "usage": "lots"}))
        self.assertEqual(agent._outbound({"type": "completed", "usage": USED})["usage"], USED)

    def test_interrupted_by_user_carries_usage(self):
        class Interrupted(WaitUntilCancelled):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                result = super().start(prompt, cwd, session_id, log_file, cancel_event)
                result.usage = USED
                return result
        adapter = Interrupted()
        extra = {}
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}], lambda: extra)
        agent = self.agent(cloud, adapter)
        out = []
        worker = threading.Thread(target=lambda: out.append(agent.run_once()))
        worker.start()
        self.assertTrue(adapter.entered.wait(10))
        extra["controls"] = [{"id": "c1", "action": "interrupt"}]
        worker.join(15)
        self.assertEqual(out, ["job j1 → interrupted"])
        last = self.events(cloud)[-1]
        self.assertEqual(last["type"], "interrupted_by_user")
        self.assertEqual(last["usage"], USED)

    def test_an_appended_turn_adds_its_usage_to_the_first(self):
        class Turn(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.entered.set()
                time.sleep(.5)
                return RunResult(exit_code=0, ok=True, output="first", session_id="thread-1",
                                 usage=tokens("gpt-6.1-sol", 100, 10, 1000, 0))

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=0, ok=True, output="second", session_id=session_id,
                                 usage=tokens("gpt-6.1-sol", 20, 5, 900, 3))
        adapter = Turn()
        extra = {}

        def lease_extra():
            if adapter.entered.is_set():
                extra["controls"] = [{"id": "c9", "action": "append", "text": "one more thing"}]
            return extra
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}], lease_extra)
        self.assertEqual(self.agent(cloud, adapter).run_once(), "job j1 → awaiting_review")
        done = self.events(cloud)[-1]
        self.assertEqual(done["type"], "completed")
        self.assertEqual(done["usage"], tokens("gpt-6.1-sol", 120, 15, 1900, 3))


class ChatTurnUsageTest(unittest.TestCase):
    run_turn = chat_turn.AgentChatTurnTest.run_turn

    def test_chat_reply_and_failure_carry_usage(self):
        cloud = ChatCloud()
        self.run_turn(cloud, ChatAdapter(RunResult(exit_code=0, ok=True, output="hi", session_id="s1", usage=USED)))
        self.assertEqual(cloud.events[-1]["type"], "completed")
        self.assertEqual(cloud.events[-1]["usage"], USED)
        cloud = ChatCloud()
        self.run_turn(cloud, ChatAdapter(RunResult(exit_code=1, error="boom", session_id="s1", usage=USED)))
        self.assertEqual(cloud.events[-1]["type"], "failed")
        self.assertEqual(cloud.events[-1]["usage"], USED)
        cloud = ChatCloud()
        self.run_turn(cloud, ChatAdapter())
        self.assertNotIn("usage", cloud.events[-1])


if __name__ == "__main__":
    unittest.main()
