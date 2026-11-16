import subprocess
import threading
import time
import unittest
from pathlib import Path

from tests.test_remote_jobs import Adapter, Base, ControlCloud
from timetrace.cli import _runner_tools
from timetrace.models import RunResult
from timetrace.remote_control import RunControl


def job(job_id, mode="execute", **extra):
    return dict(id=job_id, plan_id="plan-" + job_id,
                kind="task_chat" if mode == "chat" else "task",
                work_session_id="session-1", work_session_turn_id="turn-" + job_id,
                work_session_message_id="message-" + job_id, work_session_mode=mode, **extra)


class SessionAdapter(Adapter):
    def capabilities(self):
        return dict(super().capabilities(), can_interact=True)

    def work_session(self, prompt, cwd, session_id, log_file, cancel_event, *,
                     control, resuming, read_only, mcp=None):
        self.calls.append((cwd, session_id, resuming, read_only))
        if not read_only and not resuming:
            (Path(cwd) / "marker").write_text("original worktree")
            subprocess.check_call(["git", "-C", cwd, "add", "marker"])
            subprocess.check_call(["git", "-C", cwd, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                   "commit", "--quiet", "-m", "Add marker"])
        self.entered.set()
        control.provider_session("native-thread")
        control.initial_delivery("applied")
        control.assistant_message("Only the user-facing reply")
        return RunResult(ok=True, session_id="native-thread", output="Only the user-facing reply")


class WorkSessionRunnerTest(Base):
    def test_cli_inventory_advertises_interactive_tool_capability(self):
        inventory = _runner_tools({}, {"codex": SessionAdapter(), "claude": Adapter()})
        self.assertEqual([profile.get("can_interact") for profile in inventory], [True, False])
    def test_next_plan_chat_keeps_the_native_session_and_exact_worktree(self):
        adapter = SessionAdapter()
        cloud = ControlCloud([
            job("first"), job("second", "chat", provider_session_id="native-thread"),
            job("third", provider_session_id="native-thread"),
        ])
        agent = self.agent(cloud, adapter, upload_output_tail=True)
        self.assertEqual(agent.run_once(), "job first → awaiting_review")
        self.assertEqual(agent.run_once(), "job second → replied")
        self.assertEqual(agent.run_once(), "job third → awaiting_review")
        cwd, _, _, _ = adapter.calls[0]
        self.assertEqual(adapter.calls[1:], [(cwd, "native-thread", True, True),
                                             (cwd, "native-thread", True, False)])
        self.assertEqual((Path(cwd) / "marker").read_text(), "original worktree")
        first = self.events(cloud, "first")[-1]
        self.assertNotEqual(first["input_head"], first["output_commit"])
        self.assertIn("marker", first["artifacts"][0]["content"])
        self.assertEqual(first["result_summary"], "completed")
        events = self.events(cloud, "second")
        self.assertEqual([e["observed_at"] for e in events], sorted(e["observed_at"] for e in events))
        self.assertEqual([e["type"] for e in events], [
            "running", "provider_session", "message_delivered", "assistant_message", "completed"])
        self.assertEqual(events[2]["work_session_message_id"], "message-second")
        self.assertEqual(events[3]["reply"], "Only the user-facing reply")
        self.assertNotIn("output_tail", events[-1])
        self.assertNotIn("branch", events[-1])
        self.assertEqual(events[-1]["provider_session_id"], "native-thread")

    def test_release_removes_only_retention_metadata(self):
        cloud = ControlCloud([job("first")])
        adapter = SessionAdapter()
        agent = self.agent(cloud, adapter)
        self.assertEqual(agent.run_once(), "job first → awaiting_review")
        self.assertIsNotNone(self.db.get_checkpoint("work-session:session-1"))
        cloud.work_session_retention = lambda token, session_id: {"release": session_id == "session-1"}
        agent.maintain(now=time.time() + 600)
        self.assertIsNone(self.db.get_checkpoint("work-session:session-1"))
        self.assertTrue((Path(adapter.calls[0][0]) / "marker").is_file())

    def test_repeated_lease_append_waits_for_ack_and_replays_no_input(self):
        def control():
            return RunControl("first", self.home, threading.Event(), turn_id="turn-1", message_id="message-1",
                              reserve_input=lambda key: self.db.reserve_work_session_input("session-1", key))
        runner = control()
        pending = []
        runner.attach_interactive(lambda key, text, finish: pending.append((key, text, finish)))
        first = {"id": "one", "action": "append", "text": "first", "work_session_turn_id": "turn-1"}
        second = {"id": "two", "action": "append", "text": "second", "work_session_turn_id": "turn-1"}
        stale = {"id": "old", "action": "append", "text": "stale", "work_session_turn_id": "old-turn"}
        lease = {"controls": [first, second, stale]}
        runner.apply_lease(lease)
        runner.apply_lease(lease)
        self.assertEqual([(key, text) for key, text, _ in pending], [("one", "first")])
        self.assertEqual([(event["control_id"], event["status"]) for event in runner.take_events()],
                         [("old", "rejected")])
        pending[0][2]("applied")
        self.assertEqual([(key, text) for key, text, _ in pending], [("one", "first"), ("two", "second")])
        runner.next_followup(ok=True)
        self.assertEqual([(event["control_id"], event["status"]) for event in runner.take_events()],
                         [("one", "applied"), ("two", "uncertain")])
        restarted = control()
        restarted.attach_interactive(lambda *args: self.fail("reserved input was sent again"))
        restarted.apply_lease(lease)
        self.assertIn(("two", "uncertain"), [(e["control_id"], e["status"]) for e in restarted.take_events()])

    def test_missing_local_checkpoint_does_not_create_a_replacement_worktree(self):
        cloud = ControlCloud([job("missing", provider_session_id="native-thread")])
        adapter = SessionAdapter()
        outcome = self.agent(cloud, adapter).run_once()
        self.assertIn("blocked", outcome)
        self.assertIn("checkpoint missing", self.events(cloud, "missing")[-1]["message"])
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
