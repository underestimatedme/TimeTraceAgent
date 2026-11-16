"""AI 用量统计: the tokens one model run used, as the terminal event of a dispatched job reports them.

Contract: docs/superpowers/specs/2026-11-16-token-usage-design.md (TimeTrace repository). Four counts - input (cache
reads not included), output, cache read, cache write - and the model name. Valley refuses a whole event whose usage has
a count above 1e12 or a model name over 64 characters, so every value made here is bounded; a run that reported nothing
(or only zeros) has no usage at all rather than an empty one. Numbers and a model name are not secret: no redaction.
"""
import math
from typing import Any, Dict, Optional

MODEL_CHARS = 64
MAX_COUNT = 10 ** 12
KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# The terminal events that carry what the run used (`waiting_quota`, `cancelled` and the rest do not).
EVENT_TYPES = ("completed", "failed", "interrupted_by_user")

# Codex reports a thread's running totals (input includes cached input), in these names: `codex exec --json` and the
# rollout files in snake case, the App Server in camel case.
CODEX_SNAKE = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
CODEX_CAMEL = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens")


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
    """At most MODEL_CHARS characters (and as many UTF-8 bytes, whichever way Valley counts)."""
    if not isinstance(value, str):
        return ""
    name = value.strip()[:MODEL_CHARS]
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


def codex_delta(end: Any, start: Any, model: Any, names=CODEX_SNAKE) -> Optional[Dict[str, Any]]:
    """What one Codex run used: the thread's running totals when the run ended minus those it had when it began (a
    resumed thread continues from its saved totals). Cached input is counted inside input, so it is taken out."""
    def raw(totals, key):
        return count(totals.get(key)) if isinstance(totals, dict) else 0
    inputs, cached, written, output = (max(0, raw(end, key) - raw(start, key)) for key in names)
    return make(model, max(0, inputs - cached), output, cached, written)


def attach(event: Dict[str, Any], result: Any) -> Dict[str, Any]:
    """`event["usage"]` for a terminal event of a run that reported some; the event is returned."""
    if event.get("type") in EVENT_TYPES:
        usage = clean(getattr(result, "usage", None))
        if usage is not None:
            event["usage"] = usage
    return event
