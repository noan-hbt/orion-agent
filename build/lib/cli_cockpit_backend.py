"""Data provider for the cockpit CLI.

The backend deliberately uses duck typing: an application may expose any of
the Orion services (runtime, task store, memory store, ...), while absent
services are reported as unavailable rather than being represented by fake
values.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from cli_cockpit_actions import execute_action
from cli_cockpit_observability import collect_observability


class CockpitBackend:
    COMMANDS = ("status", "agents", "spawn", "kill", "graph", "tasks", "delegate",
                "watch", "memory", "events", "galaxy", "send", "tools", "skills",
                "model", "context", "cost", "autonomy", "approve", "log", "workspace", "doctor")

    def __init__(self, application: Any):
        self.application = application

    def _get(self, name: str, default: Any = None) -> Any:
        if isinstance(self.application, dict):
            return self.application.get(name, default)
        return getattr(self.application, name, default)

    @staticmethod
    def _plain(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "to_dict"):
            try: return value.to_dict()
            except Exception: pass
        if hasattr(value, "value") and not isinstance(value, (str, bytes)):
            return value.value
        if isinstance(value, (list, tuple, set)):
            return [CockpitBackend._plain(x) for x in value]
        if isinstance(value, dict):
            return {str(k): CockpitBackend._plain(v) for k, v in value.items()}
        return value

    def _service(self, name: str) -> Any:
        return self._get(name) or self._get(f"{name}_store") or self._get(f"{name}_manager")

    def snapshot(self) -> dict:
        """Return a stable, read-only overview for dashboard rendering."""
        observed = collect_observability(self.application)
        runtime = self._get("runtime")
        tasks = self._service("task")
        agents = self._service("subagent") or self._service("agent")
        result = {
            "runtime": {"state": self._plain(getattr(runtime, "state", None)),
                        "running": getattr(runtime, "running", None)} if runtime else None,
            "agents": self._list(agents), "tasks": self._list(tasks),
            "workspace": self._get("workspace"),
        }
        # Observability providers are authoritative for optional sections;
        # retain the compact runtime/task view for backwards compatibility.
        for key in ("timestamp", "model", "usage", "cost", "context", "events", "memory", "galaxy", "skills"):
            if observed.get(key) is not None:
                result[key] = observed[key]
        return result

    def commands(self) -> tuple[str, ...]:
        """Registry exposed to UI/help consumers."""
        return self.COMMANDS

    def _list(self, service: Any) -> Any:
        if service is None: return None
        for method in ("list", "all", "snapshot"):
            fn = getattr(service, method, None)
            if callable(fn):
                try: return self._plain(fn())
                except TypeError: continue
                except Exception as exc: return {"error": str(exc)}
        return self._plain(service)

    def execute(self, command: str) -> dict:
        raw = (command or "").strip()
        if raw.startswith("/"): raw = raw[1:]
        parts = raw.split(); name = parts[0].lower() if parts else "status"
        args = parts[1:]
        aliases = {"agent": "agents", "task": "tasks"}
        name = aliases.get(name, name)
        providers = {"status": self.snapshot, "agents": lambda: self._list(self._service("subagent") or self._service("agent")),
                     "tasks": lambda: self._list(self._service("task")),
                     "watch": lambda: self._read("event"),
                     "graph": lambda: self._graph(), "memory": lambda: self._read("memory"),
                     "events": lambda: self._read("event"), "galaxy": lambda: self._read("galaxy"),
                     "tools": lambda: self._read("tool"), "skills": lambda: self._read("skill"),
                     "model": lambda: self._read("model"), "context": lambda: self._read("context"),
                     "cost": lambda: self._read("cost"), "autonomy": lambda: self._read("autonomy"),
                     "approve": lambda: self._read("approval"), "log": lambda: self._read("log"),
                     "workspace": lambda: self._read("workspace"), "doctor": lambda: self._read("doctor")}
        if name in {"spawn", "kill", "delegate", "send"} or (name in {"approve", "autonomy", "workspace"} and args):
            return self._action(name, args)
        if name not in providers:
            return {"title": name, "data": None, "error": f"Commande inconnue: /{name}"}
        try:
            data = providers[name]()
            if data is None: return {"title": name, "data": None, "error": f"Provider indisponible: {name}"}
            return {"title": name, "data": self._plain(data)}
        except Exception as exc:
            return {"title": name, "data": None, "error": str(exc)}

    def _read(self, name: str) -> Any:
        service = self._service(name)
        if service is None: return None
        for method in ("snapshot", "status", "list", "get"):
            fn = getattr(service, method, None)
            if callable(fn):
                try: return fn()
                except TypeError: continue
        return service

    def _action(self, name: str, args: list[str]) -> dict:
        # Mutations are delegated to the explicit action layer, which checks
        # capabilities and never fabricates success.
        result = execute_action(self.application, name, args)
        out = {"title": name, "data": result.get("data")}
        if result.get("error"):
            out["error"] = result["error"]
        return out

    def _graph(self) -> Any:
        """Return provider graph, or agents with only declared relations."""
        direct = self._read("graph")
        if direct is not None:
            return direct
        agents = self._list(self._service("subagent") or self._service("agent"))
        if agents is None:
            return None
        # Preserve records as supplied; UI can render a flat graph when no
        # parent/child relation is actually present.
        return {"nodes": agents, "edges": [], "hierarchical": False}
