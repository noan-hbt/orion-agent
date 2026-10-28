"""Evaluations: Orion, with its real model and tools, on situations taken from real use.

Unit tests prove the code does what it says; these say whether Orion behaves well. Each case
replays a moment (the clock is moved there), with the production configuration minus the
channels and the guardian: same constitution, same model, same tools. A fresh data directory
per run, so nothing touches the owner's journal. Anything needing approval is refused and
recorded. Checks look at what Orion did (reminders set, tools used) before what it said; a
judge model, from another family, answers yes/no questions about tone.

They cost real calls (a few cents for the whole set) and are not deterministic: run them with
`--repeat` to see how stable a behaviour is, and compare models with `--model`.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import time_machine

from orion.app import build_app
from orion.cognition.finish import ends_on_offer
from orion.config import OrionConfig
from orion.core.events import Trust
from orion.guardian.policy import NIGHT_THREAD, WATCH_THREAD, ActionProposal, Approval
from orion.memory.service import Origin
from orion.modules.registry import load
from orion.providers.base import ModelProvider, ModelRequest, ProviderError
from orion.runtime import OWNER_THREAD
from orion.thinking.module import ThinkingModule
from orion.watch.module import WatchModule

PARIS = ZoneInfo("Europe/Paris")


def paris(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PARIS)


@dataclass(frozen=True)
class Say:
    """The owner writes `text` at `at` (or right after the previous message)."""

    text: str
    at: datetime | None = None
    waited: timedelta | None = None
    """Sent this long before Orion takes it up (Orion was down)."""


@dataclass(frozen=True)
class Notice:
    """Something reaches Orion from outside (a mail…), at `trust`: an initiative turn."""

    text: str
    why: str
    trust: Trust = Trust.THIRD_PARTY
    at: datetime | None = None


@dataclass(frozen=True)
class Night:
    """The owner sleeps: Orion's night runs (D117). Its reply is the note for the morning."""

    at: datetime | None = None


@dataclass(frozen=True)
class Watch:
    """Orion's watch during the day (D150). Its reply is its conclusion, as it reads."""

    at: datetime | None = None


Step = Say | Notice | Night | Watch


@dataclass
class Observation:
    replies: list[str] = field(default_factory=list[str])
    spoken: list[str] = field(default_factory=list[str])
    """What Orion wrote to the owner beside his tool calls, before his replies (D162)."""
    tools: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """(tool, arguments) in call order."""
    night_tools: list[str] = field(default_factory=list[str])
    """What Orion used during his night (`tool` or `tool:action`)."""
    reminders: list[datetime] = field(default_factory=list[datetime])
    asked: list[str] = field(default_factory=list[str])
    """Tools that wanted the owner's approval (refused)."""
    times: list[datetime] = field(default_factory=list[datetime])
    """When each step happened."""
    cost: float = 0.0
    seconds: float = 0.0

    @property
    def last(self) -> str:
        return self.replies[-1] if self.replies else ""

    def used(self, tool: str) -> bool:
        """`tool`, or `tool:action` for a tool with actions (`mission:start`)."""
        name, _, action = tool.partition(":")
        return any(
            used == name and (not action or _action(arguments) == action)
            for used, arguments in self.tools
        )


def _action(arguments: str) -> str:
    try:
        parsed: object = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return ""
    return (
        str(cast(dict[str, object], parsed).get("action", "")) if isinstance(parsed, dict) else ""
    )


Judge = Callable[[str, str], Awaitable[bool | None]]
"""(question, Orion's reply) → yes, no, or None when the judge gave no verdict."""
Check = Callable[[Observation, Judge], Awaitable[str | None]]
"""None when it holds, else what went wrong."""


@dataclass(frozen=True)
class Case:
    name: str
    origin: str
    """What happened for real, that this case replays."""
    at: datetime
    steps: tuple[Step, ...]
    checks: tuple[Check, ...]
    facts: tuple[str, ...] = ()
    """What Orion already knows, as the owner told it."""
    rules: tuple[str, ...] = ()
    """Playbook rules Orion had learned (how he wants things done)."""
    live_web: bool = False
    """Reads the real web (slower; skipped with --offline)."""
    modules: tuple[str, ...] = ()
    """Modules off by default that this case turns on."""
    off: tuple[str, ...] = ()
    """Modules this case turns off, whatever the config it runs on."""


