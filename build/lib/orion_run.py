"""Lance Orion comme processus long-vivant.

Usage : ``python orion_run.py --config orion.toml``.
"""

from __future__ import annotations

import argparse
import inspect
import json
import shlex
import signal
import sys
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import MethodType
from typing import Any

from cli_cockpit import CockpitCLIAdapter
from cli_cockpit_backend import CockpitBackend
from channels import InboundMessage
from openrouter_client import OpenRouterError
from orion_config import load_orion
from observability import doctor, health, readiness


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 3
EXIT_REQUEST_FAILED = 4
EXIT_INTERRUPTED = 130


def _now_iso() -> str:
    """Retourne un horodatage explicite pour les événements JSONL."""
    return datetime.now().astimezone().isoformat()


def _normalized_correlation(value: Any, metadata: Mapping[str, Any] | None = None) -> str | None:
    """Retourne une corrélation canonique, ou ``None`` si elle est absente."""
    candidate = value
    if candidate is None and metadata is not None:
        candidate = metadata.get("correlation_id")
    if candidate is None:
        return None
    normalized = str(candidate).strip()
    return normalized or None


def _report_error(event: object, error: Exception) -> None:
    event_id = getattr(event, "id", "unknown")
    event_type = getattr(event, "type", "unknown")
    print(
        f"[orion:error] event={event_type} id={event_id}: "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )


def _install_signal_handlers(
    shutdown: threading.Event,
    *,
    on_shutdown: Any = None,
) -> dict[int, Any]:
    """Installe les signaux d'arrêt et retourne les handlers précédents."""
    previous: dict[int, Any] = {}

    def request_shutdown(*_: Any) -> None:
        shutdown.set()
        if callable(on_shutdown):
            on_shutdown()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        previous[signal_value] = signal.getsignal(signal_value)
        signal.signal(signal_value, request_shutdown)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signal_value, handler in previous.items():
        try:
            signal.signal(signal_value, handler)
        except (OSError, ValueError):
            continue


def configure_cli(application: object, shutdown: threading.Event) -> object | None:
    """Raccorde la CLI aux providers d'observation du runtime.

    Ce raccordement reste optionnel pour les installations headless. Les
    providers sont installés par capacité, ce qui permet à une UI de fournir
    seulement le sous-ensemble qu'elle expose.
    """
    channels = getattr(application, "channels", None)
    adapters = getattr(channels, "adapters", {}) if channels is not None else {}
    cli_adapter = adapters.get("cli") if isinstance(adapters, dict) else None
    if cli_adapter is None:
        return None

    set_exit_handler = getattr(cli_adapter, "set_exit_handler", None)
    if callable(set_exit_handler):
        set_exit_handler(shutdown.set)

    runtime = application.runtime

    def cli_status() -> dict[str, object]:
        task = runtime.current_task
        run_context = runtime.run_context
        values: dict[str, object] = {
            "Runtime": runtime.state.value.upper(),
            "Modèle": application.llm.model,
            "Événements traités": runtime.wake_count,
            "Événements en file": runtime.pending_events,
            "Reçus pendant RUN": runtime.pending_events_during_run,
            "Pré-réflexion": (
                "active" if runtime.reflection_engine is not None else "désactivée"
            ),
            # ``run_context`` can outlive a completed wake.  Never advertise
            # a stale RUN phase while the runtime is asleep; that status was
            # particularly confusing in long-lived CLI sessions.
            "Phase RUN": (
                run_context.phase.value.upper()
                if run_context is not None and str(runtime.state.value).lower() == "run"
                else "aucune"
            ),
            "Tâche active": (
                f"#{task.id} · {task.objective}" if task is not None else "aucune"
            ),
        }
        if runtime.last_error is not None:
            values["Dernière erreur"] = type(runtime.last_error).__name__
        subagents = getattr(application, "subagents", None)
        if subagents is not None:
            jobs = subagents.list_jobs(limit=100)
            values["Sous-agents"] = len(subagents.list_agents())
            values["Jobs actifs"] = sum(
                1 for job in jobs if job.status.value in {"queued", "running"}
            )
        return values

    def cli_tools() -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for definition in runtime._tool_definitions():
            function = definition.get("function", {})
            rows.append(
                {
                    "id": str(function.get("name", "tool")),
                    "label": str(function.get("description", "")),
                }
            )
        return sorted(rows, key=lambda item: item["id"])

    def cli_tasks() -> list[dict[str, object]]:
        tasks = runtime.task_store.list()
        return [
            {
                "id": f"#{task.id}",
                "objective": task.objective,
                "status": task.status.value,
            }
            for task in reversed(tasks[-10:])
        ]

    def cli_agents() -> list[dict[str, object]]:
        subagents = getattr(application, "subagents", None)
        if subagents is None:
            return []
        return [
            {
                "id": agent.id,
                "name": f"{agent.name} · {agent.model}",
                "status": agent.status.value,
            }
            for agent in subagents.list_agents()
        ]

    def cli_jobs() -> list[dict[str, object]]:
        subagents = getattr(application, "subagents", None)
        if subagents is None:
            return []
        return [
            {
                "id": job.id,
                "objective": job.objective,
                "status": job.status.value,
            }
            for job in subagents.list_jobs(limit=20)
        ]

    def cli_threads() -> list[dict[str, object]]:
        """Expose les conversations persistantes avec leur intent courant."""
        source = getattr(application, "threads", None) or getattr(application, "conversations", None)
        if source is None:
            source = getattr(runtime, "threads", None) or getattr(runtime, "conversations", None)
        if source is None:
            return []
        try:
            values = source() if callable(source) else source
            if hasattr(values, "list") and callable(values.list):
                values = values.list()
            return [dict(item) if isinstance(item, dict) else item for item in (values or [])]
        except Exception:
            return []

    def cli_trace() -> list[dict[str, object]]:
        """Expose la trace persistante (thread, intent, événements)."""
        source = getattr(application, "context_trace", None) or getattr(runtime, "context_trace", None)
        if source is None:
            return []
        try:
            values = source() if callable(source) else source
            return [dict(item) if isinstance(item, dict) else item for item in (values or [])]
        except Exception:
            return []

    for setter_name, provider in {
        "set_status_provider": cli_status,
        "set_tools_provider": cli_tools,
        "set_tasks_provider": cli_tasks,
        "set_agents_provider": cli_agents,
        "set_jobs_provider": cli_jobs,
        "set_threads_provider": cli_threads,
        "set_trace_provider": cli_trace,
    }.items():
        setter = getattr(cli_adapter, setter_name, None)
        if callable(setter):
            setter(provider)
    return cli_adapter


