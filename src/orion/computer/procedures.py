"""How a démarche went through on a site, kept once it worked, given back when the site returns.

Orion explored Nibelis, INPI or Hyperplanning from scratch at every visit. Once a démarche
worked, `browser_save_procedure` keeps how: the path, the vault login, the traps. It is kept
only when this very turn's browser backs it (ORION-5, D255): the proof is words a page of that
site showed, not another site's nor another tool's; the path names nothing the browser did not
show there; and the verifier, another model (D252), reads the site's pages and agrees. The
model saying so is not enough. Without a verifier nothing is kept (fail closed). What the
browser did is kept with it; the traps, which nothing checks, are given back as unverified.

What is kept is the path followed, not a success: the outcome counts as confirmed only when the
proof is a signal of it, words no page of the site showed before the last action that acts
(click, typing, choice, key, upload) and that a page showed from then on. A proof already there
before, like « Bienvenue » or « Mon compte », proves nothing: the path is given back as followed,
its outcome unconfirmed.

The next time Orion's browser shows that site, in any conversation, the first page comes with the
procedure: it follows the path instead of exploring again.

    browser.procedure
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.verifier import Verifier
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.guardian.logins import site_of
from orion.tools.base import ToolContext
from orion.tools.outcome import Outcome

log = structlog.get_logger(__name__)

PROCEDURE = "browser.procedure"
EVIDENCE_CHARS = 12_000
"""What the verifier reads of what the browser showed on the site this turn, newest kept."""
PATH_STEPS = 15
"""Actions of the observed path kept with a procedure, the last ones."""
_ADDRESS = re.compile(r"https?://[^\s»\"”)]+")
_LABEL = re.compile(r"«([^»]+)»|“([^”]+)”|\"([^\"]+)\"")
_ACTS = ("click", "type", "select", "press", "upload")
"""Actions that change something on the site: what an outcome follows."""


@dataclass(frozen=True)
class Seen:
    """A page Orion's browser showed during a turn, and the action that led there."""

    action: str
    url: str
    text: str
    """Its text and its elements, as Orion read them (secrets masked)."""
    action_id: str = ""
    """The outcome id of the call that acted to get here (D258); empty for a mere read."""
    acted_on: str = ""
    """The site the call acted on, when it landed on another one (a payment, a sign-in)."""


class ProcedureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str = Field(max_length=300, description="The site's address, e.g. client.nibelis.com")
    task: str = Field(max_length=200, description="The démarche, e.g. « télécharger le bulletin »")
    steps: str = Field(
        max_length=1500,
        description="The path that worked, step by step (menus, buttons, addresses)",
    )
    login: str = Field(default="", max_length=60, description="The vault login used, if any")
    traps: str = Field(default="", max_length=800, description="What went wrong on the way")
    proof: str = Field(
        min_length=3,
        max_length=300,
        description="Words copied exactly from the page that shows it went through",
    )
    actions: list[str] = Field(
        default_factory=list[str],
        max_length=30,
        description="The action ids (« action » in the [issue …] lines) of this démarche's "
        "steps, typing and clicks; its last step, after which the proof appeared, is always "
        "counted. Only these are confirmed, never the rest of the site",
    )


@dataclass(frozen=True)
class Procedure:
    site: str
    task: str
    steps: str
    path: tuple[str, ...]
    """What the browser actually did on the site that turn (action → address)."""
    login: str
    traps: str
    confirmed: bool
    """Whether the proof appeared only after the last action (D255), not just the path."""
    when: str
    """The clock of the event that kept it: when it last worked."""
    trust: Trust
    source: str


