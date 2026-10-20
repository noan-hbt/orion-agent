"""Phone calls as a module, on the iMessage module's Inkbox line."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uvicorn

from orion.channels.inkbox import verify_signature
from orion.channels.module import IMessageModule
from orion.modules.base import Daemon, Module, ModuleError, Plate
from orion.voice.channel import VoiceChannel, VoiceSettings
from orion.voice.live import LiveVoice, Verify
from orion.voice.tools import PhoneLink, voice_tools

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class VoiceModule(Module):
    name = "voice"
    requires = ("imessage",)
    instructions = "voice.md"

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.link = PhoneLink()
        self.voice: VoiceChannel | None = None
        self.live: LiveVoice | None = None

    @property
    def enabled(self) -> bool:
        return self.config.voice.enabled

    async def assemble(self, plate: Plate) -> list[Tool]:
        return voice_tools(self.link)

    async def serve(self, daemon: Daemon) -> None:
        line = daemon.plate.module(IMessageModule)
        if line is None or line.imessage is None:  # pragma: no cover - `requires` guarantees it
            raise ModuleError("voice needs the iMessage line to be connected first")
        cfg, voice_cfg = self.config.channels.imessage, self.config.voice
        voice = VoiceChannel(
            line.client,
            daemon.plate.journal,
            daemon.inbox,
            VoiceSettings(
                owner_number=cfg.owner_number,
                handle=cfg.identity,
                identity_id=line.identity_id,
                timezone=self.config.agent.timezone,
                origination=voice_cfg.origination,
                reply_via=line.imessage.name,
                voice=voice_cfg.voice,
                live_url=voice_cfg.live_url if voice_cfg.live else None,
            ),
            daemon.plate.memory.store,
            daemon.plate.timers.projection,
            daemon.orion.threads,
        )
        await voice.start()
        self.voice = voice
        self.link.target = voice
        daemon.reports.append(voice)
        daemon.caller = voice
        line.on_event("call.", voice.handle_webhook)
        daemon.add_service("voice.brief", lambda: voice.run_briefs(voice_cfg.brief_seconds))
        if cfg.mode == "poll":
            daemon.add_service(
                "voice.poll", lambda: voice.poll_forever(voice_cfg.calls_poll_seconds)
            )
        if voice_cfg.live:
            self._serve_live(daemon, voice)

    def _serve_live(self, daemon: Daemon, voice: VoiceChannel) -> None:
        voice_cfg = self.config.voice
        if not voice_cfg.live_url.startswith("wss://"):
            raise ModuleError('[voice] live needs live_url = "wss://…/voice/live"')
        guardian = daemon.plate.guardian
        key = self.config.channels.imessage.signing_key

        async def locally(request_id: str, timestamp: str, signature: str, body: bytes) -> bool:
            return verify_signature(
                key.resolve().get_secret_value(), request_id, timestamp, signature, body
            )

        verify: Verify = guardian.verify_webhook if guardian is not None else locally

        live = LiveVoice(
            daemon.plate.journal,
            daemon.inbox,
            self.config.channels.imessage.owner_number,
            verify,
            voice.opening,
        )
        self.live = live
        daemon.inbox.register(live)
        server = uvicorn.Server(
            uvicorn.Config(
                live.app(),
                host=voice_cfg.live_host,
                port=voice_cfg.live_port,
                lifespan="off",
                log_config=None,
                access_log=False,
            )
        )
        daemon.add_service("voice.live", server.serve)

    def doctor(self) -> list[str]:
        v = self.config.voice
        live = f", live at {v.live_url or '(live_url missing)'}" if v.live else ""
        return [f"calls through Inkbox ({v.origination}){live}"]