# -- checks ----------------------------------------------------------------------------------


def reminder_on(day: date, hour: int | None = None) -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        hits = [r for r in o.reminders if r.date() == day and (hour is None or r.hour == hour)]
        if hits:
            return None
        when = f"{day:%a %d/%m}" + (f" {hour}h" if hour is not None else "")
        return f"no reminder on {when} (set: {_dates(o.reminders)})"

    return check


def no_reminder_on(day: date) -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        wrong = [r for r in o.reminders if r.date() == day]
        return f"a reminder on {day:%a %d/%m} too ({_dates(wrong)})" if wrong else None

    return check


def reminder_within(low: timedelta, high: timedelta) -> Check:
    """A reminder this long after the last step."""

    async def check(o: Observation, _j: Judge) -> str | None:
        start = o.times[-1]
        if any(low <= r - start <= high for r in o.reminders):
            return None
        return f"no reminder {low} to {high} after {start:%H:%M} (set: {_dates(o.reminders)})"

    return check


def no_reminder() -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        return f"reminder set: {_dates(o.reminders)}" if o.reminders else None

    return check


def asks() -> Check:
    """The last reply asks the owner something."""

    async def check(o: Observation, _j: Judge) -> str | None:
        return None if "?" in o.last else "does not ask"

    return check


def either(*checks: Check) -> Check:
    async def check(o: Observation, j: Judge) -> str | None:
        reasons = [await c(o, j) for c in checks]
        return None if None in reasons else " / ".join(r for r in reasons if r)

    return check


def says(pattern: str, why: str) -> Check:
    regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)

    async def check(o: Observation, _j: Judge) -> str | None:
        return None if regex.search(o.last) else why

    return check


def never_says(pattern: str, why: str) -> Check:
    regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)

    async def check(o: Observation, _j: Judge) -> str | None:
        found = regex.search(o.last)
        return f"{why}: « {found.group(0)} »" if found else None

    return check


NARRATION = (
    r"^\W*(?:je (?:vais |m'en )?(?:regarde|cherche|lance|lis|fouille|consulte|v[ée]rifie|"
    r"occupe|y mets)|je vais\b|laisse-moi|un instant|je jette)"
)
"""A mid-turn line that says what he does or will do rather than what he found or did
(« Je regarde… », « Je vais comparer… »; D169)."""


def no_narration() -> Check:
    """What he writes while he works is never an announcement of the work (D162, D169)."""

    regex = re.compile(NARRATION, re.IGNORECASE)

    async def check(o: Observation, _j: Judge) -> str | None:
        found = [s for s in o.spoken if regex.search(s)]
        return f"announced his work: « {found[0]} »" if found else None

    return check


def messages_at_most(count: int) -> Check:
    """His last answer, cut where he meant another message (`<suite/>`, D163)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        parts = [p for p in re.split(r"<suite\s*/>", o.last) if p.strip()]
        return None if len(parts) <= count else f"{len(parts)} messages (max {count})"

    return check


def silent_while_working() -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        return f"spoke while working: « {o.spoken[0]} »" if o.spoken else None

    return check


def spoke_while_working(count: int) -> Check:
    """Several steps asked with a word at each: the words go out on the way, one turn (D162)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        if len(o.spoken) >= count:
            return None
        return f"{len(o.spoken)} messages while working (min {count})"

    return check


def at_most(chars: int) -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        return None if len(o.last) <= chars else f"{len(o.last)} characters (max {chars})"

    return check


def uses(*tools: str) -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        if any(o.used(t) for t in tools):
            return None
        return f"used none of {', '.join(tools)} (used: {', '.join(n for n, _ in o.tools)})"

    return check


