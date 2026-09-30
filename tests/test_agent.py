import json
import io
import tempfile
import threading
import time
import unittest
import subprocess
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path

from timetrace.agent import Agent
from timetrace import worktree
from timetrace.checkpoints import Checkpoint
from timetrace.db import Database
from timetrace.models import RunResult, Sample


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=path, check=True)


class FakeCloud:
    def __init__(self):
        self.events = []
        self.quota_posts = []

    def post_quota_samples(self, token, samples):
        self.quota_posts.append((token, samples))
        return {"accepted": len(samples)}

    def claim(self, token):
        return {"job": {"id": "j1", "workspace_id": "ws1", "tool_profile_id": "codex-default", "provider": "codex", "prompt": "do it"},
                "attempt_id": "a1", "lease_epoch": 1,
                "lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        self.renewed = (attempt_id, epoch)
        return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}


class Adapter:
    def capabilities(self):
        # A verified subscription profile: zero additional spend guaranteed.
        return {"can_record": True, "can_read_quota": True, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.args = (prompt, cwd)
        return RunResult(exit_code=0, ok=True, output="finished", session_id=session_id)


class AgentOccurrenceTest(unittest.TestCase):
    def test_go_rfc3339_lease_fraction_is_valid_on_python39(self):
        for fraction, expected in [("1", .1), ("12", .12), ("12345", .12345), ("123456789", .123456)]:
            with self.subTest(fraction=fraction):
                deadline = Agent._deadline({"lease_expires_at": "2030-01-01T00:00:00." + fraction + "Z"})
                self.assertAlmostEqual(deadline, 1893456000 + expected, places=5)

    def check_boundary(self, delay):
        class Clock:
            value = datetime.now(timezone.utc)
            @classmethod
            def now(cls, zone):
                return cls.value
            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)
            @classmethod
            def advance(cls, seconds):
                cls.value += timedelta(seconds=seconds)

        class SlowCloud(FakeCloud):
            def append_events(self, *args):
                if delay == "running_flush" and args[-1][0]["type"] == "running":
                    Clock.advance(7)
                return super().append_events(*args)
            def renew(self, *args):
                if delay == "renew":
                    Clock.advance(11)
                return super().renew(*args)
            def post_quota_samples(self, *args):
                Clock.advance(13)
                return super().post_quota_samples(*args)

        class TimedAdapter(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.entered = Clock.value
                Clock.advance(2)
                self.exited = Clock.value
                if delay == "cancel_return":
                    cancel_event.set()
                    return RunResult(exit_code=143, ok=False, error="cancelled")
                if delay in ("exception", "cancel"):
                    if delay == "cancel":
                        cancel_event.set()
                    raise RuntimeError("adapter stopped")
                if delay == "quota_upload":
                    return RunResult(exit_code=1, blocked=True, session_id=session_id,
                                     samples=[Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=100, reset_at=2000, window_mins=300)])
                return RunResult(exit_code=0, ok=True)

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = SlowCloud(), TimedAdapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda *args: (str(repo), "main"))
            original_snapshot = worktree.snapshot
            def slow_snapshot(path):
                Clock.advance(17)
                return original_snapshot(path)
            with patch("timetrace.agent.datetime", Clock), patch("timetrace.agent.worktree.snapshot", slow_snapshot):
                agent.run_once()
            self.assertEqual([e["type"] for e in cloud.events], ["running", {"quota_upload": "waiting_quota", "exception": "failed", "cancel": "cancelled", "cancel_return": "cancelled"}.get(delay, "completed")])
            start, end = [datetime.fromisoformat(e["observed_at"].replace("Z", "+00:00")) for e in cloud.events]
            self.assertEqual(start, adapter.entered, "pre-start network wait is not AI activity")
            self.assertEqual(end, adapter.exited, "post-adapter IO is not AI activity")
            self.assertEqual((end - start).total_seconds(), 2)
            self.assertEqual(db.pending_remote_events(), [])

    def test_slow_running_flush_not_active(self): self.check_boundary("running_flush")
    def test_slow_renew_not_active(self): self.check_boundary("renew")
    def test_slow_quota_upload_and_snapshot_not_active(self): self.check_boundary("quota_upload")
    def test_exception_ends_at_adapter_boundary(self): self.check_boundary("exception")
    def test_cancel_exception_ends_at_adapter_boundary(self): self.check_boundary("cancel")
    def test_cancel_return_ends_at_adapter_boundary(self): self.check_boundary("cancel_return")


