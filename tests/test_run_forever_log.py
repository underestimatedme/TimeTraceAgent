import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from timetrace.agent import Agent
from timetrace.db import Database

# S3-4: cmd_agent_run builds the Agent with a timestamping `log` and then calls
# run_forever(interval) without one. run_forever must not replace the log it was
# built with by a bare print: the daemon's own messages (409s, loop errors,
# authorization failures) were the lines that had no time.


class RunForeverLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "old.db")
        self.addCleanup(self.db.close)

    def agent(self, **kwargs):
        return Agent(self.db, object(), {}, Path(self.tmp.name), lambda: "token", **kwargs)

    def test_run_forever_keeps_the_log_the_agent_was_built_with(self):
        lines = []
        agent = self.agent(log=lines.append)
        agent.stop()          # return at once: only the wiring is under test
        agent.run_forever(interval=0)
        agent.log("a message from inside the Agent")
        # run_forever's own closing line and the Agent's later messages both go through the given log.
        self.assertEqual(lines, ["stopped", "a message from inside the Agent"])

    def test_an_agent_built_without_a_log_still_prints_by_default(self):
        agent = self.agent()
        agent.stop()
        out = io.StringIO()
        with redirect_stdout(out):
            agent.run_forever(interval=0)
        self.assertEqual(out.getvalue(), "stopped\n")
