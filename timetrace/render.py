"""Plain-text tables for status / ls / events. No colours, no TUI."""
import time
from typing import Any, Dict, List, Optional


def _table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join("%%-%ds" % w for w in widths)
    out = [fmt % tuple(headers), fmt % tuple("-" * w for w in widths)]
    out += [fmt % tuple(r) for r in rows]
    return "\n".join(out)


def countdown(reset_at: Optional[int], now: int) -> str:
    if not reset_at:
        return "-"
    d = int(reset_at) - now
    if d <= 0:
        return "reset"
    h, m = divmod(d // 60, 60)
    if h >= 24:
        return "%dd%dh" % (h // 24, h % 24)
    return "%dh%02dm" % (h, m)


def local_time(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))


def age(ts: Optional[int], now: int) -> str:
    if not ts:
        return "-"
    d = max(0, now - int(ts))
    if d < 60:
        return "%ds ago" % d
    if d < 3600:
        return "%dm ago" % (d // 60)
    return "%dh%02dm ago" % (d // 3600, (d % 3600) // 60)


def status_table(rows: List[Dict[str, Any]], binding: Optional[Dict[str, Any]], now: int) -> str:
    if not rows:
        return ("no samples yet\n"
                "  codex : run `timetrace status` again (live read failed?)\n"
                "  claude: samples appear after the first task run; there is no on-demand read")
    body = []
    for r in rows:
        mark = "*" if binding and r["bucket_key"] == binding["bucket_key"] else " "
        rep = "rep" if r.get("is_representative") else ""
        body.append([
            mark, r["tool"], r["bucket_key"], "%5.1f%%" % r["remaining_pct"],
            "%5.1f%%" % r["used_pct"], countdown(r.get("reset_at"), now),
            local_time(r.get("reset_at")), age(r.get("at"), now), r.get("source", ""), rep,
        ])
    table = _table(["", "tool", "bucket", "left", "used", "resets in", "resets at", "sampled",
                    "src", ""], body)
    if binding:
        table += "\n\n* binding constraint: %s has %.1f%% left, resets in %s" % (
            binding["bucket_key"], binding["remaining_pct"], countdown(binding.get("reset_at"), now))
    table += "\n(numbers are planning hints, not permission to run: only a real block counts)"
    return table


def tasks_table(tasks: List[Dict[str, Any]], now: int) -> str:
    if not tasks:
        return "no tasks"
    body = []
    for t in tasks:
        extra = ""
        if t["state"] == "blocked" and t.get("blocked_until"):
            extra = "until %s" % local_time(t["blocked_until"])
        elif t["state"] == "pending" and t.get("depends_on"):
            extra = "after #%d" % t["depends_on"]
        elif t["state"] == "failed" and t.get("last_error"):
            extra = (t["last_error"] or "")[:40]
        elif t["state"] == "done" and t.get("branch"):
            extra = t["branch"]
        body.append([
            str(t["id"]), t["state"], (t.get("tool") or ("any" if t.get("any_tool") else "auto")),
            str(t.get("priority", 0)), "g%d" % (t.get("generation") or 0),
            _short(t["prompt"], 48), _short(t["repo"], 28), extra,
        ])
    return _table(["id", "state", "tool", "pri", "gen", "prompt", "repo", "note"], body)


def events_table(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "no events"
    body = []
    for e in events:
        payload = e.get("payload") or {}
        if isinstance(payload, dict):
            summary = " ".join("%s=%s" % (k, payload[k]) for k in sorted(payload))
        else:
            summary = str(payload)
        body.append([local_time(e["at"]), e["type"], e.get("tool") or "", _short(summary, 90)])
    return _table(["time", "type", "tool", "payload"], body)


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"
