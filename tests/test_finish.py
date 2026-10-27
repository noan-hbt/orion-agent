"""Before handing back: a reply that is not done yet gets one more step (D118, D144)."""

from __future__ import annotations

import pytest

from orion.cognition.finish import OFFER_NOTE, ends_on_offer, finish_check
from orion.core.journal import SqliteJournal
from orion.runtime import OWNER_THREAD
from tests.fakes import ScriptedProvider, call_tools, reply
from tests.test_runtime_cache import assert_prefix_stable, at, make_orion


@pytest.mark.parametrize(
    "text",
    [
        "Il y a trois piscines à Dreux. Tu veux que je regarde les horaires du dimanche ?",
        "Je peux te chercher des offres si tu veux.",
        "Je peux aussi comparer les prix de Lemonade et Luko ?",
        "Pour l'assurance, je te compare les offres ?",
        "Dis-moi si tu veux que je lance la recherche.",
        "Ça te dit qu'on regarde ça ensemble ?",
    ],
)
def test_an_offer_at_the_end_is_seen(text: str) -> None:
    assert ends_on_offer(text)


@pytest.mark.parametrize(
    "text",
    [
        "Les horaires du dimanche : 9 h à 12 h. Bonne baignade !",
        "Lyon ou Nantes, c'est toi qui vois : tu penches pour laquelle ?",
        "C'est réservé pour jeudi 14 h, je t'ai mis un rappel la veille.",
        "Tu veux du rouge ou du blanc pour samedi ?",
        "Si tu veux mon avis, prends Lemonade : le vol est inclus.",
        "Le vol est inclus chez Lemonade, si tu veux mon avis.",
    ],
)
def test_a_finished_reply_or_a_real_choice_goes(text: str) -> None:
    assert not ends_on_offer(text)


@pytest.mark.parametrize(
    ("text", "called", "sent_back"),
    [
        ("Je regarde les annonces et je te tiens au courant.", [], True),
        ("Je regarde les annonces et je te tiens au courant.", ["mission"], False),
        ("C'est noté, je reviens vers toi demain.", ["remind_me"], False),
        ("Bien sûr ! Le train part à 8 h 12.", [], True),
        ("Le train part à 8 h 12. N'hésite pas si tu veux autre chose.", [], True),
        ("## Horaires\nLe train part à 8 h 12.", [], True),
        ("Le train part à 8 h 12, quai 3.", [], False),
    ],
)
def test_a_promise_without_follow_up_or_assistant_filler_is_sent_back(
    text: str, called: list[str], sent_back: bool
) -> None:
    assert (finish_check(text, called) is not None) is sent_back


async def test_an_offer_sends_orion_back_to_work_once(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply("Je peux te chercher les horaires si tu veux ?"),
        call_tools(("echo", {"text": "dimanche 9 h - 12 h"})),
        reply("Le dimanche c'est 9 h à 12 h."),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    turn = await orion.handle_owner_message(OWNER_THREAD, "La piscine de Dreux ?", "t", now=at(1))
    assert turn.text == "Le dimanche c'est 9 h à 12 h."
    assert OFFER_NOTE in provider.requests[1].messages[-1]["content"]
    assert_prefix_stable(provider)  # the note is journaled: the next turn replays it as sent

    provider.extend(reply("Ok."))
    await orion.handle_owner_message(OWNER_THREAD, "Merci", "t", now=at(2))
    assert_prefix_stable(provider)


async def test_the_check_runs_once_and_a_kept_reply_goes(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply("Tu veux que je réserve, ou tu préfères appeler ?"),
        reply("Tu veux que je réserve, ou tu préfères appeler ?"),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    turn = await orion.handle_owner_message(OWNER_THREAD, "Le resto de samedi ?", "t", now=at(1))
    assert turn.text.startswith("Tu veux que je réserve")
    assert len(provider.requests) == 2  # one more step, never a loop


async def test_only_his_messages_are_checked(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply("Ton rendez-vous est demain. Tu veux que je te le rappelle ?")
    )
    orion = make_orion(journal, provider)
    await orion.start()
    turn = await orion.handle_initiative(OWNER_THREAD, "un rendez-vous demain", "rappel")
    assert "Tu veux que je" in turn.text  # a proactive message may end on a one-word reply
    assert len(provider.requests) == 1
