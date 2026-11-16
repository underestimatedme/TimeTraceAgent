import json
import sys
import uuid


def reply(message):
    print(json.dumps(message), flush=True)


args = sys.argv[1:]
assert "--replay-user-messages" in args
assert "--strict-mcp-config" in args
assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
session_id = args[args.index("--resume") + 1]
messages = 0
for line in sys.stdin:
    data = json.loads(line)
    if data.get("type") != "user":
        continue
    uuid.UUID(data["uuid"])
    messages += 1
    reply({"type": "user", "uuid": str(uuid.uuid4()), "isReplay": True, "session_id": session_id})
    if messages == 1:
        reply({"type": "system", "subtype": "init", "session_id": session_id})
        reply({"type": "user", "uuid": data["uuid"], "isReplay": True, "session_id": session_id})
        continue
    reply({"type": "assistant", "session_id": session_id, "parent_tool_use_id": None,
           "message": {"content": [{"type": "thinking", "thinking": "Do not upload"},
                                   {"type": "text", "text": "Public reply"}]}})
    reply({"type": "result", "subtype": "success", "session_id": session_id, "result": "Public reply"})
    reply({"type": "command_lifecycle", "session_id": session_id})
