"""Before handing back: a reply that is not done yet gets one more step (D118, D144)."""

from __future__ import annotations

import pytest

from orion.cognition.finish import OFFER_NOTE, ends_on_offer, finish_check
from orion.config import OrionConfig
from orion.core.journal import SqliteJournal
from orion.files.module import FilesModule
from orion.runtime import OWNER_THREAD
from orion.tools.builtin import builtin_tools
from orion.vocal.module import VocalSettings, vocal_tools
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
        "Tour 1 terminé : shell répond bien. Envoie 'continue' et je lance le tour 2.",
        "Je te prépare la lettre. Je peux aussi te programmer un rappel avant la date limite.",
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
        ("Je te prépare la lettre et je te rappelle de l'envoyer avant l'échéance.", [], True),
        ("Bien sûr ! Le train part à 8 h 12.", [], True),
        ("Le train part à 8 h 12. N'hésite pas si tu veux autre chose.", [], True),
        ("## Horaires\nLe train part à 8 h 12.", [], True),
        ("Le train part à 8 h 12, quai 3.", [], False),
        # D151: put off without trying now
        ("C'est lancé, je surveille les annonces.", ["mission:start"], True),
        ("C'est lancé, je surveille les annonces.", ["web_search", "mission:start"], False),
        ("Je cherche en fond et je te dis.", ["task:start"], True),
        ("Voilà les trois missions en cours.", ["mission:list"], False),
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


async def test_his_corrections_are_found_and_become_a_case(journal: SqliteJournal) -> None:
    """D152: what went wrong for real, from his own words, as the start of an eval case."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orion.adaptation.misses import as_case, corrects, misses

    assert corrects("non, c'est pas ça que je voulais") and corrects("tu t'es trompé de jour")
    assert not corrects("super, merci !")
    provider = ScriptedProvider(
        reply("C'est lancé : une mission toutes les 6 h."), reply("Pardon.")
    )
    orion = make_orion(journal, provider)
    await orion.start()
    await orion.handle_owner_message(
        OWNER_THREAD, "le resto est ouvert ce soir ?", "app", now=at(0)
    )
    await orion.handle_owner_message(
        OWNER_THREAD, "non c'est pas ce que je voulais, réponds juste", "app", now=at(1)
    )
    zone = ZoneInfo("Europe/Paris")
    (miss,) = misses(await journal.read(0, 100_000), zone, datetime.now(zone))
    assert miss.asked == "le resto est ouvert ce soir ?" and "toutes les 6 h" in miss.answered
    case = as_case(miss, "rate")
    assert "Say('le resto est ouvert ce soir ?')" in case and "judged(" in case


async def test_tell_reaches_him_while_the_turn_goes_on(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        call_tools(("tell", {"text": "Paris : 18°, soleil."}), ("echo", {"text": "Lyon"})),
        call_tools(("tell", {"text": "Lyon : 15°, pluie."}), ("echo", {"text": "Marseille"})),
        reply("Marseille : 22°, grand bleu."),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    heard: list[str] = []

    async def progress(text: str, _cid: str) -> None:
        heard.append(text)

    turn = await orion.handle_owner_message(
        OWNER_THREAD, "La météo à Paris, Lyon, Marseille ?", "t", now=at(1), progress=progress
    )
    assert heard == ["Paris : 18°, soleil.", "Lyon : 15°, pluie."]
    assert turn.text == "Marseille : 22°, grand bleu."


def test_the_voice_has_a_tool_name_of_its_own() -> None:
    # A built-in `say` (D173) kept Orion from starting: two tools of the same name.
    class Speaker:
        async def speak(self, model: str, text: str, voice: str) -> bytes:
            return b""

    voice = {
        t.name for t in vocal_tools(FilesModule(OrionConfig()), Speaker(), VocalSettings(), "m")
    }
    assert not voice & {t.name for t in builtin_tools()}
