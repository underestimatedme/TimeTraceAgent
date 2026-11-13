"""P0-1B: the anonymous result-outbox summary rides on the protocol-2 inventory as `health.outbox`.

What these tests pin down (real SQLite files with synthetic data; a fake Valley; nothing sleeps, no model, no pairing):

* the object sent is exactly `Database.remote_outbox_health()` (schema 1): counts, one reason code and epoch seconds,
  no job / attempt id, no event content, no command, path or token; paused, retrying and set-aside are told apart, and
  results that merely wait to be sent are neither failed nor lost, so they are not counted;
* it is read at a bounded rate and pushed when its content changes (or as the ordinary heartbeat), never because a
  clock reading moved; a protocol-1 Runner neither reads nor sends it;
* an unreadable or malformed summary is left out (not reported), never sent as zero; the rest of the health stays; the
  failure is logged and recorded once, and neither a claim nor an upload is held up by it;
* a Valley that ignores the new field changes nothing; one that refuses it gets the same inventory without it;
* once the outbox is cleared on the same Runner the next inventory carries an explicit zero.
"""
import copy
import json
import sqlite3
import unittest

from timetrace import runner_state, sync
from timetrace.agent import (OUTBOX_HEALTH_INTERVAL_SECONDS, OUTBOX_HEALTH_REFUSED_SECONDS, VERSION_REJECTED_CODE,
                             Agent, outbox_summary, OutboxHealthInvalid)
from timetrace import __version__
from timetrace.cloud import CloudClient, CloudError

from tests.test_cloud import Response
from tests.test_outbox_isolation import READ_TIMEOUT, SERVER_ERROR
from tests.test_outbox_refusal_pause import PAUSE_AFTER, RefusalFixture

HEARTBEAT = 30
INTERVAL = OUTBOX_HEALTH_INTERVAL_SECONDS
HEALTH = {"claude_login": "ok", "disk_free_gb": 42.0, "workspaces": [], "checked_at": "2026-11-13T00:00:00Z"}
ZERO = {"schema": 1, "paused_batches": 0, "paused_events": 0, "paused_reason": None, "paused_since": None,
        "oldest_paused_event_at": None, "retrying_batches": 0, "retrying_events": 0, "set_aside_events": 0}
KEYS = set(ZERO)
# What nothing but the sender may ever hold: ids, event bodies, commands, paths, tokens.
PRIVATE = ("job-aaa", "job-bbb", "job-ccc", "job-ddd", "attempt", "SECRET-BODY", "rm -rf", "/Users/synthetic", "token")


class ReportFixture(RefusalFixture):
    """The refusal fixture (outbox failures, flush clock) plus an Agent that reports the inventory like the daemon does."""

    def fresh(self, parallel=False):
        super().fresh(parallel)
        self.pushes = []  # the extras of every inventory push
        outer = self

        class Valley:
            def update_inventory(_, token, workspaces, tools, *args, extras=None, **kwargs):
                outer.pushes.append(copy.deepcopy(extras))

        self.valley = Valley()
        self.reporter = self.make_reporter()
        self.reads = 0
        inner = self.db.remote_outbox_health

        def counted():
            self.reads += 1
            return inner()
        self.db.remote_outbox_health = counted

    def make_reporter(self, cloud=None, **kw):
        kw.setdefault("health_check", lambda: dict(HEALTH))
        return Agent(self.db, cloud or self.valley, {}, self.agent.home, lambda: "synthetic-credential",
                     report_protocol=kw.pop("report_protocol", True), inventory=lambda: ([], []),
                     log=self.logs.append, **kw)

    def outbox(self, push=-1):
        return self.pushes[push]["health"].get("outbox")

    def break_reading(self, error):
        def failing():
            self.reads += 1
            raise error
        self.db.remote_outbox_health = failing

    def queue_private(self, job):
        """A batch whose events carry everything that must never leave: bodies, a command, a path."""
        events = [{"seq": 1, "type": "running", "observed_at": "2026-10-30T00:32:00Z"},
                  {"seq": 2, "type": "completed", "observed_at": "2026-10-30T00:32:34Z",
                   "message": "SECRET-BODY rm -rf /Users/synthetic/private", "output_tail": "token=abc"}]
        for event in events:
            self.db.queue_remote_event(job, job + "-attempt", 3, event)
        self.queued[job] = events


