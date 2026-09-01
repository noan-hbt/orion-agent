"""Lance Orion comme processus long-vivant.

Usage : ``python orion_run.py --config orion.toml``.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from orion_config import load_orion


def _report_error(event: object, error: Exception) -> None:
    event_id = getattr(event, "id", "unknown")
    event_type = getattr(event, "type", "unknown")
    print(
        f"[orion:error] event={event_type} id={event_id}: "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lance Orion en mode continu")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    args = parser.parse_args()

    shutdown = threading.Event()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, lambda *_: shutdown.set())

    application = load_orion(args.config)
    cli_adapter = application.channels.adapters.get("cli")
    if cli_adapter is not None:
        if callable(getattr(cli_adapter, "set_exit_handler", None)):
            cli_adapter.set_exit_handler(shutdown.set)

        def cli_status() -> dict[str, object]:
            runtime = application.runtime
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
                "Phase RUN": (
                    run_context.phase.value.upper()
                    if run_context is not None
                    else "aucune"
                ),
                "Tâche active": (
                    f"#{task.id} · {task.objective}" if task is not None else "aucune"
                ),
            }
            if runtime.last_error is not None:
                values["Dernière erreur"] = type(runtime.last_error).__name__
            if application.subagents is not None:
                jobs = application.subagents.list_jobs(limit=100)
                values["Sous-agents"] = len(application.subagents.list_agents())
                values["Jobs actifs"] = sum(
                    1 for job in jobs if job.status.value in {"queued", "running"}
                )
            return values

        def cli_tools() -> list[dict[str, str]]:
            rows: list[dict[str, str]] = []
            for definition in application.runtime._tool_definitions():
                function = definition.get("function", {})
                name = str(function.get("name", "tool"))
                description = str(function.get("description", ""))
                rows.append({"id": name, "label": description})
            return sorted(rows, key=lambda item: item["id"])

        def cli_tasks() -> list[dict[str, object]]:
            tasks = application.runtime.task_store.list()
            return [
                {
                    "id": f"#{task.id}",
                    "objective": task.objective,
                    "status": task.status.value,
                }
                for task in reversed(tasks[-10:])
            ]

        def cli_agents() -> list[dict[str, object]]:
            if application.subagents is None:
                return []
            return [
                {
                    "id": agent.id,
                    "name": f"{agent.name} · {agent.model}",
                    "status": agent.status.value,
                }
                for agent in application.subagents.list_agents()
            ]

        def cli_jobs() -> list[dict[str, object]]:
            if application.subagents is None:
                return []
            return [
                {
                    "id": job.id,
                    "objective": job.objective,
                    "status": job.status.value,
                }
                for job in application.subagents.list_jobs(limit=20)
            ]

        cli_adapter.set_status_provider(cli_status)
        cli_adapter.set_tools_provider(cli_tools)
        cli_adapter.set_tasks_provider(cli_tasks)
        cli_adapter.set_agents_provider(cli_agents)
        cli_adapter.set_jobs_provider(cli_jobs)

        def report_error(event: object, error: Exception) -> None:
            cli_adapter.report_error(event, error)

    else:
        report_error = _report_error

    # Les workers sont asynchrones : sans callbacks, une erreur OpenRouter ou
    # de channel peut sinon laisser uniquement le prompt CLI visible.
    application.events.on_error = report_error
    application.runtime.on_error = report_error
    if cli_adapter is None:
        print(
            f"Orion demarre (channels: {', '.join(application.channels.adapters) or 'aucun'})",
            flush=True,
        )
    application.run_forever(shutdown)


if __name__ == "__main__":
    main()
