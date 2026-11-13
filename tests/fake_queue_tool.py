#!/usr/bin/env python3
"""A side-effect-free stand-in for `claude -p ... --output-format stream-json`, for the local-queue
interruption tests (tests/test_local_stop.py). It touches nothing but files under $TT_FAKE_HOME.

  fake_queue_tool.py auth status          the answer a signed-in subscription login gives
  fake_queue_tool.py -p ... PROMPT        one run; the task's label is the last line of PROMPT

How a run behaves comes from $TT_FAKE_HOME/scenario.json: {label: {...}} (default: finish at once).
  "hang": true            wait (up to 40 s) for $TT_FAKE_HOME/release, as a tool in the middle of its work
  "ignore_term": true     ignore SIGTERM, so only SIGKILL stops it
  "finish_on_term": true  on SIGTERM write its (successful) result and exit 0: completes as it is stopped
  "grandchild": true      fork a member that outlives the leader unless the whole process group is killed

Evidence, all plain files: exec.log gets one JSON line per run (event start|resume, label, session, pid, cwd);
running-<label> appears once the tool is "working"; alive-<label>.lock is flock-ed by the tool and by every
process it forked for as long as any of them lives, so a test can tell "the whole tree is gone" without
signalling any pid.
"""
import fcntl
import json
import os
import signal
import sys
import time

DEADLINE_SECONDS = 40


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(session):
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": session})


def main(argv):
    if argv[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                          "subscriptionType": "max"}))
        return 0
    home = os.environ["TT_FAKE_HOME"]
    kind, session = "start", ""
    for flag, name in (("--session-id", "start"), ("--resume", "resume")):
        if flag in argv:
            kind, session = name, argv[argv.index(flag) + 1]
    label = argv[-1].rsplit("\n", 1)[-1].strip()
    try:
        with open(os.path.join(home, "scenario.json"), encoding="utf-8") as fh:
            plan = json.load(fh).get(label, {})
    except OSError:
        plan = {}
    alive = open(os.path.join(home, "alive-%s.lock" % label), "a+")
    try:
        fcntl.flock(alive, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pass  # a previous copy (a test of a tool that was never stopped) still holds it
    with open(os.path.join(home, "exec.log"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": kind, "label": label, "session": session, "pid": os.getpid(),
                             "cwd": os.getcwd()}) + "\n")
    emit({"type": "system", "subtype": "init", "session_id": session})
    if plan.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if plan.get("finish_on_term"):
        def finish(signum, frame):
            result(session)
            os._exit(0)
        signal.signal(signal.SIGTERM, finish)
    release = os.path.join(home, "release")
    deadline = time.time() + DEADLINE_SECONDS
    if plan.get("grandchild") and os.fork() == 0:
        while time.time() < deadline and not os.path.exists(release):
            time.sleep(0.05)
        os._exit(0)
    if plan.get("hang"):
        with open(os.path.join(home, "running-%s" % label), "w") as fh:
            fh.write(str(os.getpid()))
        while time.time() < deadline and not os.path.exists(release):
            time.sleep(0.05)
    result(session)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
