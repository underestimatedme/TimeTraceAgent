"""chat_turn jobs: a phone conversation answered read-only by the local AI."""
import json
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from timetrace.adapters import claude, codex
from timetrace.adapters.base import CHAT_RULES, SAFETY_RULES
from timetrace.agent import Agent, REPLY_BYTES
from timetrace.db import Database
from timetrace.models import RunResult


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "initial"], cwd=path, check=True)


def overrides(cmd):
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == "-c"]


class ClaudeChatCmdTest(unittest.TestCase):
    cfg = {"bin": "claude", "permission_mode": "acceptEdits", "model": "sonnet",
           "allowed_tools": ["Bash(git add:*)", "Bash(git commit:*)"],
           "extra_args": ["--dangerously-skip-permissions"]}

    def test_first_turn_is_plan_mode_with_a_new_session(self):
        cmd = claude.build_chat_cmd(self.cfg, "what does main.py do?", session_id="s-new")
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "s-new")
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], CHAT_RULES)
        # No MCP server (user or project config) is reachable from a chat turn.
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)
        # Nothing that widens a task run's permissions reaches a read-only chat.
        for flag in ("--allowedTools", "--add-dir", "acceptEdits", "--dangerously-skip-permissions"):
            self.assertNotIn(flag, cmd)
        self.assertEqual(cmd[-1], "what does main.py do?")

    def test_later_turn_resumes(self):
        cmd = claude.build_chat_cmd(self.cfg, "and then?", resume="s-old")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-old")
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")

    def test_option_looking_prompt_stays_positional(self):
        cmd = claude.build_chat_cmd(self.cfg, "--permission-mode=bypassPermissions", resume="s")
        self.assertFalse(cmd[-1].startswith("-"))

    def test_adapter_creates_a_uuid_session_on_the_first_turn(self):
        seen = {}

        def fake_run(cmd, cwd, log, **kwargs):
            seen["cmd"], seen["cwd"] = cmd, cwd
            return 0, [json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                   "result": "It prints hello.", "session_id": cmd[cmd.index("--session-id") + 1]})]

        original = claude.run_streaming
        claude.run_streaming = fake_run
        try:
            ad = claude.ClaudeAdapter({"bin": "claude"}, billing=object(), credentials=lambda: {})
            res = ad.chat("explain", "/repo", "", "/dev/null")
        finally:
            claude.run_streaming = original
        sid = seen["cmd"][seen["cmd"].index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        self.assertEqual(res.session_id, sid)
        self.assertEqual(res.output, "It prints hello.")
        self.assertTrue(res.ok)
        self.assertEqual(seen["cwd"], "/repo")


class CodexChatCmdTest(unittest.TestCase):
    cfg = {"bin": "codex", "sandbox": "danger-full-access", "model": "gpt-5.6",
           "extra_args": ["--dangerously-bypass-approvals-and-sandbox"]}

    def test_first_turn_is_read_only(self):
        cmd = codex.build_chat_cmd(self.cfg, "explain", "/repo", last_msg_file="/log.last.md")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("-C") + 1], "/repo")
        self.assertIn('sandbox_mode="read-only"', overrides(cmd))
        self.assertEqual(cmd[cmd.index("-o") + 1], "/log.last.md")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.6")
        self.assertFalse(any(o.startswith("sandbox_workspace_write.writable_roots") for o in overrides(cmd)))
        for flag in ("--add-dir", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(flag, " ".join(cmd[:-1]))
        self.assertTrue(cmd[-1].startswith(CHAT_RULES))
        self.assertTrue(cmd[-1].endswith("explain"))
        self.assertNotIn(SAFETY_RULES, cmd[-1])

    def test_later_turn_resumes_read_only(self):
        cmd = codex.build_chat_cmd(self.cfg, "go on", "/repo", resume="tid", last_msg_file="/l")
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "tid"])
        self.assertNotIn("-s", cmd)
        self.assertNotIn("-C", cmd)
        self.assertIn('sandbox_mode="read-only"', overrides(cmd))
        self.assertEqual(cmd[cmd.index("-o") + 1], "/l")

    def test_adapter_reply_comes_from_the_last_message_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = str(Path(d) / "log")
            Path(log + ".last.md").write_text("stale from an earlier turn")
            seen = {}

            def fake_run(cmd, cwd, log_file, **kwargs):
                seen["cmd"] = cmd
                Path(cmd[cmd.index("-o") + 1]).write_text("Full final answer.")
                return 0, ['{"type":"thread.started","thread_id":"tid-1"}',
                           '{"type":"item.completed","item":{"type":"agent_message","text":"short"}}',
                           '{"type":"turn.completed"}']

            original = codex.run_streaming
            codex.run_streaming = fake_run
            try:
                res = codex.CodexAdapter({"bin": "codex"}, billing=object()).chat("q", d, "", log)
            finally:
                codex.run_streaming = original
            self.assertTrue(res.ok)
            self.assertEqual(res.output, "Full final answer.")
            self.assertEqual(res.session_id, "tid-1")
            self.assertNotIn("resume", seen["cmd"])

            def no_output(cmd, cwd, log_file, **kwargs):
                seen["cmd"] = cmd
                return 0, ['{"type":"item.completed","item":{"type":"agent_message","text":"only stream"}}',
                           '{"type":"turn.completed"}']
            codex.run_streaming = no_output
            try:
                res = codex.CodexAdapter({"bin": "codex"}, billing=object()).chat("q", d, "tid-1", log)
            finally:
                codex.run_streaming = original
            # A stale file from an earlier turn is never taken as this turn's reply.
            self.assertEqual(res.output, "only stream")
            self.assertEqual(res.session_id, "tid-1")
            self.assertEqual(seen["cmd"][:4], ["codex", "exec", "resume", "tid-1"])