_MISSING = object()


def _configured_cli_setting(adapter: object, name: str) -> Any:
    """Read a safe presentation setting from the configured CLI adapter.

    The legacy CLI keeps some constructor options on its console rather than
    the adapter itself.  Only presentation/IO values are copied here; runtime
    ownership and security-sensitive services remain owned by the application.
    """
    console = getattr(adapter, "console", None)
    direct = {
        "prompt": (adapter, "prompt"),
        "input": (adapter, "input"),
        "output": (adapter, "output"),
        "slow_request_seconds": (adapter, "slow_request_seconds"),
        "history_path": (adapter, "history_path"),
    }
    console_values = {
        "input": (console, "input_stream"),
        "output": (console, "output"),
        "style": (console, "use_color"),
        "banner": (console, "show_banner"),
        "name": (console, "name"),
        "model": (console, "model"),
        "markdown": (console, "render_markdown"),
        "timestamps": (console, "show_timestamps"),
    }
    source = direct.get(name)
    if source is not None and source[0] is not None and hasattr(source[0], source[1]):
        return getattr(source[0], source[1])
    source = console_values.get(name)
    if source is not None and source[0] is not None and hasattr(source[0], source[1]):
        return getattr(source[0], source[1])
    if name in {"usage_provider", "usage_ledger", "cost_provider"}:
        provider = getattr(adapter, "_usage_provider", _MISSING)
        if provider is _MISSING and console is not None:
            provider = getattr(console, "_usage_provider", _MISSING)
        return provider
    return _MISSING


def _cockpit_constructor_kwargs(configured_adapter: object) -> dict[str, Any]:
    """Return only configured CLI settings accepted by this cockpit build."""
    try:
        parameters = inspect.signature(CockpitCLIAdapter).parameters
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    candidates = (
        "input",
        "output",
        "prompt",
        "style",
        "banner",
        "name",
        "model",
        "history_path",
        "markdown",
        "timestamps",
        "slow_request_seconds",
        "usage_provider",
        "usage_ledger",
        "cost_provider",
    )
    result: dict[str, Any] = {}
    for name in candidates:
        if not accepts_kwargs and name not in parameters:
            continue
        value = _configured_cli_setting(configured_adapter, name)
        if value is not _MISSING:
            result[name] = value
    return result


def _copy_cli_handlers(configured_adapter: object, cockpit: object) -> None:
    """Best-effort copy of already configured capability providers."""
    for attribute, setter_name in (
        ("_status_provider", "set_status_provider"),
        ("_tools_provider", "set_tools_provider"),
        ("_tasks_provider", "set_tasks_provider"),
        ("_agents_provider", "set_agents_provider"),
        ("_jobs_provider", "set_jobs_provider"),
        ("_threads_provider", "set_threads_provider"),
        ("_trace_provider", "set_trace_provider"),
        ("_usage_provider", "set_usage_provider"),
        ("_exit_handler", "set_exit_handler"),
    ):
        value = getattr(configured_adapter, attribute, None)
        setter = getattr(cockpit, setter_name, None)
        if value is not None and callable(setter):
            setter(value)


