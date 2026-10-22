"""Local account/model proof for server-selected project policies.

Only an explicit model is reported. An implicit CLI default can change and
therefore cannot satisfy a model whitelist. Account IDs are full SHA-256
account fingerprints, never credentials.
"""
import re


def identity(adapter):
    try:
        provider = getattr(adapter, "name", "")
        key = adapter.routing_account_key()
        cfg = getattr(adapter, "cfg", {}) or {}
        model = cfg.get("model")
        # Extra CLI arguments can override --model or select a fallback.
        # Their semantics vary by provider; do not claim a strict model proof.
        if cfg.get("extra_args"):
            model = None
        if provider not in ("codex", "claude") or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            return "", ""
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,99}", model):
            model = ""
        return "account-" + provider + "-" + key, model
    except Exception:
        return "", ""


def problem(adapter, job):
    if job.get("routing_required") is not True:
        return ""
    account, model = identity(adapter)
    if not account or account != job.get("routing_account"):
        return "routing_account_changed"
    if not model or model != job.get("routing_model"):
        return "routing_model_changed"
    if job.get("tool_profile_id") != getattr(adapter, "name", "") + "-default":
        return "routing_profile_changed"
    return ""
