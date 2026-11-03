"""The claim's MCP access reaches the AI tool's adapter for an execution run, and only then."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timetrace.agent import Agent
from timetrace.db import Database
from timetrace.models import RunResult
from tests.test_agent import Adapter, FakeCloud, init_repo

CLOUD = "https://apis.atlaspaces.com/timetrace/api/v1"
SENTINEL = "ttm_SENTINEL_" + "A1b2C3d4E5" * 4


def mcp_claim(token=SENTINEL, url=CLOUD + "/mcp", expires=None):
    expires = expires or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    return {"url": url, "token": token, "expires_at": expires}


class CloudWithMcp(FakeCloud):
    base_url = CLOUD

    def __init__(self, mcp=None):
        super().__init__()
        self.mcp = mcp

    def claim(self, token):
        value = super().claim(token)
        if self.mcp is not None:
            value["mcp"] = self.mcp
        return value


class TakesMcp(Adapter):
    supports_mcp = True

    def start(self, prompt, cwd, session_id, log_file, cancel_event=None, mcp=None):
        self.mcp = mcp
        return RunResult(exit_code=0, ok=True, output="finished", session_id=session_id)


class LacksMcp(Adapter):
    def start(self, prompt, cwd, session_id, log_file, cancel_event=None):
        return RunResult(exit_code=0, ok=True, output="finished", session_id=session_id)


class FolderAdapter(TakesMcp):
    def start_folder(self, prompt, cwd, workspace, session_id, log_file, cancel_event=None, **kw):
        self.folder_kwargs = dict(kw)
        return RunResult(exit_code=0, ok=True, output="finished", session_id=session_id)

    def resume_folder(self, *args, **kw):
        return self.start_folder(*args, **kw)


class ClaimCarriesMcpTest(unittest.TestCase):
    def run_job(self, adapter, cloud, kind="git"):
        logs = []
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            repo = Path(d) / "repo"
            init_repo(repo)
            db.upsert_workspace("ws1", "repo", str(repo), "main", kind=kind)
            agent = Agent(db, cloud, {"codex": adapter}, Path(d), lambda: "token", log=logs.append,
                          prepare_workspace=lambda *args: (str(repo), "main"))
            agent.run_once()
        return logs

    def test_an_execution_run_in_a_worktree_is_given_the_access(self):
        adapter = TakesMcp()
        self.run_job(adapter, CloudWithMcp(mcp_claim()))
        self.assertIsNotNone(adapter.mcp)
        self.assertEqual(adapter.mcp.env(), {"TIMETRACE_MCP_TOKEN": SENTINEL})

    def test_a_claim_without_it_gives_the_adapter_none(self):
        adapter = TakesMcp()
        self.run_job(adapter, CloudWithMcp(None))
        self.assertIsNone(adapter.mcp)

    def test_a_claim_that_is_not_good_is_run_without_it_and_the_log_says_which_rule_not_what_value(self):
        for name, claim in {"another host": mcp_claim(url="https://evil.example/timetrace/api/v1/mcp"),
                            "a query": mcp_claim(url=CLOUD + "/mcp?token=" + SENTINEL),
                            "expired": mcp_claim(expires="2001-01-01T00:00:00Z"),
                            "not a token": mcp_claim(token="nothing-" + SENTINEL)}.items():
            adapter = TakesMcp()
            logs = self.run_job(adapter, CloudWithMcp(claim))
            self.assertIsNone(adapter.mcp, name)
            notes = [line for line in logs if line.startswith("mcp: skipped")]
            self.assertEqual(len(notes), 1, name)
            for secret in (SENTINEL, "evil.example", "token="):
                self.assertNotIn(secret, " ".join(logs), name)

    def test_an_access_that_is_about_to_run_out_is_not_handed_to_the_tool(self):
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        adapter = TakesMcp()
        logs = self.run_job(adapter, CloudWithMcp(mcp_claim(expires=soon)))
        self.assertIsNone(adapter.mcp)
        self.assertEqual(len([line for line in logs if line.startswith("mcp: skipped")]), 1)
        self.assertNotIn(SENTINEL, " ".join(logs))

    def test_a_tool_whose_adapter_does_not_take_it_is_run_as_before(self):
        logs = self.run_job(LacksMcp(), CloudWithMcp(mcp_claim()))
        self.assertTrue(any(line.startswith("mcp: skipped") for line in logs))
        self.assertNotIn(SENTINEL, " ".join(logs))

    def test_a_folder_run_never_gets_it(self):
        adapter = FolderAdapter()
        self.run_job(adapter, CloudWithMcp(mcp_claim()), kind="folder")
        self.assertTrue(hasattr(adapter, "folder_kwargs"), "the folder run did not happen")
        self.assertNotIn("mcp", adapter.folder_kwargs)


if __name__ == "__main__":
    unittest.main()
