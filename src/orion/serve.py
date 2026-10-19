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
from orion.channels.inbox import OwnerInbox
from orion.channels.inkbox import InkboxClient, WebhookEvent
from orion.channels.webhook import webhook_app
from orion.cognition.verifier import Verifier
from orion.core.supervisor import Supervisor
from orion.initiative.engine import InitiativeEngine, InitiativeSettings
from orion.runtime import OWNER_THREAD, TurnResult
from orion.voice.channel import VoiceChannel, VoiceSettings

log = structlog.get_logger(__name__)


class ServeError(RuntimeError):
    """Configuration problem that prevents the daemon from starting."""


def owner_inbox(app: App) -> OwnerInbox:
    async def consolidate(turn: TurnResult) -> None:
        report = await app.orion.consolidate(turn)
        if report and report.applied:
            log.info("memory.consolidated", applied=report.applied)

    return OwnerInbox(app.orion, app.config.channels.imessage.coalesce_seconds, on_turn=consolidate)


async def connect_imessage(app: App, client: InkboxClient, inbox: OwnerInbox) -> IMessageChannel:
    cfg = app.config.channels.imessage
    if not cfg.identity or not cfg.owner_number:
        raise ServeError("channels.imessage needs `identity` and `owner_number` in orion.toml")
    identity = await client.identity(cfg.identity)
    if identity is None:
        raise ServeError(f"Inkbox identity {cfg.identity!r} not found: run `orion inkbox setup`")
    if not identity.imessage_enabled:
        raise ServeError(f"iMessage is not enabled on {cfg.identity!r}: run `orion inkbox setup`")

    channel = IMessageChannel(
        client,
        app.journal,
        inbox,
        IMessageSettings(
            owner_number=cfg.owner_number,
            identity_id=identity.id,
            approval_timeout_seconds=cfg.approval_timeout_seconds,
        ),
    )
    await channel.start()
    app.approver.target = channel
    return channel


async def connect_voice(
    app: App, client: InkboxClient, inbox: OwnerInbox, imessage: IMessageChannel
) -> VoiceChannel:
    cfg = app.config.channels.imessage
    identity = await client.identity(cfg.identity)
    if identity is None:  # pragma: no cover - connect_imessage checked it a moment ago
        raise ServeError(f"Inkbox identity {cfg.identity!r} not found")
    voice = VoiceChannel(
        client,
        app.journal,
        inbox,
        VoiceSettings(
            owner_number=cfg.owner_number,
            handle=cfg.identity,
            identity_id=identity.id,
            timezone=app.config.agent.timezone,
            origination=app.config.voice.origination,
            reply_via=imessage.name,
            voice=app.config.voice.voice,
        ),
        app.memory.store,
        app.timers.projection,
        app.orion.threads,
    )
    await voice.start()
    if app.phone is not None:
        app.phone.target = voice
    return voice


async def serve(app: App, client: InkboxClient) -> None:
    inbox = owner_inbox(app)
    channel = await connect_imessage(app, client, inbox)
    cfg = app.config.channels.imessage

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
    supervisor.add("inbox", inbox.run)

    voice = await connect_voice(app, client, inbox, channel) if app.config.voice.enabled else None
    if voice is not None:
        voice_cfg = app.config.voice
        supervisor.add("voice.brief", lambda: voice.run_briefs(voice_cfg.brief_seconds))
        if cfg.mode == "poll":
            supervisor.add("voice.poll", lambda: voice.poll_forever(voice_cfg.calls_poll_seconds))

    async def dispatch(event: WebhookEvent) -> None:
        if event.event_type.startswith("call.") and voice is not None:
            await voice.handle_webhook(event)
        else:
            await channel.handle_webhook(event)

    if cfg.mode == "poll":
        supervisor.add("imessage.poll", lambda: channel.poll_forever(cfg.poll_seconds))
    else:
        signing_key = cfg.signing_key.resolve().get_secret_value()
        server = uvicorn.Server(
            uvicorn.Config(
                webhook_app(cfg.webhook_path, signing_key, dispatch),
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
            reports=[r for r in (app.jobs, voice) if r is not None],
            caller=voice,
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
        voice=voice is not None,
    )
    if not await channel.available():
        triage = await client.triage_number()
        log.warning(
            "imessage.owner_not_connected",
            hint=f"text '{triage.connect_command}' to {triage.number} from {cfg.owner_number}",
        )
    await supervisor.run()
