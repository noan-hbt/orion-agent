"""`reread`: a tool result shortened in the window, whole again from the journal (D204).

Pruning (`thread.pruned`) shows the start of an old result; the journal keeps all of it. Reading
it back costs one call instead of fetching the page again, which may have changed since. It comes
back with the trust it had when it was received: a web page stays a web page.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event
from orion.core.journal import EventStore
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

PAGE = 1000
"""Journal events read at a time while looking for a result."""


class RereadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str = Field(
        min_length=1, max_length=200, description="The id given in the shortened result's note"
    )


async def _find(journal: EventStore, thread_id: str, call_id: str) -> Event | None:
    """The latest result of this call in this thread; a forgotten one is not found."""
    found: Event | None = None
    after = 0
    while events := await journal.read(after, PAGE):
        for event in events:
            if (
                event.kind == MESSAGE_KIND
                and not event.redacted
                and event.payload.get("thread_id") == thread_id
                and event.payload["message"].get("role") == "tool"
                and event.payload["message"].get("tool_call_id") == call_id
            ):
                found = event
        after = events[-1].seq
    return found


def reread_tools(journal: EventStore) -> list[Tool]:
    async def reread(args: Any, ctx: ToolContext) -> ToolResult:
        event = await _find(journal, ctx.thread_id, args.result)
        if event is None:
            return ToolResult(f"Aucun résultat {args.result} dans cette conversation.", True)
        return ToolResult(str(event.payload["message"].get("content") or ""), trust=event.trust)

    return [
        Tool(
            name="reread",
            description="A shortened old tool result, whole, when its note names an id and the "
            "part you need was cut. Cheaper and more faithful than fetching it again.",
            input_model=RereadInput,
            handler=reread,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]
