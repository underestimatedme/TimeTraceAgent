#!/usr/bin/env python3
"""A fake `claude -p --input-format stream-json --output-format stream-json`.

It speaks the protocol as the installed Claude Code 2.1.285 does (confirmed
from the CLI's own zod schemas and the Agent SDK 0.3.156 host code):

- stdin, one JSON object per line:
  {"type":"user","session_id":"","message":{"role":"user","content":[{"type":"text","text":…}]},"parent_tool_use_id":null}
  {"type":"control_request","request_id":…,"request":{"subtype":"initialize"}}
  {"type":"control_response","response":{"subtype":"success","request_id":…,
      "response":{"behavior":"allow","updatedInput":{…},"toolUseID":…}
               |{"behavior":"deny","message":…,"toolUseID":…}}}
- stdout, one JSON object per line: system/init, assistant, rate_limit_event,
  result, and for a permission prompt
  {"type":"control_request","request_id":…,"request":{"subtype":"can_use_tool",
      "tool_name":…,"display_name":…,"input":{…},"tool_use_id":…,"permission_suggestions":[]}}
  (and {"type":"control_cancel_request","request_id":…} when it gives up on one).

Behaviour comes from the JSON file named by $TT_FAKE_SCENARIO:
  {"turns": [[step, …], …], "exit_code": 0}
Each user message runs the next turn (the last one repeats). Steps:
  {"permission": {"tool": "Bash", "input": {...}}}   ask the host, wait for its answer
  {"cancel_permission": {"tool": …, "input": …}}     ask, then cancel the request
  {"sleep": seconds}
  {"rate_limit": {...rate_limit_info...}}
  {"result": "text", "is_error": false, "subtype": "success"}
Everything received, the argv and each permission answer are appended to
$TT_FAKE_TRANSCRIPT as JSON lines. EOF on stdin ends the process once the
queued turns are done.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def record(obj):
    path = os.environ.get("TT_FAKE_TRANSCRIPT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")


def main():
    argv = sys.argv[1:]
    record({"argv": argv})
    with open(os.environ["TT_FAKE_SCENARIO"], encoding="utf-8") as fh:
        scenario = json.load(fh)
    session = None
    for flag in ("--session-id", "--resume"):
        if flag in argv:
            session = argv[argv.index(flag) + 1]
    session = session or str(uuid.uuid4())
    inbox = queue.Queue()
    answers = {}
    answered = threading.Condition()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            record({"received": msg})
            if msg.get("type") == "control_response":
                with answered:
                    answers[msg["response"]["request_id"]] = msg["response"]
                    answered.notify_all()
            elif msg.get("type") == "control_request" and msg["request"].get("subtype") == "initialize":
                emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": msg["request_id"], "response": {"commands": []}}})
            else:
                inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    emit({"type": "system", "subtype": "init", "session_id": session, "permissionMode": "default"})
    turns = scenario.get("turns") or [[{"result": "done"}]]
    turn_index = 0
    while True:
        msg = inbox.get()
        if msg is None:
            break
        if msg.get("type") != "user":
            continue
        steps = turns[min(turn_index, len(turns) - 1)]
        turn_index += 1
        for step in steps:
            if "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "permission" in step or "cancel_permission" in step:
                ask = step.get("permission") or step.get("cancel_permission")
                request_id = str(uuid.uuid4())
                tool_use_id = "toolu_" + uuid.uuid4().hex[:12]
                emit({"type": "control_request", "request_id": request_id, "request": {
                    "subtype": "can_use_tool", "tool_name": ask["tool"], "display_name": ask["tool"],
                    "input": ask["input"], "tool_use_id": tool_use_id, "permission_suggestions": []}})
                if "cancel_permission" in step:
                    time.sleep(float(ask.get("after", 0.3)))
                    emit({"type": "control_cancel_request", "request_id": request_id})
                    record({"cancelled": request_id})
                    continue
                with answered:
                    while request_id not in answers:
                        if not answered.wait(60):
                            record({"timeout": request_id})
                            sys.exit(3)
                reply = answers[request_id]
                record({"decision": {"tool": ask["tool"], "reply": reply}})
                emit({"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "permission %s" % reply.get("response", {}).get("behavior")}]}})
            elif "rate_limit" in step:
                emit({"type": "rate_limit_event", "session_id": session, "rate_limit_info": step["rate_limit"]})
            elif "result" in step:
                emit({"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
                    {"type": "text", "text": step["result"]}]}})
                emit({"type": "result", "subtype": step.get("subtype", "success"),
                      "is_error": bool(step.get("is_error", False)), "result": step["result"],
                      "session_id": session, "api_error_status": step.get("api_error_status")})
    sys.exit(int(scenario.get("exit_code", 0)))


if __name__ == "__main__":
    main()
