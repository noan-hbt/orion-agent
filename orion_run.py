"""Lance Orion comme processus long-vivant.

Usage : ``python orion_run.py --config orion.toml``.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from channels import InboundMessage
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


def _report_error(event: object, error: Exception) -> None:
    event_id = getattr(event, "id", "unknown")
    event_type = getattr(event, "type", "unknown")
    print(
        f"[orion:error] event={event_type} id={event_id}: "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )


def _install_signal_handlers(shutdown: threading.Event) -> dict[int, Any]:
    """Installe les signaux d'arrêt et retourne les handlers précédents."""
    previous: dict[int, Any] = {}
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        previous[signal_value] = signal.getsignal(signal_value)
        signal.signal(signal_value, lambda *_: shutdown.set())
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


def run(
    config_path: str | Path = "orion.toml",
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Construit et exécute Orion jusqu'à EOF, ``/exit`` ou signal."""
    shutdown = stop_event or threading.Event()
    previous = _install_signal_handlers(shutdown)
    try:
        application = load_orion(config_path)
        cli_adapter = configure_cli(application, shutdown)
        if cli_adapter is not None:
            def report_error(event: object, error: Exception) -> None:
                cli_adapter.report_error(event, error)
        else:
            report_error = _report_error
            print(
                f"Orion demarre (channels: {', '.join(application.channels.adapters) or 'aucun'})",
                flush=True,
            )

        application.events.on_error = report_error
        application.runtime.on_error = report_error
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
        "correlation_id": output.correlation_id or output.metadata.get("correlation_id"),
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

    def capture(agent_output: Any) -> None:
        with output_lock:
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
                "seq": 0,
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
                    seq=0,
                    error_type=type(exc).__name__,
                ), ensure_ascii=False, default=str), flush=True)
                print(json.dumps({
                    "kind": "session.stopping",
                    "request_id": None,
                    "correlation_id": correlation_id,
                    "state": "stopping",
                    "seq": 1,
                    "timestamp": _now_iso(),
                }, ensure_ascii=False, default=str), flush=True)
            else:
                print(f"[orion:error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return EXIT_RUNTIME

        if not output_done.wait(timeout):
            if output == "jsonl":
                print(json.dumps(_once_error_event(
                    "TimeoutError: délai dépassé en attente de la réponse.",
                    correlation_id=correlation_id,
                    seq=0,
                    error_type="TimeoutError",
                ), ensure_ascii=False, default=str), flush=True)
                print(json.dumps({
                    "kind": "session.stopping",
                    "request_id": None,
                    "correlation_id": correlation_id,
                    "state": "stopping",
                    "seq": 1,
                    "timestamp": _now_iso(),
                }, ensure_ascii=False, default=str), flush=True)
            else:
                print("[orion:error] délai dépassé en attente de la réponse.", file=sys.stderr, flush=True)
            return EXIT_REQUEST_FAILED

        with output_lock:
            captured = list(outputs)
        if output == "jsonl":
            for seq, item in enumerate(captured):
                print(json.dumps(_once_event(item, seq=seq), ensure_ascii=False, default=str), flush=True)
            print(json.dumps({
                "kind": "session.stopping",
                "request_id": captured[-1].event_id if captured else None,
                "correlation_id": correlation_id,
                "state": "stopping",
                "seq": len(captured),
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
    application = load_orion(config_path)
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
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[orion:error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_RUNTIME


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_INTERRUPTED)
