"""ORI-019: a connector that falls is one event, and the tool says it is unavailable."""

from __future__ import annotations

from orion.capabilities.module import CHANGED, Health, NoInput
from orion.config import OrionConfig
from orion.core.journal import SqliteJournal
from orion.modules.base import Capability, Module
from orion.tools.base import ToolContext


class Inbox(Module):
    name = "inbox"

    def __init__(self) -> None:
        super().__init__(OrionConfig())
        self.up = True

    def capabilities(self) -> list[Capability]:
        return [Capability("mail:perso", self.up, "" if self.up else "mot de passe refusé")]


async def test_a_connector_that_falls_is_one_event_and_said_unavailable(
    journal: SqliteJournal,
) -> None:
    inbox = Inbox()
    health = Health(journal, [inbox])
    await health.check()
    inbox.up = False
    for _ in range(3):  # checked again and again while it is down
        await health.check()
    events = [e for e in await journal.read(0, 100) if e.kind == CHANGED]
    assert [e.payload["available"] for e in events] == [True, False]

    said = await health.handle(NoInput(), ToolContext("t", "c"))
    assert "INDISPONIBLE" in said.content and "mail:perso" in said.content
