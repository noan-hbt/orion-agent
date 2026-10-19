"""`orion serve`: the always-on daemon.

Supervised services: the iMessage inbound side (polling or webhook server), the owner-turn
worker, and the initiative engine. If a service keeps crashing, the supervisor stops everything
after trying to tell the owner, rather than running half-dead.
"""

from __future__ import annotations

import structlog
import uvicorn

from orion.adaptation.reflector import Reflector
from orion.adaptation.service import AdaptationSettings, Adapter
from orion.app import App
from orion.channels.imessage import IMessageChannel, IMessageSettings
from orion.channels.inkbox import InkboxClient
from orion.channels.webhook import webhook_app
from orion.cognition.verifier import Verifier
from orion.core.supervisor import Supervisor
from orion.initiative.engine import InitiativeEngine, InitiativeSettings
from orion.runtime import OWNER_THREAD, TurnResult

log = structlog.get_logger(__name__)


class ServeError(RuntimeError):
    """Configuration problem that prevents the daemon from starting."""


async def connect_imessage(app: App, client: InkboxClient) -> IMessageChannel:
    cfg = app.config.channels.imessage
    if not cfg.identity or not cfg.owner_number:
        raise ServeError("channels.imessage needs `identity` and `owner_number` in orion.toml")
    identity = await client.identity(cfg.identity)
    if identity is None:
        raise ServeError(f"Inkbox identity {cfg.identity!r} not found: run `orion inkbox setup`")
    if not identity.imessage_enabled:
        raise ServeError(f"iMessage is not enabled on {cfg.identity!r}: run `orion inkbox setup`")

    async def consolidate(turn: TurnResult) -> None:
        report = await app.orion.consolidate(turn)
        if report and report.applied:
            log.info("memory.consolidated", applied=report.applied)

    channel = IMessageChannel(
        client,
        app.journal,
        app.orion,
        IMessageSettings(
            owner_number=cfg.owner_number,
            identity_id=identity.id,
            coalesce_seconds=cfg.coalesce_seconds,
            approval_timeout_seconds=cfg.approval_timeout_seconds,
        ),
        on_turn=consolidate,
    )
    await channel.start()
    app.approver.target = channel
    return channel


async def serve(app: App, client: InkboxClient) -> None:
    channel = await connect_imessage(app, client)
    cfg = app.config.channels.imessage

    async def escalate(service: str, error: BaseException) -> None:
        try:
            await channel.notify(
                f"Je m'arrête : le service {service} plante en boucle ({error!r}). "
                "Regarde les logs du serveur.",
                correlation_id="supervisor",
            )
        except Exception:
            log.exception("supervisor.escalation_failed", service=service)

    supervisor = Supervisor(escalate)
    supervisor.add("imessage.turns", channel.run_turns)
    if cfg.mode == "poll":
        supervisor.add("imessage.poll", lambda: channel.poll_forever(cfg.poll_seconds))
    else:
        signing_key = cfg.signing_key.resolve().get_secret_value()
        server = uvicorn.Server(
            uvicorn.Config(
                webhook_app(cfg.webhook_path, signing_key, channel.handle_webhook),
                host=cfg.webhook_host,
                port=cfg.webhook_port,
                log_config=None,
                access_log=False,
            )
        )
        supervisor.add("imessage.webhook", server.serve)

    initiative = app.config.initiative
    if initiative.enabled:
        engine = InitiativeEngine(
            app.orion,
            app.journal,
            app.timers,
            channel,
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
        )
        supervisor.add("initiative", lambda: engine.run(initiative.tick_seconds))

    adaptation = app.config.adaptation
    if adaptation.enabled:
        adapter = Adapter(
            app.journal,
            app.memory,
            Reflector(app.provider, app.memory, app.config.agent.extractor_model),
            AdaptationSettings(
                timezone=app.config.agent.timezone,
                digests=tuple(initiative.digests),
                reflect_after_messages=adaptation.reflect_after_messages,
                reflect_daily_after_messages=adaptation.reflect_daily_after_messages,
            ),
        )
        supervisor.add("adaptation", lambda: adapter.run(adaptation.tick_seconds))

    log.info(
        "orion.serving",
        mode=cfg.mode,
        initiative=initiative.enabled,
        adaptation=adaptation.enabled,
    )
    if not await channel.available():
        triage = await client.triage_number()
        log.warning(
            "imessage.owner_not_connected",
            hint=f"text '{triage.connect_command}' to {triage.number} from {cfg.owner_number}",
        )
    await supervisor.run()
