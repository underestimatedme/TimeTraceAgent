#!/usr/bin/env python3
"""A fake ACP v1 agent (what `gemini --acp` / `cursor-agent acp` are to the runner), for tests.

It speaks the protocol as the official schema (agentclientprotocol/agent-client-protocol, schema v1) describes it:
JSON-RPC 2.0, one object per line on stdin / stdout, logs on stderr. It answers `initialize`, `session/new`,
`session/load` (replaying history first), `session/resume` and `session/prompt`, takes the `session/cancel`
notification, and sends `session/update` notifications and `session/request_permission` / `fs/*` requests of its
own. It exits when its stdin closes (unless told to ignore that).

Behaviour comes from the JSON file named by $TT_ACP_SCENARIO:
  {"initialize": {"delay": s, "protocolVersion": 1, "loadSession": bool, "resume": bool, "mcpHttp": bool},
   "new_session": {"sessionId": "...", "error": {"code": ..., "message": ...}},
   "replay": [update, ...],        session/load sends these before it answers
   "turn": [step, ...],            what session/prompt does
   "garbage_first": bool, "ignore_eof": bool, "env_probe": [NAME, ...], "spawn_child": bool,
   "stop_reading_after": method,  after that request, stdin is never read again (the client's writes then block)
   "version": "0.60.0"}           what `--version` prints (the default when there is no scenario yet)
Steps:
  {"chunk": text} {"thought": text} {"update": {...session update...}} {"stderr": text} {"garbage": text}
  {"permission": {"toolCall": {...}, "options": [...]}}   ask, wait for the answer, record it
  {"request": {"method": ..., "params": {...}}}           any other agent -> client request (fs/*, terminal/*, ...)
  {"wait_cancel": seconds}       wait for session/cancel, then answer the prompt "cancelled"
  {"sleep": seconds} {"crash": exit_code}
  {"stop": stopReason}  {"error": {"code": ..., "message": ...}}   how the prompt is answered (default end_turn)
Everything received, the argv, the probed environment and every answer to the agent's own requests are appended to
$TT_ACP_TRANSCRIPT as JSON lines.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def record(obj):
    path = os.environ.get("TT_ACP_TRANSCRIPT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")


def version():
    """`--version`: print the scenario's version and exit, recording the environment it was asked with (in a file of
    its own, so a version check never looks like a started session)."""
    try:
        with open(os.environ["TT_ACP_SCENARIO"], encoding="utf-8") as fh:
            printed = json.load(fh).get("version", "0.60.0")
    except (KeyError, OSError, ValueError):
        printed = "0.60.0"
    path = os.environ.get("TT_ACP_TRANSCRIPT")
    if path:
        with open(path + ".version", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"env": sorted(os.environ), "cwd": os.getcwd()}) + "\n")
    print(printed)


def main():
    if sys.argv[1:] == ["--version"]:
        version()
        return
    with open(os.environ["TT_ACP_SCENARIO"], encoding="utf-8") as fh:
        scenario = json.load(fh)
    record({"argv": sys.argv[1:]})
    record({"env": {name: os.environ.get(name) for name in scenario.get("env_probe", [])}})
    if scenario.get("spawn_child"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record({"child": child.pid})
    if scenario.get("garbage_first"):
        sys.stdout.write("this is not JSON\n")
        sys.stdout.flush()

    incoming = queue.Queue()
    answers = {}
    answered = threading.Condition()
    cancelled = threading.Event()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            record({"received": msg})
            if "method" in msg:
                if msg["method"] == "session/cancel":
                    cancelled.set()
                incoming.put(msg)
                if msg["method"] == scenario.get("stop_reading_after"):
                    threading.Event().wait()   # never reads again: the pipe fills and the client's writes block
            else:
                with answered:
                    answers[msg.get("id")] = msg
                    answered.notify_all()
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()
    counter = [0]

    def ask(method, params):
        counter[0] += 1
        request_id = "agent-%d" % counter[0]
        emit({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        with answered:
            answered.wait_for(lambda: request_id in answers, timeout=30)
            answer = answers.get(request_id)
        record({"answer": {"method": method, "response": answer}})
        return answer

    def update(session, body):
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": body}})

    def respond(msg, result=None, error=None):
        out = {"jsonrpc": "2.0", "id": msg["id"]}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        emit(out)

    init = scenario.get("initialize") or {}
    session_id = (scenario.get("new_session") or {}).get("sessionId") or "fake-session-1"
    while True:
        msg = incoming.get()
        if msg is None:
            break
        method = msg.get("method")
        if "id" not in msg:
            continue  # notifications (session/cancel) are handled by the reader
        if method == "initialize":
            time.sleep(init.get("delay", 0))
            caps = {"loadSession": init.get("loadSession", False),
                    "mcpCapabilities": {"http": init.get("mcpHttp", False), "sse": False},
                    "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                    "sessionCapabilities": {"resume": {}} if init.get("resume") else {}}
            respond(msg, {"protocolVersion": init.get("protocolVersion", 1), "agentCapabilities": caps,
                          "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"},
                                          {"id": "gemini-api-key", "name": "Use Gemini API key"}]})
        elif method == "session/new":
            error = (scenario.get("new_session") or {}).get("error")
            if error:
                respond(msg, error=error)
            else:
                respond(msg, {"sessionId": session_id})
        elif method == "session/load":
            for body in scenario.get("replay") or []:
                update(msg["params"]["sessionId"], body)
            respond(msg, None)
        elif method == "session/resume":
            respond(msg, {})
        elif method == "session/prompt":
            session = msg["params"]["sessionId"]
            stop, error = "end_turn", None
            for step in scenario.get("turn") or []:
                if "chunk" in step:
                    update(session, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": step["chunk"]}})
                elif "thought" in step:
                    update(session, {"sessionUpdate": "agent_thought_chunk",
                                     "content": {"type": "text", "text": step["thought"]}})
                elif "update" in step:
                    update(session, step["update"])
                elif "stderr" in step:
                    sys.stderr.write(step["stderr"] + "\n")
                    sys.stderr.flush()
                elif "garbage" in step:
                    sys.stdout.write(step["garbage"] + "\n")
                    sys.stdout.flush()
                elif "permission" in step:
                    ask("session/request_permission", dict(step["permission"], sessionId=session))
                elif "request" in step:
                    ask(step["request"]["method"], step["request"].get("params") or {})
                elif "wait_cancel" in step:
                    if cancelled.wait(step["wait_cancel"]):
                        stop = "cancelled"
                        break
                elif "sleep" in step:
                    time.sleep(step["sleep"])
                elif "crash" in step:
                    os._exit(step["crash"])
                elif "stop" in step:
                    stop = step["stop"]
                elif "error" in step:
                    error = step["error"]
            if error is not None:
                respond(msg, error=error)
            else:
                respond(msg, {"stopReason": stop})
        else:
            respond(msg, error={"code": -32601, "message": "Method not found"})
    if scenario.get("ignore_eof"):
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
