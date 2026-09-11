"""Structured, read-only runtime snapshot used by the cockpit.

This module intentionally uses duck typing: installations may expose only a
subset of Orion services.  Missing values are represented by ``None`` (or an
empty collection when a service actually provides one), never by invented
metrics.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _plain(value.to_dict())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _plain(value.value)
    return value


def _get(app: Any, name: str, default: Any = None) -> Any:
    return app.get(name, default) if isinstance(app, dict) else getattr(app, name, default)


def _service(app: Any, name: str) -> Any:
    return _get(app, name) or _get(app, name + "_store") or _get(app, name + "_manager")


def _read(value: Any) -> Any:
    if value is None:
        return None
    for method in ("snapshot", "status", "list", "all", "get"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return _plain(fn())
            except TypeError:
                continue
            except Exception:
                return None
    return _plain(value)


def _section(app: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = _read(_service(app, name))
        if value is not None:
            return value
    return None


def _action_ledger_snapshot(runtime: Any) -> Any:
    """Read only the ActionLedger's explicit safe snapshot contract.

    Unlike generic service discovery, this deliberately does not fall back to
    ``list``/``all``/``get`` because those APIs may expose raw action records,
    arguments, results, targets, or errors.
    """
    if runtime is None:
        return None
    ledger = _get(runtime, "action_ledger")
    if ledger is None:
        return None
    snapshot = getattr(ledger, "snapshot", None)
    if not callable(snapshot):
        return None
    try:
        return _plain(snapshot())
    except Exception:
        return None


def collect_observability(application: Any) -> dict[str, Any]:
    """Collect a stable snapshot from the supplied application object."""
    runtime = _get(application, "runtime")
    runtime_data = _read(runtime)
    if runtime is not None and runtime_data is runtime:
        runtime_data = None
    if runtime_data is None and runtime is not None:
        runtime_data = {"state": _plain(getattr(runtime, "state", None)),
                        "running": getattr(runtime, "running", None)}
    result = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "runtime": runtime_data,
        "model": _section(application, ("model", "router", "openrouter")),
        "usage": _section(application, ("usage", "metrics", "budget", "budgets")),
        "cost": _section(application, ("cost", "usage", "budget", "budgets")),
        "context": _section(application, ("context", "context_registry", "context_manager")),
        "agents": _section(application, ("subagent", "agent")),
        "tasks": _section(application, ("task", "tasks")),
        "events": _section(application, ("event", "events", "event_handler")),
        "memory": _section(application, ("memory", "memory_store")),
        "galaxy": _section(application, ("galaxy", "galaxy_store")),
        "skills": _section(application, ("skill", "skills", "tool_manager")),
        "actions": _action_ledger_snapshot(runtime),
    }
    return result


__all__ = ["collect_observability"]
