"""The evaluation harness itself, on a scripted model: replays the moment, sees what was done."""

from __future__ import annotations

from datetime import date, timedelta

from evals.harness import (
    Case,
    Observation,
    Say,
    asks,
    either,
    evaluate,
    judged,
    model_judge,
    no_reminder_on,
    paris,
    reminder_on,
    reminder_within,
    run_case,
)

from orion.config import OrionConfig
from tests.fakes import ScriptedProvider, call_tools, reply


async def never(_question: str, _reply: str) -> bool | None:
    return False


async def test_a_case_is_replayed_at_its_moment_and_judged_on_what_was_done() -> None:
    case = Case(
        name="demain",
        origin="test",
        at=paris(2026, 10, 20, 1, 16),
        steps=(Say("Demain faudra que je pense aux papiers"),),
        checks=(
            either(reminder_on(date(2026, 10, 20)), asks()),
            no_reminder_on(date(2026, 10, 21)),
        ),
    )
    provider = ScriptedProvider(
        call_tools(("remind_me", {"when": "2026-10-21T10:00", "what": "papiers"})),
        reply("C'est noté pour mercredi."),
    )
    observation = await run_case(case, OrionConfig(), provider)

    # The model saw the moment of the case, not today.
    assert "[mardi 2026-10-20 01:16 · imessage · nuit" in str(provider.requests[0].messages[-1])
    assert [r.date() for r in observation.reminders] == [date(2026, 10, 21)]
    outcome = await evaluate(case, observation, never)
    assert not outcome.passed
    assert "no reminder on Tue 20/10" in outcome.failures[0]
    assert "a reminder on Wed 21/10 too" in outcome.failures[1]


async def test_a_reminder_relative_to_the_last_step() -> None:
    case = Case(
        name="minute",
        origin="test",
        at=paris(2026, 10, 20, 1, 28),
        steps=(Say("Rappelle-moi"), Say("Dans une minute", at=paris(2026, 10, 20, 1, 29))),
        checks=(reminder_within(timedelta(0), timedelta(minutes=3)),),
    )
    provider = ScriptedProvider(
        reply("Quand ?"),
        call_tools(("remind_me", {"when": "2026-10-20T01:30", "what": "réveil"})),
        reply("Fait."),
    )
    outcome = await evaluate(case, await run_case(case, OrionConfig(), provider), never)
    assert outcome.passed, outcome.failures


async def test_a_judge_without_a_verdict_is_not_a_no() -> None:
    """A judge that thinks first ran out of room and said nothing: that was counted as « no »,
    and `il_etait_coupe` failed on replies that said « j'étais coupé »."""
    judge = model_judge(ScriptedProvider(reply(""), reply("**OUI**"), reply("NON")), "judge")
    assert await judge("?", "r") is None
    assert await judge("?", "r") is True
    assert await judge("?", "r") is False

    async def silent(_question: str, _reply: str) -> bool | None:
        return None

    reason = await judged("coupé ?")(Observation(replies=["j'étais coupé"]), silent)
    assert reason == "judge gave no verdict: coupé ?"