@dataclass
class Procedures:
    """Site → the démarches that went through there, the latest of each. Rebuilt from the
    journal alone."""

    name: str = "procedures"
    by_site: dict[str, dict[str, Procedure]] = field(
        default_factory=dict[str, dict[str, Procedure]]
    )
    _checkpoint: int = 0

    async def checkpoint(self) -> int:
        return self._checkpoint

    async def reset(self) -> None:
        self.by_site.clear()
        self._checkpoint = 0

    async def apply(self, event: Event) -> None:
        self._checkpoint = event.seq
        if event.kind != PROCEDURE or event.redacted:
            return
        payload: dict[str, Any] = event.payload
        site = str(payload["site"])
        self.by_site.setdefault(site, {})[str(payload["task"]).casefold()] = Procedure(
            site=site,
            task=str(payload["task"]),
            steps=str(payload["steps"]),
            path=tuple(str(step) for step in payload.get("path", [])),
            login=str(payload.get("login", "")),
            traps=str(payload.get("traps", "")),
            confirmed=bool(payload.get("confirmed", False)),
            when=event.hlc,
            trust=event.trust,
            source=event.provenance.source,
        )

    def hint(self, url: str) -> str:
        """What is known of this page's site, for the first page Orion sees of it."""
        site = site_of(url)
        known = self.by_site.get(site, {}) if site else {}
        if not known:
            return ""
        lines = [
            f"[Démarches déjà faites sur {site}, parcours suivi la dernière fois : suis-le "
            "plutôt que d'explorer, et vérifie l'issue sur la page."
        ]
        for p in known.values():
            day = datetime.fromtimestamp(Timestamp.decode(p.when).wall_ms / 1000, UTC)
            outcome = (
                "issue confirmée par la page"
                if p.confirmed
                else "issue non confirmée : la page n'a pas montré qu'elle avait abouti"
            )
            line = f"- {p.task} (parcours suivi le {day:%d/%m/%Y}, {outcome}) : {p.steps}"
            if p.login:
                line += f" (login du coffre : {p.login})"
            if p.path:
                line += f" Parcours observé : {' ; '.join(p.path)}."
            if p.traps:
                line += f" Pièges notés (non vérifiés) : {p.traps}"
            lines.append(line)
        return "\n".join(lines) + "]"


def _flat(text: str) -> str:
    return " ".join(text.casefold().split())


def _page(url: str) -> str:
    """An address without its query: what a path step names, whatever the session token."""
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/')}"


def _confirming(proof: str, pages: list[Seen]) -> Seen | None:
    """The step whose outcome the proof shows: the last action on the site that acts, when the
    proof is absent from every page before it and shown on a page from then on. Generic words
    already there prove nothing."""
    last = max((i for i, p in enumerate(pages) if p.action.startswith(_ACTS)), default=None)
    if last is None:
        return None
    before = any(proof in _flat(p.text) for p in pages[:last])
    shown = any(proof in _flat(p.text) for p in pages[last:])
    return pages[last] if shown and not before else None


def settled_steps(args: ProcedureInput, seen: list[Seen]) -> list[str]:
    """The actions a confirmed démarche settles (D258): its last step, the one the proof
    follows, and the steps it names that this turn's browser took on its site (or on the site
    that step acted on) up to it. Never the rest of the site: another démarche there keeps its
    own outcome."""
    site = site_of(args.site)
    step = _confirming(_flat(args.proof), [p for p in seen if site_of(p.url) == site])
    if step is None or not step.action_id:
        return []
    upto = next(i for i, p in enumerate(seen) if p is step)
    sites = {site, step.acted_on or site}
    taken = {
        p.action_id for p in seen[:upto] if p.action_id and {site_of(p.url), p.acted_on} & sites
    }
    return [step.action_id, *(a for a in dict.fromkeys(args.actions) if a in taken)]


def _contradicted(steps: str, site: str, pages: list[Seen]) -> list[str]:
    """What the path names that this turn's browser never showed on the site: an address it did
    not open there, a label (« … » or "…") no page of the site had."""
    opened = {_page(p.url) for p in pages}
    shown = _flat("\n".join(f"{p.action}\n{p.url}\n{p.text}" for p in pages))
    wrong: list[str] = []
    for address in _ADDRESS.findall(steps):
        if site_of(address) != site or _page(address) not in opened:
            wrong.append(address)
    for match in _LABEL.finditer(steps):
        label = next(g for g in match.groups() if g is not None)
        if _flat(label) and _flat(label) not in shown:
            wrong.append(f"« {label.strip()} »")
    return wrong


