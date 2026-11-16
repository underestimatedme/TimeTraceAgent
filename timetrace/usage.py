"""AI 用量统计: the tokens one model run used, as the terminal event of a dispatched job reports them.

Contract: docs/superpowers/specs/2026-11-16-token-usage-design.md (TimeTrace repository). Four counts - input (cache
reads not included), output, cache read, cache write - and the model name. Valley refuses a whole event whose usage has
a count above 1e12 or a model name over 64 characters, so every value made here is bounded; a run that reported nothing
(or only zeros) has no usage at all rather than an empty one. Numbers and a model name are not secret: no redaction.
"""
import math
from typing import Any, Dict, List, Optional, Tuple

MODEL_CHARS = 64
MAX_COUNT = 10 ** 12
KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# Every event that ends an attempt carries what its run used: a run paused for quota or stopped part way has used
# tokens too (and a resumed Codex thread's next run counts only what it adds).
EVENT_TYPES = ("completed", "failed", "interrupted_by_user", "waiting_quota", "cancelled", "waiting_input",
               "waiting_local_auth")

# Codex reports a thread's running totals (input includes cached input), in these names: `codex exec --json` and the
# rollout files in snake case, the App Server in camel case.
CODEX_SNAKE = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
CODEX_CAMEL = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens")
# A thread's running totals as (input including cached, cached, cache write, output).
Totals = Tuple[int, int, int, int]
ZERO: Totals = (0, 0, 0, 0)


def count(value: Any) -> int:
    """A token count as reported, or 0 for anything that is not a finite non-negative number; at most MAX_COUNT."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        return 0
    return min(value, MAX_COUNT)


def model_name(value: Any) -> str:
    """At most MODEL_CHARS characters (and as many UTF-8 bytes, whichever way Valley counts). Code points UTF-8
    cannot carry (a lone surrogate from JSON's "\\ud800") are dropped."""
    if not isinstance(value, str):
        return ""
    name = value.encode("utf-8", "ignore").decode("utf-8").strip()[:MODEL_CHARS]
    while len(name.encode("utf-8")) > MODEL_CHARS:
        name = name[:-1]
    return name


def make(model: Any, input_tokens: Any = 0, output_tokens: Any = 0, cache_read_tokens: Any = 0,
         cache_write_tokens: Any = 0) -> Optional[Dict[str, Any]]:
    counts = [count(v) for v in (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)]
    if not any(counts):
        return None
    return dict(zip(KEYS, counts), model=model_name(model))


def clean(usage: Any) -> Optional[Dict[str, Any]]:
    """A usage dict in the contract's shape and bounds, or None."""
    if not isinstance(usage, dict):
        return None
    return make(usage.get("model"), *(usage.get(key) for key in KEYS))


def merge(first: Any, second: Any) -> Optional[Dict[str, Any]]:
    """Two runs of one job (an instruction appended after the run ended is a resumed run): counts added, the later
    run's model unless it named none."""
    a, b = clean(first), clean(second)
    if a is None or b is None:
        return a or b
    return make(b["model"] or a["model"], *(a[key] + b[key] for key in KEYS))


def codex_totals(totals: Any, names=CODEX_SNAKE) -> Optional[Totals]:
    """A Codex running total, only when every count is a whole number in range (cache writes may be absent: older
    Codex); None for anything else. Never a guess: a total that is not certain is no total."""
    if not isinstance(totals, dict):
        return None
    counts = []
    for i, key in enumerate(names):
        value = totals.get(key, 0) if i == 2 else totals.get(key)
        if type(value) is not int or value < 0 or value > MAX_COUNT:
            return None
        counts.append(value)
    return tuple(counts)


def codex_used(end: Optional[Totals], start: Optional[Totals], model: Any) -> Optional[Dict[str, Any]]:
    """What one Codex run used: the thread's running totals when it ended minus those it had when it began (a resumed
    thread continues from its saved totals). Cached input is counted inside input, so it is taken out. None when either
    end is unknown or they do not fit together (a total that went down, more cached than input)."""
    if end is None or start is None:
        return None
    inputs, cached, written, output = (e - s for e, s in zip(end, start))
    if min(inputs, cached, written, output) < 0 or cached > inputs:
        return None
    return make(model, inputs - cached, output, cached, written)


def grows(earlier: Totals, later: Totals) -> bool:
    """Running totals never go down."""
    return all(b >= a for a, b in zip(earlier, later))


def codex_run_used(start: Optional[Totals], reported: List[Optional[Totals]], model: Any) -> Optional[Dict[str, Any]]:
    """What one Codex run used from `start` (the thread's totals before it) and every running total it reported, in
    order: the last one minus `start`. None when `start` is unknown, when the last report (or any) has no whole total,
    or when any total is below the one before it - `start` included - in any field."""
    if start is None or not reported or None in reported:
        return None
    chain = [start] + list(reported)
    if not all(grows(a, b) for a, b in zip(chain, chain[1:])):
        return None
    return codex_used(reported[-1], start, model)


def attach(event: Dict[str, Any], result: Any) -> Dict[str, Any]:
    """`event["usage"]` for a terminal event of a run that reported some; the event is returned."""
    if event.get("type") in EVENT_TYPES:
        usage = clean(getattr(result, "usage", None))
        if usage is not None:
            event["usage"] = usage
    return event
