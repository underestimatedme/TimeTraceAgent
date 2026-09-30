"""Parallel slots counted per tool (config `max_parallel_per_tool`)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from timetrace import cli, config
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.dispatch import coding_slot_lock, tool_slot_lock
from timetrace.models import RunResult
from tests.test_parallel import BarrierAdapter, QueueCloud, init_repo


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(config.DEFAULTS["max_parallel_per_tool"], {"claude": 2, "codex": 2})
        # 0 = not set: the overall cap is the sum of the per-tool values.
        self.assertEqual(config.DEFAULTS["max_parallel"], 0)
        self.assertEqual(cli._max_parallel_per_tool(config.DEFAULTS), {"claude": 2, "codex": 2})
        self.assertEqual(cli._max_parallel(config.DEFAULTS), 4)

    def test_per_tool_values_are_bounded(self):
        cfg = {"max_parallel_per_tool": {"claude": 99, "codex": 0, "cursor": 3, "BAD NAME": 2}}
        self.assertEqual(cli._max_parallel_per_tool(cfg), {"claude": 8, "codex": 1})
        self.assertEqual(cli._max_parallel_per_tool({"max_parallel_per_tool": {"claude": "x"}}),
                         {"claude": 1, "codex": 2})
        self.assertEqual(cli._max_parallel_per_tool({"max_parallel_per_tool": "nope"}),
                         {"claude": 2, "codex": 2})

    def test_explicit_max_parallel_is_the_overall_cap(self):
        per = {"max_parallel_per_tool": {"claude": 3, "codex": 3}}
        self.assertEqual(cli._max_parallel(dict(per)), 6)
        self.assertEqual(cli._max_parallel(dict(per, max_parallel=2)), 2)
        self.assertEqual(cli._max_parallel(dict(per, max_parallel=1)), 1)
        # Valley accepts 1–8.
        self.assertEqual(cli._max_parallel({"max_parallel_per_tool": {"claude": 8, "codex": 8}}), 8)
        self.assertEqual(cli._max_parallel(dict(per, max_parallel=99)), 8)
        self.assertEqual(cli._max_parallel(dict(per, max_parallel="x")), 1)

    def test_user_override_merges_with_the_default_per_tool_values(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "config.json").write_text(json.dumps({"max_parallel_per_tool": {"claude": 3}}))
            cfg = config.load(Path(d))
            self.assertEqual(cli._max_parallel_per_tool(cfg), {"claude": 3, "codex": 2})
            self.assertEqual(cli._max_parallel(cfg), 5)

    def test_config_set_per_tool_value(self):
        with tempfile.TemporaryDirectory() as d:
            config.set_value(Path(d), "max_parallel_per_tool.codex", "3")
            config.set_value(Path(d), "max_parallel", "0")
            doc = json.loads(Path(d, "config.json").read_text())
            self.assertEqual(doc, {"max_parallel_per_tool": {"codex": 3}, "max_parallel": 0})
            self.assertEqual(cli._max_parallel_per_tool(config.load(Path(d))), {"claude": 2, "codex": 3})
            for key, raw in (("max_parallel_per_tool.codex", "0"), ("max_parallel_per_tool.codex", "9"),
                             ("max_parallel_per_tool.cursor", "2"), ("max_parallel_per_tool", "2")):
                with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                    config.set_value(Path(d), key, raw)

    def test_inventory_reports_per_tool_values(self):
        from timetrace.cloud import CloudClient
        sent = []
        client = CloudClient("https://example.invalid")
        client.request = lambda method, path, body=None, token=None: sent.append(body) or {}
        client.update_inventory("t", [], [], 4, {"claude": 2, "codex": 2})
        self.assertEqual(sent, [{"workspaces": [], "tools": [], "max_parallel": 4,
                                 "max_parallel_per_tool": {"claude": 2, "codex": 2}}])


class ToolSlotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.repo = self.d / "repo"; init_repo(self.repo)
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.home = self.d / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, cloud, adapters, **kwargs):
        return Agent(self.db, cloud, adapters, self.home, lambda: "token", workspace_wait=10, **kwargs)

    def run_concurrently(self, agent, n):
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(agent.run_once())) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        return sorted(outcomes)

    def test_tool_slot_lock_names(self):
        self.assertEqual(Path(tool_slot_lock(self.home, "codex", 0).path).name, "coding-slot-codex-0.lock")
        self.assertEqual(Path(tool_slot_lock(self.home, "../x", 1).path).name, "coding-slot-___x-1.lock")

    def test_claude_and_codex_run_side_by_side_with_one_slot_each(self):
        barrier = BarrierAdapter(parties=2)
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "provider": "codex"},
                            {"id": "j2", "plan_id": "p2", "provider": "claude",
                             "tool_profile_id": "claude-default"}])
        agent = self.agent(cloud, {"codex": barrier, "claude": barrier}, max_parallel=2,
                           max_parallel_per_tool={"codex": 1, "claude": 1})
        outcomes = self.run_concurrently(agent, 2)
        self.assertEqual(outcomes, ["job j1 → awaiting_review", "job j2 → awaiting_review"])

    def test_a_tool_beyond_its_slots_is_deferred_although_the_runner_has_room(self):
        adapter = BarrierAdapter(parties=2, timeout=1)
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}, {"id": "j2", "plan_id": "p2"}])
        agent = self.agent(cloud, {"codex": adapter}, max_parallel=4,
                           max_parallel_per_tool={"codex": 1, "claude": 2})
        outcomes = self.run_concurrently(agent, 2)
        self.assertEqual(len(adapter.cwds), 1)
        self.assertTrue(any("deferred (tool busy: codex)" in o for o in outcomes), outcomes)

    def test_overall_cap_still_applies(self):
        adapter = BarrierAdapter(parties=2, timeout=1)
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "provider": "codex"},
                            {"id": "j2", "plan_id": "p2", "provider": "claude",
                             "tool_profile_id": "claude-default"}])
        agent = self.agent(cloud, {"codex": adapter, "claude": adapter}, max_parallel=1,
                           max_parallel_per_tool={"codex": 2, "claude": 2})
        outcomes = self.run_concurrently(agent, 2)
        self.assertEqual(len(adapter.cwds), 1)
        self.assertTrue(any("deferred (runner busy)" in o for o in outcomes), outcomes)

    def test_fenced_tool_slot_is_skipped_and_all_locks_are_released(self):
        path = Path(tool_slot_lock(self.home, "codex", 0).path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, {"codex": BarrierAdapter(parties=1)}, max_parallel=2,
                           max_parallel_per_tool={"codex": 2})
        self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(path.read_text(), "1234", "the fence stays for manual recovery")
        tool_slot_lock(self.home, "codex", 1).acquire().release()
        coding_slot_lock(self.home).acquire().release()

    def test_fence_on_every_tool_slot_surfaces(self):
        for index in (0, 1):
            path = Path(tool_slot_lock(self.home, "codex", index).path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1234")
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
        agent = self.agent(cloud, {"codex": BarrierAdapter(parties=1)}, max_parallel=2,
                           max_parallel_per_tool={"codex": 2})
        outcome = agent.run_once()
        self.assertIn("deferred (tool busy: codex)", outcome)
        self.assertIn("manual recovery required", outcome)
        # The overall slot taken is never left behind.
        coding_slot_lock(self.home).acquire().release()

    def test_overall_slot_failure_releases_the_tool_slot(self):
        busy = coding_slot_lock(self.home).acquire()
        try:
            cloud = QueueCloud([{"id": "j1", "plan_id": "p1"}])
            agent = self.agent(cloud, {"codex": BarrierAdapter(parties=1)}, max_parallel=1,
                               max_parallel_per_tool={"codex": 1})
            self.assertIn("deferred (runner busy)", agent.run_once())
        finally:
            busy.release()
        tool_slot_lock(self.home, "codex", 0).acquire().release()

    def test_chat_turns_count_against_their_tool(self):
        class Chat(BarrierAdapter):
            def chat(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=0, ok=True, output="hi", session_id="s")
        held = tool_slot_lock(self.home, "codex", 0).acquire()
        try:
            cloud = QueueCloud([{"id": "c1", "kind": "chat_turn"}])
            agent = self.agent(cloud, {"codex": Chat(parties=1)}, max_parallel=2,
                               max_parallel_per_tool={"codex": 1})
            self.assertIn("deferred (tool busy: codex)", agent.run_once())
        finally:
            held.release()

    def test_stop_cancels_jobs_of_both_tools_and_frees_every_slot(self):
        import time
        from tests.test_shutdown import WaitingAdapter
        cloud = QueueCloud([{"id": "j1", "plan_id": "p1", "provider": "codex"},
                            {"id": "j2", "plan_id": "p2", "provider": "claude",
                             "tool_profile_id": "claude-default"}])
        adapter = WaitingAdapter()
        agent = self.agent(cloud, {"codex": adapter, "claude": adapter}, max_parallel=2,
                           max_parallel_per_tool={"codex": 1, "claude": 1})
        done = threading.Event()
        loop = threading.Thread(target=lambda: (agent.run_forever(interval=60, log=lambda m: None), done.set()),
                                daemon=True)
        loop.start()
        for _ in range(200):
            if adapter.runs == 2:
                break
            time.sleep(.05)
        self.assertEqual(adapter.runs, 2)
        agent.stop()
        self.assertTrue(done.wait(15))
        stopped = sorted(job for job, e in cloud.events
                         if e["type"] == "waiting_input" and "runner_stopped" in e["message"])
        self.assertEqual(stopped, ["j1", "j2"])
        for tool in ("codex", "claude"):
            tool_slot_lock(self.home, tool, 0).acquire().release()
        coding_slot_lock(self.home).acquire().release()
        coding_slot_lock(self.home, 1).acquire().release()


if __name__ == "__main__":
    unittest.main()