class AgentTest(unittest.TestCase):
    def test_pool_authority_must_be_explicit_not_inferred_from_callback(self):
        for binding, expected in ((lambda provider: ("pool", "custom-profile"), False),
                                  (lambda provider: ("pool", "custom-profile", True), True)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as d:
                db = Database(Path(d) / "timetrace.db")
                cloud = FakeCloud()
                agent = Agent(db, cloud, {}, Path(d), lambda: "token", pool_binding=binding)
                samples = [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=6,
                                  reset_at=2000, window_mins=300)]
                self.assertEqual(agent._post_samples("codex", Adapter(), samples, now=1000), 1)
                self.assertEqual(cloud.quota_posts[0][1][0]["pool_authoritative"], expected)

    def test_explicit_provider_dispatches_custom_profile_and_missing_provider_blocks(self):
        for provider, expected in (("codex", "awaiting_review"), (None, "rejected (tool unavailable)")):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as d:
                db = Database(Path(d) / "timetrace.db")
                repo = Path(d) / "repo"; init_repo(repo)
                db.upsert_workspace("ws1", "repo", str(repo), "main")
                cloud, adapter = FakeCloud(), Adapter()
                claim = cloud.claim("token")
                claim["job"]["tool_profile_id"] = "custom-profile" if provider else "codex-default"
                claim["job"]["provider"] = provider
                cloud.claim = lambda token: claim
                agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                              prepare_workspace=lambda repo, task_id, home, base: (repo, "branch"))
                self.assertEqual(agent.run_once(), "job j1 → " + expected)
                self.assertEqual(hasattr(adapter, "args"), provider is not None)

    def test_claim_is_persisted_before_execution_and_completed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = FakeCloud(), Adapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            self.assertEqual(adapter.args, ("do it", str(repo.resolve())))
            self.assertEqual([event["type"] for event in cloud.events], ["running", "completed"])
            self.assertEqual(db.get_remote_claim("j1")["state"], "reported")
            self.assertEqual(db.pending_remote_events(), [])

    def test_registered_default_branch_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "release")
            bases = []
            agent = Agent(db, FakeCloud(), {"codex": Adapter()}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (bases.append(base) or repo, "branch"))
            agent.run_once()
            self.assertEqual(bases, ["release"])

    def test_duplicate_running_claim_is_not_started_again(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = FakeCloud(), Adapter()
            db.save_remote_claim(cloud.claim("token"), state="running")
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token")
            self.assertIn("duplicate", agent.run_once())
            self.assertFalse(hasattr(adapter, "args"))

    def test_long_run_renews_lease(self):
        class SlowAdapter(Adapter):
            def start(self, *args):
                time.sleep(.04)
                return super().start(*args)
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": SlowAdapter()}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "branch"), heartbeat_interval=.01)
            agent.run_once()
            self.assertEqual(cloud.renewed, ("a1", 1))

    def test_cancel_command_stops_adapter_and_is_acknowledged(self):
        class CancelCloud(FakeCloud):
            def renew(self, token, attempt_id, epoch):
                if hasattr(self, "renewed"):
                    return {"desired_action": "cancel"}
                return super().renew(token, attempt_id, epoch)
        class CancellableAdapter(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.cancelled = cancel_event.wait(.5)
                return RunResult(exit_code=143, ok=False, error="terminated")
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = CancelCloud(), CancellableAdapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "branch"), heartbeat_interval=.01)
            self.assertEqual(agent.run_once(), "job j1 → cancelled")
            self.assertTrue(adapter.cancelled)
            self.assertEqual(cloud.events[-1]["type"], "cancelled")

    def test_unknown_workspace_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": Adapter()}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → rejected (unknown workspace)")
            self.assertEqual(cloud.events[-1]["type"], "failed")

    def test_completion_survives_network_failure_in_outbox(self):
        class FlakyCloud(FakeCloud):
            def append_events(self, token, job_id, attempt_id, epoch, events):
                if any(event["seq"] == 2 for event in events) and not getattr(self, "recovered", False):
                    raise OSError("offline")
                super().append_events(token, job_id, attempt_id, epoch, events)
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FlakyCloud()
            agent = Agent(db, cloud, {"codex": Adapter()}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            self.assertEqual([row["seq"] for row in db.pending_remote_events()], [1, 2])
            queued_event = db.pending_remote_events()[1]["payload"]
            self.assertIn("observed_at", queued_event)
            observed = queued_event["observed_at"]
            self.assertLessEqual(datetime.fromisoformat(observed.replace("Z", "+00:00")).timestamp(), time.time())
            cloud.recovered = True
            agent.flush_outbox()
            self.assertEqual(db.pending_remote_events(), [])
            self.assertEqual(cloud.events[-1]["type"], "completed")
            self.assertEqual(cloud.events[-1]["observed_at"], observed)

    def test_unverified_billing_blocks_execution(self):
        class UnverifiedAdapter(Adapter):
            def capabilities(self):
                caps = super().capabilities()
                caps["can_enforce_zero_spend"] = False
                return caps

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = FakeCloud(), UnverifiedAdapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → blocked (billing_unverified)")
            # The adapter must never have been started.
            self.assertFalse(hasattr(adapter, "args"))
            self.assertEqual(cloud.events[-1]["type"], "waiting_input")

    def test_malformed_capabilities_block_without_execution(self):
        def raising_capabilities():
            raise RuntimeError("unavailable")

        class MalformedAdapter:
            def __init__(self, capabilities):
                self.capabilities = capabilities
                self.calls = []

            def start(self, *args):
                self.calls.append("start")
                return RunResult(exit_code=0, ok=True)

            def resume(self, *args):
                self.calls.append("resume")
                return RunResult(exit_code=0, ok=True)

        for capabilities in (raising_capabilities, lambda: ["not a mapping"], {}):
            with self.subTest(capabilities=capabilities):
                with tempfile.TemporaryDirectory() as d:
                    db = Database(Path(d) / "timetrace.db")
                    repo = Path(d) / "repo"; init_repo(repo)
                    db.upsert_workspace("ws1", "repo", str(repo), "main")
                    cloud, adapter = FakeCloud(), MalformedAdapter(capabilities)
                    agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                                  prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
                    self.assertEqual(agent.run_once(), "job j1 → blocked (billing_unverified)")
                    self.assertEqual(adapter.calls, [])
                    self.assertEqual(db.get_remote_claim("j1")["state"], "reported")
                    self.assertEqual(cloud.events[-1]["type"], "waiting_input")

    def test_raising_capabilities_property_blocks_without_execution(self):
        class RaisingPropertyAdapter:
            def __init__(self):
                self.calls = []

            @property
            def capabilities(self):
                raise RuntimeError("property unavailable")

            def start(self, *args):
                self.calls.append("start")
                return RunResult(exit_code=0, ok=True)

            def resume(self, *args):
                self.calls.append("resume")
                return RunResult(exit_code=0, ok=True)

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = FakeCloud(), RaisingPropertyAdapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → blocked (billing_unverified)")
            self.assertEqual(adapter.calls, [])
            self.assertEqual(db.get_remote_claim("j1")["state"], "reported")
            self.assertEqual(cloud.events[-1]["type"], "waiting_input")

    def test_unverified_billing_blocks_resume_before_running(self):
        class UnverifiedResumingAdapter(Adapter):
            def __init__(self):
                self.calls = []

            def capabilities(self):
                caps = super().capabilities()
                caps["can_enforce_zero_spend"] = False
                return caps

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.calls.append((prompt, cwd, session_id))
                return RunResult(exit_code=0, ok=True, session_id=session_id)

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            db.save_checkpoint(Checkpoint(
                plan_id="j1", job_id="j1", attempt_id="a1", tool_profile_id="codex-default",
                provider_session_id="provider-session", canonical_workspace=str(repo), git_head="",
                dirty_paths_digest="", last_output_offset=0, completed_criteria=[],
                side_effect_summary="", reason="waiting_quota",
            ))
            cloud, adapter = FakeCloud(), UnverifiedResumingAdapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → blocked (billing_unverified)")
            self.assertEqual(adapter.calls, [])
            self.assertEqual(db.get_remote_claim("j1")["state"], "reported")
            self.assertEqual(cloud.events[-1]["type"], "waiting_input")

    def test_quota_block_checkpoints_then_resumes_same_session(self):
        class ReleasingCloud(FakeCloud):
            def __init__(self):
                super().__init__()
                self.n = 0

            def claim(self, token):
                self.n += 1
                c = super().claim(token)
                c["attempt_id"] = "a%d" % self.n  # each re-lease is a new attempt
                return c

        class BlockThenResume(Adapter):
            def __init__(self):
                self.calls = []

            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.calls.append(("start", session_id))
                return RunResult(exit_code=1, blocked=True, error="usage limit", session_id="prov-sess")

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                self.calls.append(("resume", session_id))
                return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = ReleasingCloud(), BlockThenResume()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → waiting_quota")
            cp = db.get_checkpoint("j1")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.provider_session_id, "prov-sess")
            # Natural recovery re-leases: the agent resumes the original session.
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            self.assertEqual(adapter.calls[0][0], "start")
            self.assertEqual(adapter.calls[1], ("resume", "prov-sess"))
            self.assertIsNone(db.get_checkpoint("j1"))  # cleared on completion

    def test_reports_quota_from_readable_adapters(self):
        class ReadingAdapter(Adapter):
            def read_limits(self):
                return [Sample(bucket_key="codex:weekly", tool="codex", used_pct=42.0,
                               reset_at=None, window_mins=10080, source="app-server")]

        class BlindAdapter(Adapter):
            def capabilities(self):
                caps = super().capabilities()
                caps["can_read_quota"] = False
                return caps

            def read_limits(self):
                raise AssertionError("must not read quota when incapable")

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": ReadingAdapter(), "claude": BlindAdapter()},
                          Path(d), lambda: "secrettoken")
            n = agent.report_quota(now=1000.0)
            self.assertEqual(n, 1)  # only the readable adapter reports
            token, samples = cloud.quota_posts[0]
            p = samples[0]
            self.assertEqual((p["kind"], p["scope"], p["used_percent"], p["pool_id"]),
                             ("codex", "weekly", 42.0, "pool-codex"))
            self.assertNotIn("secrettoken", json.dumps(p))
            self.assertNotIn("@", json.dumps(p))

    def test_quota_block_reports_exhaustion_sample(self):
        class BlockingAdapter(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=1, blocked=True, error="usage limit", session_id="s",
                                 samples=[Sample(bucket_key="codex:weekly", tool="codex",
                                                 used_pct=100.0, reset_at=None, window_mins=10080)])

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": BlockingAdapter()}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → waiting_quota")
            self.assertEqual(len(cloud.quota_posts), 1)
            self.assertEqual(cloud.quota_posts[0][1][0]["used_percent"], 100.0)

    def test_workspace_busy_defers_without_second_run(self):
        from timetrace.dispatch import workspace_lock

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud, adapter = FakeCloud(), Adapter()
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            # Another process already writing this canonical workspace.
            held = workspace_lock(Path(d), str(repo)).acquire()
            try:
                self.assertEqual(agent.run_once(), "job j1 → deferred (workspace busy)")
                self.assertFalse(hasattr(adapter, "args"))
            finally:
                held.release()


