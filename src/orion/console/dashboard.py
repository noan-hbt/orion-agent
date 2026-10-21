"""The dashboard (D101): a week of Orion at a glance, read from the journal alone.

Four questions, each answered from events already journaled: how much of today's attention it
used (and on what), what it chose not to say, what it did without asking, and what it cost.
Plain HTML, bars drawn with widths: no script, like the rest of the console.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from orion.core.events import Event
from orion.core.hlc import Timestamp
from orion.guardian.policy import ALONE
from orion.initiative.engine import InitiativeSettings

DAYS = 7
SILENCES = {
    "daily attention budget spent": "budget du jour épuisé",
    "model chose silence": "pas assez important",
    "missed its window": "trop tard",
}
"""Why it kept quiet, for the reasons worth showing ("nothing to say" is not a choice)."""
UNASKED = (ALONE, "external effect in an untrusted context")
"""Acting outside Orion without asking: what he let it do alone, or what the cautious-but-not-
strict mode lets through. Its own memory and notes are not actions on the world."""
WEEKDAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def moment(event: Event, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC).astimezone(zone)


def _bar(value: float, top: float, warn: bool = False) -> str:
    width = 0 if top <= 0 or value <= 0 else max(2, round(100 * min(value, top) / top))
    colour = "var(--warn)" if warn else "var(--accent)"
    return (
        '<div style="background:var(--line);border-radius:4px;height:10px;min-width:80px">'
        f'<div style="width:{width}%;background:{colour};height:10px;border-radius:4px"></div>'
        "</div>"
    )


def _rows(rows: Iterable[str], empty: str) -> str:
    found = list(rows)
    return "<table>" + "".join(found) + "</table>" if found else f"<p class=muted>{empty}</p>"


def dashboard(events: list[Event], settings: InitiativeSettings, now: datetime) -> str:
    zone = ZoneInfo(settings.timezone)
    today = settings.attention_day(now)
    since = now - timedelta(days=DAYS)
    week = [e for e in events if not e.redacted and moment(e, zone) >= since]
    return (
        _attention(week, settings, today, zone)
        + _silences(week, zone)
        + _alone(week, zone)
        + _spending(week, now, zone)
    )


def _attention(
    events: list[Event], settings: InitiativeSettings, today: date, zone: ZoneInfo
) -> str:
    said = [
        e
        for e in events
        if e.kind == "initiative.delivered" and e.payload.get("day") == today.isoformat()
    ]
    counted = [e for e in said if not e.payload.get("explicit") and not e.payload.get("bundled")]
    budget = settings.daily_budget
    spent = len(counted)
    rows = (
        f"<tr><td class=muted>{moment(e, zone):%H:%M}</td>"
        f"<td>{escape(str(e.payload.get('why', '')))}</td>"
        f"<td class=muted>{'demandé' if e.payload.get('explicit') else ''}</td></tr>"
        for e in reversed(said)
    )
    return (
        "<h2>Mon attention aujourd'hui</h2>"
        f"<p>{spent} message(s) de moi-même sur {budget} par jour"
        + (" : je me tais jusqu'à demain, sauf ce que tu m'as demandé." if spent >= budget else ".")
        + " Ce que tu m'as demandé (rappels, réponses attendues) ne compte pas.</p>"
        + _bar(spent, budget, warn=spent >= budget)
        + _rows(rows, "Je ne t'ai encore rien écrit de moi-même aujourd'hui.")
    )


def _silences(events: list[Event], zone: ZoneInfo) -> str:
    kept = [
        e
        for e in events
        if e.kind == "initiative.suppressed" and str(e.payload.get("reason")) in SILENCES
    ]
    rows = (
        f"<tr><td class=muted>{moment(e, zone):%d/%m %H:%M}</td>"
        f"<td>{escape(str(e.payload.get('fallback') or e.payload.get('why', '')))}</td>"
        f"<td class=muted>{SILENCES[str(e.payload.get('reason'))]}</td></tr>"
        for e in reversed(kept[-40:])
    )
    return (
        "<h2>Ce que j'ai choisi de ne pas te dire</h2>"
        f"<p class=muted>Les {DAYS} derniers jours. Si quelque chose ici aurait dû passer, "
        "dis-le-moi : j'apprends de ce que tu corriges.</p>"
        + _rows(rows, "Rien : tout ce qui méritait un message est parti.")
    )


def _alone(events: list[Event], zone: ZoneInfo) -> str:
    done = [
        e
        for e in events
        if e.kind == "tool.executed"
        and not e.payload.get("is_error")
        and any(why in str(e.payload.get("decision", "")) for why in UNASKED)
    ]
    rows = (
        f"<tr><td class=muted>{moment(e, zone):%d/%m %H:%M}</td>"
        f"<td>{escape(str(e.payload.get('tool', '')))}</td>"
        f"<td class=muted>{escape(_short(e.payload.get('arguments')))}</td></tr>"
        for e in reversed(done[-40:])
    )
    return (
        "<h2>Ce que j'ai fait sans te demander</h2>"
        "<p class=muted>Les actions que tu m'as laissé faire seul (page Autonomie).</p>"
        + _rows(rows, "Rien : je t'ai demandé avant chaque action.")
    )


def _short(arguments: Any) -> str:
    """The arguments as he would say them: the values, not the JSON."""
    try:
        parsed: Any = json.loads(str(arguments or "{}"))
    except json.JSONDecodeError:
        parsed = arguments
    values: list[Any] = (
        list(parsed.values()) if isinstance(parsed, dict) else [parsed]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    text = " · ".join(" ".join(str(v).split()) for v in values if v not in (None, "", [], {}))
    return text if len(text) <= 90 else text[:89] + "…"


def _spending(events: list[Event], now: datetime, zone: ZoneInfo) -> str:
    days: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    sessions: dict[str, float] = defaultdict(float)
    prompt = cached = 0
    for e in events:
        if e.kind == "spend.recorded":
            cost = float(e.payload.get("cost_usd") or 0)
            days[moment(e, zone).date()][0 if e.payload.get("foreground") else 1] += cost
            sessions[str(e.payload.get("session", "?")).removeprefix("orion:")] += cost
        elif e.kind == "model.usage":
            prompt += int(e.payload.get("prompt_tokens") or 0)
            cached += int(e.payload.get("cached_tokens") or 0)
    order = [(now.astimezone(zone) - timedelta(days=i)).date() for i in reversed(range(DAYS))]
    top = max((sum(days[d]) for d in order), default=0.0)
    rows = (
        f"<tr><td class=muted style=white-space:nowrap>{WEEKDAYS[d.weekday()]} {d:%d/%m}</td>"
        f"<td style=width:70%>{_bar(sum(days[d]), top)}</td>"
        f"<td style=white-space:nowrap>{sum(days[d]):.2f} $"
        + (f"<br><small class=muted>{days[d][1]:.2f} $ seul</small>" if days[d][1] else "")
        + "</td></tr>"
        for d in order
    )
    total = sum(sum(days[d]) for d in order)
    where = ", ".join(
        f"{escape(s)} {c:.2f} $"
        for s, c in sorted(sessions.items(), key=lambda i: -i[1])[:5]
        if c >= 0.005
    )
    ratio = f"{100 * cached / prompt:.0f} %" if prompt else "—"
    return (
        "<h2>Ce que j'ai coûté</h2>"
        f"<p>{total:.2f} $ sur {DAYS} jours. Relu depuis le cache : {ratio} de ce que j'ai lu."
        + (f"<br><span class=muted>Surtout : {where}.</span>" if where else "")
        + "</p>"
        + "<table>"
        + "".join(rows)
        + "</table>"
    )
