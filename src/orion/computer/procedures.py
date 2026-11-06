"""How a démarche went through on a site, kept once it worked, given back when the site returns.

Orion explored Nibelis, INPI or Hyperplanning from scratch at every visit. Once a démarche
worked, `browser_save_procedure` keeps how: the path, the vault login, the traps. It is kept
only when it is verified (ORION-5, D252): this very turn's browser must have shown the site, and
the verifier, another model, must find in what the tools returned that the démarche did go
through. The model saying so is not enough. Without a verifier nothing is kept (fail closed).

The next time Orion's browser shows that site, in any conversation, the first page comes with the
procedure: it follows the path instead of exploring again.

    browser.procedure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.verifier import Verifier
from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.guardian.logins import site_of
from orion.tools.base import ToolContext

log = structlog.get_logger(__name__)

PROCEDURE = "browser.procedure"
READ_PAGE = 1000
"""Journal events read at a time while gathering this turn's evidence."""
EVIDENCE_CHARS = 12_000
"""What the verifier reads of this turn's tool results, newest kept."""


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
    proof: str = Field(max_length=300, description="What the page showed that says it went through")


@dataclass(frozen=True)
class Procedure:
    site: str
    task: str
    steps: str
    login: str
    traps: str
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
            login=str(payload.get("login", "")),
            traps=str(payload.get("traps", "")),
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
            f"[Démarches qui ont déjà marché sur {site} : suis ce chemin plutôt que d'explorer."
        ]
        for p in known.values():
            day = datetime.fromtimestamp(Timestamp.decode(p.when).wall_ms / 1000, UTC)
            line = f"- {p.task} (réussie le {day:%d/%m/%Y}) : {p.steps}"
            if p.login:
                line += f" (login du coffre : {p.login})"
            if p.traps:
                line += f" Pièges : {p.traps}"
            lines.append(line)
        return "\n".join(lines) + "]"


async def _turn(journal: EventStore, ctx: ToolContext) -> tuple[set[str], str]:
    """(sites the browser showed, what the tools returned) during this turn."""
    after = 0
    if ctx.origin_event_id:
        origin = await journal.get(ctx.origin_event_id)
        after = origin.seq - 1 if origin is not None else 0
    sites: set[str] = set()
    results: list[str] = []
    while events := await journal.read(after, READ_PAGE):
        for event in events:
            if event.correlation_id != ctx.correlation_id or event.redacted:
                continue
            if event.kind == "browser.page":
                site = site_of(str(event.payload.get("url", "")))
                if site:
                    sites.add(site)
            elif event.kind == MESSAGE_KIND:
                message = event.payload["message"]
                content = message.get("content")
                if message.get("role") == "tool" and isinstance(content, str) and content:
                    results.append(content)
        after = events[-1].seq
    return sites, "\n---\n".join(results)[-EVIDENCE_CHARS:]


async def keep_procedure(
    journal: EventStore, verifier: Verifier | None, args: ProcedureInput, ctx: ToolContext
) -> tuple[bool, str]:
    """(kept, what to tell the model). Kept only when this turn shows it went through."""
    site = site_of(args.site)
    if not site:
        return False, f"{args.site} n'est pas une adresse de site."
    if verifier is None:
        return False, "Pas de vérificateur configuré : procédure non gardée (rien de vérifié)."
    sites, shown = await _turn(journal, ctx)
    if site not in sites:
        return False, f"Ton navigateur n'a pas montré {site} pendant ce tour : rien à garder."
    draft = f"Sur {site}, la démarche « {args.task} » a abouti. La page l'a montré : {args.proof}"
    verdict = await verifier.check(draft, "", shown)
    if not verdict.ok:
        log.info("procedure.unverified", site=site, task=args.task, issues=verdict.issues)
        return False, (
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
                    "login": args.login,
                    "traps": args.traps,
                    "proof": args.proof,
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
    return True, f"Procédure gardée pour {site} : la prochaine visite la reçoit."
