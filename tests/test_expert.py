"""A stronger model on call: consulted for one question, or used for the rest of a turn."""

from __future__ import annotations

from orion.cognition.loop import LoopSettings, run_agent
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.expert.module import CONSULTED, SWITCHED, Expert, ExpertSettings, expert_tools
from orion.guardian.policy import Guardian
from orion.providers.base import Message, ModelRequest, ModelResponse
from orion.tools.base import ToolContext, ToolRegistry
from tests.fakes import ScriptedProvider, call_tools, reply


class Routed(ScriptedProvider):
    """The expert answers itself; the conversation follows the script."""

    def __init__(self, *script: object) -> None:
        super().__init__(*script)  # pyright: ignore[reportArgumentType]
        self.expert: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.session_id == "orion:expert":
            self.expert.append(request)
            return reply("Prends le T2 de Dreux : 640 €, gare à 5 min.")(request)
        return await super().complete(request)


async def test_orion_consults_the_expert_and_stays_on_its_model(journal: SqliteJournal) -> None:
    provider = Routed(
        call_tools(
            ("consult_expert", {"question": "Lequel des deux ?", "context": "T2 640 €, T3 720 €"})
        ),
        reply("Le T2, d'après mon calcul."),
    )
    expert = Expert(journal, provider, ExpertSettings(model="big/model"))
    messages: list[Message] = [{"role": "user", "content": "lequel ?"}]
    settings = LoopSettings(model="small/model", session_id="s", thread_id="t", correlation_id="c")
    _ = [
        e
        async for e in run_agent(
            provider, ToolRegistry(expert_tools(expert)), Guardian(), messages, settings, Trust.WEB
        )
    ]

    [asked] = provider.expert
    assert asked.model == "big/model" and "T2 640 €" in str(asked.messages[-1]["content"])
    assert all(r.model == "small/model" for r in provider.requests)
    assert "gare à 5 min" in str(messages[2]["content"])
    assert [e.kind for e in await journal.read()] == [CONSULTED]


async def test_thinking_harder_moves_the_rest_of_the_turn(journal: SqliteJournal) -> None:
    provider = Routed(
        call_tools(("think_harder", {"why": "plan de déménagement"})),
        call_tools(("think_harder", {"why": "encore"})),
        reply("Voici le plan."),
    )
    expert = Expert(journal, provider, ExpertSettings(model="big/model", max_per_day=1))
    messages: list[Message] = [{"role": "user", "content": "organise mon déménagement"}]
    settings = LoopSettings(model="small/model", session_id="s", thread_id="t", correlation_id="c")
    _ = [
        e
        async for e in run_agent(
            provider, ToolRegistry(expert_tools(expert)), Guardian(), messages, settings
        )
    ]
    assert [r.model for r in provider.requests] == ["small/model", "big/model", "big/model"]
    # The same conversation, read by another model: the prefix still extends.
    assert provider.requests[2].messages[: len(provider.requests[1].messages)] == (
        provider.requests[1].messages
    )
    # The daily cap holds: the second switch is refused.
    assert "No switch left today" in str(messages[4]["content"])
    assert [e.kind for e in await journal.read()] == [SWITCHED]


async def test_a_consultation_is_no_more_trusted_than_its_context(journal: SqliteJournal) -> None:
    provider = Routed()
    expert = Expert(journal, provider, ExpertSettings())
    result = await expert.consult("q ?", "", ToolContext("t", "c", context_trust=Trust.WEB))
    assert result.trust is Trust.WEB


async def test_an_expert_that_only_thought_says_so(journal: SqliteJournal) -> None:
    class Silent(Routed):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            answer = reply("")(request)
            return answer.model_copy(update={"finish_reason": "length"})

    expert = Expert(journal, Silent(), ExpertSettings())
    result = await expert.consult("q ?", "", ToolContext("t", "c"))
    assert result.is_error and "wrote nothing" in result.content
