"""One way to say « later » (D157): the model says when and what; the code picks the mechanism,
and from there on the call is that mechanism's own (journal, guardian, evaluations)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orion.app import build_app
from orion.config import OrionConfig
from orion.initiative.later import LaterInput, route
from orion.runtime import OWNER_THREAD
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")


def routed(**args: Any) -> tuple[str, dict[str, Any]]:
    return route(LaterInput.model_validate(args))


def test_when_and_what_pick_the_mechanism() -> None:
    assert routed(then="tell", what="appeler le garage", at="2026-10-29 09:00") == (
        "remind_me",
        {"when": "2026-10-29T09:00", "what": "appeler le garage", "by_call": False},
    )
    name, args = routed(
        then="ask", what="Ça s'est passé comment ?", at="2026-10-29 18:00", context="entretien"
    )
    assert name == "follow_up" and args["action"] == "plan" and args["about"] == "entretien"
    assert routed(then="think", what="Changer de boulot ?")[0] == "think_later"
    name, args = routed(then="work", what="un appart à Dreux", every_hours=12)
    assert (name, args["action"], args["every_hours"]) == ("mission", "start", 12)
    name, args = routed(
        then="tell", what="la réponse de Paul", reply_from="contact:Paul", at="2026-10-30 10:00"
    )
    assert (name, args["who"], args["name"]) == ("awaiting", "contact", "Paul")
    assert routed(then="cancel", id="fu_1") == (
        "follow_up",
        {"action": "cancel", "follow_up": "fu_1"},
    )
    assert routed(then="cancel", id="r_1")[1]["reminder_id"] == "r_1"
    for wrong in (
        {"then": "tell", "what": "le garage"},  # when?
        {"then": "work", "what": "un appart"},  # at what pace?
        {"then": "cancel", "id": "x"},
        {"then": "tell", "what": "", "at": "2026-10-29 09:00"},
        {"then": "tell", "what": "x y z", "reply_from": "pigeon", "at": "2026-10-29 09:00"},
    ):
        with pytest.raises(ValueError):
            routed(**wrong)


async def test_later_becomes_the_call_it_stands_for(tmp_path: Path) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    provider = ScriptedProvider(
        call_tools(
            ("later", {"then": "tell", "what": "appeler le garage", "at": "2026-10-29 09:00"}),
            (
                "later",
                {
                    "then": "ask",
                    "what": "Ton entretien, ça a donné quoi ?",
                    "at": "2026-10-29 18:00",
                },
            ),
            ("later", {"then": "tell", "what": "appeler le garage"}),
        ),
        reply("C'est noté pour jeudi 9 h, et je te demande le soir comment ça s'est passé."),
    )
    async with build_app(config, provider) as app:
        menu = [s.name for s in app.orion.registry.specs()]
        assert "later" in menu and "remind_me" not in menu and "follow_up" not in menu
        (drawer,) = [s for s in app.orion.registry.specs() if s.name == "orion"]
        assert "remind_me" not in drawer.description  # reached through `later`, not listed
        now = datetime(2026, 10, 27, 15, 0, tzinfo=PARIS)
        await app.orion.handle_owner_message(
            OWNER_THREAD,
            "rappelle-moi jeudi le garage ; et j'ai un entretien jeudi",
            "app",
            now=now,
        )
        events = await app.journal.read(0, 100_000)
    ran = [(e.payload["tool"], e.payload["is_error"]) for e in events if e.kind == "tool.executed"]
    assert ran == [("remind_me", False), ("follow_up", False), ("later", True)]
    assert any(e.kind == "timer.scheduled" for e in events)
    assert any(e.kind == "followup.planned" for e in events)
    said = [
        str(e.payload["message"].get("content"))
        for e in events
        if e.kind == "conversation.message" and e.payload["message"].get("role") == "tool"
    ]
    assert any("`at` is needed" in s for s in said)
