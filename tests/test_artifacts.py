"""ORI-017: a long tool output is clipped for the model but kept whole, and read back."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from orion.artifacts.module import ArtifactInput, Artifacts
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.runtime import Orion
from orion.tools.base import Effect, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import ScriptedProvider, call_tools, reply


class _NoInput(BaseModel):
    pass


async def test_a_needle_in_a_cut_output_is_found_again(journal: SqliteJournal) -> None:
    page = "\n".join(f"ligne {n} sans intérêt" for n in range(5000))
    page = page.replace("ligne 2500 sans intérêt", "ligne 2500 : code d'accès AIGUILLE-42")

    async def read(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult(page, trust=Trust.THIRD_PARTY)

    site = Tool(name="site", description="d", input_model=_NoInput, handler=read,
                effect=Effect.NONE, accepts_untrusted_context=True)  # fmt: skip
    provider = ScriptedProvider(call_tools(("site", {})), reply("Lu."))
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([site]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=AgentConfig(),
    )
    await orion.start()
    await orion.handle_owner_message("owner", "lis la page", "terminal")
    shown = str(next(m["content"] for m in provider.requests[1].messages if m["role"] == "tool"))
    assert "AIGUILLE" not in shown  # cut out of what the model read

    artifact_id = shown.split("kept as ", 1)[1].split(":", 1)[0]
    found = await Artifacts(journal).handle(
        ArtifactInput(action="search", id=artifact_id, query="aiguille"), ToolContext("t", "c")
    )
    assert "AIGUILLE-42" in found.content
    assert found.trust == Trust.THIRD_PARTY  # what is read keeps the trust of its source
