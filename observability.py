"""Lightweight structured observability primitives for Orion.

The module is dependency-free and deliberately does not record prompts,
messages, exception text, or environment values.  Applications can opt in to
JSON logs with ``ORION_LOG_JSON=1``; otherwise it remains a no-op logger.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections import Counter
from typing import Any, TextIO


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def collect_observability(application: Any) -> dict[str, Any]:
    """Collect safe operational state exposed by core runtime services.

    This deliberately avoids generic object serialization: a service must
    provide an explicit ``snapshot()`` contract before its data is surfaced.
    That prevents action arguments/results and other accidental payloads from
    leaking into observability output.
    """
    runtime = _get(application, "runtime")
    if runtime is None and _get(application, "action_ledger") is not None:
        runtime = application
    ledger = _get(runtime, "action_ledger") if runtime is not None else None
    ledger_snapshot = None
    snapshot = getattr(ledger, "snapshot", None)
    if callable(snapshot):
        try:
            ledger_snapshot = snapshot()
        except Exception:
            ledger_snapshot = {
                "component": "action_ledger",
                "available": False,
                "closed": bool(getattr(ledger, "_closed", False)),
            }
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action_ledger": ledger_snapshot,
    }


class Metrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.timings: dict[str, list[float]] = {}

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] += value

    def observe(self, name: str, seconds: float) -> None:
        self.timings.setdefault(name, []).append(round(float(seconds), 6))

    def snapshot(self) -> dict[str, Any]:
        return {"counters": dict(self.counters), "timings": {
            k: {"count": len(v), "total_seconds": round(sum(v), 6),
                "max_seconds": max(v) if v else 0.0} for k, v in self.timings.items()
        }}


class JsonLogger:
    def __init__(self, stream: TextIO | None = None, *, enabled: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.enabled = (os.getenv("ORION_LOG_JSON", "").lower() in {"1", "true", "yes"}) if enabled is None else enabled

    def log(self, level: str, event: str, *, correlation_id: str | None = None, **fields: Any) -> None:
        if not self.enabled:
            return
        safe = {k: v for k, v in fields.items() if k not in {"text", "prompt", "message", "payload", "content", "token", "secret"}}
        record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "event": event}
        if correlation_id:
            record["correlation_id"] = correlation_id
        record.update(safe)
        print(json.dumps(record, ensure_ascii=False, default=str), file=self.stream, flush=True)


def doctor(config_path: str | os.PathLike[str] = "orion.toml") -> dict[str, Any]:
    """Validate configuration without starting network clients or workers."""
    try:
        from orion_config import OrionConfig
        config = OrionConfig.from_file(config_path)
        return {"ok": True, "config": os.fspath(config_path), "channels": list(config.channels.enabled)}
    except Exception as exc:
        return {"ok": False, "config": os.fspath(config_path), "error_type": type(exc).__name__}


def health(config_path: str | os.PathLike[str] = "orion.toml") -> dict[str, Any]:
    result = doctor(config_path)
    result["status"] = "ok" if result["ok"] else "error"
    return result


def readiness(config_path: str | os.PathLike[str] = "orion.toml") -> dict[str, Any]:
    result = doctor(config_path)
    if not result["ok"]:
        result["ready"] = False
        return result

    from orion_config import OrionConfig

    config = OrionConfig.from_file(config_path)
    failures: list[dict[str, str]] = []

    api_key_env = config.llm.api_key_env.strip()
    if not os.getenv(api_key_env):
        failures.append({
            "code": "missing_env",
            "name": api_key_env,
            "component": "llm",
        })

    core_path = config.path(config.prompt.core_path)
    if not os.path.isfile(core_path):
        failures.append({
            "code": "missing_file",
            "path": core_path,
            "component": "prompt_core",
        })

    if config.reflection.enabled:
        reflection_path = config.path(config.reflection.prompt_path)
        if not os.path.isfile(reflection_path):
            failures.append({
                "code": "missing_file",
                "path": reflection_path,
                "component": "reflection",
            })

    result["ready"] = not failures
    if failures:
        result["failures"] = failures
    return result
