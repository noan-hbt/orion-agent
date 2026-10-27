"""What Orion has planned for the owner, gathered from every module, in one place (D78)."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.app import build_app
from orion.config import OrionConfig
from orion.core.events import Provenance, Trust
from orion.initiative.planned import everything_planned, render
from orion.modules.base import next_weekly
from orion.tools.base import ToolContext
from tests.fakes import ScriptedProvider

PARIS = ZoneInfo("Europe/Paris")
CTX = ToolContext("owner", "c", context_trust=Trust.OWNER)


def test_next_weekly() -> None:
    tuesday = datetime(2026, 10, 20, 15, 0, tzinfo=PARIS)
    assert next_weekly(tuesday, 6, time(19, 0)) == datetime(2026, 10, 25, 19, 0, tzinfo=PARIS)
    assert next_weekly(tuesday, 1, time(16, 0)) == tuesday.replace(hour=16)
    assert next_weekly(tuesday, 1, time(9, 0)) == datetime(2026, 10, 27, 9, 0, tzinfo=PARIS)


async def test_everything_orion_will_do_by_day_with_how_to_undo_it(tmp_path: Path) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    now = datetime.now(PARIS).replace(microsecond=0)
    in_two_days = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0)
    async with build_app(config, ScriptedProvider()) as app:

        async def call(name: str, args: dict[str, object]) -> str:
            tool = app.orion.registry.get(name)
            assert tool is not None, name
            return (await tool.handler(tool.parse(json.dumps(args)), CTX)).content

        await call(
            "follow_up",
            {
                "action": "plan",
                "about": "son entretien",
                "at": f"{in_two_days:%Y-%m-%d %H:%M}",
                "question": "Alors, cet entretien ?",
            },
        )
        await call(
            "departure",
            {"action": "plan", "what": "Train pour Lyon", "at": f"{in_two_days:%Y-%m-%d %H:%M}"},
        )
        await app.timers.schedule(
            now + timedelta(days=1),
            "appeler le garage",
            "owner",
            Provenance(source="test", actor="owner:main"),
            Trust.OWNER,
        )
        await call(
            "relations",
            {
                "action": "birthday",
                "person": "Maman",
                "month": in_two_days.month,
                "day": in_two_days.day,
            },
        )

        found = await everything_planned(app.modules, app.timers, now, now + timedelta(days=7))
        whats = [p.what for p in found]
        assert "te rappeler : appeler le garage" in whats
        assert "te demander : Alors, cet entretien ?" in whats
        assert any(w.startswith("préparer : Train pour Lyon") for w in whats)
        assert "écrire la page de ta semaine" in whats
        assert any("anniversaire de Maman aujourd'hui" in w for w in whats)
        assert [p.at for p in found] == sorted(p.at for p in found)

        shown = await call("planned", {"days": 7})
        assert "[annuler : later (cancel, r_" in shown
        assert "[annuler : later (cancel, fu_" in shown
        assert render([], PARIS) == "Rien de prévu sur cette période."
