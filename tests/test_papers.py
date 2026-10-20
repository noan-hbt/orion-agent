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
    said = await call(tool, ctx, action="file", attachment="at_id", kind="passeport",
                      label="Passeport", valid_until="2027-03-10")  # fmt: skip
    assert "Rangé" in said.content and "préviendrai" in said.content
    for i, month in enumerate(("06", "07", "08", "09")):
        await call(tool, ctx, action="file", attachment=f"at_pay{i}", kind="fiche de paie",
                   label=f"Paie {month}", issued=f"2026-{month}-30")  # fmt: skip
    missing = await call(tool, ctx, action="file", attachment="at_nope", kind="rib", label="RIB")
    assert missing.is_error

    found = await call(tool, ctx, action="find", kind="fiche de paie")
    assert found.content.index("Paie 09") < found.content.index("Paie 06")

    # Three months ahead, then one month ahead: two alerts, each once (by key).
    early = await vault.alerts(datetime(2026, 10, 20, 9, tzinfo=PARIS))
    assert early == []
    (three,) = await vault.alerts(datetime(2026, 12, 15, 9, tzinfo=PARIS))
    assert three.key.endswith(":90") and "Passeport" in three.notice
    (one,) = await vault.alerts(datetime(2027, 2, 15, 9, tzinfo=PARIS))
    assert one.key.endswith(":30")

    made = await call(tool, ctx, action="dossier", purpose="location")
    assert "Il manque" in made.content and "avis d'imposition" in made.content
    dossier = store.get(made.data["file_id"])
    assert dossier is not None
    # Cover, ID card image, the three newest payslips.
    assert len(PdfReader(dossier.path).pages) == 5

    passport = next(p for p in vault.papers() if p.kind == "passeport")
    await call(tool, ctx, action="remove", paper=passport.id)
    assert all(p.kind != "passeport" for p in vault.papers())


async def test_a_kbis_is_filed_as_one(journal: SqliteJournal, tmp_path: Path) -> None:
    store = FileStore(journal, tmp_path / "files")
    vault = Vault(journal, store, Kept(), "Europe/Paris", render)  # type: ignore[arg-type]
    (tool,) = paper_tools(vault)
    said = await call(tool, ToolContext("owner", "c1"), action="file", attachment="at_pay0",
                      kind="extrait Kbis", label="Kbis NERB Solutions")  # fmt: skip
    assert not said.is_error, said.content
    found = await call(tool, ToolContext("owner", "c1"), action="find", kind="extrait Kbis")
    assert "Kbis NERB Solutions" in found.content


async def test_a_kind_not_in_the_list_is_filed_as_other(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    store = FileStore(journal, tmp_path / "files")
    vault = Vault(journal, store, Kept(), "Europe/Paris", render)  # type: ignore[arg-type]
    (tool,) = paper_tools(vault)
    said = await call(tool, ToolContext("owner", "c1"), action="file", attachment="at_pay1",
                      kind="relevé bancaire", label="Relevé Revolut juin")  # fmt: skip
    assert not said.is_error, said.content
    assert [p.kind for p in vault.papers()] == ["autre"]