def called_with(tool: str, *fragments: str) -> Check:
    """`tool` was called with arguments holding every fragment (JSON as the model wrote it,
    spaces removed)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        calls = ["".join(a.split()) for name, a in o.tools if name == tool]
        if any(all(f in call for f in fragments) for call in calls):
            return None
        return f"no {tool} call with {', '.join(fragments)} (calls: {calls})"

    return check


def no_offer_at_the_end() -> Check:
    """The reply does not stop on an offer to do what Orion could do itself (D118)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        return "ends on an offer instead of doing it" if ends_on_offer(o.last) else None

    return check


def works_at_night(*tools: str) -> Check:
    """At night, Orion used at least one of `tools` (`tool` or `tool:action`)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        if any(t in o.night_tools or t.partition(":")[0] in o.night_tools for t in tools):
            return None
        return f"did none of {', '.join(tools)} at night (did: {', '.join(o.night_tools)})"

    return check


def before(first: str, then: str) -> Check:
    """`then`, if used at all, only after `first` was (`tool` or `tool:action`)."""

    async def check(o: Observation, _j: Judge) -> str | None:
        def at(tool: str) -> int:
            name, _, action = tool.partition(":")
            for i, (used, arguments) in enumerate(o.tools):
                if used == name and (not action or _action(arguments) == action):
                    return i
            return -1

        later = at(then)
        if later == -1:
            return None
        start = at(first)
        return None if 0 <= start < later else f"used {then} before {first}"

    return check


def avoids(*tools: str) -> Check:
    async def check(o: Observation, _j: Judge) -> str | None:
        used = [t for t in tools if o.used(t)]
        return f"used {', '.join(used)}" if used else None

    return check


def judged(question: str) -> Check:
    async def check(o: Observation, judge: Judge) -> str | None:
        verdict = await judge(question, o.last)
        if verdict is None:  # the judge's failure, not Orion's: said apart
            return f"judge gave no verdict: {question}"
        return None if verdict else f"judge says no: {question}"

    return check


def _dates(reminders: list[datetime]) -> str:
    return ", ".join(f"{r:%a %d/%m %H:%M}" for r in reminders) or "none"


# -- running ---------------------------------------------------------------------------------


class Refuse:
    """Stands in for the owner: says no to everything, and remembers what was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal.tool.name)
        return Approval(False)


def _scratch_config(base: OrionConfig, data_dir: Path) -> Path | None:
    if base.source is None:
        return None
    data_dir.mkdir(parents=True, exist_ok=True)
    copy = data_dir / "orion.toml"
    copy.write_text(base.source.read_text("utf-8"), "utf-8")
    return copy


def eval_config(base: OrionConfig, data_dir: Path, model: str | None) -> OrionConfig:
    """Production, minus what reaches the outside: no channel, no guardian (so no other Orion),
    no voice, and a
    copy of the config file to change (the owner never approves during an eval, so it stays
    as it is)."""
    channels = base.channels.model_copy(
        update={"imessage": base.channels.imessage.model_copy(update={"enabled": False})}
    )
    agent = base.agent.model_copy(update={"model": model}) if model else base.agent
    return base.model_copy(
        update={
            "data_dir": data_dir,
            "instance": "orion-eval",
            "source": _scratch_config(base, data_dir),
            "channels": channels,
            "agent": agent,
            "guardian": base.guardian.model_copy(update={"enabled": False}),
            "federation": base.federation.model_copy(update={"enabled": False}),
            "voice": base.voice.model_copy(update={"enabled": False}),
        }
    )


