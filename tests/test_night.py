"""At 1 a.m., the owner's « demain » is the day that starts (a reminder was once a day late)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from orion.context.compiler import render_user_message

PARIS = ZoneInfo("Europe/Paris")


def test_the_night_says_which_day_demain_is() -> None:
    night = render_user_message(
        "demain faudra que…", datetime(2026, 10, 20, 1, 16, tzinfo=PARIS), "imessage"
    )
    first = night["content"].splitlines()[0]
    assert first == (
        "[mardi 2026-10-20 01:16 · imessage · nuit : son « demain » est sans doute ce mardi 20]"
    )
    day = render_user_message("demain", datetime(2026, 10, 20, 9, 0, tzinfo=PARIS), "imessage")
    assert day["content"].splitlines()[0] == "[mardi 2026-10-20 09:00 · imessage]"
