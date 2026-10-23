"""Bounded remote settings, applied at an idle boundary with durable receipts.

Only model and concurrency fields cross this channel. Credentials, commands,
paths and permission flags cannot be changed by a configuration operation.
"""
import copy
import re
import subprocess
import time
from pathlib import Path
from timetrace import config
from timetrace.routing import identity
from timetrace.adapters import build_adapters
from timetrace.adapters.codex import app_server_request


class ConfigFailure(Exception):
    pass


def effective(cfg, provider):
    from timetrace.cli import _max_parallel, _max_parallel_per_tool
    return {"provider": provider, "model": (cfg.get(provider) or {}).get("model") or "",
            "parallel": _max_parallel_per_tool(cfg)[provider], "max_parallel": _max_parallel(cfg)}


def validate_model(adapter, desired):
    if adapter.cfg.get("extra_args"):
        raise ConfigFailure("unsupported_profile")
    account, _ = identity(adapter)
    if not account:
        raise ConfigFailure("login_required")
    model = desired["model"]
    if desired["provider"] == "codex":
        try:
            names = set()
            cursor = None
            for _ in range(10):
                data = app_server_request(adapter.cfg.get("bin", "codex"), "model/list",
                                          {"limit": 100, **({"cursor": cursor} if cursor else {})})
                for row in data.get("data", []):
                    names.update(str(row[k]) for k in ("id", "model") if row.get(k))
                cursor = data.get("nextCursor")
                if not cursor:
                    break
            if not names:
                raise ValueError("empty catalog")
        except Exception:
            raise ConfigFailure("model_catalog_unavailable") from None
        if model not in names:
            raise ConfigFailure("model_unavailable")
    else:
        # Claude has no read-only account model catalog. Only allow the current
        # explicit ID or aliases advertised by the installed CLI itself.
        if model == adapter.cfg.get("model"):
            return
        try:
            help_text = subprocess.run([adapter.cfg.get("bin", "claude"), "--help"],
                                       capture_output=True, text=True, timeout=10, check=True).stdout
            section = help_text.partition("--model <model>")[2].split("\n  --", 1)[0]
            aliases = set(re.findall(r"'([A-Za-z][A-Za-z0-9_-]*)'", section))
        except Exception:
            raise ConfigFailure("model_catalog_unavailable") from None
        if model not in aliases:
            raise ConfigFailure("model_unavailable")


class RemoteConfig:
    def __init__(self, home, cfg, adapters, validator=validate_model):
        self.home, self.cfg, self.adapters = Path(home), cfg, adapters
        self.validator = validator
        self.next_poll = 0
        self.holding = False

    def __call__(self, agent, busy=False):
        if time.monotonic() < self.next_poll:
            return self.holding
        self.next_poll = time.monotonic() + 5
        try:
            view = agent.cloud.runner_config(agent.access_token())
        except Exception:
            # Existing tasks remain valid; server fences claims while pending.
            return self.holding
        op = view.get("latest")
        if not op or op.get("status") in ("applied", "failed"):
            self.holding = False
            return False
        self.holding = True
        revision = op.get("revision")
        try:
            if busy:
                agent.cloud.ack_runner_config(agent.access_token(), revision, "waiting_idle")
                return True
            self.apply(agent, op)
            # Publish runtime values before acknowledging: Valley compares its
            # canonical inventory and refuses a premature applied acknowledgement.
            agent._last_inventory_base = None
            agent._last_inventory = None
            agent.maintain(force=True)
            agent.cloud.ack_runner_config(agent.access_token(), revision, "applied")
            self.holding = False
        except ConfigFailure as exc:
            try:
                agent.cloud.ack_runner_config(agent.access_token(), revision, "failed", str(exc))
                self.holding = False
            except Exception:
                pass
        except Exception as exc:
            agent.log("remote configuration awaiting confirmation: %s" % type(exc).__name__)
        return self.holding

    def apply(self, agent, op):
        with config.locked(self.home):
            self._apply_locked(agent, op)

    def _apply_locked(self, agent, op):
        d = op.get("desired") or {}
        provider = d.get("provider")
        if (set(d) != {"provider", "model", "parallel", "max_parallel"}
                or provider not in ("codex", "claude")
                or not isinstance(d.get("model"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", d["model"])
                or any(type(d.get(k)) is not int or not 1 <= d[k] <= 8 for k in ("parallel", "max_parallel"))
                or type(op.get("revision")) is not int or op["revision"] < 1):
            raise ConfigFailure("invalid_config")
        try:
            user = config.load_user(self.home)
            current = config.load(self.home)
        except Exception:
            raise ConfigFailure("write_failed") from None
        receipt = user.get("_remote_configuration") or {}
        if receipt.get("runner_id") != op.get("runner_id"):
            receipt = {}
        if receipt.get("revision", 0) > op["revision"]:
            raise ConfigFailure("local_conflict")
        replay = receipt.get("revision") == op["revision"] and receipt.get("desired") == d
        actual = effective(current, provider)
        if actual != (d if replay else op.get("before")):
            raise ConfigFailure("local_conflict")
        adapter = build_adapters(current)[provider]
        account, _ = identity(adapter)
        if op.get("account") and account != op["account"]:
            raise ConfigFailure("local_conflict")
        if not replay:
            self.validator(adapter, d)
        updated = copy.deepcopy(user)
        updated.setdefault(provider, {})["model"] = d["model"]
        updated.setdefault("max_parallel_per_tool", {})[provider] = d["parallel"]
        updated["max_parallel"] = d["max_parallel"]
        updated["_remote_configuration"] = {"runner_id": op.get("runner_id"), "revision": op["revision"], "desired": d}
        # Prepare adapters first. No job is active while the runtime is swapped.
        effective_cfg = config._merge(config.DEFAULTS, updated)
        try:
            adapters = build_adapters(effective_cfg)
            if not replay:
                config._write_user(self.home, updated)
        except Exception:
            raise ConfigFailure("write_failed") from None
        self.cfg.clear()
        self.cfg.update(effective_cfg)
        self.adapters.clear()
        self.adapters.update(adapters)
        agent.max_parallel = d["max_parallel"]
        from timetrace.cli import _max_parallel_per_tool
        agent.max_parallel_per_tool = _max_parallel_per_tool(effective_cfg)
        agent._health_due = True
