"""The papers vault: filed from attachments, warned before expiry, assembled into a file (D74)."""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image
from pypdf import PdfReader, PdfWriter

from orion.core.journal import SqliteJournal
from orion.files.module import FileStore
from orion.papers.module import Vault, paper_tools
from orion.tools.base import ToolContext

PARIS = ZoneInfo("Europe/Paris")


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(out, "PNG")
    return out.getvalue()


def pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class Kept:
    def __init__(self) -> None:
        self.files = {"at_id": (png(), "image/png", "at_id.png")}
        self.files |= {f"at_pay{i}": (pdf(), "application/pdf", f"p{i}.pdf") for i in range(4)}

    async def file(self, file_id: str) -> tuple[bytes, str, str] | None:
        return self.files.get(file_id)


async def render(_page: str) -> bytes:
    return pdf()


async def call(tool: Any, ctx: ToolContext, **args: Any) -> Any:
    return await tool.handler(tool.parse(json.dumps(args)), ctx)


async def test_papers_are_filed_found_warned_and_assembled(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    store = FileStore(journal, tmp_path / "files")
    vault = Vault(journal, store, Kept(), "Europe/Paris", render)  # type: ignore[arg-type]
    (tool,) = paper_tools(vault)
    ctx = ToolContext("owner", "c1")
    said = await call(tool, ctx, action="file", attachment="at_id", label="Passeport",
                      about="passeport français", valid_until="2027-03-10")  # fmt: skip
    assert "Rangé" in said.content and "préviendrai" in said.content
    for i, month in enumerate(("06", "07", "08", "09")):
        await call(tool, ctx, action="file", attachment=f"at_pay{i}", label=f"Paie {month}",
                   about="fiche de paie, Décathlon", issued=f"2026-{month}-30")  # fmt: skip
    missing = await call(tool, ctx, action="file", attachment="at_nope", label="RIB")
    assert missing.is_error

    found = await call(tool, ctx, action="find", query="fiches de PAIE")
    assert found.content.index("Paie 09") < found.content.index("Paie 06")
    assert "Passeport" not in found.content
    everything = await call(tool, ctx, action="find")
    assert everything.content.count("pa_") == 5
    nothing = await call(tool, ctx, action="find", query="assurance habitation")
    assert nothing.content == "Aucun papier correspondant."

    # Three months ahead, then one month ahead: two alerts, each once (by key).
    early = await vault.alerts(datetime(2026, 10, 20, 9, tzinfo=PARIS))
    assert early == []
    (three,) = await vault.alerts(datetime(2026, 12, 15, 9, tzinfo=PARIS))
    assert three.key.endswith(":90") and "Passeport" in three.notice
    (one,) = await vault.alerts(datetime(2027, 2, 15, 9, tzinfo=PARIS))
    assert one.key.endswith(":30")

    # Orion picks the papers, in order, and says what is missing.
    passport = (await vault.search("passeport"))[0]
    pay = [p.id for p in await vault.search("paie")][:3]
    made = await call(tool, ctx, action="dossier", papers=[passport.id, *pay],
                      title="Dossier de location", missing=["Avis d'imposition"])  # fmt: skip
    assert "Il manque : Avis d'imposition" in made.content
    dossier = store.get(made.data["file_id"])
    assert dossier is not None
    # Cover, ID card image, three payslips.
    assert len(PdfReader(dossier.path).pages) == 5
    wrong = await call(tool, ctx, action="dossier", papers=["pa_nope"])
    assert wrong.is_error and "find" in wrong.content

    await call(tool, ctx, action="remove", paper=passport.id)
    assert all(p.label != "Passeport" for p in vault.papers())


async def test_a_paper_is_found_by_meaning(journal: SqliteJournal, tmp_path: Path) -> None:
    """« mon attestation d'assurance » finds « Attestation MAAF » even without a shared word,
    when embeddings are there; papers filed before D131 are found by their old kind."""
    from orion.core.events import NewEvent, Provenance, Trust

    near = {"mutuelle": [1.0, 0.0], "Attestation MAAF habitation, appartement Dreux": [0.9, 0.1]}

    async def vectors(texts: Any) -> dict[str, Any]:
        return {t: near.get(t, [0.0, 1.0]) for t in texts}

    store = FileStore(journal, tmp_path / "files")
    vault = Vault(journal, store, Kept(), "Europe/Paris", render, vectors)  # type: ignore[arg-type]
    (tool,) = paper_tools(vault)
    ctx = ToolContext("owner", "c1")
    await call(tool, ctx, action="file", attachment="at_pay0", label="Attestation MAAF",
               about="habitation, appartement Dreux")  # fmt: skip
    found = await call(tool, ctx, action="find", query="mutuelle")
    assert "Attestation MAAF" in found.content

    made = await store.save("kbis.pdf", pdf(), "application/pdf", "paper", ctx, Trust.THIRD_PARTY)
    await journal.append(
        [
            NewEvent(
                kind="paper.filed",
                payload={"paper_id": "pa_old", "file_id": made.id, "kind": "extrait Kbis",
                         "label": "NERB Solutions", "holder": "", "issued": None,
                         "valid_until": None, "filed_at": "2026-10-20T10:00:00+02:00"},
                provenance=Provenance(source="thread:owner", actor="orion:core"),
                trust=Trust.THIRD_PARTY,
            )
        ]
    )  # fmt: skip
    await vault.sync()
    old = await call(tool, ctx, action="find", query="kbis")
    assert "NERB Solutions (extrait Kbis)" in old.content
