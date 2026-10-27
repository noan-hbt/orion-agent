"""The week's report: what went wrong, and what it cost (chantier 4 of the rework).

`orion misses` (D152) lists his 👎 and corrections one by one. To choose what to work on next,
the owner also needs the week at a glance: the turns that stopped on a limit or an error
instead of finishing, the tools that failed most, the answers he corrected, and the cost,
beside the week before. All of it is read from the journal; nothing new is recorded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.adaptation.misses import Miss, misses
from orion.adaptation.signals import event_time
from orion.core.events import Event
from orion.runtime import OWNER_THREAD

WEEK = timedelta(days=7)
STOPPED = {
    "max_steps": "à court de pas",
    "budget_exhausted": "à court de budget",
    "stalled": "tournait en rond",
    "truncated": "réponse coupée",
    "provider_error": "le modèle n'a pas répondu",
    "tool_error": "un outil a planté",
}
"""How a turn that did not finish ended: the limits, then the errors."""


@dataclass
class Week:
    start: datetime
    end: datetime
    turns: int = 0
    """Turns finished, all threads."""
    owner_turns: int = 0
    stopped: Counter[str] = field(default_factory=Counter[str])
    """Why turns stopped short → how many."""
    stopped_owner: int = 0
    """Of those, the ones answering him."""
    tool_calls: int = 0
    tool_errors: Counter[str] = field(default_factory=Counter[str])
    """Tool → calls that failed (refusals included)."""
    misses: list[Miss] = field(default_factory=list[Miss])
    cost_usd: float = 0.0
    background_usd: float = 0.0
    """Of the cost, what he did not ask for (the night, the watch, missions…)."""
    cost_by: dict[str, float] = field(default_factory=dict[str, float])
    """Session → dollars."""

    def top_costs(self, n: int) -> list[tuple[str, float]]:
        ranked = sorted(self.cost_by.items(), key=lambda item: -item[1])
        return [(session, usd) for session, usd in ranked[:n] if usd >= 0.005]

    @property
    def corrected(self) -> int:
        return len(self.misses)

    def payload(self, before: Week | None = None) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "turns": self.turns,
            "owner_turns": self.owner_turns,
            "stopped": [
                {"why": why, "label": STOPPED.get(why, why), "count": count}
                for why, count in self.stopped.most_common()
            ],
            "stopped_owner": self.stopped_owner,
            "tool_calls": self.tool_calls,
            "tool_errors": [
                {"tool": tool, "count": count} for tool, count in self.tool_errors.most_common(8)
            ],
            "corrected": self.corrected,
            "misses": [
                {"at": m.at.isoformat(), "how": m.how, "asked": m.asked[:300], "said": m.said[:300]}
                for m in self.misses[-10:]
            ],
            "cost_usd": round(self.cost_usd, 2),
            "background_usd": round(self.background_usd, 2),
            "cost_by": [
                {"session": session, "usd": round(usd, 2)} for session, usd in self.top_costs(6)
            ],
            **({"before": before.payload()} if before is not None else {}),
        }


def week(events: Sequence[Event], zone: ZoneInfo, end: datetime) -> Week:
    """The seven days up to `end`. `events`: the whole journal (a correction needs the answer
    it corrects, which may be older)."""
    found = Week(end - WEEK, end)
    for e in events:
        if e.redacted or e.kind not in ("turn.finished", "tool.executed", "spend.recorded"):
            continue
        if not found.start <= event_time(e, zone) < end:
            continue
        payload = e.payload
        if e.kind == "turn.finished":
            owner = payload.get("thread_id") == OWNER_THREAD
            found.turns += 1
            found.owner_turns += owner
            reason = str(payload.get("reason", ""))
            if reason in STOPPED:
                found.stopped[reason] += 1
                found.stopped_owner += owner
        elif e.kind == "tool.executed":
            found.tool_calls += 1
            if payload.get("is_error"):
                found.tool_errors[str(payload.get("tool", "?"))] += 1
        else:
            cost = float(payload.get("cost_usd") or 0)
            found.cost_usd += cost
            if not payload.get("foreground"):
                found.background_usd += cost
            session = str(payload.get("session", "?")).removeprefix("orion:")
            found.cost_by[session] = found.cost_by.get(session, 0.0) + cost
    found.misses = [m for m in misses(events, zone, end) if found.start <= m.at < end]
    return found


def weeks(events: Sequence[Event], zone: ZoneInfo, end: datetime) -> tuple[Week, Week]:
    """This week and the one before."""
    return week(events, zone, end), week(events, zone, end - WEEK)


def report(this: Week, before: Week) -> str:
    """The week in a few lines, for `orion week`."""

    def versus(now: float, then: float, unit: str = "") -> str:
        fmt = "{:.2f}" if unit else "{:.0f}"
        return f"{fmt.format(now)}{unit} (semaine d'avant : {fmt.format(then)}{unit})"

    lines = [
        f"Semaine du {this.start:%d/%m} au {this.end:%d/%m}",
        f"Tours : {versus(this.turns, before.turns)}, dont {this.owner_turns} pour lui.",
        f"Arrêtés avant la fin : {versus(sum(this.stopped.values()), sum(before.stopped.values()))}"
        + (f", dont {this.stopped_owner} en lui répondant." if this.stopped_owner else "."),
    ]
    lines += [f"  - {STOPPED.get(why, why)} : {count}" for why, count in this.stopped.most_common()]
    errors = sum(this.tool_errors.values())
    lines.append(
        f"Outils en erreur : {versus(errors, sum(before.tool_errors.values()))} "
        f"sur {this.tool_calls} appels."
    )
    lines += [f"  - {tool} : {count}" for tool, count in this.tool_errors.most_common(8)]
    lines.append(
        f"Réponses corrigées (👎 ou ses mots) : {versus(this.corrected, before.corrected)}."
    )
    lines += [
        f"  - {m.at:%a %d/%m %H:%M} · {m.how} : {(m.said or m.answered)[:120]}"
        for m in this.misses[-5:]
    ]
    lines.append(
        f"Coût : {versus(this.cost_usd, before.cost_usd, ' $')}, "
        f"dont {this.background_usd:.2f} $ sans qu'il ait rien demandé."
    )
    top = [f"{s} {usd:.2f} $" for s, usd in this.top_costs(5)]
    if top:
        lines.append(f"  surtout : {', '.join(top)}")
    return "\n".join(lines)
