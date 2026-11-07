"""The week's report: what went wrong, and what it cost (chantier 4 of the rework).

`orion misses` (D152) lists his 👎 and corrections one by one. To choose what to work on next,
the owner also needs the week at a glance: the turns that stopped on a limit or an error
instead of finishing, the tools that failed most, the answers he corrected, and the cost,
beside the week before. All of it is read from the journal; nothing new is recorded.

How well each role does on each model (ORI-020, D286): a turn succeeds when he did not correct
it, did not stop it, it was not cut short, and none of its actions ended UNCERTAIN or FAILED.
Measured before any routing by risk is considered (D147 holds).

What was really done (ORI-013, D273): the outcomes of the actions (D258), the calls cut at
their deadline, the workflows and their uncertain steps, the goals reached, and the noise:
how often Orion spoke unasked, how often that went without a reaction, and what it held back.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.adaptation.misses import Miss, misses
from orion.adaptation.signals import event_time
from orion.cognition.loop import TIMED_OUT
from orion.core.events import Event
from orion.goals.module import REACHED
from orion.initiative.engine import (
    DELIVERED,
    ENGAGED_WITHIN,
    FEEDBACK_REACTION,
    SUPPRESSED,
)
from orion.runtime import OWNER_THREAD
from orion.workflows.engine import FINISHED as WORKFLOW_FINISHED
from orion.workflows.engine import STEP_UNCERTAIN

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
OUTCOMES = {
    "CONFIRMED": "confirmées",
    "PENDING": "en attente",
    "ATTEMPTED": "tentées sans résultat vu",
    "FAILED": "échouées",
    "UNCERTAIN": "incertaines",
}
KINDS = (
    "turn.finished",
    "tool.executed",
    "spend.recorded",
    WORKFLOW_FINISHED,
    STEP_UNCERTAIN,
    REACHED,
    DELIVERED,
    SUPPRESSED,
    "model.usage",
)
MISS_WITHIN = timedelta(minutes=10)
"""A correction counts against his answered turn this close to the exchange it notes."""


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
    outcomes: Counter[str] = field(default_factory=Counter[str])
    """Outcome of the actions (D258) → how many."""
    uncertain: list[str] = field(default_factory=list[str])
    """The uncertain actions, `tool: action`."""
    timed_out: int = 0
    workflows: Counter[str] = field(default_factory=Counter[str])
    """State a workflow ended in → how many."""
    uncertain_steps: int = 0
    goals_reached: int = 0
    spoke: int = 0
    """Times Orion spoke unasked."""
    unanswered: int = 0
    """Of those, the ones he did not react to within `ENGAGED_WITHIN`."""
    held: int = 0
    """Things Orion decided not to say."""
    success: dict[tuple[str, str], list[int]] = field(
        default_factory=dict[tuple[str, str], list[int]]
    )
    """(role, model) → [turns that succeeded, turns] (D286)."""

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
                {
                    "at": m.at.isoformat(),
                    "why": m.why[:300],
                    "asked": m.asked[:300],
                    "said": m.said[:300],
                }
                for m in self.misses[-10:]
            ],
            "cost_usd": round(self.cost_usd, 2),
            "background_usd": round(self.background_usd, 2),
            "cost_by": [
                {"session": session, "usd": round(usd, 2)} for session, usd in self.top_costs(6)
            ],
            "outcomes": dict(self.outcomes),
            "uncertain": self.uncertain[-10:],
            "timed_out": self.timed_out,
            "workflows": dict(self.workflows),
            "uncertain_steps": self.uncertain_steps,
            "goals_reached": self.goals_reached,
            "spoke": self.spoke,
            "unanswered": self.unanswered,
            "held": self.held,
            "success": [
                {"role": role, "model": model, "ok": ok, "turns": total}
                for (role, model), (ok, total) in sorted(self.success.items())
            ],
            **({"before": before.payload()} if before is not None else {}),
        }


def week(events: Sequence[Event], zone: ZoneInfo, end: datetime) -> Week:
    """The seven days up to `end`. `events`: the whole journal (a correction needs the answer
    it corrects, which may be older)."""
    found = Week(end - WEEK, end)
    spoken: list[datetime] = []
    engaged: list[datetime] = []
    models: dict[str, str] = {}
    """Turn (correlation id) → the model that answered it."""
    failed: set[str] = set()
    """Turns with an action left UNCERTAIN or FAILED."""
    finished: list[tuple[datetime, str, str, bool]] = []
    """(when, correlation id, role, ended well) for each turn."""
    for e in events:
        if e.redacted:
            continue
        if e.kind == FEEDBACK_REACTION or (
            e.kind == "message.received" and e.provenance.actor == "owner:main"
        ):
            engaged.append(event_time(e, zone))
        if e.kind not in KINDS or not found.start <= event_time(e, zone) < end:
            continue
        payload = e.payload
        turn = e.correlation_id or ""
        if e.kind == "model.usage":
            models[turn] = str(payload.get("model") or "?")
            continue
        if e.kind == WORKFLOW_FINISHED:
            found.workflows[str(payload.get("state", "?"))] += 1
            continue
        if e.kind == STEP_UNCERTAIN:
            found.uncertain_steps += 1
            continue
        if e.kind == REACHED:
            found.goals_reached += 1
            continue
        if e.kind == SUPPRESSED:
            found.held += 1
            continue
        if e.kind == DELIVERED:
            if not payload.get("explicit"):
                spoken.append(event_time(e, zone))
            continue
        if e.kind == "turn.finished":
            owner = payload.get("thread_id") == OWNER_THREAD
            found.turns += 1
            found.owner_turns += owner
            reason = str(payload.get("reason", ""))
            if reason in STOPPED:
                found.stopped[reason] += 1
                found.stopped_owner += owner
            role = "conversation" if owner else str(payload.get("thread_id", "?")).split(":")[0]
            finished.append((event_time(e, zone), turn, role, reason == "completed"))
        elif e.kind == "tool.executed":
            found.tool_calls += 1
            if payload.get("is_error"):
                found.tool_errors[str(payload.get("tool", "?"))] += 1
            if payload.get("decision") == TIMED_OUT:
                found.timed_out += 1
            if outcome := payload.get("outcome"):
                found.outcomes[str(outcome)] += 1
                if outcome in ("UNCERTAIN", "FAILED"):
                    failed.add(turn)
                if outcome == "UNCERTAIN":
                    found.uncertain.append(
                        f"{payload.get('tool', '?')}: {payload.get('action_id') or '?'}"
                    )
        else:
            cost = float(payload.get("cost_usd") or 0)
            found.cost_usd += cost
            if not payload.get("foreground"):
                found.background_usd += cost
            session = str(payload.get("session", "?")).removeprefix("orion:")
            found.cost_by[session] = found.cost_by.get(session, 0.0) + cost
    found.spoke = len(spoken)
    found.unanswered = sum(
        not any(timedelta(0) <= at - said <= ENGAGED_WITHIN for at in engaged) for said in spoken
    )
    found.misses = [m for m in misses(events, zone) if found.start <= m.at < end]
    corrected: set[str] = set()
    for miss in found.misses:
        answered = [
            (at, turn)
            for at, turn, role, _ in finished
            if role == "conversation" and abs(miss.at - at) <= MISS_WITHIN
        ]
        if answered:
            corrected.add(max(answered)[1])
    for _, turn, role, ended_well in finished:
        tally = found.success.setdefault((role, models.get(turn, "?")), [0, 0])
        tally[0] += ended_well and turn not in failed and turn not in corrected
        tally[1] += 1
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
    lines.append(f"Ratés relevés le soir : {versus(this.corrected, before.corrected)}.")
    lines += [
        f"  - {m.at:%a %d/%m %H:%M} : {(m.why or m.said or m.answered)[:120]}"
        for m in this.misses[-5:]
    ]
    lines.append(
        f"Coût : {versus(this.cost_usd, before.cost_usd, ' $')}, "
        f"dont {this.background_usd:.2f} $ sans qu'il ait rien demandé."
    )
    top = [f"{s} {usd:.2f} $" for s, usd in this.top_costs(5)]
    if top:
        lines.append(f"  surtout : {', '.join(top)}")
    done = ", ".join(
        f"{this.outcomes[o]} {label}" for o, label in OUTCOMES.items() if this.outcomes[o]
    )
    lines.append(f"Actions : {done or 'aucune'}.")
    lines += [f"  - incertaine : {u}" for u in this.uncertain[-5:]]
    if this.timed_out:
        lines.append(f"Appels coupés au délai : {this.timed_out}.")
    if this.workflows or this.uncertain_steps:
        ended = ", ".join(f"{n} {state}" for state, n in this.workflows.most_common())
        lines.append(
            f"Travaux de fond : {ended or 'aucun fini'} ; "
            f"{this.uncertain_steps} étape(s) restée(s) incertaine(s)."
        )
    lines.append(f"Objectifs atteints : {versus(this.goals_reached, before.goals_reached)}.")
    if this.success:
        lines.append("Réussite par rôle et modèle (ni corrigé, ni arrêté, ni action ratée) :")
        lines += [
            f"  - {role} sur {model} : {ok}/{total} ({100 * ok // total} %)"
            for (role, model), (ok, total) in sorted(
                this.success.items(), key=lambda item: -item[1][1]
            )[:10]
        ]
    lines.append(
        f"Bruit : parlé de lui-même {versus(this.spoke, before.spoke)}, dont {this.unanswered}"
        f" sans réaction ; {this.held} chose(s) tue(s)."
    )
    return "\n".join(lines)
