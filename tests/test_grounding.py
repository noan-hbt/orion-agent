"""A reply that states how things stand rests on what a tool showed (A-FAIRE #9)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from orion.cognition.grounding import UNCHECKED_NOTE, claims_a_state
from orion.cognition.review import GroundingReview
from orion.cognition.verifier import Verifier
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.context.threads import pruned
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.runtime import OWNER_THREAD, Orion
from orion.tools.base import Effect, Tool, ToolContext, ToolRegistry, ToolResult
from orion.tools.builtin import builtin_tools
from orion.tools.outcome import Outcome
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


class _ActInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    action: str
    proof: str = ""


async def _act(args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult(
        "done what was asked", outcome=args.outcome, action_id=args.action, proof=args.proof
    )


def _acting(journal: SqliteJournal, provider: ScriptedProvider) -> Orion:
    act = Tool("act", "Act.", _ActInput, _act, effect=Effect.NONE, accepts_untrusted_context=True)
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), act]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=AgentConfig(),
    )


def _a(outcome: Outcome, action: str, proof: str = "") -> tuple[str, dict[str, object]]:
    return "act", {"outcome": outcome.value, "action": action, "proof": proof}


QUEUED = _a(Outcome.PENDING, "mail:m1", "smtp perso")
FORM = _a(Outcome.ATTEMPTED, "browser:impots.gouv.fr")
UNSURE = _a(Outcome.UNCERTAIN, "workflow:w1")


@pytest.mark.parametrize(
    ("calls", "text", "sent_back"),
    [
        # A mail the server took is sent, never delivered (ORION-20).
        ([QUEUED], "Ton mail est bien arrivé chez Paul.", "mail:m1"),
        ([QUEUED], "C'est envoyé à Paul.", None),
        # A form sent with nothing seen after it is not finished.
        ([FORM], "C'est fait, ta déclaration est soumise.", "browser:impots.gouv.fr"),
        (
            [FORM, _a(Outcome.CONFIRMED, "browser:impots.gouv.fr", "Accusé de réception")],
            "C'est fait, ta déclaration est soumise.",
            None,
        ),
        # Uncertain: reconcile first, or say plainly it is not verified.
        ([UNSURE], "Le déploiement est terminé.", "workflow:w1"),
        ([UNSURE], "Le déploiement : je ne sais pas si c'est passé, non vérifié.", None),
        # A careful word covers the action it is about, never the whole reply.
        (
            [QUEUED, FORM],
            "Le mail est en attente de remise, j'ai soumis la déclaration.",
            "browser:impots.gouv.fr",
        ),
        (
            [QUEUED, FORM],
            "Le mail est en attente. La déclaration est soumise, pas de confirmation vue.",
            None,
        ),
        # Another action's proof proves nothing about this one.
        (
            [QUEUED, _a(Outcome.CONFIRMED, "mail:m2", "outbox delivered")],
            "Ton mail est bien arrivé chez Paul.",
            "mail:m1",
        ),
    ],
)
async def test_an_action_is_reported_no_further_than_its_outcome(
    journal: SqliteJournal,
    calls: list[tuple[str, dict[str, object]]],
    text: str,
    sent_back: str | None,
) -> None:
    provider = ScriptedProvider(call_tools(*calls), reply(text), reply("Où ça en est, vraiment."))
    orion = _acting(journal, provider)
    await orion.start()
    await orion.handle_owner_message(OWNER_THREAD, "Alors ?", "t", now=at(1))
    if sent_back is None:
        assert len(provider.requests) == 2
    else:
        note = str(provider.requests[2].messages[-1]["content"])
        assert note.startswith("[Orion") and sent_back in note
        assert_prefix_stable(provider)
    # Journaled with the call, and leading its result where pruning keeps it.
    executed = [e.payload for e in await journal.read() if e.kind == "tool.executed"]
    assert [p["outcome"] for p in executed] == [c[1]["outcome"] for c in calls]
    assert executed[0]["action_id"] == calls[0][1]["action"]
    result = next(m for m in provider.requests[1].messages if m["role"] == "tool")
    long = {**result, "content": str(result["content"]) + "x" * 500}
    assert str(calls[0][1]["action"]) in str(pruned(long)["content"])