class _CockpitCompatibilityBackend:
    """Bridge legacy request controls while the new cockpit owns stdin/stdout.

    The old CLI request tracker is intentionally retained as UI state only. It
    never owns the runtime and `/stop` keeps the historical semantics: cancel
    the visible request and suppress its late output, without stopping Orion's
    durable runtime worker.
    """

    _COMMANDS = (
        "requests",
        "stop",
        "retry",
        "resume",
        "jobs",
        "threads",
        "trace",
        "debug",
    )
    _COMMAND_HELP = {
        "requests": ("/requests [request-id]", "Lister les requêtes récentes."),
        "stop": ("/stop [request-id|all]", "Annuler une requête active."),
        "retry": ("/retry <request-id>", "Relancer une requête échouée ou annulée."),
        "resume": ("/resume [request-id]", "Reprendre une requête avec son contexte."),
        "jobs": ("/jobs [job-id]", "Lister les travaux délégués récents."),
        "threads": ("/threads [thread-id]", "Afficher les conversations et intentions persistantes."),
        "trace": ("/trace [thread-id]", "Afficher la trace de contexte d'une conversation."),
        "debug": ("/debug", "Afficher les informations de diagnostic de la CLI."),
    }

    def __init__(self, backend: object, application: object, configured_adapter: object) -> None:
        self._backend = backend
        self._application = application
        self._configured_adapter = configured_adapter
        self._console = getattr(configured_adapter, "console", None)
        self._tracker = getattr(self._console, "requests", None)
        self._publisher: Any = None
        self._lock = threading.RLock()
        self._request_by_correlation: dict[str, str] = {}
        self._correlation_by_event: dict[str, str] = {}
        self._cancelled_tokens: set[str] = set()
        self._stop_requested = threading.Event()

    @property
    def tracking_available(self) -> bool:
        return (
            self._tracker is not None
            and callable(getattr(self._console, "new_request", None))
            and callable(getattr(self._tracker, "snapshot", None))
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def commands(self) -> tuple[str, ...]:
        provider = getattr(self._backend, "commands", None)
        base = tuple(provider()) if callable(provider) else ()
        return tuple(dict.fromkeys((*base, *self._COMMANDS)))

    def _compat_available(self, name: str) -> bool:
        if name in {"requests", "stop", "retry", "resume", "debug"}:
            return self.tracking_available
        if name == "jobs":
            return (
                getattr(self._configured_adapter, "_jobs_provider", None) is not None
                or callable(getattr(getattr(self._application, "subagents", None), "list_jobs", None))
            )
        if name == "threads":
            return getattr(self._configured_adapter, "_threads_provider", None) is not None
        if name == "trace":
            return getattr(self._configured_adapter, "_trace_provider", None) is not None
        return False

    def _help_projection(self, target: str | None = None) -> Any:
        if target is not None:
            normalized = str(target).lstrip("/").lower()
            if normalized in self._COMMAND_HELP:
                usage, description = self._COMMAND_HELP[normalized]
                return {
                    "command": normalized,
                    "usage": usage,
                    "description": description,
                    "available": self._compat_available(normalized),
                }
            provider = getattr(self._backend, "_help_projection", None)
            return provider(normalized) if callable(provider) else None

        provider = getattr(self._backend, "_help_projection", None)
        base = provider() if callable(provider) else None
        result = dict(base) if isinstance(base, Mapping) else {"commands": []}
        commands = [
            dict(item)
            for item in result.get("commands", [])
            if isinstance(item, Mapping)
        ]
        known = {str(item.get("command", "")).lower() for item in commands}
        for name in self._COMMANDS:
            if name in known:
                continue
            usage, description = self._COMMAND_HELP[name]
            commands.append(
                {
                    "command": name,
                    "usage": usage,
                    "description": description,
                    "available": self._compat_available(name),
                }
            )
        result["commands"] = commands
        return result

    def _legacy_provider_value(self, attribute: str, fallback: Any) -> Any:
        provider = getattr(self._configured_adapter, attribute, None)
        resolver = getattr(self._configured_adapter, "_provider_value", None)
        if callable(resolver):
            return resolver(provider, fallback)
        if provider is None:
            return fallback
        try:
            return provider() if callable(provider) else provider
        except Exception:
            return fallback

    def _legacy_filter_items(self, items: Any, identifier: str | None) -> list[Any]:
        filter_items = getattr(self._configured_adapter, "_filter_items", None)
        if callable(filter_items):
            return list(filter_items(items, identifier))
        if identifier is None:
            return list(items) if isinstance(items, (list, tuple)) else []
        if not isinstance(items, (list, tuple)):
            return []
        wanted = str(identifier).lower()
        result: list[Any] = []
        for item in items:
            values = (
                (item.get("id"), item.get("request_id"), item.get("name"), item.get("label"))
                if isinstance(item, Mapping)
                else (item,)
            )
            if any(
                value is not None
                and (
                    str(value).lower() == wanted
                    or str(value).lower().startswith(wanted)
                )
                for value in values
            ):
                result.append(item)
        return result

    def _legacy_threads(self, identifier: str | None) -> dict[str, Any]:
        values = self._legacy_provider_value("_threads_provider", [])
        return {
            "title": "threads",
            "data": self._legacy_filter_items(values, identifier),
        }

    def _legacy_trace(self, identifier: str | None) -> dict[str, Any]:
        values = self._legacy_filter_items(
            self._legacy_provider_value("_trace_provider", []),
            identifier,
        )
        seen: set[str] = set()
        unique: list[Any] = []
        for value in values:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str) if isinstance(value, Mapping) else str(value)
            if key in seen:
                continue
            seen.add(key)
            unique.append(value)
        return {"title": "trace", "data": unique}

    def _legacy_debug(self) -> dict[str, Any]:
        pending = sum(
            1
            for item in self._request_rows()
            if item.get("state") not in {"succeeded", "failed", "canceled"}
        )
        return {
            "title": "debug",
            "data": {
                "Requêtes en attente": pending,
                "Arrêt demandé": self._stop_requested.is_set(),
            },
        }

    def bind_publisher(self, publisher: Any) -> None:
        if callable(publisher):
            self._publisher = publisher
            self._stop_requested.clear()

    def mark_stop_requested(self) -> None:
        self._stop_requested.set()

    @staticmethod
    def _message_context(message: InboundMessage) -> tuple[str | None, Mapping[str, Any] | None]:
        payload = message.payload if isinstance(message.payload, Mapping) else {}
        metadata = message.metadata if isinstance(message.metadata, Mapping) else {}
        parent = payload.get("parent_request_id") or metadata.get("parent_request_id")
        context = payload.get("context") or metadata.get("context")
        return (
            str(parent) if parent else None,
            context if isinstance(context, Mapping) else None,
        )

    def _new_request(
        self,
        text: str,
        *,
        correlation_id: str | None,
        parent_request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self.tracking_available:
            return None
        try:
            request = self._console.new_request(
                text,
                correlation_id=correlation_id,
                parent_request_id=parent_request_id,
                context=context,
            )
            update = getattr(self._console, "update_request", None)
            if callable(update):
                update(request.request_id, "running")
            else:
                self._tracker.update(request.request_id, "running")
            with self._lock:
                self._request_by_correlation[str(request.correlation_id)] = str(request.request_id)
            return request
        except Exception:
            # Request tracking is presentation state. A broken legacy console
            # must never prevent delivery to the durable application pipeline.
            return None

    def forward_submission(self, message: InboundMessage, publisher: Any) -> Any:
        self.bind_publisher(publisher)
        parent, context = self._message_context(message)
        text = message.text
        if text is None and isinstance(message.payload, Mapping):
            text = message.payload.get("text")
        request = self._new_request(
            str(text or ""),
            correlation_id=message.correlation_id,
            parent_request_id=parent,
            context=context,
        )
        try:
            event = publisher(message)
        except Exception as exc:
            if request is not None:
                self._update_request(
                    str(request.request_id),
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if request is not None:
            event_id = getattr(event, "id", None)
            if event_id:
                with self._lock:
                    self._correlation_by_event[str(event_id)] = str(request.correlation_id)
        return event

    def _update_request(
        self,
        request_id: str,
        state: str,
        *,
        error: str | None = None,
        text: str | None = None,
        seq: int | None = None,
    ) -> None:
        try:
            update = getattr(self._console, "update_request", None)
            if callable(update):
                update(request_id, state, error=error, text=text, seq=seq)
            elif self._tracker is not None:
                self._tracker.update(request_id, state, error=error, text=text, seq=seq)
        except (KeyError, RuntimeError, TypeError, ValueError):
            pass

    def observe_output(self, output: object) -> bool:
        """Update legacy request state; return False for cancelled late output."""
        metadata = getattr(output, "metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        event_id = str(getattr(output, "event_id", None) or "")
        correlation_id = str(
            getattr(output, "correlation_id", None)
            or metadata.get("correlation_id")
            or ""
        )
        tokens = {
            token
            for token in (
                event_id,
                correlation_id,
                str(getattr(output, "output_id", None) or ""),
                str(getattr(output, "idempotency_key", None) or ""),
            )
            if token
        }
        with self._lock:
            if tokens & self._cancelled_tokens:
                return False
            if not correlation_id and event_id:
                correlation_id = self._correlation_by_event.get(event_id, "")
            request_id = self._request_by_correlation.get(correlation_id, "")
        if not request_id:
            return True

        intermediate = bool(metadata.get("intermediate", False))
        error_value = metadata.get("error")
        text = str(getattr(output, "content", None) or getattr(output, "text", None) or "")
        sequence = metadata.get("seq")
        try:
            normalized_seq = int(sequence) if sequence is not None else None
        except (TypeError, ValueError, OverflowError):
            normalized_seq = None
        if intermediate:
            self._update_request(request_id, "streaming", text=text, seq=normalized_seq)
            return True

        self._update_request(
            request_id,
            "failed" if error_value else "succeeded",
            error=text if error_value else None,
            text=text,
            seq=normalized_seq,
        )
        with self._lock:
            self._request_by_correlation.pop(correlation_id, None)
            if event_id:
                self._correlation_by_event.pop(event_id, None)
        return True

    def _request_rows(self) -> list[dict[str, Any]]:
        if self._tracker is None:
            return []
        try:
            values = self._tracker.snapshot()
        except Exception:
            return []
        return [dict(item) for item in values if isinstance(item, Mapping)]

    @staticmethod
    def _filter_rows(rows: list[dict[str, Any]], identifier: str | None) -> list[dict[str, Any]]:
        if not identifier:
            return rows
        wanted = str(identifier).lower()
        return [
            row
            for row in rows
            if any(
                value is not None and str(value).lower().startswith(wanted)
                for value in (row.get("request_id"), row.get("id"), row.get("name"), row.get("objective"))
            )
        ]

    def _resolve_request(self, identifier: str) -> tuple[dict[str, Any] | None, str | None]:
        rows = self._filter_rows(self._request_rows(), identifier)
        exact = [row for row in rows if str(row.get("request_id", "")).lower() == identifier.lower()]
        if exact:
            return exact[0], None
        if len(rows) == 1:
            return rows[0], None
        if not rows:
            return None, f"Requête introuvable : {identifier}"
        return None, f"Identifiant ambigu : {identifier} ({len(rows)} requêtes correspondent)."

    def _cancel_row(self, row: Mapping[str, Any]) -> str | None:
        request_id = str(row.get("request_id") or "")
        if not request_id or self._tracker is None:
            return None
        try:
            item = self._tracker.cancel(request_id)
        except Exception:
            return None
        if item is None:
            return None
        correlation_id = str(row.get("correlation_id") or getattr(item, "correlation_id", "") or "")
        with self._lock:
            self._cancelled_tokens.add(request_id)
            if correlation_id:
                self._cancelled_tokens.add(correlation_id)
                self._request_by_correlation.pop(correlation_id, None)
                for event_id, mapped in list(self._correlation_by_event.items()):
                    if mapped == correlation_id:
                        self._cancelled_tokens.add(event_id)
                        self._correlation_by_event.pop(event_id, None)
            if len(self._cancelled_tokens) > 1024:
                self._cancelled_tokens.clear()
        return request_id

    def _resubmit(
        self,
        text: str,
        *,
        parent_request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not callable(self._publisher):
            return {"title": "request", "data": None, "error": "CLI non démarrée."}
        correlation_id = f"cli-{uuid.uuid4().hex}"
        payload: dict[str, Any] = {"text": text}
        metadata: dict[str, Any] = {}
        if parent_request_id:
            payload["parent_request_id"] = parent_request_id
            metadata["parent_request_id"] = parent_request_id
        if context is not None:
            payload["context"] = dict(context)
            metadata["context"] = dict(context)
        message = InboundMessage(
            channel="cli",
            source="cli",
            payload=payload,
            reply_to="stdout",
            correlation_id=correlation_id,
            metadata=metadata,
            text=text,
        )
        self.forward_submission(message, self._publisher)
        with self._lock:
            request_id = self._request_by_correlation.get(correlation_id)
        data = self._tracker.status(request_id) if request_id and self._tracker is not None else None
        return {"title": "request", "data": data or {"correlation_id": correlation_id, "state": "running"}}

    def _jobs(self, identifier: str | None) -> dict[str, Any]:
        configured_provider = getattr(self._configured_adapter, "_jobs_provider", None)
        if configured_provider is not None:
            values = self._legacy_provider_value("_jobs_provider", [])
            return {
                "title": "jobs",
                "data": self._legacy_filter_items(values, identifier),
            }
        subagents = getattr(self._application, "subagents", None)
        if subagents is None:
            return {"title": "jobs", "data": []}
        listing = getattr(subagents, "list_jobs", None)
        if not callable(listing):
            return {"title": "jobs", "data": []}
        try:
            values = listing(limit=20)
        except TypeError:
            values = listing()
        plain = getattr(self._backend, "_plain", None)
        rows = plain(values) if callable(plain) else values
        if not isinstance(rows, list):
            rows = list(rows) if isinstance(rows, tuple) else []
        rows = [dict(item) if isinstance(item, Mapping) else {"value": str(item)} for item in rows]
        return {"title": "jobs", "data": self._filter_rows(rows, identifier)}

    def execute(self, command: str) -> Any:
        raw = str(command or "").strip()
        normalized = raw[1:] if raw.startswith("/") else raw
        try:
            parts = shlex.split(normalized)
        except ValueError as exc:
            return {"title": "command", "data": None, "error": str(exc)}
        name = parts[0].lower() if parts else "status"
        args = parts[1:]
        positional = [item for item in args if not item.startswith("--")]

        if "--help" in args and name in self._COMMAND_HELP:
            return {"title": "help", "data": self._help_projection(name)}

        if name in {"help", "commands"}:
            target = positional[0] if positional else None
            data = self._help_projection(target)
            if target is not None and data is None:
                return {
                    "title": name,
                    "data": None,
                    "error": f"Commande inconnue ou indisponible: /{target.lstrip('/')}",
                }
            return {"title": name, "data": data}

        if name == "requests":
            rows = self._request_rows()
            return {
                "title": "requests",
                "data": self._filter_rows(rows, positional[0] if positional else None),
            }
        if name == "jobs":
            return self._jobs(positional[0] if positional else None)
        if name == "threads":
            if len(positional) > 1:
                return {"title": name, "data": None, "error": "Usage: /threads [thread-id]"}
            return self._legacy_threads(positional[0] if positional else None)
        if name == "trace":
            if len(positional) > 1:
                return {"title": name, "data": None, "error": "Usage: /trace [thread-id]"}
            return self._legacy_trace(positional[0] if positional else None)
        if name == "debug":
            if positional:
                return {"title": name, "data": None, "error": "Usage: /debug"}
            return self._legacy_debug()
        if name == "stop":
            if self._tracker is None:
                return {"title": "stop", "data": None, "error": "Provider indisponible: requests"}
            target = positional[0] if positional else ("all" if "--force" in args else None)
            if target == "all":
                stopped = [
                    request_id
                    for row in self._request_rows()
                    if row.get("state") not in {"succeeded", "failed", "canceled"}
                    for request_id in [self._cancel_row(row)]
                    if request_id
                ]
                return {"title": "stop", "data": {"stopped": stopped}}
            if target:
                row, error = self._resolve_request(target)
                if error:
                    return {"title": "stop", "data": None, "error": error}
            else:
                active = getattr(self._tracker, "status", None)
                row = active() if callable(active) else None
            if not isinstance(row, Mapping):
                return {"title": "stop", "data": {"stopped": []}}
            request_id = self._cancel_row(row)
            return {"title": "stop", "data": {"stopped": [request_id] if request_id else []}}
        if name == "retry":
            if not positional:
                return {"title": "retry", "data": None, "error": "Usage: /retry <request-id>"}
            row, error = self._resolve_request(positional[0])
            if error:
                return {"title": "retry", "data": None, "error": error}
            if row is None or not row.get("text"):
                return {"title": "retry", "data": None, "error": f"Requête introuvable : {positional[0]}"}
            if row.get("state") not in {"failed", "canceled"}:
                return {
                    "title": "retry",
                    "data": None,
                    "error": "Seules les requêtes échouées ou annulées peuvent être relancées.",
                }
            return self._resubmit(str(row["text"]))
        if name == "resume":
            row: dict[str, Any] | None = None
            if positional:
                row, error = self._resolve_request(positional[0])
                if error:
                    return {"title": "resume", "data": None, "error": error}
            else:
                candidates = [
                    item
                    for item in self._request_rows()
                    if item.get("state") in {"failed", "canceled"} and item.get("text")
                ]
                candidates.sort(
                    key=lambda item: (str(item.get("updated_at") or ""), str(item.get("request_id") or "")),
                    reverse=True,
                )
                row = candidates[0] if candidates else None
            if row is None or not row.get("text"):
                return {"title": "resume", "data": None, "error": "Aucune requête à reprendre."}
            request_id = str(row.get("request_id") or "")
            context = row.get("context") if isinstance(row.get("context"), Mapping) else None
            resume_context = getattr(self._console, "resume_context", None)
            if callable(resume_context) and request_id:
                try:
                    resumed = resume_context(request_id)
                    if isinstance(resumed, Mapping):
                        text = str(resumed.get("text") or row["text"])
                        parent = str(resumed.get("parent_request_id") or request_id)
                        value = resumed.get("context")
                        context = value if isinstance(value, Mapping) else context
                        return self._resubmit(text, parent_request_id=parent, context=context)
                except Exception:
                    pass
            return self._resubmit(str(row["text"]), parent_request_id=request_id, context=context)

        execute = getattr(self._backend, "execute", None)
        if callable(execute):
            return execute(command)
        return {"title": name, "data": None, "error": f"Commande inconnue: /{name}"}


def _wire_cockpit_compatibility(cockpit: object, backend: _CockpitCompatibilityBackend) -> None:
    """Attach request lifecycle hooks without changing cockpit implementation."""
    original_start = getattr(cockpit, "start", None)
    if callable(original_start):
        def start_with_tracking(_self: object, on_message: Any) -> Any:
            backend.bind_publisher(on_message)
            return original_start(lambda message: backend.forward_submission(message, on_message))

        try:
            setattr(cockpit, "start", MethodType(start_with_tracking, cockpit))
        except (AttributeError, TypeError):
            pass

    original_send = getattr(cockpit, "send", None)
    if callable(original_send):
        def send_with_tracking(_self: object, output: object) -> Any:
            if not backend.observe_output(output):
                return output
            return original_send(output)

        try:
            setattr(cockpit, "send", MethodType(send_with_tracking, cockpit))
        except (AttributeError, TypeError):
            pass

    original_stop = getattr(cockpit, "stop", None)
    if callable(original_stop):
        def stop_with_tracking(_self: object) -> Any:
            backend.mark_stop_requested()
            return original_stop()

        try:
            setattr(cockpit, "stop", MethodType(stop_with_tracking, cockpit))
        except (AttributeError, TypeError):
            pass


def _install_cockpit(application: object) -> CockpitCLIAdapter | None:
    """Remplace le CLI configuré par le cockpit avant le démarrage.

    Une instance headless ne doit pas acquérir de lecteur stdin implicitement :
    le remplacement n'a donc lieu que lorsqu'un channel ``cli`` existe déjà.
    L'ancien adaptateur est retiré avant ``application.start()`` afin qu'il ne
    puisse jamais démarrer son propre thread de lecture en parallèle.
    """
    channels = getattr(application, "channels", None)
    if channels is None:
        return None
    adapters = getattr(channels, "adapters", {})
    if not isinstance(adapters, Mapping) or "cli" not in adapters:
        return None

    current = adapters["cli"]
    if isinstance(current, CockpitCLIAdapter):
        return current

    unregister = getattr(channels, "unregister", None)
    register = getattr(channels, "register", None)
    if not callable(unregister) or not callable(register):
        return None

    backend = _CockpitCompatibilityBackend(
        CockpitBackend(application),
        application,
        current,
    )
    adapter = CockpitCLIAdapter(
        backend=backend,
        **_cockpit_constructor_kwargs(current),
    )
    _copy_cli_handlers(current, adapter)
    _wire_cockpit_compatibility(adapter, backend)
    unregister("cli")
    try:
        register(adapter)
    except Exception:
        # Le remplacement doit être transactionnel même pour un router injecté
        # par une intégration : si le cockpit ne peut pas être enregistré,
        # restaurer le channel configuré plutôt que laisser l'application sans CLI.
        register(current)
        raise
    return adapter


def run(
    config_path: str | Path = "orion.toml",
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Construit et exécute Orion jusqu'à EOF, ``/exit`` ou signal."""
    shutdown = stop_event or threading.Event()
    previous: dict[int, Any] = {}
    try:
        application = load_orion(config_path)
        # Configure the adapter produced by OrionConfig before replacing it so
        # the cockpit bridge can retain legacy-only providers such as threads,
        # trace and jobs.  This only wires callbacks; the legacy adapter is
        # still unregistered before application.start() and never owns stdin.
        configure_cli(application, shutdown)
        cli_adapter = _install_cockpit(application)
        configured_adapter = configure_cli(application, shutdown)
        if configured_adapter is not None:
            cli_adapter = configured_adapter
        if cli_adapter is not None:
            def report_error(event: object, error: Exception) -> None:
                handler = getattr(cli_adapter, "report_error", None)
                if callable(handler):
                    handler(event, error)
                else:
                    _report_error(event, error)
        else:
            report_error = _report_error
            print(
                f"Orion demarre (channels: {', '.join(application.channels.adapters) or 'aucun'})",
                flush=True,
            )

        application.events.on_error = report_error
        application.runtime.on_error = report_error
        interactive_loop = getattr(cli_adapter, "loop", None) if cli_adapter is not None else None
        stop_cli = getattr(cli_adapter, "stop", None) if callable(interactive_loop) else None
        previous = _install_signal_handlers(
            shutdown,
            on_shutdown=stop_cli if callable(stop_cli) else None,
        )
        if callable(interactive_loop):
            # ChannelRouter.start() only installs the cockpit callback; the
            # foreground loop below is the sole owner/reader of stdin.
            application.start()
            try:
                interactive_loop()
            finally:
                shutdown.set()
                application.stop()
        else:
            application.run_forever(shutdown)
    finally:
        _restore_signal_handlers(previous)
    return EXIT_OK


def _once_event(output: Any, *, seq: int) -> dict[str, Any]:
    """Convertit une sortie runtime en événement JSONL stable."""
    intermediate = bool(output.metadata.get("intermediate", False))
    error = bool(output.metadata.get("error", False))
    return {
        "kind": "request.streaming" if intermediate else "request.updated",
        "request_id": output.event_id,
        "correlation_id": _normalized_correlation(output.correlation_id, output.metadata),
        "state": "streaming" if intermediate else ("failed" if error else "succeeded"),
        "seq": seq,
        "text": output.content,
        # Certains adapters (et les tests) ne fournissent pas de timestamp.
        # Le contrat JSONL exige néanmoins une valeur réelle.
        "timestamp": output.metadata.get("timestamp") or _now_iso(),
        "error": output.metadata.get("error") if error else None,
        "meta": {
            key: value
            for key, value in output.metadata.items()
            if key not in {"intermediate", "error", "timestamp"}
        },
    }


def _once_error_event(
    message: str,
    *,
    correlation_id: str,
    seq: int,
    error_type: str = "RuntimeError",
    parent_request_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "request.updated",
        "request_id": None,
        "correlation_id": correlation_id,
        "state": "failed",
        "seq": seq,
        "text": None,
        "timestamp": _now_iso(),
        "error": {"type": error_type, "message": message},
        "meta": {"parent_request_id": parent_request_id, "context": dict(context or {})},
    }


def _system_error_event(error: Exception, *, command: str) -> dict[str, Any]:
    """Construit l'erreur structurée des commandes non interactives."""
    return {
        "kind": "system.error",
        "request_id": None,
        "correlation_id": None,
        "state": "failed",
        "seq": 0,
        "timestamp": _now_iso(),
        "error": {"type": type(error).__name__, "message": str(error)},
        "meta": {"command": command},
    }


def run_once(
    config_path: str | Path,
    prompt: str,
    *,
    output: str = "text",
    timeout: float = 120.0,
    parent_request_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """Exécute une seule demande sans lancer la lecture interactive.

    Le callback runtime est remplacé par un collecteur local : cela rend le
    mode script indépendant d'un TTY et évite de démarrer un second lecteur
    stdin via ``CLIAdapter``.
    """
    if output not in {"text", "jsonl"}:
        raise ValueError("output doit être 'text' ou 'jsonl'.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("--once demande un texte non vide.")
    if timeout <= 0:
        raise ValueError("--timeout doit être positif.")

    application = load_orion(config_path)
    # La demande est envoyée directement au router ; le CLI interactif n'a
    # donc aucune raison de créer son thread de lecture stdin.
    if application.channels is not None:
        application.channels.unregister("cli")

    outputs: list[Any] = []
    output_done = threading.Event()
    output_lock = threading.Lock()
    correlation_id = uuid.uuid4().hex
    accepting = True
    sequence = 0

    def next_seq() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    def capture(agent_output: Any) -> None:
        # Un run --once ne doit ni se terminer sur la sortie d'un autre run,
        # ni imprimer une réponse arrivée après timeout/stop.
        candidate = _normalized_correlation(
            getattr(agent_output, "correlation_id", None),
            getattr(agent_output, "metadata", {}),
        )
        if candidate != correlation_id:
            return
        with output_lock:
            if not accepting:
                return
            # Les transports peuvent rejouer exactement la même sortie.
            output_key = (
                str(getattr(agent_output, "output_id", None) or getattr(agent_output, "event_id", None) or ""),
                str(getattr(agent_output, "metadata", {}).get("seq", "")),
                str(getattr(agent_output, "content", "")),
            )
            if output_key in {
                (str(getattr(item, "output_id", None) or getattr(item, "event_id", None) or ""),
                 str(getattr(item, "metadata", {}).get("seq", "")),
                 str(getattr(item, "content", "")))
                for item in outputs
            }:
                return
            outputs.append(agent_output)
        if not bool(agent_output.metadata.get("intermediate", False)):
            output_done.set()

    application.runtime.on_output = capture
    try:
        application.start()
        if output == "jsonl":
            print(json.dumps({
                "kind": "session.ready",
                "request_id": None,
                "correlation_id": correlation_id,
                "state": "ready",
                "seq": next_seq(),
                "timestamp": _now_iso(),
            }, ensure_ascii=False, default=str), flush=True)
        try:
            application.channels.receive(
                InboundMessage(
                    channel="cli",
                    source="cli",
                    payload={"text": prompt.strip()},
                    reply_to="stdout",
                    correlation_id=correlation_id,
                    metadata={"correlation_id": correlation_id, "parent_request_id": parent_request_id,
                              "context": dict(context or {})},
                    text=prompt.strip(),
                )
            )
        except Exception as exc:
            if output == "jsonl":
                print(json.dumps(_once_error_event(
                    f"{type(exc).__name__}: {exc}",
                    correlation_id=correlation_id,
                    seq=next_seq(),
                    error_type=type(exc).__name__,
                ), ensure_ascii=False, default=str), flush=True)
                print(json.dumps({
                    "kind": "session.stopping",
                    "request_id": None,
                    "correlation_id": correlation_id,
                    "state": "stopping",
                    "seq": next_seq(),
                    "timestamp": _now_iso(),
                }, ensure_ascii=False, default=str), flush=True)
            else:
                print(f"[orion:error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return EXIT_RUNTIME

        if not output_done.wait(timeout):
            with output_lock:
                accepting = False
            if output == "jsonl":
                print(json.dumps(_once_error_event(
                    "TimeoutError: délai dépassé en attente de la réponse.",
                    correlation_id=correlation_id,
                    seq=next_seq(),
                    error_type="TimeoutError",
                ), ensure_ascii=False, default=str), flush=True)
                print(json.dumps({
                    "kind": "session.stopping",
                    "request_id": None,
                    "correlation_id": correlation_id,
                    "state": "stopping",
                    "seq": next_seq(),
                    "timestamp": _now_iso(),
                }, ensure_ascii=False, default=str), flush=True)
            else:
                print("[orion:error] délai dépassé en attente de la réponse.", file=sys.stderr, flush=True)
            return EXIT_REQUEST_FAILED

        with output_lock:
            accepting = False
            captured = list(outputs)
        if output == "jsonl":
            for item in captured:
                print(json.dumps(_once_event(item, seq=next_seq()), ensure_ascii=False, default=str), flush=True)
            print(json.dumps({
                "kind": "session.stopping",
                "request_id": captured[-1].event_id if captured else None,
                "correlation_id": correlation_id,
                "state": "stopping",
                "seq": next_seq(),
                "timestamp": _now_iso(),
            }, ensure_ascii=False, default=str), flush=True)
        else:
            final = next(
                (item for item in reversed(captured)
                 if not bool(item.metadata.get("intermediate", False))),
                None,
            )
            if final is not None:
                print(final.content, flush=True)
        final = next(
            (item for item in reversed(captured)
             if not bool(item.metadata.get("intermediate", False))),
            None,
        )
        return EXIT_REQUEST_FAILED if final is not None and final.metadata.get("error") else EXIT_OK
    finally:
        # ``OrionApplication.stop`` est idempotent et arrête aussi les
        # workers événementiels avant de fermer le client HTTP.
        application.stop()


def run_command(
    config_path: str | Path,
    command: str,
    *,
    output: str = "text",
) -> int:
    """Exécute une commande d'observation locale sans lancer de conversation."""
    normalized = str(command).strip().lstrip("/").lower()
    if normalized not in {"status", "doctor", "health", "readiness"}:
        raise ValueError("--command accepte : status, doctor, health ou readiness.")
    if normalized != "status":
        value = {"doctor": doctor, "health": health, "readiness": readiness}[normalized](config_path)
        if output == "jsonl":
            print(json.dumps({"kind": "system." + normalized, **value}, ensure_ascii=False), flush=True)
        else:
            for key, item in value.items():
                print(f"{key}: {item}")
        return EXIT_OK if value.get("ok", value.get("ready", False)) else EXIT_RUNTIME
    if output not in {"text", "jsonl"}:
        raise ValueError("output doit être 'text' ou 'jsonl'.")
    try:
        application = load_orion(config_path)
    except Exception as exc:
        if output == "jsonl":
            print(json.dumps(_system_error_event(exc, command=normalized), ensure_ascii=False), flush=True)
            return EXIT_RUNTIME
        raise
    try:
        runtime = application.runtime
        values: dict[str, Any] = {
            "runtime": getattr(getattr(runtime, "state", None), "value", "unknown").upper(),
            "model": getattr(application.llm, "model", None),
            "events_processed": getattr(runtime, "wake_count", 0),
            "events_pending": getattr(runtime, "pending_events", 0),
        }
        if output == "jsonl":
            print(json.dumps({
                "kind": "system.status",
                "request_id": None,
                "correlation_id": None,
                "state": values["runtime"],
                "seq": 0,
                "timestamp": _now_iso(),
                "meta": values,
            }, ensure_ascii=False, default=str), flush=True)
        else:
            for key, value in values.items():
                print(f"{key}: {value}")
        return EXIT_OK
    finally:
        application.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lance Orion en mode continu ou une demande unique")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument("--output", choices=("text", "jsonl"), default="text")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", metavar="DEMANDE", help="envoie une demande puis quitte")
    mode.add_argument("--command", metavar="COMMAND", help="exécute une commande locale puis quitte")
    parser.add_argument("--timeout", type=float, default=120.0, help="délai maximal de --once (secondes)")
    args = parser.parse_args(argv)
    try:
        if args.once is not None:
            return run_once(args.config, args.once, output=args.output, timeout=args.timeout)
        if args.command is not None:
            normalized_command = str(args.command).strip().lstrip("/").lower()
            if normalized_command not in {"status", "doctor", "health", "readiness"}:
                parser.print_usage(sys.stderr)
                print(
                    f"{parser.prog}: error: commande inconnue (status, doctor, health, readiness).",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            return run_command(args.config, args.command, output=args.output)
        if args.output != "text":
            parser.error("--output jsonl nécessite --once")
        return run(args.config)
    except (OSError, RuntimeError, ValueError, OpenRouterError) as exc:
        print(f"[orion:error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_RUNTIME


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_INTERRUPTED)
