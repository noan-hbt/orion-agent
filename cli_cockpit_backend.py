"""Data provider for the cockpit CLI.

The backend deliberately uses duck typing: an application may expose any of
the Orion services (runtime, task store, memory store, ...), while absent
services are reported as unavailable rather than being represented by fake
values.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
import shlex
from typing import Any, Mapping

from cli_cockpit_actions import execute_action
from cli_cockpit_observability import collect_observability


class CockpitBackend:
    # This is the compatibility superset understood by ``execute``.  User-facing
    # help is generated dynamically and only advertises capabilities that the
    # supplied application can actually fulfil.
    COMMANDS = (
        "help", "commands", "status", "dashboard", "agents", "spawn", "kill",
        "graph", "tasks", "jobs", "delegate", "watch", "memory", "events", "galaxy",
        "send", "tools", "skills", "model", "context", "cost", "autonomy",
        "approve", "reject", "log", "workspace", "doctor",
    )

    COMMAND_HELP = {
        "help": ("/help [command]", "Affiche les commandes réellement disponibles."),
        "commands": ("/commands", "Alias de /help."),
        "status": ("/status", "État synthétique du runtime et des services."),
        "dashboard": ("/dashboard", "Même snapshot que /status; rendu cockpit côté CLI."),
        "agents": ("/agents", "Liste les sous-agents exposés."),
        "spawn": ("/spawn <name>", "Crée explicitement un sous-agent."),
        "kill": ("/kill <agent_id>", "Supprime explicitement un sous-agent."),
        "graph": ("/graph", "Affiche les relations d'agents disponibles."),
        "tasks": ("/tasks", "Liste les tâches du runtime."),
        "jobs": ("/jobs", "Liste les travaux de sous-agents récents."),
        "delegate": ("/delegate [agent_id] <objective>", "Délègue une tâche à un sous-agent."),
        "watch": ("/watch", "État opérationnel sûr de la file d'événements."),
        "events": ("/events", "État opérationnel sûr de la file d'événements."),
        "memory": ("/memory", "Liste un échantillon borné de la mémoire active."),
        "galaxy": ("/galaxy", "État Galaxy si le service est chargé."),
        "send": ("/send <recipient> <message>", "Envoie un message via TeamBus/sous-agents."),
        "tools": ("/tools", "Tools réellement appelables avec source, état et classification."),
        "skills": ("/skills", "Métadonnées publiques des skills installés."),
        "model": ("/model", "Route modèle active, sans clé ni header secret."),
        "context": ("/context", "État du registre/contexte si exposé."),
        "cost": ("/cost", "Usage/coût si un ledger est exposé."),
        "autonomy": ("/autonomy [level]", "Lit ou modifie l'autonomie si le service existe."),
        "approve": ("/approve [approval_id]", "Liste les approvals en attente ou en approuve un."),
        "reject": ("/reject <approval_id> [reason]", "Rejette explicitement une approval."),
        "log": ("/log", "Vue logs si un provider sûr est exposé."),
        "workspace": ("/workspace [name]", "Lit ou modifie le workspace si exposé."),
        "doctor": ("/doctor", "Diagnostic local sûr des composants et de la configuration."),
    }

    def __init__(self, application: Any):
        self.application = application

    def _get(self, name: str, default: Any = None) -> Any:
        if isinstance(self.application, dict):
            return self.application.get(name, default)
        return getattr(self.application, name, default)

    @staticmethod
    def _plain(value: Any) -> Any:
        if is_dataclass(value):
            return CockpitBackend._plain(asdict(value))
        if hasattr(value, "to_dict"):
            try:
                return CockpitBackend._plain(value.to_dict())
            except Exception:
                pass
        # ``Enum`` before the ``str`` bail-out: a str-valued enum is an instance
        # of str, so it used to pass through here unchanged and rendered as
        # "RuntimeState.EVALUATING" wherever the value was interpolated.
        if isinstance(value, Enum):
            return CockpitBackend._plain(value.value)
        if hasattr(value, "value") and not isinstance(value, (str, bytes)):
            return value.value
        if isinstance(value, (list, tuple, set)):
            return [CockpitBackend._plain(x) for x in value]
        if isinstance(value, dict):
            return {str(k): CockpitBackend._plain(v) for k, v in value.items()}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Decimal):
            return str(value)
        return value

    def _service(self, name: str) -> Any:
        aliases = {
            "task": ("task", "task_store", "task_manager"),
            "subagent": ("subagent", "subagents", "subagent_manager", "agent_manager"),
            "agent": ("agent", "subagents", "subagent_manager", "agent_manager"),
            "tool": ("tool", "tool_manager"),
            "skill": ("skill", "skills", "tool_manager"),
            "approval": ("approval", "approvals", "approval_store"),
            "event": ("event", "events", "event_handler"),
            "model": ("model", "llm", "router", "openrouter"),
            "memory": ("memory", "memory_store", "retrieval_store"),
            "context": ("context", "context_registry", "context_manager"),
            "usage": ("usage", "usage_ledger", "metrics", "budget", "budgets"),
            "cost": ("cost", "usage_ledger", "usage", "budget", "budgets"),
        }
        for candidate in aliases.get(
            name,
            (name, f"{name}_store", f"{name}_manager"),
        ):
            value = self._get(candidate)
            if value is not None:
                return value
        return None

    def _memory_service(self) -> Any:
        service = self._service("memory")
        if service is not None:
            return service
        runtime = self._get("runtime")
        if runtime is None:
            return None
        direct = getattr(runtime, "retrieval_store", None)
        if direct is not None:
            return direct
        assembler = getattr(runtime, "context_assembler", None)
        return getattr(assembler, "memory_store", None) if assembler is not None else None

    def _has_capability(self, name: str) -> bool:
        manager = self._service("subagent") or self._service("agent")
        if name in {"help", "commands", "status", "dashboard", "doctor"}:
            return True
        if name == "agents":
            return manager is not None
        if name == "spawn":
            return callable(getattr(manager, "create_agent", None))
        if name == "kill":
            return callable(getattr(manager, "delete_agent", None))
        if name == "delegate":
            return callable(getattr(manager, "submit", None))
        if name == "send":
            teams = self._get("team_bus") or self._get("teams")
            return callable(getattr(teams, "send", None)) or callable(getattr(manager, "send_message", None))
        if name == "graph":
            return self._service("graph") is not None or manager is not None
        if name == "tasks":
            return self._service("task") is not None
        if name == "jobs":
            return callable(getattr(manager, "list_jobs", None))
        if name in {"watch", "events"}:
            return self._service("event") is not None
        if name == "memory":
            if self._memory_service() is not None:
                return True
            runtime = self._get("runtime")
            return callable(getattr(getattr(runtime, "prompt_store", None), "snapshot", None))
        if name == "tools":
            runtime = self._get("runtime")
            if callable(getattr(runtime, "_tool_definitions", None)):
                return True
            service = self._service("tool")
            return service is not None and callable(getattr(service, "installed", None))
        if name == "skills":
            service = self._service("tool")
            return service is not None and callable(getattr(service, "installed", None))
        if name == "approve":
            service = self._service("approval")
            return service is not None and callable(getattr(service, "pending", None))
        if name == "reject":
            return callable(getattr(self._service("approval"), "reject", None))
        if name == "workspace":
            return self._get("workspace") is not None or callable(self._get("set_workspace"))
        if name == "autonomy":
            return self._service("autonomy") is not None or callable(self._get("set_autonomy"))
        return self._service(name) is not None

    def snapshot(self) -> dict:
        """Return a stable, read-only overview for dashboard rendering."""
        observed = collect_observability(self.application)
        runtime = self._get("runtime")
        result = {
            "runtime": {"state": self._plain(getattr(runtime, "state", None)),
                        "running": getattr(runtime, "running", None)} if runtime else None,
            "agents": self._agents_projection(),
            "tasks": self._tasks_projection(),
            "workspace": self._workspace_projection(),
        }
        # Keep only observability fields with an explicit safe contract. Generic
        # service objects are never copied into the dashboard: their ``repr`` may
        # contain configuration or other operator-only data.
        if observed.get("timestamp") is not None:
            result["timestamp"] = observed["timestamp"]
        if observed.get("actions") is not None:
            result["actions"] = observed["actions"]
        for key, service_name in {
            "usage": "usage",
            "cost": "cost",
        }.items():
            value = self._read(service_name)
            if value is not None:
                result[key] = self._plain(value)
        context = self._context_projection()
        if context is not None:
            result["context"] = context
        for key, projection in (
            ("model", self._model_projection),
            ("events", self._event_projection),
            ("skills", self._skills_projection),
            ("approvals", self._pending_approvals),
        ):
            value = projection()
            if value is not None:
                result[key] = self._plain(value)
        return result

    def commands(self) -> tuple[str, ...]:
        """Stable command surface; ``/help`` reports per-instance availability."""
        return self.COMMANDS

    def _list(self, service: Any) -> Any:
        if service is None:
            return None
        for method in ("list", "all", "snapshot"):
            fn = getattr(service, method, None)
            if callable(fn):
                try:
                    return self._plain(fn())
                except TypeError:
                    continue
                except Exception as exc:
                    return {"error": str(exc)}
        return self._plain(service)

    @staticmethod
    def _public_field(value: Any, name: str, default: Any = None) -> Any:
        return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)

    @staticmethod
    def _bounded_text(value: Any, limit: int = 240) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"

    def _tasks_projection(self) -> Any:
        """Return public task summaries, never task state/results/history."""
        service = self._service("task")
        if service is None:
            return None
        listing = getattr(service, "list", None)
        if not callable(listing):
            return None
        values = listing()
        rows = []
        for item in values or ():
            status = self._public_field(item, "status")
            if hasattr(status, "value"):
                status = status.value
            row = {
                "id": self._plain(self._public_field(item, "id")),
                "status": self._plain(status),
            }
            objective = self._public_field(item, "objective", None)
            if objective is not None:
                row["objective"] = self._bounded_text(objective)
            priority = self._public_field(item, "priority", None)
            if priority is not None:
                row["priority"] = self._plain(priority)
            waiting = self._public_field(item, "waiting_for", None)
            if isinstance(waiting, (list, tuple, set)):
                row["waiting_count"] = len(waiting)
            updated_at = self._public_field(item, "updated_at", None)
            if updated_at is not None:
                row["updated_at"] = self._plain(updated_at)
            rows.append(row)
        return rows

    def _agents_projection(self) -> Any:
        """Return public sub-agent summaries without system prompts or job context."""
        service = self._service("subagent") or self._service("agent")
        if service is None:
            return None
        listing = getattr(service, "list_agents", None) or getattr(service, "list", None)
        if not callable(listing):
            return None
        values = listing()
        rows = []
        for item in values or ():
            status = self._public_field(item, "status")
            if hasattr(status, "value"):
                status = status.value
            capabilities = self._public_field(item, "capabilities", ())
            allowed_tools = self._public_field(item, "allowed_tools", ())
            rows.append(
                {
                    "id": self._plain(self._public_field(item, "id")),
                    "name": self._bounded_text(self._public_field(item, "name"), 120),
                    "model": self._bounded_text(self._public_field(item, "model"), 160),
                    "status": self._plain(status),
                    "capability_count": len(capabilities) if isinstance(capabilities, (list, tuple, set)) else 0,
                    "tool_count": len(allowed_tools) if isinstance(allowed_tools, (list, tuple, set)) else 0,
                }
            )
        return rows

    def _jobs_projection(self) -> Any:
        """Return compact delegated-job summaries without result/context payloads."""
        service = self._service("subagent") or self._service("agent")
        listing = getattr(service, "list_jobs", None) if service is not None else None
        if not callable(listing):
            return None
        rows = []
        for item in listing() or ():
            status = self._public_field(item, "status")
            if hasattr(status, "value"):
                status = status.value
            row = {
                "id": self._plain(self._public_field(item, "id")),
                "status": self._plain(status),
            }
            for key, limit in (("agent_id", 100), ("objective", 240)):
                value = self._public_field(item, key, None)
                if value is not None:
                    row[key] = self._bounded_text(value, limit)
            priority = self._public_field(item, "priority", None)
            if priority is not None:
                row["priority"] = self._plain(priority)
            updated_at = self._public_field(item, "updated_at", None)
            if updated_at is not None:
                row["updated_at"] = self._plain(updated_at)
            rows.append(row)
        return rows

    def _context_projection(self) -> Any:
        """Expose Context OS structure only; registry payload ``data`` stays private."""
        service = self._service("context")
        if service is None:
            return None
        snapshot = getattr(service, "snapshot", None)
        if not callable(snapshot):
            return None
        try:
            value = snapshot(limit=100)
        except TypeError:
            value = snapshot()
        if not isinstance(value, Mapping):
            return None
        counts: dict[str, int] = {}
        for key in ("principals", "threads", "intents", "bindings"):
            items = value.get(key)
            if isinstance(items, (list, tuple, set)):
                counts[key] = len(items)
        # An empty registry is still a valid/available context service.  Keep
        # it distinct from ``None`` so the command renderer can show a proper
        # empty state instead of claiming that the provider is unavailable.
        return counts

    def _workspace_projection(self) -> Any:
        """Return a workspace label/id without serializing arbitrary objects."""
        value = self._get("workspace")
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return value.name
        result: dict[str, Any] = {}
        for key in ("id", "name", "label"):
            item = self._public_field(value, key)
            if isinstance(item, (str, int, float, bool)):
                result[key] = self._bounded_text(item, 160) if isinstance(item, str) else item
        return result or None

    def execute(self, command: str) -> dict:
        raw = (command or "").strip()
        if raw.startswith("/"):
            raw = raw[1:]
        try:
            parts = shlex.split(raw, posix=True, comments=False)
        except ValueError as exc:
            return {"title": "command", "data": None, "error": f"Commande mal formée: {exc}"}
        name = parts[0].lower() if parts else "status"
        args = parts[1:]
        aliases = {"agent": "agents", "task": "tasks", "commands": "commands"}
        name = aliases.get(name, name)
        if name in {"help", "commands"}:
            target = args[0].lstrip("/").lower() if args else None
            data = self._help_projection(target)
            if target is not None and data is None:
                return {"title": name, "data": None, "error": f"Commande inconnue ou indisponible: /{target}"}
            return {"title": name, "data": data, "display": self._format_help(data)}
        providers = {"status": self.snapshot, "agents": self._agents_projection,
                     "tasks": self._tasks_projection,
                     "jobs": self._jobs_projection,
                     "watch": self._event_projection,
                     "dashboard": self.snapshot,
                     "graph": lambda: self._graph(), "memory": self._memory_projection,
                     "events": self._event_projection, "galaxy": lambda: self._read("galaxy"),
                     "tools": self._tools_projection, "skills": self._skills_projection,
                     "model": self._model_projection, "context": self._context_projection,
                     "cost": lambda: self._read("cost"), "autonomy": lambda: self._read("autonomy"),
                     "approve": self._pending_approvals, "log": lambda: self._read("log"),
                     "workspace": self._workspace_projection, "doctor": self._doctor_projection,
                     "cancel": self._cancel_projection}
        if name in {"spawn", "kill", "delegate", "send", "reject"} or (name in {"approve", "autonomy", "workspace"} and args):
            return self._action(name, args)
        if name not in providers:
            return {"title": name, "data": None, "error": f"Commande inconnue: /{name}"}
        try:
            data = providers[name]()
            if data is None:
                return {
                    "title": name,
                    "data": None,
                    "error": f"Provider indisponible: {name}",
                }
            plain = self._plain(data)
            return {
                "title": name,
                "data": plain,
                "display": self._format_command_data(name, plain),
            }
        except Exception as exc:
            return {
                "title": name,
                "data": None,
                "error": self._bounded_text(exc, 320) or type(exc).__name__,
            }

    @staticmethod
    def _yes_no(value: Any) -> str:
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "-" if value is None else str(value)

    @staticmethod
    def _display_scalar(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def _format_rows(self, rows: Any, *, kind: str) -> str:
        if not isinstance(rows, list) or not rows:
            labels = {
                "tasks": "Aucune tâche.",
                "agents": "Aucun sous-agent.",
                "jobs": "Aucun travail délégué.",
                "tools": "Aucun tool appelable ou installé.",
                "memory": "Aucune mémoire disponible.",
            }
            return labels.get(kind, "Aucun élément.")
        lines: list[str] = []
        for index, item in enumerate(rows, 1):
            if not isinstance(item, Mapping):
                lines.append(f"{index:>2}. {self._bounded_text(item, 220)}")
                continue
            if kind == "tasks":
                identifier = self._display_scalar(item.get("id"))
                status = self._display_scalar(item.get("status"))
                objective = self._bounded_text(item.get("objective") or "Sans objectif", 180)
                lines.append(f"{index:>2}. #{identifier} [{status}] {objective}")
                details = []
                if item.get("priority") is not None:
                    details.append(f"priority={item['priority']}")
                if item.get("waiting_count"):
                    details.append(f"waiting={item['waiting_count']}")
                if details:
                    lines.append("    " + " | ".join(details))
            elif kind == "agents":
                name = self._bounded_text(item.get("name") or item.get("id") or "agent", 80)
                status = self._display_scalar(item.get("status"))
                model = self._bounded_text(item.get("model") or "-", 80)
                lines.append(f"{index:>2}. {name} [{status}]")
                lines.append(
                    "    "
                    + " | ".join(
                        (
                            f"id={self._display_scalar(item.get('id'))}",
                            f"model={model}",
                            f"tools={self._display_scalar(item.get('tool_count'))}",
                            f"capabilities={self._display_scalar(item.get('capability_count'))}",
                        )
                    )
                )
            elif kind == "jobs":
                identifier = self._display_scalar(item.get("id"))
                status = self._display_scalar(item.get("status"))
                objective = self._bounded_text(item.get("objective") or "Sans objectif", 180)
                lines.append(f"{index:>2}. {identifier} [{status}] {objective}")
                if item.get("agent_id") is not None:
                    lines.append(f"    agent={self._bounded_text(item['agent_id'], 100)}")
            elif kind == "tools":
                name = self._bounded_text(item.get("name") or item.get("id") or "tool", 90)
                status = self._display_scalar(item.get("status"))
                source = self._display_scalar(item.get("source") or item.get("package"))
                classification = self._display_scalar(item.get("classification"))
                lines.append(f"{index:>2}. {name} [{status}]")
                lines.append(f"    source={source} | class={classification}")
                description = self._bounded_text(item.get("description"), 180)
                if description:
                    lines.append(f"    {description}")
            elif kind == "memory":
                content = (
                    item.get("content")
                    or item.get("text")
                    or item.get("memory")
                    or item.get("summary")
                )
                identifier = item.get("id")
                prefix = f"{index:>2}."
                if identifier is not None:
                    prefix += f" {self._bounded_text(identifier, 60)}"
                lines.append(f"{prefix} {self._bounded_text(content or 'Mémoire sans texte', 260)}")
            else:
                summary = ", ".join(
                    f"{key}={self._display_scalar(value)}"
                    for key, value in item.items()
                    if not isinstance(value, (Mapping, list, tuple, set))
                )
                lines.append(f"{index:>2}. {self._bounded_text(summary, 260)}")
        return "\n".join(lines)

    def _format_mapping(self, value: Any, *, empty: str = "Aucune donnée.") -> str:
        if not isinstance(value, Mapping) or not value:
            return empty
        return "\n".join(
            f"{str(key).replace('_', ' ')}: {self._display_scalar(item)}"
            for key, item in value.items()
            if not isinstance(item, (Mapping, list, tuple, set))
        ) or empty

    def _format_status(self, snapshot: Any) -> str:
        if not isinstance(snapshot, Mapping):
            return "État indisponible."
        sections: list[str] = []
        runtime = snapshot.get("runtime")
        sections.append("RUNTIME")
        sections.append(
            "  " + self._format_mapping(runtime, empty="indisponible").replace("\n", "\n  ")
            if isinstance(runtime, Mapping)
            else "  indisponible"
        )
        for key, label, kind in (
            ("tasks", "TASKS", "tasks"),
            ("agents", "AGENTS", "agents"),
        ):
            sections.append("")
            sections.append(label)
            value = snapshot.get(key)
            body = "indisponible" if value is None else self._format_rows(value, kind=kind)
            sections.extend(f"  {line}" for line in body.splitlines())
        for key, label in (("events", "EVENTS"), ("context", "CONTEXT"), ("model", "MODEL")):
            if key not in snapshot:
                continue
            sections.append("")
            sections.append(label)
            body = self._format_mapping(snapshot.get(key), empty="Aucune donnée.")
            sections.extend(f"  {line}" for line in body.splitlines())
        if snapshot.get("workspace") is not None:
            sections.append("")
            sections.append("WORKSPACE")
            workspace = snapshot["workspace"]
            body = self._format_mapping(workspace) if isinstance(workspace, Mapping) else str(workspace)
            sections.extend(f"  {line}" for line in body.splitlines())
        return "\n".join(sections)

    def _format_help(self, data: Any) -> str:
        if not isinstance(data, Mapping):
            return "Aide indisponible."
        if "command" in data:
            status = "available" if data.get("available") else "unavailable"
            return "\n".join(
                (
                    str(data.get("usage") or "/" + str(data.get("command") or "")),
                    f"status: {status}",
                    str(data.get("description") or "Aucune description."),
                )
            )
        commands = [item for item in data.get("commands", []) if isinstance(item, Mapping)]
        if not commands:
            return "Aucune commande disponible."
        available = [item for item in commands if item.get("available")]
        unavailable = [item for item in commands if not item.get("available")]
        lines = ["AVAILABLE"]
        lines.extend(
            f"  {item.get('usage')}  -  {item.get('description')}" for item in available
        )
        if unavailable:
            lines.extend(("", "UNAVAILABLE"))
            lines.extend(f"  {item.get('usage')}" for item in unavailable)
        aliases = data.get("aliases")
        if isinstance(aliases, Mapping) and aliases:
            lines.extend(("", "ALIASES"))
            lines.extend(f"  {key} -> {value}" for key, value in aliases.items())
        note = data.get("note")
        if note:
            lines.extend(("", str(note)))
        return "\n".join(lines)

    def _format_command_data(self, name: str, data: Any) -> str:
        if name in {"status", "dashboard"}:
            return self._format_status(data)
        if name in {"tasks", "agents", "jobs", "tools", "memory"}:
            return self._format_rows(data, kind=name)
        if name in {"watch", "events", "context", "model", "workspace", "cost"}:
            if name in {"watch", "events"} and isinstance(data, Mapping):
                running = self._yes_no(data.get("running"))
                queued = self._display_scalar(data.get("queued"))
                unfinished = self._display_scalar(data.get("unfinished"))
                workers = self._display_scalar(data.get("workers"))
                durable = self._yes_no(data.get("durable_enabled"))
                dead = self._display_scalar(data.get("dead_letters"))
                callbacks = self._display_scalar(data.get("callback_errors"))
                return "\n".join(
                    (
                        f"state: running={running} | workers={workers} | durable={durable}",
                        f"queue: queued={queued} | unfinished={unfinished}",
                        f"errors: dead_letters={dead} | callback_errors={callbacks}",
                    )
                )
            return self._format_mapping(data)
        if name in {"help", "commands"}:
            return self._format_help(data)
        if isinstance(data, list):
            return self._format_rows(data, kind=name)
        if isinstance(data, Mapping):
            return self._format_mapping(data)
        return self._display_scalar(data)

    def _read(self, name: str) -> Any:
        service = self._service(name)
        if service is None:
            return None
        for method in ("snapshot", "status", "list", "get"):
            fn = getattr(service, method, None)
            if callable(fn):
                try:
                    return fn()
                except TypeError:
                    continue
        if isinstance(service, (str, int, float, bool, Mapping, list, tuple, set, Decimal, Path)):
            return service
        return None

    def _help_projection(self, target: str | None = None) -> Any:
        if target is not None:
            target = {"agent": "agents", "task": "tasks"}.get(target, target)
            if target not in self.COMMAND_HELP:
                return None
            usage, description = self.COMMAND_HELP[target]
            return {
                "command": target,
                "usage": usage,
                "description": description,
                "available": self._has_capability(target),
            }
        commands = []
        for name in self.COMMANDS:
            usage, description = self.COMMAND_HELP[name]
            commands.append({
                "command": name,
                "usage": usage,
                "description": description,
                "available": self._has_capability(name),
            })
        return {
            "commands": commands,
            "aliases": {"/agent": "/agents", "/task": "/tasks"},
            "note": "available=false signifie que cette instance n'expose pas le service requis.",
        }

    def _memory_projection(self) -> Any:
        service = self._memory_service()
        if service is not None:
            search = getattr(service, "search", None)
            if callable(search):
                runtime = self._get("runtime")
                assembler = getattr(runtime, "context_assembler", None) if runtime is not None else None
                namespace = getattr(assembler, "memory_namespace", "default")
                for kwargs in ({"namespace": namespace, "limit": 20}, {"limit": 20}, {}):
                    try:
                        return search("", **kwargs)
                    except TypeError:
                        continue
            for method in ("snapshot", "list", "all"):
                fn = getattr(service, method, None)
                if callable(fn):
                    try:
                        return fn()
                    except TypeError:
                        continue

        # Legacy/default Orion still has a small PromptContextStore even when
        # vector/retrieval memory is disabled. Expose only its explicit
        # ``memories`` field rather than serializing the whole prompt context.
        runtime = self._get("runtime")
        prompt_store = getattr(runtime, "prompt_store", None) if runtime is not None else None
        snapshot = getattr(prompt_store, "snapshot", None)
        if callable(snapshot):
            try:
                value = snapshot()
                memories = value.get("memories") if isinstance(value, Mapping) else getattr(value, "memories", None)
                if isinstance(memories, (list, tuple)):
                    return list(memories)[:20]
            except Exception:
                return None
        return None

    @staticmethod
    def _manifest_value(manifest: Any, name: str, default: Any = None) -> Any:
        return manifest.get(name, default) if isinstance(manifest, Mapping) else getattr(manifest, name, default)

    def _tools_projection(self) -> Any:
        """Project callable runtime tools and their safe package/policy status."""
        service = self._service("tool")
        installed = getattr(service, "installed", None) if service is not None else None
        runtime = self._get("runtime")
        definitions_getter = getattr(runtime, "_tool_definitions", None) if runtime is not None else None

        config = getattr(service, "config", {}) if service is not None else {}
        config = config if isinstance(config, Mapping) else {}
        enabled_ids = {str(item) for item in config.get("enabled", []) if str(item).strip()}
        disabled_ids = {str(item) for item in config.get("disabled", []) if str(item).strip()}

        loaded_ids: set[str] | None = None
        loaded_guidance = getattr(service, "loaded_guidance", None)
        if callable(loaded_guidance):
            try:
                loaded = loaded_guidance()
                if isinstance(loaded, Mapping):
                    loaded_ids = {str(item) for item in loaded}
            except Exception:
                loaded_ids = None

        policy = getattr(runtime, "tool_policy", None) if runtime is not None else None
        policy_getter = getattr(service, "tool_policy", None) if service is not None else None
        if callable(policy_getter):
            try:
                policy = policy or policy_getter()
            except Exception:
                pass

        package_rows: dict[str, dict[str, Any]] = {}
        callable_packages: dict[str, str] = {}
        installed_items = installed() if callable(installed) else []
        for item in installed_items:
            manifest = item[0] if isinstance(item, tuple) and item else item
            tool_id = str(self._manifest_value(manifest, "id", "") or "")
            if not tool_id:
                continue
            manifest_policy = self._manifest_value(manifest, "policy")
            classification = None
            requires_explicit = bool(getattr(manifest_policy, "requires_explicit_enable", False))
            tool_classifications: dict[str, str] = {}
            raw_tool_classes = getattr(manifest_policy, "tool_classifications", ()) if manifest_policy is not None else ()
            for tool_name, tool_class in raw_tool_classes or ():
                tool_classifications[str(tool_name)] = str(getattr(tool_class, "value", tool_class))
            if policy is not None and callable(getattr(policy, "rule_for", None)):
                try:
                    rule = policy.rule_for(tool_id)
                    classification = str(getattr(getattr(rule, "classification", None), "value", getattr(rule, "classification", ""))) or None
                    requires_explicit = requires_explicit or bool(getattr(rule, "approval_required", False))
                except Exception:
                    pass
            if classification is None and manifest_policy is not None:
                raw_classification = getattr(manifest_policy, "classification", None)
                classification = str(getattr(raw_classification, "value", raw_classification)) if raw_classification is not None else None

            explicitly_enabled = tool_id in enabled_ids
            # ToolManager deliberately treats installation and authorization as
            # separate states: Python packages are imported only when their id
            # is explicitly present in ``tools.enabled``.  Do not recreate the
            # old ``enabled=[] => auto-load everything`` semantics in the UI,
            # otherwise /tools would claim an untrusted package is enabled even
            # though the runtime correctly refused to import it.
            enabled = tool_id not in disabled_ids and explicitly_enabled
            loaded = tool_id in loaded_ids if loaded_ids is not None else None
            status = "disabled" if tool_id in disabled_ids else "installed_not_enabled"
            if enabled and loaded is True:
                status = "loaded"
            elif enabled and loaded is False:
                status = "enabled_not_loaded"
            elif enabled:
                status = "enabled"
            row = {
                "id": tool_id,
                "name": self._manifest_value(manifest, "name", tool_id),
                "version": self._manifest_value(manifest, "version"),
                "installed": True,
                "enabled": enabled,
                "loaded": loaded,
                "classification": classification,
                "status": status,
                "requires_explicit_enable": requires_explicit,
                "tool_classifications": tool_classifications,
            }
            package_rows[tool_id] = row
            for callable_name in tool_classifications:
                callable_packages.setdefault(callable_name, tool_id)
            short_id = tool_id.rsplit(".", 1)[-1]
            callable_packages.setdefault(short_id, tool_id)

        definitions = None
        if callable(definitions_getter):
            try:
                value = definitions_getter()
                definitions = value if isinstance(value, list) else list(value or [])
            except Exception:
                definitions = None
        if definitions is None:
            return list(package_rows.values()) if callable(installed) else None

        native_names: set[str] = set()
        native_getter = getattr(runtime, "_runtime_tool_definitions", None)
        if callable(native_getter):
            try:
                for item in native_getter():
                    function = item.get("function") if isinstance(item, Mapping) else None
                    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
                        native_names.add(function["name"])
            except Exception:
                native_names = set()

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for definition in definitions:
            function = definition.get("function") if isinstance(definition, Mapping) else None
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip() or name in seen:
                continue
            seen.add(name)
            package_id = callable_packages.get(name)
            package = package_rows.get(package_id) if package_id else None
            classification = None
            if policy is not None and callable(getattr(policy, "rule_for", None)):
                try:
                    rule = policy.rule_for(name)
                    raw = getattr(rule, "classification", None)
                    classification = str(getattr(raw, "value", raw)) if raw is not None else None
                except Exception:
                    pass
            if classification is None and package is not None:
                classification = package["tool_classifications"].get(name) or package["classification"]
            source = "runtime" if name in native_names else (package_id or "registered")
            result.append({
                "id": name,
                "name": name,
                "description": str(function.get("description") or ""),
                "source": source,
                "package": package_id,
                "version": package.get("version") if package is not None else None,
                "installed": True,
                "enabled": True,
                "loaded": True,
                "classification": classification,
                "status": "loaded",
                "requires_explicit_enable": bool(package.get("requires_explicit_enable")) if package is not None else False,
            })
        return result

    def _doctor_projection(self) -> dict[str, Any]:
        """Return bounded diagnostics without environment/config secret values."""
        runtime = self._get("runtime")
        events = self._service("event")
        tools = self._service("tool")
        approvals = self._service("approval")
        checks: dict[str, Any] = {
            "runtime": {
                "available": runtime is not None,
                "running": getattr(runtime, "running", None) if runtime is not None else None,
                "durable": getattr(runtime, "_durable_store", None) is not None if runtime is not None else False,
            },
            "events": {
                "available": events is not None,
                "running": getattr(events, "running", None) if events is not None else None,
                "durable": getattr(events, "_durable_store", None) is not None if events is not None else False,
            },
            "tasks": {"available": self._service("task") is not None},
            "memory": {"available": self._has_capability("memory")},
            "tools": {"available": self._has_capability("tools")},
            "approvals": {"available": approvals is not None},
        }
        config_check = None
        try:
            from observability import doctor as config_doctor

            config_path = self._get("config_path")
            if config_path is None and tools is not None:
                root_dir = getattr(tools, "root_dir", None)
                if root_dir is not None:
                    candidate = Path(root_dir) / "orion.toml"
                    if candidate.is_file():
                        config_path = candidate
            if config_path is None:
                candidate = Path.cwd() / "orion.toml"
                if candidate.is_file():
                    config_path = candidate
            if config_path is not None:
                raw = config_doctor(config_path)
                config_check = {
                    "available": True,
                    "ok": bool(raw.get("ok")),
                    "channels": list(raw.get("channels", [])),
                    "error_type": raw.get("error_type"),
                }
        except Exception as exc:
            config_check = {"available": True, "ok": False, "error_type": type(exc).__name__}
        checks["config"] = config_check or {"available": False}
        return {"ok": all(section.get("available", False) for key, section in checks.items() if key in {"runtime", "events"}), "checks": checks}

    def _pending_approvals(self) -> Any:
        service = self._service("approval")
        if service is None:
            return None
        pending = getattr(service, "pending", None)
        if not callable(pending):
            return None
        result = []
        for item in pending():
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            preview = payload.get("preview")
            result.append(
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "requester": item.get("requester"),
                    "scope": item.get("scope"),
                    "created_at": item.get("created_at"),
                    "expires_at": item.get("expires_at"),
                    "tool_id": payload.get("tool_id"),
                    "args_hash": payload.get("args_hash"),
                    "preview": preview if isinstance(preview, dict) else None,
                }
            )
        return result

    def _cancel_projection(self) -> dict:
        """Request cancellation of the active RUN.

        The runtime aborts at its next interrupt point (between model turns and
        tool calls), so the currently executing call still finishes. Reporting
        that explicitly avoids implying the stop was instantaneous.
        """
        runtime = self._get("runtime")
        request = getattr(runtime, "request_cancel", None)
        if not callable(request):
            return {"cancelled": False, "reason": "runtime_unavailable"}
        try:
            active = bool(request())
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return {"cancelled": False, "reason": type(exc).__name__}
        if active:
            return {
                "cancelled": True,
                "reason": "requested",
                "note": "Le run s'arretera a la prochaine etape sure; "
                "l'appel en cours doit d'abord se terminer.",
            }
        return {"cancelled": False, "reason": "no_active_run"}

    def _event_projection(self) -> Any:
        """Project EventHandler operational state without event payloads/errors."""
        service = self._service("event")
        if service is None:
            return None
        queue = getattr(service, "queue", None)
        qsize = getattr(queue, "qsize", None)
        unfinished = getattr(queue, "unfinished_tasks", None)
        dead_letters = getattr(service, "dead_letters", None)
        callback_errors = getattr(service, "callback_errors", None)
        return {
            "running": bool(getattr(service, "running", False)),
            "workers": getattr(service, "workers", None),
            "queued": qsize() if callable(qsize) else None,
            "unfinished": unfinished if isinstance(unfinished, int) else None,
            "dead_letters": len(dead_letters) if isinstance(dead_letters, list) else None,
            "callback_errors": len(callback_errors) if isinstance(callback_errors, list) else None,
            "durable_enabled": getattr(service, "_durable_store", None) is not None,
        }

    def _skills_projection(self) -> Any:
        """Return public ToolManager manifest metadata only."""
        service = self._service("skill")
        if service is None:
            return None
        installed = getattr(service, "installed", None)
        if not callable(installed):
            return None
        result = []
        for item in installed():
            manifest = item[0] if isinstance(item, tuple) and item else item
            result.append({
                "id": getattr(manifest, "id", None),
                "name": getattr(manifest, "name", None),
                "version": getattr(manifest, "version", None),
                "description": getattr(manifest, "description", ""),
                "api_version": getattr(manifest, "api_version", None),
                "permissions": list(getattr(manifest, "permissions", ()) or ()),
            })
        return result

    def _model_projection(self) -> Any:
        """Expose non-secret LLM routing information only."""
        service = self._service("model")
        if service is None:
            return None
        return {
            "model": getattr(service, "model", None),
            "base_url": getattr(service, "base_url", None),
            "timeout": self._plain(getattr(service, "timeout", None)),
            "max_retries": getattr(service, "max_retries", None),
        }

    def _action(self, name: str, args: list[str]) -> dict:
        # Mutations are delegated to the explicit action layer, which checks
        # capabilities and never fabricates success.
        result = execute_action(self.application, name, args)
        out = {"title": name, "data": result.get("data")}
        if result.get("error"):
            out["error"] = result["error"]
        return out

    def _graph(self) -> Any:
        """Return a public agent graph without prompts, contexts or raw jobs."""
        agents = self._agents_projection()
        if agents is None:
            return None
        return {"nodes": agents, "edges": [], "hierarchical": False}
