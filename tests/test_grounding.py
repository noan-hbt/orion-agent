"""A reply that states how things stand rests on what a tool showed (A-FAIRE #9)."""

from __future__ import annotations

import pytest

from orion.cognition.grounding import GROUND_NOTE, UNCHECKED_NOTE, claims_a_state
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


async def test_the_verifier_sends_back_what_no_result_shows(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        call_tools(("echo", {"text": "login page: form filled, waiting for the code"})),
        # No pattern would pick this one: the verifier reads every reply, not a list's choice.
        reply("Le coffre a dit non pour Nibelis, il faudra retenter demain."),
        reply("Je n'ai pas vu de refus : la page attend un code."),
    )
    checks = ScriptedProvider(
        reply('{"ok": false, "issues": ["« le coffre a dit non » : rien ne le montre"]}')
    )
    orion = make_orion(journal, provider)
    orion.verifier = Verifier(checks, "verifier")
    await orion.start()
    turn = await orion.handle_owner_message(OWNER_THREAD, "Nibelis ?", "t", now=at(1))
    assert turn.text == "Je n'ai pas vu de refus : la page attend un code."
    note = provider.requests[2].messages[-1]["content"]
    assert note.startswith(GROUND_NOTE.split("{")[0]) and "rien ne le montre" in note
    assert "waiting for the code" in checks.requests[0].messages[-1]["content"]
    assert_prefix_stable(provider)


async def test_a_claim_the_results_support_goes(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        call_tools(("echo", {"text": "mail sent, id 42"})),
        reply("C'est envoyé."),
    )
    orion = make_orion(journal, provider)
    orion.verifier = Verifier(ScriptedProvider(reply('{"ok": true, "issues": []}')), "verifier")
    await orion.start()
    turn = await orion.handle_owner_message(OWNER_THREAD, "Envoie-le", "t", now=at(1))
    assert turn.text == "C'est envoyé."
    assert len(provider.requests) == 2