class DefaultBindingTest(unittest.TestCase):
    def test_default_binding_is_authoritative_and_matches_registered_tool_id(self):
        from timetrace.agent import _default_pool_binding
        self.assertEqual(_default_pool_binding("codex"), ("pool-codex", "codex-default", True))
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cloud = FakeCloud()

            class Reading(Adapter):
                def read_limits(self):
                    return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=42.0,
                                   reset_at=2000, window_mins=300, source="live")]

            agent = Agent(db, cloud, {"codex": Reading()}, Path(d), lambda: "t")
            agent.report_quota(now=1000.0)
            payload = cloud.quota_posts[0][1][0]
            self.assertEqual(payload["profile_id"], "codex-default")
            self.assertIs(payload["pool_authoritative"], True)
            self.assertEqual(payload["confidence"], "exact")


class MaintenanceTest(unittest.TestCase):
    class Reading(Adapter):
        def read_limits(self):
            return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=42.0,
                           reset_at=2000, window_mins=300, source="live")]

    def test_maintain_throttles_quota_and_repushes_changed_inventory(self):
        class Cloud(FakeCloud):
            def __init__(self):
                super().__init__()
                self.inventories = []

            def update_inventory(self, token, workspaces, tools):
                self.inventories.append((workspaces, tools))
                return {}

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cloud = Cloud()
            tools = [{"id": "codex-default", "status": "available"}]
            agent = Agent(db, cloud, {"codex": self.Reading()}, Path(d), lambda: "t",
                          inventory=lambda: ([], [dict(t) for t in tools]), quota_interval=300)
            agent.maintain(now=1000.0, force=True)
            agent.maintain(now=1100.0)            # within the interval: nothing
            agent.maintain(now=1400.0)            # interval elapsed: report again
            self.assertEqual(len(cloud.quota_posts), 2)
            self.assertEqual(len(cloud.inventories), 1)  # unchanged inventory is not re-pushed
            tools[0]["status"] = "unavailable"
            agent.maintain(now=1800.0)
            self.assertEqual(len(cloud.inventories), 2)

    def test_successful_run_still_uploads_rate_limit_samples(self):
        class SamplingAdapter(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=0, ok=True, output="done", session_id="s",
                                 samples=[Sample(bucket_key="claude:five_hour", tool="claude", used_pct=55.0,
                                                 reset_at=None, window_mins=300)])

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": SamplingAdapter()}, Path(d), lambda: "t",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            self.assertEqual(agent.run_once(), "job j1 → awaiting_review")
            self.assertEqual(cloud.quota_posts[0][1][0]["used_percent"], 55.0)


