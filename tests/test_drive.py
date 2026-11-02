"""His Google Drive, through the guardian: found, read, kept, and Orion's files put in its own
folder; never a sharing (D206)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.drive.api import GOOGLE_API, DriveSettings, GoogleAuth
from orion.drive.module import UPLOADED, drive_tools
from orion.drive.service import Drive
from orion.files.module import FileStore
from orion.tools.base import ToolContext
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import CONVERSATION

DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"


class FakeDrive:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {
            "d1": {"id": "d1", "name": "Devis plombier", "mimeType": DOC, "text": "Devis : 340 €"}
        }
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(f"{request.method} {path}")
        if path == "/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer at"
        if request.method == "GET" and path == "/drive/v3/files":
            q = request.url.params["q"]
            found = [
                f
                for f in self.files.values()
                if ("appProperties" in q) == ("appProperties" in f)
                and all(w in (f["name"] + f.get("text", "")).lower() for w in _words(q))
                and ("name = '" not in q or f"name = '{f['name']}'" in q)
            ]
            return httpx.Response(200, json={"files": found})
        if request.method == "PATCH":
            file = self.files[path.rsplit("/", 1)[-1]]
            if "addParents" in request.url.params:
                file["parents"] = [request.url.params["addParents"]]
            file |= json.loads(request.content or b"{}")
            return httpx.Response(200, json=file)
        if request.method == "GET" and path.endswith("/export"):
            return httpx.Response(200, text=self.files[path.split("/")[-2]]["text"])
        if request.method == "GET":
            return httpx.Response(200, json=self.files[path.rsplit("/", 1)[-1]])
        if path == "/drive/v3/files":  # a folder
            made = json.loads(request.content) | {"id": f"f{len(self.files)}"}
        else:  # a multipart upload: its metadata is the first part
            metadata = request.content.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0]
            made = json.loads(metadata) | {"id": f"u{len(self.files)}", "mimeType": "x/y"}
        self.files[made["id"]] = made
        return httpx.Response(200, json=made)


def _words(q: str) -> list[str]:
    return [p.split("'")[0].lower() for p in q.split("fullText contains '")[1:]]


async def call(tool: Any, ctx: ToolContext, **args: Any) -> Any:
    return await tool.handler(tool.parse(json.dumps(args)), ctx)


def google_behind(google: FakeDrive, settings: DriveSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GOOGLE_API, auth=GoogleAuth(settings), transport=httpx.MockTransport(google.handle)
    )


async def test_his_drive_is_searched_read_and_orion_files_go_in_its_folder(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "r")
    google = FakeDrive()
    settings = DriveSettings(enabled=True, client_id="c")
    to_google = google_behind(google, settings)
    config = guardian_config(tmp_path, modules={"drive": settings.model_dump()})
    async with running_guardian(config, drive=to_google) as running:
        store = FileStore(journal, tmp_path / "files")
        drive = Drive(running.client.relay(), "/v1/drive/")
        relay = running.client.relay()
        tool, upload = drive_tools(drive, store, journal)
        ctx = ToolContext("owner", "c1")

        found = await call(tool, ctx, action="search", query="devis")
        assert "Devis plombier" in found.content and found.trust == Trust.THIRD_PARTY
        read = await call(tool, ctx, action="read", file="d1")
        assert "340 €" in read.content and read.trust == Trust.THIRD_PARTY

        made = await store.save("facture.pdf", b"%PDF-1.4", "application/pdf", "t", ctx)
        for name in ("facture.pdf", "facture 2.pdf"):
            put = await call(upload, ctx, file_id=made.id, name=name, folder="Factures")
            assert not put.is_error, put.content
        folders = [f for f in google.files.values() if f.get("mimeType") == FOLDER]
        assert sorted(f["name"] for f in folders) == ["Factures", "Orion"]
        assert sum(e.kind == UPLOADED for e in await journal.read()) == 2

        # Sharing one of his files never reaches Google.
        shared = await relay.post(
            "/v1/drive/drive/v3/files/d1/permissions", json={"role": "reader", "type": "anyone"}
        )
        assert shared.status_code == 403
        assert not any("permissions" in s for s in google.seen)
        await relay.aclose()
        await drive.aclose()


async def test_with_full_access_a_file_moves_only_where_he_said_yes(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "r")
    google = FakeDrive()
    google.files["d1"]["parents"] = ["inbox"]
    settings = DriveSettings(enabled=True, client_id="c", access="full")
    config = guardian_config(tmp_path, modules={"drive": settings.model_dump()})
    async with running_guardian(config, drive=google_behind(google, settings)) as running:
        running.fake.inbound("salut")
        drive = Drive(running.client.relay(), "/v1/drive/", full=True)
        _, _, change = drive_tools(drive, FileStore(journal, tmp_path / "files"), journal)
        move = {"action": "move", "file": "d1", "to": "travaux", "what": "Devis → Travaux"}
        grant, asked = await running.client.request_approval("drive_change", move, "", CONVERSATION)

        moved = await call(change, ToolContext("owner", "c1", grant=grant), **move)
        assert moved.is_error and google.files["d1"]["parents"] == ["inbox"]  # no yes yet
        running.fake.react(asked, "like")
        elsewhere = await call(
            change, ToolContext("owner", "c1", grant=grant), **move | {"to": "x"}
        )
        assert elsewhere.is_error and google.files["d1"]["parents"] == ["inbox"]
        moved = await call(change, ToolContext("owner", "c1", grant=grant), **move)
        assert not moved.is_error and google.files["d1"]["parents"] == ["travaux"]
        await drive.aclose()


def test_google_auth_turns_drive_on_in_the_config_keeping_its_comments(tmp_path: Path) -> None:
    from orion.drive.api import enable_in_config

    path = tmp_path / "orion.toml"
    path.write_text('# mon Orion\ninstance = "main"\n\n[modules.calendar]\nenabled = false\n')
    enable_in_config(path, "123.apps.googleusercontent.com", "full")
    text = path.read_text()
    assert "# mon Orion" in text and "[modules.calendar]" in text
    settings = DriveSettings.model_validate(__import__("tomllib").loads(text)["modules"]["drive"])
    assert settings.enabled and settings.access == "full"
    assert settings.client_id == "123.apps.googleusercontent.com"
