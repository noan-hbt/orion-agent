"""Voice messages: `[modules.vocal]` in orion.toml (on by default; D71).

Both ways, on iMessage:
- the owner sends a voice message while walking: it is transcribed when it arrives (the
  `photos` module, with an audio model), and his words join his message as his own;
- Orion answers aloud when that serves him: he asked for it, he is driving or his hands are
  busy, or he spoke and the answer is short. `say` turns Orion's text into speech (a speech
  model, word for word), then into an M4A, and sends it in the conversation like any file
  (through the outbox and the guardian, to the owner only).

Speech is cheap (a short sentence costs a few hundredths of a cent) but not free: the text is
capped, and written answers stay the rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.modules.base import Module, ModuleSettings, Plate
from orion.providers.base import ProviderError, Speaker
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.vocal.audio import AudioError, pcm_to_m4a, seconds_of_pcm

if TYPE_CHECKING:
    from orion.files.module import FilesModule

log = structlog.get_logger(__name__)

MAX_SPOKEN = 1200


class VocalSettings(ModuleSettings):
    enabled: bool = True
    model: str | None = Field(default=None, description="Default: agent.speech_model")
    voice: str = Field(default="alloy", description="The speech model's voice")


class SayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=MAX_SPOKEN,
        description="Exactly what to say, written to be heard: short sentences, no lists, no "
        "links, no emojis",
    )


def vocal_tools(
    files: FilesModule, speaker: Speaker, settings: VocalSettings, model: str
) -> list[Tool]:
    async def say(args: Any, ctx: ToolContext) -> ToolResult:
        if files.store is None or files.deliver is None:
            return ToolResult("No channel can carry a voice message here.", is_error=True)
        try:
            pcm = await speaker.speak(model, args.text, settings.voice)
            audio = await pcm_to_m4a(pcm)
        except (ProviderError, AudioError) as exc:
            log.warning("vocal.unspoken", error=str(exc))
            return ToolResult(f"No voice this time ({exc}): answer in writing.", is_error=True)
        made = await files.store.save("vocal.m4a", audio, "audio/mp4", "speech", ctx)
        await files.deliver(made.path, "", ctx.correlation_id)
        return ToolResult(
            f"Vocal envoyé ({seconds_of_pcm(pcm):.0f} s). Il a ta réponse : ne la répète pas "
            "par écrit ; réponds SILENCE, sauf s'il faut un lien ou un chiffre à garder."
        )

    return [
        Tool(
            name="say",
            description="Answer the owner with a voice message instead of text: when he asks, "
            "when he is driving or his hands are busy, or when he spoke to you and the answer "
            "is short.",
            input_model=SayInput,
            handler=say,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.IRREVERSIBLE,
        )
    ]


class VocalModule(Module):
    name = "vocal"
    instructions = "vocal.md"
    settings_model = VocalSettings
    requires = ("files",)

    async def assemble(self, plate: Plate) -> list[Tool]:
        from orion.files.module import FilesModule

        files = plate.module(FilesModule)
        if files is None or not isinstance(plate.provider, Speaker):
            return []  # pragma: no cover - files is required; the router can speak
        settings: VocalSettings = self.settings
        model = settings.model or self.config.agent.speech_model
        return vocal_tools(files, plate.provider, settings, model)
