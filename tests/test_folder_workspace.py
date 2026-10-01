"""Folder (non-git) workspaces: detection, output directory, change check."""
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from timetrace import cli, folder, results
from timetrace.adapters import claude, codex
from timetrace.adapters.base import FOLDER_RULES, SAFETY_RULES
from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.models import RunResult


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=path, check=True)


def overrides(cmd):
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == "-c"]


class KindTest(unittest.TestCase):
    def test_detects_git_and_folder(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"; init_repo(repo)
            footage = Path(d) / "footage"; footage.mkdir()
            self.assertEqual(folder.detect_kind(str(repo)), "git")
            self.assertEqual(folder.detect_kind(str(footage)), "folder")

    def test_add_workspace_auto_detects_and_can_be_forced(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            footage = Path(d) / "footage"; footage.mkdir()
            self.assertEqual(cli.add_workspace(db, str(repo))["kind"], "git")
            added = cli.add_workspace(db, str(footage))
            self.assertEqual(added["kind"], "folder")
            self.assertEqual(db.get_workspace(added["id"])["kind"], "folder")
            self.assertEqual(db.get_workspace(added["id"])["default_branch"], "")
            plain = Path(d) / "plain"; plain.mkdir()
            forced = cli.add_workspace(db, str(plain), workspace_id="as-folder", kind="folder")
            self.assertEqual(db.get_workspace(forced["id"])["kind"], "folder")
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(footage), kind="git")
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(Path(d) / "missing"))
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(footage), kind="svn")

    def test_home_directory_is_never_a_folder_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(Path.home()), kind="folder")
            with self.assertRaises(ValueError):
                cli.add_workspace(db, "/", kind="folder")

    def test_ancestors_of_home_and_timetrace_home_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(Path.home().parent), kind="folder")
            timetrace_home = Path(d) / "a" / "b" / ".timetrace"; timetrace_home.mkdir(parents=True)
            with unittest.mock.patch.dict(os.environ, {"TIMETRACE_HOME": str(timetrace_home)}):
                for path in (Path(d) / "a", Path(d) / "a" / "b", timetrace_home, timetrace_home / "x"):
                    path.mkdir(exist_ok=True)
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        cli.add_workspace(db, str(path), kind="folder")

    def test_folders_holding_repositories_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            with self.assertRaises(ValueError):
                cli.add_workspace(db, str(repo), kind="folder")
            for rel in ("child", "grand/child"):
                parent = Path(d) / ("holder-" + rel.replace("/", "-"))
                init_repo_dir = parent / rel
                init_repo_dir.parent.mkdir(parents=True)
                init_repo(init_repo_dir)
                with self.subTest(rel=rel), self.assertRaises(ValueError) as caught:
                    cli.add_workspace(db, str(parent), kind="folder")
                self.assertIn(".git", str(caught.exception))
            self.assertEqual(db.list_workspaces(), [])

    def test_overlapping_workspaces_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            root = Path(d) / "root"; root.mkdir()
            repo = root / "proj"; init_repo(repo)
            (repo / "assets").mkdir()
            cli.add_workspace(db, str(repo))
            with self.assertRaises(ValueError):   # inside a registered repository
                cli.add_workspace(db, str(repo / "assets"), kind="folder")
            footage = Path(d) / "footage"; footage.mkdir()
            (footage / "clips").mkdir()
            cli.add_workspace(db, str(footage), workspace_id="f1")
            with self.assertRaises(ValueError):   # inside a registered folder
                cli.add_workspace(db, str(footage / "clips"), kind="folder")
            with self.assertRaises(ValueError):   # a registered folder inside it
                cli.add_workspace(db, str(Path(d)), kind="folder")
            # Registering the same path again only updates it.
            self.assertEqual(cli.add_workspace(db, str(footage), workspace_id="f1", name="new")["id"], "f1")
            nested = footage / "code"; init_repo(nested)
            with self.assertRaises(ValueError):   # a repository inside a registered folder
                cli.add_workspace(db, str(nested))

    def test_workspace_add_command_takes_kind(self):
        with tempfile.TemporaryDirectory() as d:
            footage = Path(d) / "footage"; footage.mkdir()
            with unittest.mock.patch.dict(os.environ, {"TIMETRACE_HOME": str(Path(d) / "home")}), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["workspace", "add", str(footage), "--id", "f1"]), 0)
                self.assertEqual(cli.main(["workspace", "add", str(footage), "--id", "f2", "--kind", "git"]), 2)
                db = Database(Path(d) / "home" / "timetrace.db")
                self.assertEqual(db.get_workspace("f1")["kind"], "folder")
                self.assertIsNone(db.get_workspace("f2"))

    def test_existing_rows_migrate_to_git(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "timetrace.db"
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE remote_workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
                         " path TEXT NOT NULL UNIQUE, default_branch TEXT NOT NULL, updated_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO remote_workspace VALUES ('w','n','/x','main',1)")
            conn.commit(); conn.close()
            db = Database(path)
            self.assertEqual(db.get_workspace("w")["kind"], "git")
            Database(path)  # the migration is idempotent

    def test_inventory_carries_kind_and_max_parallel(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            footage = Path(d) / "footage"; footage.mkdir()
            cli.add_workspace(db, str(footage), workspace_id="f1")
            inventory=cli._runner_workspaces(db)
            self.assertEqual(inventory[0].pop("context")["content_state"],"empty")
            self.assertEqual(inventory,
                             [{"id": "f1", "name": "footage", "default_branch": "", "kind": "folder",
                               "checks": []}])
            self.assertEqual(cli._max_parallel({"max_parallel": 3}), 3)
            # Not set (absent / 0): the sum of the per-tool limits (default 2 + 2).
            self.assertEqual(cli._max_parallel({}), 4)
            self.assertEqual(cli._max_parallel({"max_parallel": 0}), 4)
            self.assertEqual(cli._max_parallel({"max_parallel": "x"}), 1)
            from timetrace import config
            self.assertEqual(config.DEFAULTS["max_parallel"], 0)
            self.assertEqual(cli._max_parallel({"max_parallel": 99}), 8)

    def test_update_inventory_sends_max_parallel(self):
        from timetrace.cloud import CloudClient
        sent = []
        client = CloudClient("https://example.invalid")
        client.request = lambda method, path, body=None, token=None: sent.append(body) or {}
        client.update_inventory("t", [], [], 2)
        client.update_inventory("t", [], [])
        self.assertEqual(sent, [{"workspaces": [], "tools": [], "max_parallel": 2, "workflow_inputs_version": 1},
                                {"workspaces": [], "tools": [], "workflow_inputs_version": 1}])


class OutputDirTest(unittest.TestCase):
    def test_output_name(self):
        self.assertEqual(folder.output_name({"output_name": "cut-v2.final"}, "plan-1"), "cut-v2.final")
        for bad in ("../x", "..", ".", "a/b", "", "x" * 81, 7, None, "名字"):
            with self.subTest(bad=bad):
                self.assertEqual(folder.output_name({"output_name": bad}, "plan-1"), "plan-1")
        self.assertEqual(folder.output_name({}, "p/../1"), "p_.._1")
        self.assertEqual(folder.output_name({}, ".."), "job")

    def test_prepare_creates_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; ws.mkdir()
            out = folder.prepare_output(str(ws), "p1")
            self.assertEqual(out, str((ws / "timetrace-out" / "p1").resolve()))
            self.assertTrue(Path(out).is_dir())
            self.assertEqual(folder.prepare_output(str(ws), "p1"), out)
            elsewhere = Path(d) / "elsewhere"; elsewhere.mkdir()
            (ws / "timetrace-out" / "p2").symlink_to(elsewhere)
            with self.assertRaises(folder.OutputUnsafe):
                folder.prepare_output(str(ws), "p2")
            ws2 = Path(d) / "ws2"; ws2.mkdir()
            (ws2 / "timetrace-out").symlink_to(elsewhere)
            with self.assertRaises(folder.OutputUnsafe):
                folder.prepare_output(str(ws2), "p1")


class SnapshotTest(unittest.TestCase):
    def test_detects_changes_outside_output_only(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "clips").mkdir()
            (ws / "clips" / "a.mov").write_bytes(b"a" * 10)
            (ws / "notes.txt").write_text("n")
            out = Path(folder.prepare_output(str(ws), "p1"))
            before = folder.snapshot(str(ws))
            (out / "edit.txt").write_text("output is fine")
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws))), [])
            (ws / "clips" / "a.mov").write_bytes(b"b" * 11)
            (ws / "notes.txt").unlink()
            (ws / "new.txt").write_text("x")
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws))),
                             ["clips/a.mov", "new.txt", "notes.txt"])

    def test_other_output_dirs_are_covered_own_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            own = Path(folder.prepare_output(str(ws), "p1"))
            other = Path(folder.prepare_output(str(ws), "p0"))
            (other / "cut.mp4").write_bytes(b"earlier result")
            before = folder.snapshot(str(ws), own="p1")
            (own / "new.txt").write_text("fine")
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws), own="p1")), [])
            (other / "cut.mp4").write_bytes(b"clobbered by this run")
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws), own="p1")), ["timetrace-out/p0/cut.mp4"])

    def test_symlink_retargeting_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "link").symlink_to("/tmp")
            before = folder.snapshot(str(ws))
            (ws / "link").unlink()
            (ws / "link").symlink_to("/etc")
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws))), ["link"])

    def test_same_size_edit_with_restored_mtime_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            target = ws / "a.mov"
            target.write_bytes(b"aaaa")
            st = target.stat()
            before = folder.snapshot(str(ws))
            time.sleep(.01)
            target.write_bytes(b"bbbb")
            os.utime(str(target), ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertEqual(folder.changes(before, folder.snapshot(str(ws))), ["a.mov"])

    def test_limit_falls_back_to_two_levels(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            deep = ws / "a" / "b" / "c"
            deep.mkdir(parents=True)
            for i in range(5):
                (deep / ("f%d" % i)).write_text("x")
            snap = folder.snapshot(str(ws), limit=4)
            self.assertTrue(snap.capped)
            self.assertEqual(sorted(snap.entries), ["a", "a/b"])
            self.assertFalse(folder.snapshot(str(ws)).capped)

    def test_message_names_at_most_twenty(self):
        message = folder.change_message(["f%02d" % i for i in range(25)])
        self.assertIn("25", message)
        self.assertIn("f19", message)
        self.assertNotIn("f20", message)

    def test_listing_skips_runner_dir_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / ".timetrace" / "out").mkdir(parents=True)
            (out / ".timetrace" / "out" / "result.json").write_text("{}")
            (out / "cut.mp4").write_bytes(b"x" * 3)
            (out / "sub").mkdir()
            (out / "sub" / "b.txt").write_text("b")
            self.assertEqual(folder.listing(str(out)), "cut.mp4\t3\nsub/b.txt\t1\n")
            for i in range(3000):
                (out / ("file-with-a-long-name-%04d.txt" % i)).write_text("")
            text = folder.listing(str(out))
            self.assertLessEqual(len(text.encode("utf-8")), folder.LISTING_BYTES)
            self.assertIn("共 3002 个文件", text)


class FolderCommandTest(unittest.TestCase):
    def test_codex_writes_only_the_output_dir(self):
        cfg = {"bin": "codex", "sandbox": "danger-full-access", "model": "m", "extra_args": ["--yolo"]}
        cmd = codex.build_folder_cmd(cfg, "cut it", "/ws/timetrace-out/p1", last_msg_file="/l.md")
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertEqual(cmd[cmd.index("-C") + 1], "/ws/timetrace-out/p1")
        self.assertIn('sandbox_mode="workspace-write"', overrides(cmd))
        self.assertIn("sandbox_workspace_write.network_access=false", overrides(cmd))
        self.assertIn('sandbox_workspace_write.writable_roots=["/ws/timetrace-out/p1"]', overrides(cmd))
        self.assertNotIn("--yolo", cmd)
        self.assertTrue(cmd[-1].startswith(FOLDER_RULES))
        resumed = codex.build_folder_cmd(cfg, "go on", "/ws/timetrace-out/p1", resume="t1")
        self.assertEqual(resumed[2:4], ["resume", "t1"])
        self.assertNotIn("-s", resumed)
        self.assertIn('sandbox_mode="workspace-write"', overrides(resumed))
        self.assertIn('sandbox_workspace_write.writable_roots=["/ws/timetrace-out/p1"]', overrides(resumed))

    def test_claude_reads_workspace_and_cannot_delete_or_move(self):
        cfg = {"bin": "claude", "permission_mode": "bypassPermissions", "model": "sonnet",
               "allowed_tools": ["Bash(git add:*)"], "extra_args": ["--dangerously-skip-permissions"]}
        cmd = claude.build_folder_cmd(cfg, "-cut it", "/ws", session_id="s1")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/ws")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "s1")
        # Restricted mode ignores every settings file; nothing re-adds them.
        self.assertNotIn("--setting-sources", cmd)
        denied = cmd[cmd.index("--disallowedTools") + 1:]
        denied = denied[:next(i for i, part in enumerate(denied) if part.startswith("--"))]
        # No shell at all in a folder run: deny rules win over any user
        # `permissions.allow` entry for Bash.
        self.assertIn("Bash", denied)
        # --restricted: user settings (allow rules) are ignored and file tools
        # are confined to cwd + --add-dir; --strict-mcp-config: no MCP servers.
        self.assertIn("--restricted", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--tools", cmd)
        for flag in ("--allowedTools", "bypassPermissions", "--dangerously-skip-permissions"):
            self.assertNotIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], FOLDER_RULES)
        self.assertEqual(cmd[-1], " -cut it")
        resumed = claude.build_folder_cmd(cfg, "go", "/ws", resume="s1")
        self.assertEqual(resumed[resumed.index("--resume") + 1], "s1")
        self.assertIn("--restricted", resumed)
        self.assertIn("--strict-mcp-config", resumed)

    def test_folder_rules_forbid_writes_outside(self):
        self.assertIn("current working directory", FOLDER_RULES)
        self.assertIn(".timetrace/out/result.json", FOLDER_RULES)
        self.assertNotEqual(FOLDER_RULES, SAFETY_RULES)


