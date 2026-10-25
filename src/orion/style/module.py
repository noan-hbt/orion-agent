"""Style: `[modules.style]` in orion.toml (on by default).

« Réponds à Paul que c'est ok pour jeudi. » The draft should sound like the owner, not like an
assistant: his length, his capitals or their absence, his punctuation, his « mdr ». Orion
learns it from his own messages (D68), with no model in between: a projection of the journal
counts what is countable and keeps a few real examples. What changes from one person to
another (he writes to his mother with care, to Paul in three words) comes from memory: what
he said about how he talks to them.

`owner_style` gives both, for a person if named. Orion calls it before writing anything that
will go out in the owner's name.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.memory.service import MemoryService
from orion.memory.store import name_key
from orion.modules.base import Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

RECEIVED = "message.received"
KEEP = 400
"""The owner's last messages the card is drawn from."""
EXAMPLES = 8
MIN_MESSAGES = 5
_LINK = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_WORD = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœ']+", re.IGNORECASE)
_SLANG = {
    "mdr", "ptdr", "jsp", "stp", "svp", "tkt", "jpp", "osef", "bcp", "pk", "pq", "pcq",
    "dsl", "cc", "slt", "bjr", "ouais", "wesh", "grave", "trop", "genre", "ok", "oké", "okk",
}  # fmt: skip


class StyleSettings(ModuleSettings):
    enabled: bool = True


class OwnerWriting:
    """The owner's recent messages to Orion, as he typed them. A projection of the journal."""

    name = "style"

    def __init__(self) -> None:
        self.messages: deque[str] = deque(maxlen=KEEP)
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.messages.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or event.kind != RECEIVED:
            return
        payload: dict[str, Any] = event.payload
        text = str(payload.get("text") or "").strip()
        if text and event.provenance.actor == "owner:main":
            self.messages.append(text)


def _share(part: int, whole: int) -> str:
    share = part / whole if whole else 0.0
    if share >= 0.8:
        return "presque toujours"
    if share >= 0.5:
        return "souvent"
    if share >= 0.2:
        return "parfois"
    return "rarement"


def style_card(messages: list[str]) -> str:
    """What is countable in how he writes, then a few of his real messages."""
    texts = [m for m in messages if not _LINK.fullmatch(m.strip())]
    if len(texts) < MIN_MESSAGES:
        return (
            "Pas encore assez de ses messages pour connaître son style : écris simple, court, "
            "naturel, comme un message qu'on tape vite, sans formule."
        )
    lengths = [len(m) for m in texts]
    lower = sum(1 for m in texts if m[0].isalpha() and m[0].islower())
    ending = sum(1 for m in texts if m.rstrip()[-1] in ".!?…")
    emoji = sum(1 for m in texts if _EMOJI.search(m))
    words = Counter(w.lower() for m in texts for w in _WORD.findall(m))
    slang = [w for w, _ in words.most_common() if w in _SLANG][:8]
    lines = [
        f"D'après ses {len(texts)} derniers messages :",
        f"- longueur : {int(statistics.median(lengths))} caractères en médiane",
        f"- commence par une minuscule : {_share(lower, len(texts))}",
        f"- finit par un point ou une ponctuation : {_share(ending, len(texts))}",
        f"- emojis : {_share(emoji, len(texts))}",
    ]
    if slang:
        lines.append(f"- ses mots : {', '.join(slang)}")
    middle = sorted(texts[-60:], key=lambda m: abs(len(m) - statistics.median(lengths)))
    examples = sorted(middle[:EXAMPLES], key=texts.index)
    lines.append("Exemples, tels qu'il les a écrits :")
    lines += [f"« {m[:200]} »" for m in examples]
    return "\n".join(lines)


class StyleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(
        default="", max_length=80, description="Who the text is for (« Paul », « ma mère »)"
    )


def style_tools(
    journal: EventStore, writing: OwnerWriting, memory: MemoryService | None
) -> list[Tool]:
    runner = ProjectionRunner(journal, [writing])

    async def style(args: Any, _ctx: ToolContext) -> ToolResult:
        await runner.catch_up()
        parts = [style_card(list(writing.messages))]
        if args.to and memory is not None:
            # Whole words of the name: « Paul » is « Paul Martin », not « Pauline » nor an
            # entry of one letter that every name holds.
            wanted = set(name_key(args.to).split())
            known = [
                f.statement
                for f in await memory.store.facts()
                if any(
                    (words := set(name_key(a).split())) and (words <= wanted or wanted <= words)
                    for a in f.about
                    if a != "owner"
                )
            ]
            if known:
                parts.append(
                    f"Ce que tu sais de {args.to} et de leur façon de se parler :\n"
                    + "\n".join(f"- {s}" for s in known[:10])
                )
            else:
                parts.append(f"Rien en mémoire sur sa façon d'écrire à {args.to}.")
        return ToolResult("\n\n".join(parts))

    return [
        Tool(
            name="owner_style",
            description="How the owner writes (from his own messages), and what you know of "
            "how he talks to a person: call it before drafting anything that goes out in his "
            "name, so it sounds like him.",
            input_model=StyleInput,
            handler=style,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]


class StyleModule(Module):
    name = "style"
    instructions = "style.md"
    settings_model = StyleSettings

    async def assemble(self, plate: Plate) -> list[Tool]:
        return style_tools(plate.journal, OwnerWriting(), plate.memory)