class OutputTailTest(unittest.TestCase):
    def test_completed_event_carries_output_tail(self):
        class Writing(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                Path(log_file).write_text("step 1\nstep 2\n")
                return RunResult(exit_code=0, ok=True, output="ok", session_id="s")

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": Writing()}, Path(d), lambda: "t",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            agent.run_once()
            completed = [e for e in cloud.events if e["type"] == "completed"][0]
            self.assertEqual(completed["output_tail"], "step 1\nstep 2\n")

    def test_failed_event_carries_output_tail(self):
        class Failing(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                Path(log_file).write_text("boom\n")
                return RunResult(exit_code=2, ok=False, error="exit 2", session_id="s")

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            cloud = FakeCloud()
            agent = Agent(db, cloud, {"codex": Failing()}, Path(d), lambda: "t",
                          prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
            agent.run_once()
            failed = [e for e in cloud.events if e["type"] == "failed"][0]
            self.assertEqual(failed["output_tail"], "boom\n")


    def run_with(self, adapter, **agent_kwargs):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        db = Database(Path(d) / "timetrace.db")
        repo = Path(d) / "repo"
        init_repo(repo)
        db.upsert_workspace("ws1", "repo", str(repo), "main")
        cloud = FakeCloud()
        agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "t",
                      prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"), **agent_kwargs)
        agent.run_once()
        return cloud, db

    def test_secrets_are_redacted_before_they_are_queued_or_uploaded(self):
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

        class Leaky(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                Path(log_file).write_text("cat .env\nGITHUB_TOKEN=%s\nAPI_KEY=abc123def456\n" % secret)
                return RunResult(exit_code=0, ok=True, output="used token %s" % secret, session_id="s")

        cloud, db = self.run_with(Leaky())
        completed = [e for e in cloud.events if e["type"] == "completed"][0]
        self.assertNotIn(secret, json.dumps(cloud.events))
        self.assertNotIn("abc123def456", completed["output_tail"])
        self.assertIn("REDACTED", completed["output_tail"])
        self.assertIn("REDACTED", completed["result_summary"])
        stored = db.conn.execute("SELECT payload FROM remote_outbox").fetchall()
        self.assertTrue(stored)
        self.assertFalse(any(secret in row[0] for row in stored))

    def test_failure_message_is_redacted(self):
        class Failing(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                return RunResult(exit_code=1, ok=False, error="auth failed for Bearer abcdefghijklmnop0123456789",
                                 session_id="s")

        cloud, _ = self.run_with(Failing())
        failed = [e for e in cloud.events if e["type"] == "failed"][0]
        self.assertNotIn("abcdefghijklmnop0123456789", failed["message"])

    def test_server_supplied_job_id_cannot_steer_the_log_path(self):
        seen = {}

        class Recording(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                seen["log"] = log_file
                return RunResult(exit_code=0, ok=True, output="ok", session_id="s")

        class OddCloud(FakeCloud):
            def claim(self, token):
                claim = super().claim(token)
                claim["job"]["id"] = "../../evil/x"
                return claim

        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        db = Database(Path(d) / "timetrace.db")
        repo = Path(d) / "repo"
        init_repo(repo)
        db.upsert_workspace("ws1", "repo", str(repo), "main")
        agent = Agent(db, OddCloud(), {"codex": Recording()}, Path(d), lambda: "t",
                      prepare_workspace=lambda repo, task_id, home, base: (repo, "timetrace/test"))
        agent.run_once()
        self.assertEqual(Path(seen["log"]).parent, Path(d) / "logs")
        self.assertNotIn("..", Path(seen["log"]).name)

    def test_output_tail_can_be_switched_off(self):
        class Writing(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                Path(log_file).write_text("private build output\n")
                return RunResult(exit_code=0, ok=True, output="ok", session_id="s")

        cloud, db = self.run_with(Writing(), upload_output_tail=False)
        completed = [e for e in cloud.events if e["type"] == "completed"][0]
        self.assertNotIn("output_tail", completed)
        stored = db.conn.execute("SELECT payload FROM remote_outbox").fetchall()
        self.assertFalse(any("private build output" in row[0] for row in stored))

    def test_redacted_tail_stays_inside_the_upload_bound(self):
        class Dense(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                # Short secrets grow when masked; the tail must still fit 8000 bytes.
                Path(log_file).write_text("".join("pw%04d password=abc12%04d\n" % (i, i) for i in range(2000)))
                return RunResult(exit_code=0, ok=True, output="ok", session_id="s")

        cloud, _ = self.run_with(Dense())
        completed = [e for e in cloud.events if e["type"] == "completed"][0]
        self.assertLessEqual(len(completed["output_tail"].encode("utf-8")), 8000)
        self.assertIn("password=[REDACTED]", completed["output_tail"])


class MaintenanceLoggingTest(unittest.TestCase):
    def test_maintain_reports_counts_and_failures_to_the_log(self):
        class Cloud(FakeCloud):
            def update_inventory(self, token, workspaces, tools):
                raise OSError("valley unreachable")

        class Reading(Adapter):
            def read_limits(self):
                return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=42.0, reset_at=2000, window_mins=300)]

        lines = []
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            agent = Agent(db, Cloud(), {"codex": Reading()}, Path(d), lambda: "t",
                          inventory=lambda: ([], [{"id": "codex-default"}]), log=lines.append)
            agent.maintain(now=1000.0, force=True)
        self.assertTrue(any("quota" in l and "codex" in l and "1" in l for l in lines), lines)
        self.assertTrue(any("inventory" in l and "OSError" in l for l in lines), lines)
        self.assertFalse(any("valley unreachable" in l and "t" == l for l in lines))


class AccountPoolTest(unittest.TestCase):
    def test_pool_id_follows_the_tool_account(self):
        class Keyed(Adapter):
            def account_key(self):
                return "ab12cd34"

            def read_limits(self):
                return [Sample(bucket_key="codex:codex:primary", tool="codex", used_pct=5.0, reset_at=2000, window_mins=10080)]

        with tempfile.TemporaryDirectory() as d:
            cloud = FakeCloud()
            Agent(Database(Path(d) / "timetrace.db"), cloud, {"codex": Keyed()}, Path(d), lambda: "t").report_quota(now=1000.0)
            self.assertEqual(cloud.quota_posts[0][1][0]["pool_id"], "pool-codex-ab12cd34")
            self.assertEqual(cloud.quota_posts[0][1][0]["profile_id"], "codex-default")


class RevokedRunnerTest(unittest.TestCase):
    def test_revoked_credentials_are_cleared_and_the_loop_waits_for_repairing(self):
        from timetrace.cloud import CloudError
        cleared, lines = [], []

        class Cloud(FakeCloud):
            def claim(self, token):
                raise CloudError("authentication required", 401, 40100)

        def token():
            raise CloudError("authentication required", 401, 40100)

        with tempfile.TemporaryDirectory() as d:
            agent = Agent(Database(Path(d) / "timetrace.db"), Cloud(), {}, Path(d), token,
                          on_revoked=lambda: cleared.append(True), log=lines.append)
            self.assertEqual(agent.run_once(), "revoked")
        self.assertEqual(cleared, [True])
        self.assertTrue(any("解绑" in l for l in lines), lines)


class RecoveryFenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / "repo"
        init_repo(self.repo)
        self.db = Database(self.home / "timetrace.db")
        self.db.upsert_workspace("ws1", "repo", str(self.repo), "main")
        self.cloud = FakeCloud()
        self.claim = self.cloud.claim("token")
        self.cloud.claim = lambda token: self.claim
        self.calls = []
        calls = self.calls

        class BlockingAdapter(Adapter):
            def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
                calls.append(("start", cwd, session_id))
                Path(cwd, "dirty.txt").write_text("first change")
                Path(log_file).write_text("quota output\n")
                return RunResult(exit_code=1, blocked=True, session_id="native-session", error="quota")

            def resume(self, prompt, cwd, session_id, log_file, cancel_event=None):
                calls.append(("resume", cwd, session_id))
                return RunResult(exit_code=0, ok=True, session_id=session_id)

        self.adapter = BlockingAdapter()
        self.agent = Agent(self.db, self.cloud, {"codex": self.adapter}, self.home, lambda: "token",
                           prepare_workspace=lambda *args: (str(self.repo), "main"), heartbeat_interval=.01)

    def checkpoint(self):
        self.assertEqual(self.agent.run_once(), "job j1 → waiting_quota")
        self.claim["attempt_id"] = "a2"
        self.calls.clear()
        return self.db.get_checkpoint("j1")

    def test_checkpoint_created_before_lock_acquisition_is_reloaded(self):
        from timetrace.dispatch import coding_slot_lock
        self.claim["job"].update(id="j2", plan_id="plan-1")
        other_cloud = FakeCloud()
        other_claim = other_cloud.claim("token")
        other_claim["job"]["plan_id"] = "plan-1"
        other_cloud.claim = lambda token: other_claim
        other = Agent(self.db, other_cloud, {"codex": self.adapter}, self.home, lambda: "token",
                      prepare_workspace=self.agent.prepare_workspace)
        def acquire_after_other(home):
            with patch("timetrace.agent.coding_slot_lock", coding_slot_lock):
                self.assertEqual(other.run_once(), "job j1 → waiting_quota")
            return coding_slot_lock(home)
        with patch("timetrace.agent.coding_slot_lock", acquire_after_other):
            self.assertEqual(self.agent.run_once(), "job j2 → awaiting_review")
        self.assertEqual([call[0] for call in self.calls], ["start", "resume"])

    def test_started_history_created_before_lock_acquisition_is_reloaded(self):
        from timetrace.dispatch import coding_slot_lock
        self.claim["job"]["plan_id"] = "plan-1"
        def acquire_after_start(home):
            self.db.mark_plan_started("plan-1", "other-job", "other-attempt")
            return coding_slot_lock(home)
        with patch("timetrace.agent.coding_slot_lock", acquire_after_start):
            self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_cancelled_resume_exception_deletes_checkpoint_and_reports_cancel(self):
        self.checkpoint()
        started = threading.Event()
        original_renew = self.cloud.renew
        def renew(*args):
            return {"desired_action": "cancel"} if started.is_set() else original_renew(*args)
        self.cloud.renew = renew
        def resume(prompt, cwd, session_id, log_file, cancel_event):
            started.set()
            self.assertTrue(cancel_event.wait(1))
            raise RuntimeError("provider interrupted")
        self.adapter.resume = resume
        self.assertIn("cancelled", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("j1"))
        self.assertEqual(self.cloud.events[-1]["type"], "cancelled")

    def test_adapter_cancel_event_dominates_resume_shutdown_exception(self):
        self.checkpoint()
        def resume(prompt, cwd, session_id, log_file, cancel_event):
            cancel_event.set()
            raise RuntimeError("provider interrupted")
        self.adapter.resume = resume
        self.assertIn("cancelled", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("j1"))
        self.assertEqual(self.cloud.events[-1]["type"], "cancelled")

    def test_resume_exception_reports_terminal_event_after_running(self):
        self.checkpoint()
        self.cloud.events.clear()
        def resume(*args):
            raise RuntimeError("provider crashed")
        self.adapter.resume = resume
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual([(event["seq"], event["type"]) for event in self.cloud.events],
                         [(1, "running"), (2, "waiting_input")])

    def test_malformed_checkpoint_job_identity_requires_input(self):
        cp = self.checkpoint()
        cp.job_id = None
        self.db.save_checkpoint(cp)
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_adapter_cannot_spawn_after_capability_revocation_inside_start(self):
        import sys
        from timetrace.process import run_streaming
        marker = self.home / "spawned"
        self.agent.heartbeat_interval = 2
        def start(prompt, cwd, session_id, log_file, cancel_event):
            self.adapter.capabilities = lambda: {"can_dispatch": True, "can_resume": True,
                                                 "can_enforce_zero_spend": False}
            code, lines = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)],
                                        cwd, log_file, cancel_event=cancel_event)
            return RunResult(exit_code=code, ok=code == 0)
        self.adapter.start = start
        self.assertIn("billing_unverified", self.agent.run_once())
        self.assertFalse(marker.exists())

    def test_resume_revocation_inside_adapter_blocks_actual_process(self):
        import sys
        from timetrace.process import run_streaming
        self.checkpoint()
        marker = self.home / "spawned"
        self.agent.heartbeat_interval = 2
        def resume(prompt, cwd, session_id, log_file, cancel_event):
            self.adapter.capabilities = lambda: {"can_dispatch": True, "can_resume": False,
                                                 "can_enforce_zero_spend": True}
            code, lines = run_streaming([sys.executable, "-c", "from pathlib import Path; Path(%r).touch()" % str(marker)],
                                        cwd, log_file, cancel_event=cancel_event)
            return RunResult(exit_code=code, ok=code == 0)
        self.adapter.resume = resume
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertFalse(marker.exists())
        self.assertIsNotNone(self.db.get_checkpoint("j1"))

    def test_crash_fence_surfaces_manual_clearance_in_agent(self):
        from timetrace.dispatch import coding_slot_lock
        path = Path(coding_slot_lock(self.home).path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        result = self.agent.run_once()
        self.assertIn("manual", result)
        self.assertIn(str(path), result)
        self.assertEqual(path.read_text(), "1234")

    def test_daemon_prints_crash_diagnostic_once_across_repeated_claims(self):
        from types import SimpleNamespace
        from timetrace import cli
        from timetrace.dispatch import coding_slot_lock
        path = Path(coding_slot_lock(self.home).path)
        path.parent.mkdir(parents=True)
        path.write_text("1234")
        claims = []
        def claim(token):
            if len(claims) == 3:
                raise KeyboardInterrupt()
            claims.append(True)
            self.claim["job"]["id"] = "job-%d" % len(claims)
            return self.claim
        self.cloud.claim = claim
        self.cloud.update_inventory = lambda *args: None
        class Output(io.StringIO):
            flushed = False

            def flush(self):
                self.flushed = True
                super().flush()
        output = Output()
        with redirect_stdout(output), patch("timetrace.agent.time.sleep"), \
                patch("timetrace.cli._open", return_value=(self.home, {"max_parallel": 1}, self.db)), \
                patch("timetrace.cli._cloud", return_value=self.cloud), \
                patch("timetrace.cli._adapters", return_value={"codex": self.adapter}), \
                patch("timetrace.cli._acquire_execution_lock", return_value=object()), \
                patch("timetrace.cli.SessionManager", return_value=SimpleNamespace(token=lambda: "token")), \
                self.assertRaises(KeyboardInterrupt):
            cli.cmd_agent_run(SimpleNamespace(once=False, interval=5))
        self.assertEqual(output.getvalue().count("manual recovery required"), 1)
        self.assertIn(str(path), output.getvalue())
        self.assertIn("writer descendants", output.getvalue())
        self.assertTrue(output.flushed, "daemon output must reach a redirected log immediately")
        self.assertEqual(path.read_text(), "1234")

    def test_invalid_embedded_plan_identity_preserves_stored_evidence(self):
        cp = self.checkpoint()
        for index, identity in enumerate(([], None, "", "other-plan")):
            with self.subTest(identity=identity):
                row = cp.to_row()
                row["plan_id"] = identity
                evidence = json.dumps(row)
                self.db.conn.execute("UPDATE checkpoint SET data=? WHERE plan_id=?", (evidence, "j1"))
                self.claim["attempt_id"] = "invalid-%d" % index
                self.assertIn("waiting_input", self.agent.run_once())
                self.assertEqual(self.calls, [])
                stored = self.db.conn.execute("SELECT data FROM checkpoint WHERE plan_id=?", ("j1",)).fetchone()
                self.assertIsNotNone(stored)
                self.assertEqual(stored["data"], evidence)
                self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0], 1)
                self.assertEqual(self.cloud.events[-1]["type"], "waiting_input")

    def test_preparation_crossing_lease_deadline_never_spawns(self):
        clock = [1000.0]
        self.claim["lease_expires_at"] = datetime.fromtimestamp(1001, timezone.utc).isoformat()
        def prepare(*args):
            clock[0] = 1002
            return str(self.repo), "main"
        self.agent.prepare_workspace = prepare
        with patch("timetrace.agent.time.time", side_effect=lambda: clock[0]):
            self.assertIn("lease", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.cloud.events[-1]["type"], "waiting_input")

    def test_renew_failure_after_preparation_never_spawns(self):
        def fail(*args):
            raise OSError("network lost")
        self.cloud.renew = fail
        self.assertIn("lease", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_resume_capability_change_during_preflight_renew_never_spawns(self):
        self.checkpoint()
        original = self.cloud.renew
        def renew(*args):
            self.adapter.capabilities = lambda: {"can_dispatch": True, "can_resume": False,
                                                 "can_enforce_zero_spend": True}
            return original(*args)
        self.cloud.renew = renew
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.db.get_checkpoint("j1"))

    def test_resume_occurrence_excludes_preflight_network_wait(self):
        self.checkpoint()
        current = [datetime.now(timezone.utc)]
        entered = []
        original = self.cloud.renew
        def renew(*args):
            current[0] += timedelta(seconds=11)
            return original(*args)
        def resume(*args):
            entered.append(current[0])
            current[0] += timedelta(seconds=2)
            return RunResult(exit_code=0, ok=True)
        self.cloud.renew, self.adapter.resume = renew, resume
        with patch("timetrace.agent.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda zone: current[0]
            self.assertIn("awaiting_review", self.agent.run_once())
        start, end = [datetime.fromisoformat(e["observed_at"].replace("Z", "+00:00")) for e in self.cloud.events[-2:]]
        self.assertEqual(start, entered[0])
        self.assertEqual((end-start).total_seconds(), 2)

    def test_cancellation_during_preflight_renew_never_spawns(self):
        self.checkpoint()
        self.cloud.renew = lambda *args: {"desired_action": "cancel"}
        self.assertIn("cancel", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_duplicate_cancelled_claim_invalidates_checkpoint(self):
        self.checkpoint()
        self.db.save_remote_claim(self.claim, state="running")
        self.claim["job"]["desired_action"] = "cancel"
        self.assertIn("cancel", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_preparation_renews_lease_and_retains_failure_until_it_finishes(self):
        preparing = threading.Event()
        renewed = threading.Event()
        def prepare(*args):
            preparing.set()
            self.assertTrue(renewed.wait(2))
            return str(self.repo), "main"
        def renew(*args):
            self.assertTrue(preparing.is_set())
            renewed.set()
            raise OSError("renew denied")
        self.agent.prepare_workspace = prepare
        self.cloud.renew = renew
        self.assertIn("lease renewal failed", self.agent.run_once())
        self.assertTrue(renewed.is_set())
        self.assertEqual(self.calls, [])

    def test_resume_preparation_crossing_deadline_never_spawns(self):
        self.checkpoint()
        clock = [1000.0]
        self.claim["lease_expires_at"] = datetime.fromtimestamp(1001, timezone.utc).isoformat()
        def prepare(*args):
            clock[0] = 1002
            return str(self.repo), "main"
        self.agent.prepare_workspace = prepare
        with patch("timetrace.agent.time.time", side_effect=lambda: clock[0]):
            self.assertIn("lease", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.db.get_checkpoint("j1"))

    def test_executor_delay_cannot_start_after_lease_deadline(self):
        from concurrent.futures import ThreadPoolExecutor
        real_submit = ThreadPoolExecutor.submit
        clock = [1000.0]
        self.claim["lease_expires_at"] = datetime.fromtimestamp(1001, timezone.utc).isoformat()
        self.cloud.renew = lambda *args: {"lease_expires_at": datetime.fromtimestamp(1001, timezone.utc).isoformat()}
        submissions = []
        def delayed_submit(pool, fn, *args, **kwargs):
            submissions.append(fn)
            if len(submissions) == 2:
                def late():
                    clock[0] = 1002
                    return fn(*args, **kwargs)
                return real_submit(pool, late)
            return real_submit(pool, fn, *args, **kwargs)
        with patch("timetrace.agent.time.time", side_effect=lambda: clock[0]), patch.object(ThreadPoolExecutor, "submit", delayed_submit):
            self.assertIn("lease", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_missing_native_session_is_not_replaced_by_generated_uuid(self):
        self.adapter.start = lambda *args: RunResult(exit_code=1, blocked=True, session_id=None)
        cp = self.checkpoint()
        self.assertFalse(cp.provider_session_id)
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_malformed_checkpoint_requires_input_and_preserves_evidence(self):
        cp = self.checkpoint()
        cp.provider_session_id = 42
        self.db.save_checkpoint(cp)
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(self.db.get_checkpoint("j1"))

    def test_result_returned_after_lease_expiry_cannot_create_checkpoint(self):
        clock = [1000.0]
        self.claim["lease_expires_at"] = datetime.fromtimestamp(1001, timezone.utc).isoformat()
        self.cloud.renew = lambda *args: {"lease_expires_at": datetime.fromtimestamp(1001, timezone.utc).isoformat()}
        def expired(*args):
            clock[0] = 1002
            return RunResult(exit_code=1, blocked=True, session_id="native")
        self.adapter.start = expired
        with patch("timetrace.agent.time.time", side_effect=lambda: clock[0]):
            self.assertIn("fenced", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_hung_renewal_cannot_extend_writer_past_deadline(self):
        expires = datetime.fromtimestamp(time.time() + .15, timezone.utc).isoformat()
        self.claim["lease_expires_at"] = expires
        renewals = []
        release = threading.Event()
        self.addCleanup(release.set)
        def renew(*args):
            renewals.append(True)
            if len(renewals) > 1:
                release.wait(.8)
            return {"lease_expires_at": expires}
        self.cloud.renew = renew
        stopped = []
        def writer(prompt, cwd, session_id, log_file, cancel_event):
            stopped.append(cancel_event.wait(1))
            return RunResult(exit_code=143, error="cancelled")
        self.adapter.start = writer
        started = time.monotonic()
        self.assertIn("fenced", self.agent.run_once())
        self.assertLess(time.monotonic() - started, .45)
        self.assertEqual(stopped, [True])

    def test_cancellation_after_preparation_invalidates_checkpoint(self):
        self.checkpoint()
        self.cloud.renew = lambda *args: {"desired_action": "cancel"}
        self.assertIn("cancel", self.agent.run_once())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_initial_cancellation_invalidates_checkpoint(self):
        self.checkpoint()
        self.claim["job"]["desired_action"] = "cancel"
        self.assertIn("cancel", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_capability_revocation_during_preparation_never_spawns(self):
        def prepare(*args):
            self.adapter.capabilities = lambda: {"can_enforce_zero_spend": False}
            return str(self.repo), "main"
        self.agent.prepare_workspace = prepare
        self.assertIn("billing_unverified", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_checkpoint_records_actual_worktree_state_and_output(self):
        actual = self.home / "execution"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", "execution", str(actual)], check=True)
        self.agent.prepare_workspace = lambda *args: (str(actual), "execution")
        cp = self.checkpoint()
        self.assertEqual(cp.execution_path, str(actual.resolve()))
        self.assertEqual(cp.provider, "codex")
        self.assertEqual(cp.git_head, subprocess.check_output(["git", "-C", str(actual), "rev-parse", "HEAD"], text=True).strip())
        self.assertTrue(cp.dirty_paths_digest)
        self.assertEqual(cp.last_output_offset, 13)
        self.assertEqual(Path(cp.output_path).read_text(), "quota output\n")
        self.assertEqual(self.agent.run_once(), "job j1 → awaiting_review")
        self.assertEqual(self.calls, [("resume", str(actual.resolve()), "native-session")])

    def test_tampered_worktree_metadata_blocks_checkpoint_without_running_git(self):
        # A model that rewrote config.worktree (core.fsmonitor) must not get
        # the unsandboxed runner to execute git in that worktree.
        actual = self.home / "execution"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", "execution", str(actual)], check=True)
        admin = Path(subprocess.check_output(["git", "-C", str(actual), "rev-parse", "--absolute-git-dir"], text=True).strip())
        marker = self.home / "fsmonitor-ran"
        original_start = self.adapter.start

        def tampering_start(prompt, cwd, session_id, log_file, cancel_event=None):
            (admin / "config.worktree").write_text("[core]\n\tfsmonitor = touch %s; false\n" % marker)
            return original_start(prompt, cwd, session_id, log_file, cancel_event)

        self.adapter.start = tampering_start
        self.agent.prepare_workspace = lambda *args: (str(actual), "execution")
        outcome = self.agent.run_once()
        self.assertIn("checkpoint capture failed", outcome)
        self.assertFalse(marker.exists())
        self.assertIsNone(self.db.get_checkpoint("j1"))

    def test_invalid_checkpoint_never_falls_back_to_start(self):
        for change in ("session", "provider", "profile", "resume", "path", "dirty", "head", "output", "legacy"):
            with self.subTest(change=change):
                # Each case has an independently generated real checkpoint.
                case = RecoveryFenceTest()
                case.setUp()
                try:
                    cp = case.checkpoint()
                    if change == "session": cp.provider_session_id = ""
                    elif change == "provider": cp.provider = "claude"
                    elif change == "profile": cp.tool_profile_id = "different-profile"
                    elif change == "resume": case.adapter.capabilities = lambda: {"can_enforce_zero_spend": True, "can_dispatch": True, "can_resume": False}
                    elif change == "path": cp.execution_path = str(case.home)
                    elif change == "dirty": Path(case.repo, "dirty.txt").write_text("other change")
                    elif change == "head":
                        subprocess.run(["git", "-C", str(case.repo), "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-qm", "changed"], check=True)
                    elif change == "output": Path(case.home, "logs", "remote-j1.log").write_text("")
                    elif change == "legacy": cp.schema_version = 1
                    case.db.save_checkpoint(cp)
                    self.assertIn("waiting_input", case.agent.run_once())
                    self.assertEqual(case.calls, [])
                    self.assertIsNotNone(case.db.get_checkpoint("j1"))
                    self.assertIn("checkpoint", case.cloud.events[-1]["message"])
                finally:
                    case.doCleanups()

    def test_started_attempt_without_checkpoint_requires_input(self):
        self.db.save_remote_claim(self.claim, state="running")
        self.claim["attempt_id"] = "a2"
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_new_job_for_started_plan_without_checkpoint_requires_input(self):
        self.claim["job"]["plan_id"] = "plan-1"
        self.assertEqual(self.agent.run_once(), "job j1 → waiting_quota")
        self.db.delete_checkpoint("plan-1")
        self.claim["job"]["id"] = "j2"
        self.claim["attempt_id"] = "a2"
        self.calls.clear()
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])

    def test_cancelling_plan_cannot_be_bypassed_with_new_job_id(self):
        self.claim["job"]["plan_id"] = "plan-1"
        self.assertEqual(self.agent.run_once(), "job j1 → waiting_quota")
        self.claim["job"]["desired_action"] = "cancel"
        self.claim["attempt_id"] = "a2"
        self.assertIn("cancel", self.agent.run_once())
        self.assertIsNone(self.db.get_checkpoint("plan-1"))
        del self.claim["job"]["desired_action"]
        self.claim["job"]["id"] = "j2"
        self.claim["attempt_id"] = "a3"
        self.calls.clear()
        self.assertIn("waiting_input", self.agent.run_once())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
