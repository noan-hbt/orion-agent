"""`orion serve`: the always-on daemon.

The core services (the owner-turn worker, the initiative engine) plus whatever the enabled
modules add: channels, voice, background learning. If a service keeps crashing, the supervisor
stops everything after trying to tell the owner, rather than running half-dead.
"""

from __future__ import annotations

import structlog

from orion.app import App
from orion.channels.inbox import OwnerInbox
from orion.cognition.verifier import Verifier
from orion.core.supervisor import Supervisor
from orion.initiative.engine import InitiativeEngine, InitiativeSettings
from orion.modules.base import Daemon, ModuleError
from orion.runtime import OWNER_THREAD, TurnResult

log = structlog.get_logger(__name__)

ServeError = ModuleError
"""Configuration problem that prevents the daemon from starting."""


def owner_inbox(app: App) -> OwnerInbox:
    async def consolidate(turn: TurnResult) -> None:
        report = await app.orion.consolidate(turn)
        if report and report.applied:
            log.info("memory.consolidated", applied=report.applied)

    return OwnerInbox(app.orion, app.config.channels.imessage.coalesce_seconds, on_turn=consolidate)


async def start(app: App, inbox: OwnerInbox, supervisor: Supervisor) -> Daemon:
    """Plug every module into the daemon, then the initiative engine on top of them."""
    if not any(module.channel for module in app.modules):
        raise ModuleError(
            "no owner channel is enabled (e.g. `enabled = true` in [channels.imessage])"
        )
    daemon = Daemon(app.plate, app.orion, inbox, supervisor)
    supervisor.add("inbox", inbox.run)
    for module in app.modules:
        await module.serve(daemon)

    initiative = app.config.initiative
    if initiative.enabled:
        engine = InitiativeEngine(
            app.orion,
            app.journal,
            app.timers,
            inbox,
            InitiativeSettings(
                timezone=app.config.agent.timezone,
                thread_id=OWNER_THREAD,
                digests=tuple(initiative.digests),
                quiet_start=initiative.quiet_start,
                quiet_end=initiative.quiet_end,
                daily_budget=initiative.daily_budget,
            ),
            memory=app.memory.store,
            verifier=Verifier(app.provider, app.config.agent.verifier_model),
            reports=daemon.reports,
            caller=daemon.caller,
            alerts=daemon.alerts,
            digests=daemon.digests,
        )
        supervisor.add("initiative", lambda: engine.run(initiative.tick_seconds))
    log.info(
        "orion.serving",
        modules=[module.name for module in app.modules],
        initiative=initiative.enabled,
    )
    return daemon


async def serve(app: App) -> None:
    inbox = owner_inbox(app)

    async def escalate(service: str, error: BaseException) -> None:
        try:
            await inbox.notify(
                f"Je m'arrête : le service {service} plante en boucle ({error!r}). "
                "Regarde les logs du serveur.",
                correlation_id="supervisor",
            )
        except Exception:
            log.exception("supervisor.escalation_failed", service=service)

    supervisor = Supervisor(escalate)
    await start(app, inbox, supervisor)
    await supervisor.run()
