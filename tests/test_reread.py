"""A shortened tool result can be read back whole, as it was received (D204)."""

from __future__ import annotations

from datetime import datetime, timedelta

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.context.reread import RereadInput, reread_tools
from orion.context.threads import MESSAGE_KIND
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.runtime import Orion
from orion.tools.base import ToolContext, ToolRegistry
from orion.tools.builtin import builtin_tools
from orion.web.tools import page_line
from tests.fakes import ScriptedProvider, call_tools, echo_tool, reply

START = datetime.fromisoformat("2026-11-02T10:00:00+01:00")


async def test_a_shortened_result_comes_back_whole_only_in_its_thread(
    journal: SqliteJournal,
) -> None:
    page = "annonce " * 600
    provider = ScriptedProvider(
        call_tools(("echo", {"text": page})), reply("vu"), reply("ok"), reply("ok"), reply("ok")
    )
    [tool] = reread_tools(journal)
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), echo_tool(), tool]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION", max_window_tokens=120_000),
        agent=AgentConfig(),
    )
    await orion.start()
    for hours in (0, 0, 0, 5):
        await orion.handle_owner_message("t1", "m", "terminal", now=START + timedelta(hours=hours))

    [shown] = [m for m in provider.requests[-1].messages if m["role"] == "tool"]
    call = str(shown["tool_call_id"])
    assert f"reread({call})" in str(shown["content"]) and page not in str(shown["content"])

    stored = [
        e
        for e in await journal.read(0, 1000)
        if e.kind == MESSAGE_KIND and e.payload["message"].get("tool_call_id") == call
    ]
    thread = str(stored[0].payload["thread_id"])
    args = RereadInput(result=call)
    back = await tool.handler(args, ToolContext(thread_id=thread, correlation_id="c"))
    assert not back.is_error and page in back.content

    other = await tool.handler(args, ToolContext(thread_id="elsewhere", correlation_id="c"))
    assert other.is_error and page not in other.content


async def test_a_reread_web_page_stays_a_web_page(journal: SqliteJournal) -> None:
    message = {"role": "tool", "tool_call_id": "call_1", "content": "page du web"}
    await journal.append(
        [
            NewEvent(
                kind=MESSAGE_KIND,
                payload={"thread_id": "t", "message": message},
                provenance=Provenance(source="web:a", actor="orion:core"),
                trust=Trust.WEB,
            )
        ]
    )
    [tool] = reread_tools(journal)
    back = await tool.handler(
        RereadInput(result="call_1"), ToolContext(thread_id="t", correlation_id="c")
    )
    assert back.content == "page du web" and back.trust == Trust.WEB


def test_a_page_without_title_writes_its_address_once() -> None:
    url = "https://a.fr/long"
    assert page_line("", url).count(url) == 1
    assert page_line(url, url).count(url) == 1
    assert "Titre" in page_line("Titre", url) and url in page_line("Titre", url)