class ChatCloud:
    def __init__(self, session=None, prompt="explain the repo"):
        self.events = []
        self.job = {"id": "c1", "kind": "chat_turn", "workspace_id": "ws1",
                    "tool_profile_id": "claude-default", "provider": "claude", "prompt": prompt}
        if session is not None:
            self.job["provider_session_id"] = session

    def claim(self, token):
        return {"job": dict(self.job), "attempt_id": "a1", "lease_epoch": 1,
                "lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def append_events(self, token, job_id, attempt_id, epoch, events):
        self.events.extend(events)

    def renew(self, token, attempt_id, epoch):
        return {"lease_expires_at": datetime.fromtimestamp(time.time() + 90, timezone.utc).isoformat()}

    def post_quota_samples(self, token, samples):
        return {}


class ChatAdapter:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def capabilities(self):
        return {"can_record": True, "can_read_quota": False, "can_dispatch": True,
                "can_resume": True, "can_enforce_zero_spend": True}

    def chat(self, prompt, cwd, session_id, log_file, cancel_event=None):
        self.calls.append((prompt, cwd, session_id))
        if self.result is not None:
            return self.result
        return RunResult(exit_code=0, ok=True, output="The repo is a CLI.", session_id=session_id or "new-sess")

    def start(self, *args, **kwargs):
        raise AssertionError("a chat turn must not start a task run")

    resume = start


class AgentChatTurnTest(unittest.TestCase):
    def run_turn(self, cloud, adapter):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"; init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo.resolve()), "main")

            def no_worktree(*args):
                raise AssertionError("a chat turn must not create a worktree")

            agent = Agent(db, cloud, {"claude": adapter}, Path(d), lambda: "token",
                          prepare_workspace=no_worktree)
            outcome = agent.run_once()
            self.assertEqual(db.pending_remote_events(), [])
            self.assertFalse((Path(d) / "worktrees").exists())
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                                            text=True).stdout, "")
            self.assertIsNone(db.get_checkpoint("c1"))
            return outcome, str(repo.resolve())

    def test_first_turn_runs_in_the_main_directory_and_uploads_reply_and_session(self):
        cloud, adapter = ChatCloud(), ChatAdapter()
        outcome, repo = self.run_turn(cloud, adapter)
        self.assertEqual(outcome, "job c1 → replied")
        self.assertEqual(adapter.calls, [("explain the repo", repo, "")])
        self.assertEqual([e["type"] for e in cloud.events], ["running", "completed"])
        done = cloud.events[1]
        self.assertEqual(done["reply"], "The repo is a CLI.")
        self.assertEqual(done["provider_session_id"], "new-sess")

    def test_later_turn_passes_the_stored_session(self):
        cloud, adapter = ChatCloud(session="sess-7"), ChatAdapter()
        self.run_turn(cloud, adapter)
        self.assertEqual(adapter.calls[0][2], "sess-7")
        self.assertEqual(cloud.events[-1]["provider_session_id"], "sess-7")

    def test_reply_is_redacted_and_truncated_to_32kb_utf8(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        text = "key " + secret + " " + "刻" * 20000  # 60 000 bytes of 3-byte characters
        cloud = ChatCloud()
        adapter = ChatAdapter(RunResult(exit_code=0, ok=True, output=text, session_id="s1"))
        self.run_turn(cloud, adapter)
        reply = cloud.events[-1]["reply"]
        self.assertNotIn(secret, reply)
        self.assertIn("[REDACTED:aws]", reply)
        self.assertLessEqual(len(reply.encode("utf-8")), REPLY_BYTES)
        self.assertEqual(REPLY_BYTES, 32 * 1024)
        self.assertGreater(len(reply.encode("utf-8")), REPLY_BYTES - 3)
        reply.encode("utf-8").decode("utf-8")  # no broken trailing character
        self.assertNotIn(secret, json.dumps(cloud.events, ensure_ascii=False))

    def test_quota_block_reports_waiting_quota(self):
        cloud = ChatCloud(session="sess-1")
        adapter = ChatAdapter(RunResult(exit_code=1, blocked=True, error="You've hit your limit", session_id="sess-1"))
        outcome, _ = self.run_turn(cloud, adapter)
        self.assertEqual(outcome, "job c1 → waiting_quota")
        self.assertEqual([e["type"] for e in cloud.events], ["running", "waiting_quota"])
        self.assertNotIn("reply", cloud.events[-1])

    def test_failure_reports_failed(self):
        cloud = ChatCloud()
        adapter = ChatAdapter(RunResult(exit_code=2, ok=False, error="boom"))
        outcome, _ = self.run_turn(cloud, adapter)
        self.assertEqual(outcome, "job c1 → failed")
        self.assertEqual(cloud.events[-1]["type"], "failed")
        self.assertIn("boom", cloud.events[-1]["message"])

    def test_session_id_that_could_be_an_option_is_refused(self):
        for bad in ("--dangerously-skip-permissions", "a b", "x" * 300):
            with self.subTest(bad=bad):
                cloud, adapter = ChatCloud(session=bad), ChatAdapter()
                outcome, _ = self.run_turn(cloud, adapter)
                self.assertEqual(adapter.calls, [])
                self.assertEqual(outcome, "job c1 → rejected (invalid session)")
                self.assertEqual([e["type"] for e in cloud.events], ["failed"])

    def test_tool_without_chat_support_is_rejected(self):
        class TaskOnly(ChatAdapter):
            chat = None
        cloud, adapter = ChatCloud(), TaskOnly()
        outcome, _ = self.run_turn(cloud, adapter)
        self.assertEqual(outcome, "job c1 → rejected (chat unsupported)")
        self.assertEqual(cloud.events[-1]["type"], "failed")

    def test_zero_spend_gate_still_applies(self):
        class Metered(ChatAdapter):
            def capabilities(self):
                return dict(super().capabilities(), can_enforce_zero_spend=False)
        cloud, adapter = ChatCloud(), Metered()
        outcome, _ = self.run_turn(cloud, adapter)
        self.assertEqual(adapter.calls, [])
        self.assertIn("blocked", outcome)


if __name__ == "__main__":
    unittest.main()
