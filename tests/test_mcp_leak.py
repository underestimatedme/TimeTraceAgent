"""The token of a dispatched run never reaches a place that outlives the process or leaves this computer.

The sentinel below plays the token. The fake tools do what a model does when it runs `env`: print the variable they were
given, raw, on stdout and stderr and inside a message, and then put it in the final answer. The runs go through the real
adapters and the real upload path (the Agent's events, its database, its log files); nothing is checked by a regex alone.
"""
import json
import logging
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from timetrace.adapters import claude, codex
from timetrace.agent import Agent
from timetrace.billing import StaticBilling
from timetrace.db import Database
from tests.test_agent import FakeCloud, init_repo

CLOUD = "https://apis.atlaspaces.com/timetrace/api/v1"
SENTINEL = "ttm_SENTINEL_" + "Lk7Jh6Gf5D" * 4


class Cloud(FakeCloud):
    base_url = CLOUD

    def __init__(self, provider, profile):
        super().__init__()
        self.provider, self.profile = provider, profile

    def claim(self, token):
        value = super().claim(token)
        value["job"].update(provider=self.provider, tool_profile_id=self.profile)
        value["mcp"] = {"url": CLOUD + "/mcp", "token": SENTINEL,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")}
        return value


FAKE_CODEX = '''#!{python}
import json, os, sys
token = os.environ.get("TIMETRACE_MCP_TOKEN", "")
with open(os.environ["TT_FAKE_TRANSCRIPT"], "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
# What Codex does: its final answer is written to the file named by -o, by the tool itself.
if "-o" in sys.argv:
    with open(sys.argv[sys.argv.index("-o") + 1], "w") as fh:
        fh.write("the token is " + token + "\\nthe last run's token was " + os.environ.get("TT_FAKE_PREV", ""))
print("diagnosticprefix" + token + "suffix")
print("TIMETRACE_MCP_TOKEN=" + token)
print("an older token, run into a word: lastrun" + os.environ.get("TT_FAKE_PREV", ""))
sys.stderr.write("token in stderr: " + token + "\\n")
sys.stderr.write("diagnosticprefix" + token + "\\n")
print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "the token is " + token}}}}))
print(json.dumps({{"type": "turn.completed"}}))
'''


class RunHarness(unittest.TestCase):
    """Runs one dispatched job through the real adapter and the real upload path, with a fake tool process."""

    def run_agent(self, provider):
        records = io.StringIO()
        handler = logging.StreamHandler(records)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        notes = []
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            repo = Path(d) / "repo"
            init_repo(repo)
            transcript = Path(d) / "transcript.jsonl"
            scenario = Path(d) / "scenario.json"
            if provider == "claude":
                adapter = claude.ClaudeAdapter({"bin": str(Path(__file__).with_name("fake_claude_stream.py")), "allowed_tools": []},
                                               billing=StaticBilling(True), credentials=lambda: {})
                scenario.write_text(json.dumps({"turns": [[
                    {"raw": "tool says " + SENTINEL}, {"raw": "diagnosticprefix" + SENTINEL + "suffix"}, {"echo_env": "TIMETRACE_MCP_TOKEN"},
                    {"stderr": "stderr says " + SENTINEL}, {"stderr": "diagnosticprefix" + SENTINEL},
                    {"result": "the token is " + SENTINEL + ", and diagnosticprefix" + SENTINEL}]]}))
                cloud, key = Cloud("claude", "claude-default"), "claude"
            else:
                script = Path(d) / "codex"
                script.write_text(FAKE_CODEX.format(python=sys.executable))
                script.chmod(0o755)
                adapter = codex.CodexAdapter({"bin": str(script), "sandbox": "workspace-write"}, billing=StaticBilling(True))
                scenario.write_text("{}")
                cloud, key = Cloud("codex", "codex-default"), "codex"
            db = Database(home / "timetrace.db")
            db.upsert_workspace("ws1", "repo", str(repo), "main")
            agent = Agent(db, cloud, {key: adapter}, home, lambda: "token", log=notes.append,
                          prepare_workspace=lambda *args: (str(repo), "main"))
            env = {"TT_FAKE_SCENARIO": str(scenario), "TT_FAKE_TRANSCRIPT": str(transcript)}
            with patch.dict("os.environ", env), patch.object(codex.CodexAdapter, "_writable_extras", staticmethod(lambda cwd: [])):
                outcome = agent.run_once()
            outbox = json.dumps(db.pending_remote_events(), default=str)
            everything = b""
            for path in sorted(Path(d).rglob("*")):
                if path.is_file() and path not in (scenario, transcript) and path != script_path(d):
                    everything += path.read_bytes()
            # (the fake's transcript also records the environment it was given, as evidence for other tests: here only the argv)
            argv = json.dumps([json.loads(line)["argv"] for line in transcript.read_text().splitlines() if '"argv"' in line]) if transcript.exists() else ""
            return {"outcome": outcome, "events": json.dumps(cloud.events, default=str), "cloud": cloud, "outbox": outbox,
                    "disk": everything, "argv": argv, "notes": "\n".join(notes), "logging": records.getvalue(),
                    "logs": [p.read_text(errors="replace") for p in Path(d).rglob("*.log")]}



class TokenNeverLeavesTest(RunHarness):
    def check(self, provider):
        got = self.run_agent(provider)
        self.assertIn("completed", got["events"], got["outcome"])
        for where in ("events", "outbox", "argv", "notes", "logging"):
            self.assertNotIn(SENTINEL, got[where], where)
            self.assertNotIn(SENTINEL[4:], got[where], where)
        self.assertNotIn(SENTINEL.encode(), got["disk"], "something on disk holds the token (a log, the database, a file)")
        self.assertTrue(got["logs"], "no run log to look at")
        for log in got["logs"]:
            self.assertNotIn(SENTINEL, log)
        # what was uploaded says that something was masked, so a missing line is not mistaken for a clean run
        self.assertIn("REDACTED", got["events"] + "".join(got["logs"]))

    def test_claude_run_through_the_real_adapter_and_upload_path(self):
        self.check("claude")

    def test_codex_run_through_the_real_adapter_and_upload_path(self):
        self.check("codex")


class FileArtifactTest(RunHarness):
    """A declared `file` artifact is read from the worktree and uploaded as it is, so what a tool wrote into it is what leaves.
    The fake tool writes the token into files in several forms and declares them in result.json, the way a model does."""

    # (The fake binary is also started for its other subcommands, such as the quota probe: only `exec` writes files, and in the
    # directory Codex is told to work in (-C), as the real one does, never in the directory the tests run from.)
    WRITER = """import codecs
if "exec" in sys.argv:
    if "-C" in sys.argv:
        os.chdir(sys.argv[sys.argv.index("-C") + 1])
    os.makedirs(".timetrace/out", exist_ok=True)
    files = {{
        "leak.bin": b"\\xff\\xfe" + token.encode(),
        "leak16.bin": codecs.BOM_UTF16_LE + token.encode("utf-16-le"),
        "leak.txt": ("log line\\nTIMETRACE_MCP_TOKEN=" + token + "\\n").encode(),
        "clean.bin": b"\\xff\\xfe\\x00\\x01 nothing secret in here",
    }}
    for name, data in files.items():
        with open(name, "wb") as fh:
            fh.write(data)
    with open(".timetrace/out/result.json", "w") as fh:
        fh.write(json.dumps({{"artifacts": [{{"kind": "file", "ref": name}} for name in files]}}))
"""

    def test_no_upload_request_carries_the_token_in_any_form_and_a_clean_file_still_goes_up(self):
        uploads = []

        class Recording(Cloud):
            def upload_job_file(self, token, job_id, name, data):
                uploads.append((name, data))
                return {"id": "file-" + name}

        script = FAKE_CODEX.replace('print("TIMETRACE_MCP_TOKEN=" + token)', self.WRITER + 'print("TIMETRACE_MCP_TOKEN=" + token)')
        module = sys.modules[__name__]
        with patch.object(module, "FAKE_CODEX", script), patch.object(module, "Cloud", Recording):
            got = self.run_agent("codex")
        self.assertIn("completed", got["events"], got["outcome"])
        self.assertIn(SENTINEL.encode(), got["disk"], "the fake tool must really have written the token into its files")
        self.assertTrue(uploads, "nothing was uploaded: the clean file should have been")
        for name, data in uploads:
            self.assertNotIn(SENTINEL.encode(), data, name)
            self.assertNotIn(SENTINEL.encode("utf-16-le"), data, name)
            self.assertNotIn(SENTINEL[4:20].encode(), data, name)
        self.assertEqual(sorted(name for name, _ in uploads), ["clean.bin", "leak.txt"])
        self.assertEqual(dict(uploads)["clean.bin"], b"\xff\xfe\x00\x01 nothing secret in here")
        self.assertIn(b"log line", dict(uploads)["leak.txt"])
        self.assertIn(b"REDACTED", dict(uploads)["leak.txt"])


def script_path(d):
    return Path(d) / "codex"


if __name__ == "__main__":
    unittest.main()
