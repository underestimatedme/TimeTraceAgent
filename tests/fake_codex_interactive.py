import json
import sys
from pathlib import Path

thread, turn = "old-thread", "native-turn"
transcript = {}


def reply(message):
    print(json.dumps(message), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method, id_, params = message.get("method"), message.get("id"), message.get("params", {})
    result = {}
    if method == "initialize":
        result = {"userAgent": "fake"}
    elif method == "initialized":
        continue
    elif method == "thread/resume":
        transcript["resume"] = params
        result = {"thread": {"id": thread}}
    elif method == "turn/start":
        transcript["start"] = params
        result = {"turn": {"id": turn, "status": "inProgress"}}
    elif method == "turn/steer":
        transcript["steer"] = params
        reply({"id": id_, "result": {"turnId": turn}})
        reply({"id": "permission-1", "method": "item/commandExecution/requestApproval",
               "params": {"threadId": thread, "turnId": turn, "itemId": "command-1",
                          "command": "touch marker", "cwd": str(Path.cwd()), "startedAtMs": 1}})
        continue
    elif method == "turn/interrupt":
        Path("interrupt.json").write_text(json.dumps(params))
        reply({"id": id_, "result": {}})
        reply({"method": "turn/completed", "params": {"threadId": thread,
               "turn": {"id": turn, "status": "interrupted"}}})
        continue
    elif id_ == "permission-1":
        transcript["approval"] = message
        Path("transcript.json").write_text(json.dumps(transcript))
        reply({"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
               "item": {"type": "reasoning", "id": "reason-1", "content": ["private reasoning"]}}})
        reply({"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
               "item": {"type": "agentMessage", "id": "message-1", "text": "User-facing answer"}}})
        reply({"method": "turn/completed", "params": {"threadId": thread,
               "turn": {"id": turn, "status": "completed"}}})
        continue
    if id_ is not None:
        reply({"id": id_, "result": result})