class OutboxHealthPayloadTest(ReportFixture, unittest.TestCase):
    def test_the_first_inventory_carries_an_explicit_zero_object(self):
        self.reporter.maintain(now=1000.0)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.outbox(), ZERO)
        self.assertEqual(self.pushes[0]["health"]["claude_login"], "ok", "the rest of the health is untouched")

    def test_paused_retrying_and_set_aside_are_reported_apart_and_anonymously(self):
        self.queue_private("job-aaa")
        self.refuse_until_paused("job-aaa")                      # stopped after PAUSE_AFTER refusals: paused
        self.queue_private("job-bbb")
        self.failing["job-bbb"] = SERVER_ERROR
        self.flush_at(5000)                                      # failed another way, still retried: retrying
        self.queue_private("job-ccc")
        self.db.defer_remote_events("job-ccc", "job-ccc-attempt", 3, 409, 1000, 1060)
        self.db.discard_conflicted_remote_events("job-ccc", "job-ccc-attempt", now=2000)   # set aside by hand
        self.queue_private("job-ddd")                            # only waiting: neither failed nor lost
        self.reporter.maintain(now=9000.0)
        sent = self.outbox()
        self.assertEqual(set(sent), KEYS)
        self.assertEqual((sent["paused_batches"], sent["paused_events"], sent["paused_reason"]), (1, 2, "http_409"))
        self.assertEqual((sent["retrying_batches"], sent["retrying_events"]), (1, 2))
        self.assertEqual(sent["set_aside_events"], 2)
        self.assertEqual(sent, self.db.remote_outbox_health(), "exactly the database's summary, nothing added")
        paused_at = self.db.conn.execute("SELECT paused_at FROM remote_outbox_hold").fetchone()["paused_at"]
        self.assertEqual(sent["paused_since"], int(paused_at))
        self.assertEqual(sent["oldest_paused_event_at"],
                         self.db.conn.execute("SELECT MIN(created_at) FROM remote_outbox WHERE job_id='job-aaa'").fetchone()[0])
        for value in [sent[key] for key in sent if key != "paused_reason"]:
            self.assertIsInstance(value, int)
        text = json.dumps(self.pushes[-1])
        for private in PRIVATE:
            self.assertNotIn(private, text)

    def test_waiting_results_alone_are_not_reported_as_failed_or_lost(self):
        self.queue_private("job-ddd")
        self.reporter.maintain(now=1000.0)
        self.assertEqual(len(self.db.pending_remote_events()), 2)
        self.assertEqual(self.outbox(), ZERO)

    def test_the_summary_is_validated_before_it_leaves(self):
        good = dict(ZERO)
        self.assertEqual(outbox_summary(good), good)
        self.assertIsNot(outbox_summary(good), good)
        for bad in (None, [], {}, dict(good, schema=2), dict(good, schema=True), dict(good, extra=1),
                    {k: v for k, v in good.items() if k != "paused_since"}, dict(good, paused_batches=-1),
                    dict(good, paused_batches=True), dict(good, retrying_events="2"), dict(good, paused_since=1.5),
                    dict(good, paused_reason="job-aaa-attempt")):
            with self.assertRaises(OutboxHealthInvalid, msg=bad):
                outbox_summary(bad)

    def test_the_real_payload_with_and_without_outbox_differs_only_by_that_field(self):
        bodies = []

        def opener(request, timeout):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return Response(200, {"code": 0, "data": {}})
        client = CloudClient("https://valley.example/timetrace/api/v1", opener=opener)
        self.refuse_until_paused("job-aaa")
        self.make_reporter(client).maintain(now=1000.0)
        self.break_reading(sqlite3.OperationalError("locked"))     # the same state, but the summary cannot be read
        self.make_reporter(client).maintain(now=1000.0)
        with_outbox, without = bodies
        self.assertEqual(with_outbox["health"]["outbox"]["paused_batches"], 1)
        self.assertNotIn("outbox", without["health"])
        stripped = copy.deepcopy(with_outbox)
        del stripped["health"]["outbox"]
        self.assertEqual(stripped, without, "no other field of the inventory changed")
        # ... and `without` is exactly what the unchanged code sends for the same state.
        legacy = []
        CloudClient("https://valley.example/timetrace/api/v1",
                    opener=lambda request, timeout: legacy.append(json.loads(request.data.decode("utf-8")))
                    or Response(200, {"code": 0, "data": {}})).update_inventory(
            "t", [], [], extras={"protocol_version": 2, "agent_version": __version__, "accepting_local": True,
                                 "project_directories_version": 1,
                                 "health": dict(HEALTH, sleep_prevention="inactive")})
        self.assertEqual(without, legacy[0])

    def test_a_protocol_one_runner_neither_reads_nor_sends_it(self):
        bodies = []
        client = CloudClient("https://valley.example/timetrace/api/v1",
                             opener=lambda request, timeout: bodies.append(json.loads(request.data.decode("utf-8")))
                             or Response(200, {"code": 0, "data": {}}))
        old = self.make_reporter(client, report_protocol=False)
        for now in (1000.0, 1100.0, 1200.0):
            old.maintain(now=now)
        self.assertEqual(self.reads, 0)
        self.assertEqual(len(bodies), 1, "an unchanged protocol-1 inventory is not repeated")
        self.assertEqual(set(bodies[0]), {"workspaces", "tools", "workflow_inputs_version", "project_directories_version",
                                          "planning_version", "operations_version"})


