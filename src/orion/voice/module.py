"""Phone calls as a module, on the iMessage module's Inkbox line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orion.channels.module import IMessageModule
from orion.modules.base import Daemon, Module, ModuleError, Plate
from orion.voice.channel import VoiceChannel, VoiceSettings
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

    def doctor(self) -> list[str]:
        return [f"calls through Inkbox ({self.config.voice.origination})"]
