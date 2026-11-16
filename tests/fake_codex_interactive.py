import json
import sys
from pathlib import Path

thread, turn = "old-thread", "native-turn"
transcript = {}


def reply(message):
    print(json.dumps(message), flush=True)


def totals(inputs, cached, written, output):
    return {"totalTokens": inputs + output, "inputTokens": inputs, "cachedInputTokens": cached,
            "cacheWriteInputTokens": written, "outputTokens": output, "reasoningOutputTokens": 0}


def token_usage(turn_id, total, last):
    reply({"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": turn_id,
           "tokenUsage": {"total": total, "last": last, "modelContextWindow": 258400}}})


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
        reply({"id": id_, "result": {"thread": {"id": thread}, "model": "gpt-fake-codex", "modelProvider": "openai"}})
        # What the resumed thread had saved, replayed to this connection for the turn it was recorded in.
        token_usage("earlier-turn", totals(1000, 600, 0, 50), totals(400, 200, 0, 20))
        continue
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
        # Running totals after each model call of this turn; the last one is sent twice (a rate-limit update resends it).
        token_usage(turn, totals(1500, 800, 0, 80), totals(500, 200, 0, 30))
        token_usage(turn, totals(2600, 1500, 10, 130), totals(1100, 700, 10, 50))
        token_usage(turn, totals(2600, 1500, 10, 130), totals(1100, 700, 10, 50))
        reply({"method": "turn/completed", "params": {"threadId": thread,
               "turn": {"id": turn, "status": "completed"}}})
        continue
    if id_ is not None:
        reply({"id": id_, "result": result})
