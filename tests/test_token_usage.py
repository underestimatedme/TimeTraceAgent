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
        saved = {"input_tokens": 16000, "cached_input_tokens": 12000, "cache_write_input_tokens": 0, "output_tokens": 1000}
        res = codex.parse_exec(lines("codex_exec_cached.jsonl"), start=saved, resumed=True)
        self.assertEqual(res.usage, tokens("", 419 - 160, 110, 160, 0))
        # Saved totals unknown: no usage rather than the whole thread's.
        res = codex.parse_exec(lines("codex_exec_cached.jsonl"), resumed=True)
        self.assertIsNone(res.usage)
        self.assertTrue(res.ok)

    def test_missing_or_garbage_usage_is_none_and_never_fails_the_run(self):
        for reported in ("lots", None, {"input_tokens": "1200", "output_tokens": -5}, {}):
            event = {"type": "turn.completed"}
            if reported is not None:
                event["usage"] = reported
            res = codex.parse_exec([json.dumps({"type": "thread.started", "thread_id": THREAD}), json.dumps(event), "[1]"])
            self.assertTrue(res.ok, reported)
            self.assertIsNone(res.usage, reported)


def write_rollout(home, thread, records, day="2026/11/16"):
    folder = Path(home) / "sessions" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("rollout-2026-11-16T09-00-00-%s.jsonl" % thread)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def token_count(inputs, cached, output):
    total = {"input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": output, "total_tokens": inputs + output}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": total,
                                                                            "last_token_usage": total}}}


class CodexThreadTotalsTest(unittest.TestCase):
    def test_last_recorded_totals_of_the_thread(self):
        with tempfile.TemporaryDirectory() as home:
            write_rollout(home, THREAD, [token_count(100, 50, 5), {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                                         token_count(16000, 12000, 1000), {"type": "response_item", "payload": {}}])
            self.assertEqual(codex.thread_totals(THREAD, Path(home))["input_tokens"], 16000)
            record = {"type": "token_usage_record", "payload": {"thread_token_usage": {"input_tokens": 17000}}}
            write_rollout(home, THREAD, [token_count(16000, 12000, 1000), record])
            self.assertEqual(codex.thread_totals(THREAD, Path(home)), {"input_tokens": 17000})

    def test_unknown_unless_the_file_can_be_read(self):
        other = "01a167a9-1103-7440-8bd4-d7f0e0a6ba5f"
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(codex.thread_totals(THREAD, Path(home)))  # no sessions directory
            write_rollout(home, other, [])
            self.assertEqual(codex.thread_totals(other, Path(home)), {})  # read: the thread recorded no usage yet
            for bad in ("tid-1", "*", "../x", None, THREAD + "*"):
                self.assertIsNone(codex.thread_totals(bad, Path(home)))
            write_rollout(home, THREAD, [token_count(100, 50, 5)] + [{"type": "response_item", "payload": {"x": "y" * 100}}] * 50)
            with patch.object(codex, "ROLLOUT_TAIL_BYTES", 1000):
                self.assertIsNone(codex.thread_totals(THREAD, Path(home)))  # not within the bytes read
            link_home = Path(home) / "linked"
            target = write_rollout(link_home, other, [token_count(1, 0, 1)])
            linked = target.parent / ("rollout-2026-11-16T09-00-00-%s.jsonl" % THREAD)
            linked.symlink_to(target)
            self.assertIsNone(codex.thread_totals(THREAD, link_home))

    def test_adapter_resume_and_chat_subtract_the_saved_totals(self):
        with tempfile.TemporaryDirectory() as home:
            write_rollout(home, THREAD, [token_count(16000, 12000, 1000)])
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

    def test_starting_totals(self):
        def turn(saved):
            t = CodexTurn([], "/", None, threading.Event(), 3, False)
            t.thread_id, t.turn_id, t.model, t.saved_totals = "th", "tu", "gpt-x", saved
            return t

        end = {"threadId": "th", "turnId": "tu",
               "tokenUsage": {"total": {"inputTokens": 900, "cachedInputTokens": 300, "outputTokens": 40}}}
        new = turn({})
        new._notification("thread/tokenUsage/updated", end)
        self.assertEqual(new.turn_usage(), tokens("gpt-x", 600, 40, 300, 0))
        # No replay: the rollout's saved totals (snake case) are the starting point.
        resumed = turn({"input_tokens": 500, "cached_input_tokens": 200, "output_tokens": 30})
        resumed._notification("thread/tokenUsage/updated", end)
        self.assertEqual(resumed.turn_usage(), tokens("gpt-x", 300, 10, 100, 0))
        unknown = turn(None)
        unknown._notification("thread/tokenUsage/updated", end)
        self.assertIsNone(unknown.turn_usage())
        for garbage in ({"threadId": "th", "turnId": "tu", "tokenUsage": "x"},
                        {"threadId": "th", "turnId": "tu", "tokenUsage": {"total": [1]}},
                        {"threadId": "other", "turnId": "tu", "tokenUsage": end["tokenUsage"]}):
            quiet = turn({})
            quiet._notification("thread/tokenUsage/updated", garbage)
            self.assertIsNone(quiet.turn_usage())


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

    def test_waiting_quota_does_not_carry_usage(self):
        result = RunResult(exit_code=1, blocked=True, error="usage limit", session_id="s", usage=USED)
        cloud = ControlCloud([{"id": "j1", "plan_id": "p1"}])
        self.assertEqual(self.agent(cloud, Finishes(result)).run_once(), "job j1 → waiting_quota")
        self.assertNotIn("usage", self.events(cloud)[-1])

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
