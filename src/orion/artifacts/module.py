"""Large tool outputs, kept whole and readable (ORI-017, D282): `[modules.artifacts]`, on by
default.

A tool result longer than its limit is clipped for the model (`cognition.loop._clip`), but the
whole of it goes in the journal as `tool.artifact` (up to `MAX_BYTES`; past that, the first
part, and the event says so). The clipped text names the artifact; the `artifact` tool reads a
range of its lines or searches it. The journal is the only store: rebuilt from it, forgotten
with `redact` like everything else. What is read keeps the trust of the output it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from orion.core.events import Event
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

ARTIFACT = "tool.artifact"
LINES_AT_ONCE = 400
HITS_AT_MOST = 30
AROUND = 1


@dataclass
class ArtifactIndex:
    """artifact id → the seq of its event. A projection of the journal."""

    name: str = "artifacts"
    seqs: dict[str, int] = field(default_factory=dict[str, int])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.seqs.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.kind == ARTIFACT and not event.redacted:
            self.seqs[str(event.payload["artifact_id"])] = event.seq


class ArtifactInput(BaseModel):
    action: Literal["read", "search"]
    id: str = Field(description="The artifact id named where the output was cut")
    start: int = Field(default=1, ge=1, description="read: first line (from 1)")
    end: int | None = Field(default=None, ge=1, description="read: last line, included")
    query: str = Field(default="", description="search: words or a regular expression")


class Artifacts:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.index = ArtifactIndex()
        self._runner = ProjectionRunner(journal, [self.index])

    async def find(self, artifact_id: str) -> Event | None:
        await self._runner.catch_up()
        seq = self.index.seqs.get(artifact_id)
        if seq is None:
            return None
        found = await self._journal.read(seq - 1, 1)
        return found[0] if found and not found[0].redacted else None

    async def handle(self, args: ArtifactInput, _ctx: ToolContext) -> ToolResult:
        event = await self.find(args.id.strip())
        if event is None:
            return ToolResult(f"No artifact {args.id} (unknown, or forgotten).", is_error=True)
        lines = str(event.payload["content"]).splitlines()
        head = f"[artifact {args.id} · {len(lines)} lignes"
        head += " · tronqué au premier Mo]" if event.payload.get("truncated") else "]"
        if args.action == "read":
            end = min(args.end or args.start + LINES_AT_ONCE - 1, args.start + LINES_AT_ONCE - 1)
            shown = lines[args.start - 1 : end]
            body = "\n".join(f"{n}: {line}" for n, line in enumerate(shown, args.start))
            more = f"\n[lignes {args.start}-{min(end, len(lines))} sur {len(lines)}]"
            return ToolResult(f"{head}\n{body}{more}", trust=event.trust)
        if not args.query.strip():
            return ToolResult("search needs a query.", is_error=True)
        try:
            pattern = re.compile(args.query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(args.query), re.IGNORECASE)
        hits = [n for n, line in enumerate(lines) if pattern.search(line)]
        if not hits:
            return ToolResult(
                f"{head}\nAucune ligne ne contient « {args.query} ».", trust=event.trust
            )
        parts: list[str] = []
        for n in hits[:HITS_AT_MOST]:
            low, high = max(0, n - AROUND), min(len(lines), n + AROUND + 1)
            parts.append("\n".join(f"{i + 1}: {lines[i]}" for i in range(low, high)))
        more = (
            f"\n[{len(hits)} lignes trouvées, {HITS_AT_MOST} montrées]"
            if len(hits) > HITS_AT_MOST
            else ""
        )
        return ToolResult(f"{head}\n" + "\n---\n".join(parts) + more, trust=event.trust)


def artifact_tool(artifacts: Artifacts) -> Tool:
    return Tool(
        name="artifact",
        description=(
            "Read a tool output that was cut: `read` a range of its lines (start, end), or "
            "`search` it for words. Its id is named where the output was cut."
        ),
        input_model=ArtifactInput,
        handler=artifacts.handle,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        concurrency_safe=True,
        accepts_untrusted_context=True,
    )


class ArtifactSettings(ModuleSettings):
    enabled: bool = True


class ArtifactsModule(Module):
    name = "artifacts"
    settings_model = ArtifactSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.artifacts: Artifacts | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.artifacts = Artifacts(plate.journal)
        return [artifact_tool(self.artifacts)]