class OutboxHealthCadenceTest(ReportFixture, unittest.TestCase):
    def test_an_unchanged_summary_is_not_pushed_often_and_is_read_at_a_bounded_rate(self):
        self.refuse_until_paused("job-aaa")
        self.reporter.maintain(now=1000.0)
        for now in range(1001, 1000 + HEARTBEAT):
            self.reporter.maintain(now=float(now))
        self.assertEqual(len(self.pushes), 1, "nothing changed: no push before the heartbeat")
        self.assertLessEqual(self.reads, 2)
        self.reporter.maintain(now=1000.0 + HEARTBEAT)
        self.assertEqual(len(self.pushes), 2, "the heartbeat is still sent")
        self.assertEqual(self.pushes[1], self.pushes[0], "...with the same content")
        end = 1000 + 20 * HEARTBEAT
        for now in range(1031, end):
            self.reporter.maintain(now=float(now))
        heartbeats = len(range(1000 + HEARTBEAT, end, HEARTBEAT))
        self.assertEqual(len(self.pushes), 1 + heartbeats, "one push per heartbeat, never one per round")
        self.assertLessEqual(self.reads, (end - 1000) // INTERVAL + 1, "read at most once per interval")
        self.assertEqual(len({json.dumps(p, sort_keys=True) for p in self.pushes}), 1,
                         "no clock reading changes the summary between pushes")

    def test_a_change_reaches_the_next_inventory_update(self):
        self.reporter.maintain(now=1000.0)
        self.queue_private("job-bbb")
        self.failing["job-bbb"] = SERVER_ERROR
        self.flush_at(1001)
        self.reporter.maintain(now=1000.0 + INTERVAL - 1)
        self.assertEqual(len(self.pushes), 1, "inside the read interval and the heartbeat: not yet")
        self.reporter.maintain(now=1000.0 + INTERVAL)
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual((self.outbox()["retrying_batches"], self.outbox()["retrying_events"]), (1, 2))
        self.reporter.maintain(now=1000.0 + INTERVAL + 1)
        self.assertEqual(len(self.pushes), 2, "pushed once for the change, not again")

    def test_agent_sync_reads_it_again_at_once(self):
        self.reporter.maintain(now=1000.0)
        self.queue_private("job-bbb")
        self.failing["job-bbb"] = SERVER_ERROR
        self.flush_at(1001)
        sync.request(self.agent.home)
        self.reporter.maintain(now=1002.0)
        self.assertEqual(self.outbox()["retrying_batches"], 1)

    def test_the_same_runner_reports_an_explicit_zero_after_the_outbox_is_cleared(self):
        self.refuse_until_paused("job-aaa")
        self.reporter.maintain(now=1000.0)
        self.assertEqual((self.outbox()["paused_batches"], self.outbox()["paused_events"]), (1, 2))
        # A person resumes the stopped batch; Valley accepts it; the records are gone.
        self.assertEqual(self.db.resume_paused_remote_events("job-aaa", "job-aaa-attempt"), 2)
        self.failing.clear()
        self.flush_at(20000)
        self.assertEqual(self.sent, ["job-aaa"])
        self.reporter.maintain(now=1000.0 + INTERVAL)
        self.assertEqual(self.outbox(), ZERO, "an explicit zero, not an absent field")
        self.assertIn("outbox", self.pushes[-1]["health"])
        self.assertIs(runner_state.load(self.agent.home)["inventory_outbox_reported"], True)

    def test_a_lost_receipt_counts_as_retrying_until_the_ack_arrives(self):
        self.queue_private("job-bbb")
        self.failing["job-bbb"] = READ_TIMEOUT          # Valley may have the events; the answer never came
        self.flush_at(1000)
        self.reporter.maintain(now=1000.0)
        self.assertEqual((self.outbox()["retrying_batches"], self.outbox()["paused_batches"]), (1, 0))
        self.failing.clear()
        self.flush_at(1100)
        self.reporter.maintain(now=1100.0)
        self.assertEqual(self.outbox(), ZERO)
        self.assertEqual(self.pending_jobs(), [])


class OutboxHealthFailureTest(ReportFixture, unittest.TestCase):
    def test_an_unreadable_summary_is_not_reported_and_never_a_zero(self):
        self.break_reading(sqlite3.OperationalError("unable to open /Users/synthetic/private.db"))
        self.reporter.maintain(now=1000.0)
        self.assertEqual(len(self.pushes), 1, "the inventory is pushed all the same")
        health = self.pushes[0]["health"]
        self.assertNotIn("outbox", health)
        self.assertEqual({k: health[k] for k in HEALTH}, HEALTH, "the other health fields are kept")
        self.assertEqual(self.pushes[0]["protocol_version"], 2)
        state = runner_state.load(self.agent.home)
        self.assertEqual((state["outbox_health"], state["outbox_health_error"]), ("unavailable", "OperationalError"))
        self.assertIs(state["inventory_outbox_reported"], False)
        notes = [line for line in self.logs if "outbox health" in line]
        self.assertEqual(notes, ["outbox health unavailable, not reported: OperationalError"])
        self.assertNotIn("/Users/synthetic", "".join(self.logs), "the class name only, never the message")

    def test_a_real_sqlite_failure_is_the_same(self):
        self.db.conn.execute("DROP TABLE remote_outbox_hold")
        self.reporter.maintain(now=1000.0)
        self.assertNotIn("outbox", self.pushes[0]["health"])
        self.assertEqual(runner_state.load(self.agent.home)["outbox_health_error"], "OperationalError")

    def test_a_failure_is_noted_once_and_recovery_reports_the_real_summary(self):
        self.break_reading(sqlite3.OperationalError("locked"))
        for now in range(1000, 1000 + 4 * INTERVAL, 5):
            self.reporter.maintain(now=float(now))
        self.assertEqual(len([line for line in self.logs if "outbox health" in line]), 1)
        self.assertTrue(all("outbox" not in push["health"] for push in self.pushes))
        del self.db.remote_outbox_health            # the database answers again
        before = len(self.pushes)
        self.reporter.maintain(now=2000.0)
        self.assertEqual(len(self.pushes), before + 1, "reported again as soon as it can be read")
        self.assertEqual(self.outbox(), ZERO)
        self.assertEqual(runner_state.load(self.agent.home)["outbox_health"], "ok")
        self.assertIsNone(runner_state.load(self.agent.home)["outbox_health_error"])

    def test_a_malformed_summary_is_not_sent(self):
        self.db.remote_outbox_health = lambda: dict(ZERO, schema=2)
        self.reporter.maintain(now=1000.0)
        self.assertNotIn("outbox", self.pushes[0]["health"])
        self.assertEqual(runner_state.load(self.agent.home)["outbox_health_error"], "OutboxHealthInvalid")

    def test_no_health_at_all_is_not_invented_for_the_summary(self):
        quiet = self.make_reporter(health_check=lambda: (_ for _ in ()).throw(OSError("self-check broke")))
        quiet.maintain(now=1000.0)
        self.assertNotIn("health", self.pushes[0])

    def test_a_failing_read_does_not_hold_up_the_claim_or_the_upload(self):
        daemon = self.make_reporter(self.agent.cloud)
        self.agent = daemon
        self.break_reading(sqlite3.OperationalError("locked"))
        self.queue_private("job-bbb")
        daemon.maintain(now=1000.0)
        self.assertEqual(daemon.run_once(), "idle")
        self.assertEqual(self.sent, ["job-bbb"], "the queued result was uploaded")
        self.assertEqual(len(self.claims), 1, "and the claim went on")

    def test_a_failing_read_does_not_stop_the_daemon_loop(self):
        self.agent = self.make_reporter(self.agent.cloud)
        self.break_reading(sqlite3.OperationalError("locked"))
        self.queue_private("job-bbb")
        self.script_claims([None, None, None])
        self.run_loop()
        self.assertEqual(self.sent, ["job-bbb"])
        self.assertEqual(len(self.claims), 3)
        self.assertEqual(len([line for line in self.logs if "outbox health" in line]), 1)

    def test_the_daemon_loop_reports_while_it_claims_and_uploads(self):
        self.agent = self.make_reporter(self.agent.cloud)
        self.queue_private("job-bbb")
        self.script_claims([None, None])
        self.run_loop()
        self.assertEqual(self.sent, ["job-bbb"])
        self.assertEqual(len(self.claims), 2)
        self.assertEqual(runner_state.load(self.agent.home)["outbox_health"], "ok")


class OutboxHealthValleyCompatibilityTest(ReportFixture, unittest.TestCase):
    def client(self, handler):
        self.requests = []

        def opener(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            self.requests.append(body)
            return handler(body)
        return CloudClient("https://valley.example/timetrace/api/v1", opener=opener)

    def test_an_older_valley_that_ignores_the_field_keeps_heartbeat_and_inventory(self):
        known = {"claude_login", "codex_login", "disk_free_gb", "workspaces", "checked_at", "sleep_prevention"}
        seen = []

        def old_valley(body):
            seen.append({k: v for k, v in body["health"].items() if k in known})   # the field is dropped silently
            return Response(200, {"code": 0, "data": {}})
        agent = self.make_reporter(self.client(old_valley))
        for now in (1000.0, 1000.0 + HEARTBEAT, 1000.0 + 2 * HEARTBEAT):
            agent.maintain(now=now)
        self.assertEqual(len(seen), 3, "presence continues every heartbeat")
        self.assertTrue(all("outbox" not in health for health in seen))
        self.assertEqual(runner_state.load(self.agent.home)["inventory_pushed_at"], 1000 + 2 * HEARTBEAT)
        self.assertEqual([line for line in self.logs if "failed" in line], [])

    def test_a_valley_that_refuses_the_field_gets_the_same_inventory_without_it(self):
        def strict_valley(body):
            if "outbox" in body.get("health", {}):
                return Response(422, {"code": 42200, "message": "unknown field health.outbox"})
            return Response(200, {"code": 0, "data": {}})
        agent = self.make_reporter(self.client(strict_valley))
        agent.maintain(now=1000.0)
        self.assertEqual(len(self.requests), 2)
        refused, accepted = self.requests
        self.assertIn("outbox", refused["health"])
        del refused["health"]["outbox"]
        self.assertEqual(refused, accepted, "the retry is the same inventory minus the field")
        self.assertEqual(runner_state.load(self.agent.home)["inventory_pushed_at"], 1000)
        self.assertIs(runner_state.load(self.agent.home)["inventory_outbox_reported"], False)
        # It is not asked again every round or every heartbeat ...
        for now in (1000.0 + HEARTBEAT, 1000.0 + 2 * HEARTBEAT, 1000.0 + 10 * HEARTBEAT):
            agent.maintain(now=now)
        self.assertEqual(len(self.requests), 2 + 3)
        self.assertTrue(all("outbox" not in body["health"] for body in self.requests[2:]))
        # ... and tried once more after the pause.
        agent.maintain(now=1000.0 + OUTBOX_HEALTH_REFUSED_SECONDS)
        self.assertIn("outbox", self.requests[-2]["health"])
        self.assertNotIn("outbox", self.requests[-1]["health"])

    def test_other_failures_are_not_mistaken_for_a_refused_field(self):
        for status, code in ((401, 0), (409, 0), (429, 0), (500, 0), (422, VERSION_REJECTED_CODE)):
            with self.subTest(status=status, code=code):
                self.requests = []
                client = self.client(lambda body: Response(status, {"code": code or 50000, "message": "no"}))
                agent = self.make_reporter(client)
                agent.maintain(now=1000.0)
                self.assertEqual(len(self.requests), 1, "one request, no second attempt")
                self.assertIsNone(runner_state.load(self.agent.home).get("inventory_pushed_at"))
                self.assertIn("inventory push failed: CloudError", self.logs[-1])
                self.logs.clear()

    def test_a_refusal_that_the_plain_inventory_shares_is_not_hidden(self):
        client = self.client(lambda body: Response(400, {"code": 40000, "message": "bad inventory"}))
        agent = self.make_reporter(client)
        agent.maintain(now=1000.0)
        self.assertEqual(len(self.requests), 2, "tried without the field once")
        self.assertIsNone(runner_state.load(self.agent.home).get("inventory_pushed_at"))
        self.assertIn("inventory push failed: CloudError", self.logs[-1])


if __name__ == "__main__":
    unittest.main()
