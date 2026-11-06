"""A reply that states how things stand rests on what a tool showed (A-FAIRE #9)."""

from __future__ import annotations

import pytest

from orion.cognition.grounding import UNCHECKED_NOTE, claims_a_state
from orion.cognition.review import GroundingReview
from orion.cognition.verifier import Verifier
from orion.core.journal import SqliteJournal
from orion.runtime import OWNER_THREAD
from tests.fakes import ScriptedProvider, call_tools, reply
from tests.test_runtime_cache import assert_prefix_stable, at, make_orion


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        ("Oui, c'est cette rubrique, vérifiée : Entreprises → Modifier.", True),
        ("Connexion Nibelis rejetée par le coffre ce matin.", True),
        ("Pas moi : je n'ai rien lancé sur Cyclades.", True),
        ("C'est envoyé.", True),
        ("Aucun mail de la banque depuis lundi.", True),
        ("Bonne nuit, à demain !", False),
        ("Change le mot de passe et dis-moi quand c'est fait.", False),
        ("Tu préfères samedi ou dimanche pour la piscine ?", False),
        ("Ton père devait t'envoyer sa carte, c'est réglé ?", False),
        (f"Non, pas moi, j{chr(0x2019)}ai pas ouvert Cyclades.", True),
    ],
)
def test_a_reported_state_is_seen(text: str, claims: bool) -> None:
    assert claims_a_state(text) is claims


async def test_a_state_claimed_with_no_tool_run_is_sent_back_once(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(
        reply("C'est envoyé."),
        call_tools(("echo", {"text": "outbox: queued, not delivered"})),
        reply("Pas encore parti : il est en file d'attente."),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    turn = await orion.handle_owner_message(OWNER_THREAD, "Le mail est parti ?", "t", now=at(1))
    assert turn.text == "Pas encore parti : il est en file d'attente."
    assert provider.requests[1].messages[-1]["content"] == UNCHECKED_NOTE
    assert_prefix_stable(provider)


async def test_an_unproven_reply_is_read_again_once_the_exchange_settles(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(
        call_tools(("echo", {"text": "login page: form filled, waiting for the code"})),
        # No pattern would pick this one: the verifier reads every reply, not a list's choice.
        reply("Le coffre a dit non pour Nibelis, il faudra retenter demain."),
        reply("Bonne soirée !"),
    )
    checks = ScriptedProvider(
        reply('{"ok": false, "issues": ["« le coffre a dit non » : rien ne le montre"]}')
    )
    orion = make_orion(journal, provider)
    await orion.start()
    now = [at(0)]
    review = GroundingReview(orion, Verifier(checks, "verifier"), journal, lambda: now[0])
    await review.tick()
    turn = await orion.handle_owner_message(OWNER_THREAD, "Nibelis ?", "t", now=at(1))
    assert turn.text.startswith("Le coffre a dit non")  # it went at once, unread
    await orion.handle_owner_message(OWNER_THREAD, "Merci", "t", now=at(2))
    assert not await review.tick()  # still talking
    now[0] = at(3)
    assert await review.tick()
    assert len(checks.requests) == 1  # both replies, one call
    assert "waiting for the code" in checks.requests[0].messages[-1]["content"]
    [alert] = await review.alerts(at(3))
    assert "rien ne le montre" in alert.notice
    assert not await review.tick()  # read once

    # After a restart, it goes on after the last reading and still has what it found.
    again = GroundingReview(orion, Verifier(checks, "verifier"), journal, lambda: at(9))
    assert not await again.tick()
    assert [a.key for a in await again.alerts(at(9))] == [alert.key]


async def test_a_silence_settles_each_doubt_and_what_stays_open_is_said_once(
    journal: SqliteJournal,
) -> None:
    """ORION-26: on 06/11, eight alerts ended in eight silences, some with no check at all."""
    provider = ScriptedProvider(
        call_tools(("echo", {"text": "inbox: 2 mails from Jira, none from the pharmacy"})),
        reply("Deux mails Jira, la pharmacie a confirmé, et Paul a payé."),
    )
    doubts = ["« deux mails Jira »", "« la pharmacie a confirmé »", "« Paul a payé »"]
    checks = ScriptedProvider(
        reply('{"ok": false, "issues": ' + str(doubts).replace("'", '"') + "}"),
        reply('{"settled": [true, false, false]}'),
        reply('{"settled": [true, false, false]}'),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    now = [at(0)]
    review = GroundingReview(orion, Verifier(checks, "verifier"), journal, lambda: now[0])
    await review.tick()
    await orion.handle_owner_message(OWNER_THREAD, "Quoi de neuf ?", "t", now=at(1))
    await review.tick()
    now[0] = at(9)
    assert await review.tick()
    [alert] = await review.alerts(at(9))
    assert alert.settle is not None

    said = await alert.settle(None, "")  # he answered SILENCE
    assert said is not None
    assert "pharmacie" in said and "Paul" in said and "Jira" not in said
    [settled] = [e.payload for e in await journal.read(0, 10_000) if e.kind == "grounding.settled"]
    assert [p["status"] for p in settled["points"]] == [  # type: ignore[index]
        "resolved_with_evidence",
        "unresolved",
        "unresolved",
    ]

    # The same doubts again, after a restart: already told, not repeated.
    again = GroundingReview(orion, Verifier(checks, "verifier"), journal, lambda: at(9))
    await again.tick()
    [same] = await again.alerts(at(9))
    assert same.settle is not None
    assert await same.settle(None, "") is None
