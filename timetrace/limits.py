"""Bucket samples in, remaining-quota view out (spec §4, v0.1 status)."""
import time
from typing import Any, Dict, List, Optional

from timetrace.db import Database
from timetrace.models import EV_RATE_LIMIT, Sample


def record_samples(db: Database, samples: List[Sample], at: int) -> None:
    """Persist samples; emit a rate_limit event for every exhausted bucket."""
    for s in samples:
        db.add_sample(s, at=at)
        if s.used_pct >= 100:
            bucket = db.get_bucket(s.bucket_key)
            db.add_event(
                EV_RATE_LIMIT,
                tool=s.tool,
                bucket_id=bucket["id"] if bucket else None,
                payload={"bucket_key": s.bucket_key, "used_pct": s.used_pct,
                         "reset_at": s.reset_at, "source": s.source},
                at=at,
            )


def snapshot(db: Database) -> List[Dict[str, Any]]:
    rows = db.latest_samples()
    for r in rows:
        r["remaining_pct"] = max(0.0, 100.0 - float(r["used_pct"]))
        r["is_representative"] = bool(r["is_representative"])
    return rows


def binding(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The bucket closest to zero; representative buckets win ties."""
    if not rows:
        return None
    return min(rows, key=lambda r: (r["remaining_pct"], 0 if r["is_representative"] else 1))


def tool_rows(rows: List[Dict[str, Any]], tool: str) -> List[Dict[str, Any]]:
    return [r for r in rows if r["tool"] == tool]


def tool_exhausted(rows: List[Dict[str, Any]], tool: str, now: Optional[int] = None) -> bool:
    """Only a bucket reading 100% blocks dispatch (spec §6 step 6).

    A 100% reading whose window has already reset is stale, not exhausted: Claude has
    no on-demand read, so without this the tool would stay "exhausted" until someone
    happens to produce a new sample.
    """
    now = now or int(time.time())
    for r in tool_rows(rows, tool):
        if float(r["used_pct"]) < 100:
            continue
        reset_at = r.get("reset_at")
        if reset_at is None or int(reset_at) > now:
            return True
    return False


def tool_min_remaining(rows: List[Dict[str, Any]], tool: str) -> Optional[float]:
    trs = tool_rows(rows, tool)
    if not trs:
        return None
    return min(r["remaining_pct"] for r in trs)