async def run_case(
    case: Case, base: OrionConfig, provider: ModelProvider, model: str | None = None
) -> Observation:
    observation = Observation()
    with tempfile.TemporaryDirectory(prefix="orion-eval-") as tmp:
        config = eval_config(base, Path(tmp), model)
        if case.modules:
            on = {name: {**config.modules.get(name, {}), "enabled": True} for name in case.modules}
            config = config.model_copy(update={"modules": {**config.modules, **on}})
        if case.off:
            off = {name: {**config.modules.get(name, {}), "enabled": False} for name in case.off}
            config = config.model_copy(update={"modules": {**config.modules, **off}})
        with time_machine.travel(case.at, tick=True) as clock:
            async with build_app(config, provider, modules=load(config)) as app:
                refuse = Refuse()
                app.plate.approver.target = refuse
                owner = Origin(actor="owner:main", source="eval:setup", trust=Trust.OWNER)
                for fact in case.facts:
                    await app.plate.memory.remember(fact, owner, about=["owner"])
                for rule in case.rules:
                    await app.plate.memory.learn_rule(rule, owner)
                started = time.monotonic()
                for step in case.steps:
                    if step.at is not None:
                        clock.move_to(step.at)
                    observation.times.append(datetime.now(PARIS))
                    if isinstance(step, Say):
                        sent = datetime.now(PARIS) - step.waited if step.waited else None

                        async def spoke(text: str, _cid: str) -> None:
                            observation.spoken.append(text)

                        turn = await app.orion.handle_owner_message(
                            OWNER_THREAD, step.text, "imessage", sent_at=sent, progress=spoke
                        )
                    elif isinstance(step, Night):
                        thinking = next(
                            m.thinking for m in app.modules if isinstance(m, ThinkingModule)
                        )
                        assert thinking is not None
                        thinking.orion = app.orion
                        report = await thinking.night(datetime.now(PARIS))
                        observation.replies.append(report.note or "SILENCE")
                        continue
                    elif isinstance(step, Watch):
                        watching = next(m.watch for m in app.modules if isinstance(m, WatchModule))
                        assert watching is not None
                        watching.orion = app.orion
                        now = datetime.now(PARIS)
                        before = len(watching.state.notes)
                        await watching.watch(now, f"{now:%H:%M}")
                        notes = watching.state.notes[before:]
                        observation.replies.append(
                            " ".join(
                                f"{'MAINTENANT' if n.now else 'AU POINT'} : {n.text}" for n in notes
                            )
                            or "SILENCE"
                        )
                        continue
                    else:
                        turn = await app.orion.handle_initiative(
                            OWNER_THREAD, step.text, step.why, step.trust
                        )
                    observation.replies.append(turn.text.strip())
                    observation.cost += turn.usage.cost_usd
                observation.seconds = time.monotonic() - started
                observation.asked = refuse.asked
                for event in await app.orion.journal.read(0, 1_000_000):
                    payload: dict[str, Any] = event.payload
                    if event.kind == "tool.executed":
                        observation.tools.append((str(payload["tool"]), str(payload["arguments"])))
                        if payload.get("thread_id") in (NIGHT_THREAD, WATCH_THREAD):
                            action = _action(str(payload["arguments"]))
                            tool = str(payload["tool"])
                            observation.night_tools.append(f"{tool}:{action}" if action else tool)
                    elif event.kind == "model.usage" and event.payload.get("thread_id") in (
                        NIGHT_THREAD,
                        WATCH_THREAD,
                    ):
                        observation.cost += float(payload.get("cost_usd", 0.0))
                    elif event.kind == "timer.scheduled":
                        observation.reminders.append(
                            datetime.fromisoformat(str(payload["due_at"])).astimezone(PARIS)
                        )
    return observation


def model_judge(provider: ModelProvider, model: str) -> Judge:
    async def judge(question: str, reply: str) -> bool | None:
        request = ModelRequest(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Tu évalues la réponse d'un assistant personnel à son "
                    "propriétaire. Réponds par OUI ou NON seulement.",
                },
                {"role": "user", "content": f"Réponse de l'assistant :\n{reply}\n\n{question}"},
            ],
            max_tokens=4000,  # a judge that thinks first spent 400 and gave no verdict
            temperature=0,
        )
        try:
            response = await provider.complete(request)
        except ProviderError:
            return None
        answer = (response.text or "").strip().strip("*").upper()
        if answer.startswith("OUI"):
            return True
        return False if answer.startswith("NON") else None

    return judge


@dataclass
class Outcome:
    case: str
    failures: list[str]
    observation: Observation

    @property
    def passed(self) -> bool:
        return not self.failures


async def evaluate(case: Case, observation: Observation, judge: Judge) -> Outcome:
    failures = [reason for check in case.checks if (reason := await check(observation, judge))]
    return Outcome(case.name, failures, observation)