async def keep_procedure(
    journal: EventStore,
    verifier: Verifier | None,
    args: ProcedureInput,
    ctx: ToolContext,
    seen: list[Seen],
    logins: dict[str, str],
) -> tuple[Outcome | None, str]:
    """(its outcome, what to tell the model): None when not kept, else CONFIRMED or, the
    outcome not shown, ATTEMPTED (D258). Kept only when this turn's browser, on this very site,
    shows it went through and does not contradict the path. `seen`: what the browser showed
    this turn; `logins`: login → where the vault last signed it in."""
    site = site_of(args.site)
    if not site:
        return None, f"{args.site} n'est pas une adresse de site."
    if verifier is None:
        return None, "Pas de vérificateur configuré : procédure non gardée (rien de vérifié)."
    pages = [p for p in seen if site_of(p.url) == site]
    if not pages:
        return None, f"Ton navigateur n'a pas montré {site} pendant ce tour : rien à garder."
    # The proof comes from the site itself: another site, or another tool, proves nothing here.
    proof = _flat(args.proof)
    if not any(proof in _flat(p.text) for p in pages):
        return None, (
            f"Procédure non gardée : « {args.proof} » n'est sur aucune page de {site} montrée "
            "par ton navigateur pendant ce tour. Une fois la démarche vraiment faite, recopie les "
            "mots exacts de la page qui le montre."
        )
    if wrong := _contradicted(args.steps, site, pages):
        return None, (
            f"Procédure non gardée : ton navigateur n'a pas montré sur {site}, pendant ce tour, "
            f"ce que ton chemin cite : {', '.join(wrong)}. Décris le chemin réellement suivi."
        )
    if args.login and site_of(logins.get(args.login, "")) != site:
        return None, (
            f"Procédure non gardée : le coffre n'a jamais connecté « {args.login} » sur {site}."
        )
    path = [f"{p.action} → {p.url}" for p in pages][-PATH_STEPS:]
    confirmed = _confirming(proof, pages) is not None
    evidence = "\n---\n".join(f"{p.action} → {p.url}\n{p.text}" for p in pages)
    draft = (
        f"Sur {site}, la démarche « {args.task} » a abouti, par ce chemin : {args.steps}. "
        f"La page l'a montré : {args.proof}"
    )
    verdict = await verifier.check(draft, "", evidence[-EVIDENCE_CHARS:])
    if not verdict.ok:
        log.info("procedure.unverified", site=site, task=args.task, issues=verdict.issues)
        return None, (
            "Procédure non gardée : ce que les pages ont montré ne confirme pas que la démarche "
            f"a abouti ({'; '.join(verdict.issues) or 'non vérifié'})."
        )
    await journal.append(
        [
            NewEvent(
                kind=PROCEDURE,
                payload={
                    "site": site,
                    "task": args.task,
                    "steps": args.steps,
                    "path": path,
                    "login": args.login,
                    "traps": args.traps,
                    "proof": args.proof,
                    "confirmed": confirmed,
                    "thread_id": ctx.thread_id,
                },
                # What it rests on is what the web showed: it is worth no more than that.
                provenance=Provenance(source=f"web:{site}", actor="orion:browser"),
                trust=Trust.WEB,
                correlation_id=ctx.correlation_id,
                causation_id=ctx.origin_event_id,
            )
        ]
    )
    if confirmed:
        return (
            Outcome.CONFIRMED,
            f"Procédure gardée pour {site}, issue confirmée : la prochaine visite la reçoit.",
        )
    return Outcome.ATTEMPTED, (
        f"Parcours gardé pour {site}, issue non confirmée : « {args.proof} » était déjà sur une "
        "page avant ta dernière action, il ne montre pas que la démarche a abouti. La prochaine "
        "visite reçoit le parcours, sans le dire réussi."
    )
