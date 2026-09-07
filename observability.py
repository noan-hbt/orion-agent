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
    result["ready"] = bool(result["ok"])
    return result