class Cloud:
    def __init__(self, job=None):
        self.events = []
        self.job = {"id": "j1", "workspace_id": "f1", "tool_profile_id": "codex-default",
                    "provider": "codex", "prompt": "make a cut", "plan_id": "plan-1"}
        self.job.update(job or {})

    def claim(self, token):
        return {"job": dict(self.job), "attempt_id": "a1", "lease_epoch": 1,
                "lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def post_quota_samples(self, token, samples):
        return {}


class FolderAdapter:
    def __init__(self, work=None, result=None):
        self.calls = []
        self.work = work
        self.result = result

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def start(self, *args, **kwargs):
        raise AssertionError("a folder job never runs the git task command")

    def start_folder(self, prompt, cwd, workspace, session_id, log_file, cancel_event=None):
        self.calls.append(("start", cwd, workspace))
        assert (Path(cwd) / ".timetrace" / "out").is_dir()
        (Path(cwd) / "cut.txt").write_text("edit decision list")
        if self.work:
            self.work(Path(cwd), Path(workspace))
        return self.result or RunResult(exit_code=0, ok=True, output="done", session_id=session_id)

    def resume_folder(self, prompt, cwd, workspace, session_id, log_file, cancel_event=None):
        self.calls.append(("resume", cwd, workspace))
        return RunResult(exit_code=0, ok=True, output="done", session_id=session_id)

    def chat(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append(("chat", cwd))
        return RunResult(exit_code=0, ok=True, output="read-only answer", session_id="s")


class FolderAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.ws = (self.d / "footage"); self.ws.mkdir()
        self.ws = self.ws.resolve()
        (self.ws / "a.mov").write_bytes(b"raw")
        self.db = Database(self.d / "timetrace.db")
        self.db.upsert_workspace("f1", "footage", str(self.ws), "", kind="folder")

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, adapter, job=None):
        cloud = Cloud(job)
        agent = Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "token")
        return agent.run_once(), cloud

    def test_runs_in_output_dir_without_worktree_and_registers_folder_artifact(self):
        adapter = FolderAdapter()
        outcome, cloud = self.run_job(adapter, {"output_name": "cut-1"})
        self.assertEqual(outcome, "job j1 → awaiting_review")
        out = self.ws / "timetrace-out" / "cut-1"
        self.assertEqual(adapter.calls, [("start", str(out), str(self.ws))])
        self.assertFalse((self.d / "home" / "worktrees").exists() and any((self.d / "home" / "worktrees").iterdir()))
        event = cloud.events[-1]
        self.assertEqual(event["type"], "completed")
        self.assertEqual(event["artifacts"], [{"kind": "folder", "ref": "timetrace-out/cut-1",
                                               "content": "cut.txt\t18\n"}])

    def test_output_dir_defaults_to_plan_id(self):
        adapter = FolderAdapter()
        self.run_job(adapter, {"output_name": "../escape"})
        self.assertEqual(adapter.calls[0][1], str(self.ws / "timetrace-out" / "plan-1"))

    def test_result_json_artifacts_are_merged(self):
        def work(out, ws):
            (out / "notes.md").write_text("# notes\n")
            (out / ".timetrace" / "out" / "result.json").write_text(json.dumps(
                {"artifacts": [{"kind": "doc", "ref": "notes.md"}]}))
        outcome, cloud = self.run_job(FolderAdapter(work))
        artifacts = cloud.events[-1]["artifacts"]
        self.assertEqual(artifacts[0]["kind"], "folder")
        self.assertEqual(artifacts[0]["content"], "cut.txt\t18\nnotes.md\t8\n")
        self.assertEqual(artifacts[1], {"kind": "doc", "ref": "notes.md", "content": "# notes\n"})

    def test_stale_result_json_in_a_reused_output_dir_is_not_reported(self):
        stale = self.ws / "timetrace-out" / "plan-1" / ".timetrace" / "out"
        stale.mkdir(parents=True)
        (stale / "result.json").write_text(json.dumps({"artifacts": [{"kind": "note", "ref": "old run"}],
                                                       "pipeline_draft": {"old": 1}}))
        outcome, cloud = self.run_job(FolderAdapter())
        self.assertEqual(outcome, "job j1 → awaiting_review")
        event = cloud.events[-1]
        self.assertEqual([a["kind"] for a in event["artifacts"]], ["folder"])
        self.assertNotIn("result", event)

    def test_clobbering_another_jobs_output_fails_the_job(self):
        earlier = Path(folder.prepare_output(str(self.ws), "earlier"))
        (earlier / "cut.mp4").write_bytes(b"earlier result")

        def work(out, ws):
            (ws / "timetrace-out" / "earlier" / "cut.mp4").write_bytes(b"overwritten")
        outcome, cloud = self.run_job(FolderAdapter(work))
        self.assertEqual(outcome, "job j1 → failed")
        self.assertIn("timetrace-out/earlier/cut.mp4", cloud.events[-1]["message"])

    def test_changing_source_material_fails_the_job(self):
        def work(out, ws):
            (ws / "a.mov").write_bytes(b"overwritten")
            (ws / "stray.txt").write_text("x")
        outcome, cloud = self.run_job(FolderAdapter(work))
        self.assertEqual(outcome, "job j1 → failed")
        event = cloud.events[-1]
        self.assertEqual(event["type"], "failed")
        self.assertIn("a.mov", event["message"])
        self.assertIn("stray.txt", event["message"])
        self.assertNotIn("artifacts", event)

    def test_changes_are_reported_when_the_run_is_stopped(self):
        holder = {}

        def work(out, ws):
            (ws / "a.mov").write_bytes(b"overwritten")
            holder["agent"].stop()  # SIGTERM arrives while the tool runs
        cloud = Cloud()
        adapter = FolderAdapter(work)
        agent = holder["agent"] = Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "token")
        outcome = agent.run_once()
        self.assertEqual(outcome, "job j1 → blocked (runner_stopped)")
        event = cloud.events[-1]
        self.assertIn("runner_stopped", event["message"])
        self.assertIn("a.mov", event["message"])

    def test_changes_are_reported_when_the_job_is_cancelled(self):
        def work(out, ws):
            (ws / "stray.txt").write_text("x")
        class Cancelling(FolderAdapter):
            def start_folder(self, prompt, cwd, workspace, session_id, log_file, cancel_event=None):
                result = super().start_folder(prompt, cwd, workspace, session_id, log_file, cancel_event)
                cancel_event.set()
                return result
        outcome, cloud = self.run_job(Cancelling(work))
        self.assertEqual(outcome, "job j1 → cancelled")
        self.assertEqual(cloud.events[-1]["type"], "cancelled")
        self.assertIn("stray.txt", cloud.events[-1]["message"])

    def test_changes_are_reported_when_the_adapter_crashes(self):
        def work(out, ws):
            (ws / "a.mov").write_bytes(b"overwritten")
            raise RuntimeError("tool crashed")
        outcome, cloud = self.run_job(FolderAdapter(work))
        self.assertEqual(outcome, "job j1 → failed")
        event = cloud.events[-1]
        self.assertEqual(event["type"], "failed")
        self.assertIn("tool crashed", event["message"])
        self.assertIn("a.mov", event["message"])

    def test_changes_are_logged_when_the_run_is_fenced(self):
        def work(out, ws):
            (ws / "a.mov").write_bytes(b"overwritten")
        cloud = Cloud()
        lines = []
        agent = Agent(self.db, cloud, {"codex": FolderAdapter(work)}, self.d / "home", lambda: "token",
                      log=lines.append)
        gate = agent._gate
        calls = []

        def expiring(adapter, job, deadline):
            # The lease is lost while the tool runs (checked right after it).
            calls.append(1)
            return "lease_expired" if getattr(agent, "_spawned_once", False) else gate(adapter, job, deadline)
        agent._gate = expiring
        original = FolderAdapter.start_folder

        def start_folder(adapter_self, *args, **kwargs):
            result = original(adapter_self, *args, **kwargs)
            agent._spawned_once = True
            return result
        with unittest.mock.patch.object(FolderAdapter, "start_folder", start_folder):
            outcome = agent.run_once()
        self.assertTrue(outcome.startswith("job j1 → fenced (lease_expired)"), outcome)
        self.assertIn("a.mov", outcome)
        self.assertTrue(any("a.mov" in line for line in lines), lines)

    def test_replaced_output_dir_fails_the_job(self):
        def work(out, ws):
            import shutil
            shutil.rmtree(str(out))
            out.symlink_to(ws)
        outcome, cloud = self.run_job(FolderAdapter(work))
        self.assertEqual(outcome, "job j1 → failed")
        self.assertEqual(cloud.events[-1]["type"], "failed")

    def test_quota_block_checkpoints_output_dir_and_resumes(self):
        adapter = FolderAdapter(result=RunResult(exit_code=1, blocked=True, session_id="native", error="quota"))
        outcome, _ = self.run_job(adapter)
        self.assertEqual(outcome, "job j1 → waiting_quota")
        cp = self.db.get_checkpoint("plan-1")
        self.assertEqual(cp.execution_path, str(self.ws / "timetrace-out" / "plan-1"))
        self.assertEqual(cp.git_head, "folder")
        cloud = Cloud({"id": "j2"})
        agent = Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "token")
        self.assertEqual(agent.run_once(), "job j2 → awaiting_review")
        self.assertEqual(adapter.calls[-1][0], "resume")

    def test_changed_output_dir_blocks_resume(self):
        adapter = FolderAdapter(result=RunResult(exit_code=1, blocked=True, session_id="native", error="quota"))
        self.run_job(adapter)
        (self.ws / "timetrace-out" / "plan-1" / "cut.txt").write_text("edited by hand, longer")
        cloud = Cloud({"id": "j2"})
        agent = Agent(self.db, cloud, {"codex": adapter}, self.d / "home", lambda: "token")
        self.assertIn("waiting_input", agent.run_once())
        self.assertEqual(adapter.calls[-1][0], "start")

    def test_adapter_without_folder_support_is_rejected(self):
        class GitOnly(FolderAdapter):
            start_folder = None
        outcome, cloud = self.run_job(GitOnly())
        self.assertIn("rejected", outcome)
        self.assertEqual(cloud.events[-1]["type"], "failed")

    def test_chat_turn_on_folder_stays_read_only_in_workspace(self):
        adapter = FolderAdapter()
        outcome, cloud = self.run_job(adapter, {"kind": "chat_turn"})
        self.assertEqual(outcome, "job j1 → replied")
        self.assertEqual(adapter.calls, [("chat", str(self.ws))])
        self.assertFalse((self.ws / "timetrace-out").exists())

    def test_git_kind_requires_a_repository(self):
        self.db.upsert_workspace("f1", "footage", str(self.ws), "main", kind="git")
        outcome, cloud = self.run_job(FolderAdapter())
        self.assertEqual(outcome, "job j1 → rejected (unknown workspace)")


class FolderArtifactKindTest(unittest.TestCase):
    def test_result_json_may_declare_folder_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".timetrace" / "out"; out.mkdir(parents=True)
            (out / "result.json").write_text(json.dumps({"artifacts": [{"kind": "folder", "ref": "timetrace-out/x"}]}))
            self.assertEqual(results.collect(d)["artifacts"], [{"kind": "folder", "ref": "timetrace-out/x"}])


if __name__ == "__main__":
    unittest.main()
