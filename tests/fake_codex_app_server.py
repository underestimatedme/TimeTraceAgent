#!/usr/bin/env python3
"""A fake `codex app-server` (JSON-RPC over stdio, codex-cli 0.155.1 schema).

Every request method received is appended to $TT_FAKE_TRANSCRIPT. The reply to
account/rateLimits/read depends on $TT_FAKE_MODE: ok | no_credits | error |
silent (never answers). Exits on stdin EOF."""
import json
import os
import sys

MODE = os.environ.get("TT_FAKE_MODE", "ok")
RESULT = {
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1788370200}},
    "rateLimitResetCredits": {"availableCount": 2, "credits": [
        {"id": "cred-1", "grantedAt": 1788000000, "expiresAt": 1789000000, "resetType": "codexRateLimits",
         "status": "available", "title": "Welcome reset", "description": None},
        {"id": "cred-2", "grantedAt": 1788100000, "expiresAt": None, "resetType": "codexRateLimits",
         "status": "brand-new-status", "description": "Promo"}]},
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "app-server":
        sys.exit(2)
    for line in sys.stdin:
        msg = json.loads(line)
        path = os.environ.get("TT_FAKE_TRANSCRIPT")
        if path:
            with open(path, "a") as fh:
                fh.write(json.dumps({"method": msg.get("method"), "id": msg.get("id")}) + "\n")
        if "id" not in msg:
            continue
        if msg.get("method") == "initialize":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"userAgent": "fake"}}
        elif MODE == "silent":
            continue
        elif MODE == "error":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "not logged in"}}
        elif MODE == "no_credits":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"rateLimits": RESULT["rateLimits"]}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": RESULT}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
