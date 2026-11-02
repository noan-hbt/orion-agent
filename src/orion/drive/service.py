"""Talking to Google Drive: search, read, fetch, upload (D206).

The same code runs with the credentials (Orion in one process) or through the guardian
(`/v1/drive/…`, D28): only the base of the address changes. What comes back is written by the
owner or by whoever shared it with him: it carries `Trust.THIRD_PARTY`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from orion.channels.inkbox import GRANT_HEADER
from orion.core.ids import new_id
from orion.drive.api import DriveError

FOLDER = "application/vnd.google-apps.folder"
_APPS = "application/vnd.google-apps."
TEXT_EXPORTS = {
    _APPS + "document": "text/plain",
    _APPS + "spreadsheet": "text/csv",
    _APPS + "presentation": "text/plain",
}
"""Google's own formats, read as text (a spreadsheet: its first sheet)."""
FILE_EXPORTS = {
    _APPS + "document": ("application/pdf", ".pdf"),
    _APPS + "spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    _APPS + "presentation": ("application/pdf", ".pdf"),
    _APPS + "drawing": ("image/png", ".png"),
}
"""Google's own formats, kept as a file anyone can open."""
KINDS = {
    FOLDER: "dossier",
    _APPS + "document": "Google Doc",
    _APPS + "spreadsheet": "Google Sheets",
    _APPS + "presentation": "Google Slides",
    "application/pdf": "PDF",
}
FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName,me),parents"
MAX_READ = 20 * 1024 * 1024


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime: str
    modified: datetime | None
    size: int | None
    link: str
    owner: str
    """Who owns it, when it is not the owner: a file shared with him."""
    parents: tuple[str, ...] = ()

    @classmethod
    def of(cls, data: dict[str, Any]) -> DriveFile:
        owners: list[dict[str, Any]] = data.get("owners") or []
        theirs = next((o for o in owners if not o.get("me")), None) if owners else None
        modified = data.get("modifiedTime")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            mime=str(data.get("mimeType", "")),
            modified=datetime.fromisoformat(str(modified)) if modified else None,
            size=int(data["size"]) if data.get("size") else None,
            link=str(data.get("webViewLink", "")),
            owner=str(theirs.get("displayName", "")) if theirs else "",
            parents=tuple(str(p) for p in data.get("parents") or ()),
        )

    @property
    def folder(self) -> bool:
        return self.mime == FOLDER

    def describe(self) -> str:
        kind = KINDS.get(self.mime) or self.mime.rsplit("/", 1)[-1]
        parts = [f"({self.id}) {self.name} — {kind}"]
        if self.modified:
            parts.append(f"modifié le {self.modified:%d/%m/%Y}")
        if self.owner:
            parts.append(f"partagé par {self.owner}")
        if self.parents:
            parts.append(f"dans {', '.join(self.parents)}")
        return ", ".join(parts)


def _quoted(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _refusal(response: httpx.Response) -> str:
    try:
        said = str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        said = response.text[:200]
    return f"Drive answered {response.status_code}: {said}"


class Drive:
    def __init__(
        self, http: httpx.AsyncClient, base: str, folder: str = "Orion", full: bool = False
    ) -> None:
        self._http = http
        self._base = base
        self.folder_name = folder
        self.full = full
        """He let Orion change his own files too (`access = "full"`)."""
        self._folders: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        grant: str | None = None,
    ) -> httpx.Response:
        headers = {"content-type": content_type} if content_type else {}
        if grant and self._base:  # his yes, for the guardian to check; never sent to Google
            headers[GRANT_HEADER] = grant
        try:
            response = await self._http.request(
                method,
                self._base + path,
                params=params,
                content=content,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"Drive unreachable: {exc!r}") from exc
        if response.status_code >= 400:
            raise DriveError(_refusal(response))
        return response

    async def _list(self, q: str, limit: int, order: str | None = None) -> list[DriveFile]:
        params = {"q": q, "pageSize": str(limit), "fields": f"files({FIELDS})"}
        if order:
            params["orderBy"] = order
        found = (await self._call("GET", "drive/v3/files", params)).json()
        return [DriveFile.of(f) for f in found.get("files", [])]

    async def search(self, query: str, limit: int = 20) -> list[DriveFile]:
        """By its words, in the name or the text; no words: the last ones modified."""
        words = query.split()
        clauses = ["trashed = false", *(f"fullText contains {_quoted(w)}" for w in words)]
        return await self._list(
            " and ".join(clauses), limit, None if words else "modifiedTime desc"
        )

    async def inside(self, folder: DriveFile, limit: int = 50) -> list[DriveFile]:
        q = f"{_quoted(folder.id)} in parents and trashed = false"
        return await self._list(q, limit, "folder,name")

    async def get(self, file_id: str) -> DriveFile:
        found = await self._call("GET", f"drive/v3/files/{file_id}", {"fields": FIELDS})
        return DriveFile.of(found.json())

    async def download(self, file: DriveFile, as_text: bool) -> tuple[bytes, str, str]:
        """(data, type, name): Google's own formats exported, as text or as a file."""
        if file.mime.startswith(_APPS):
            exports = {m: (t, ".txt") for m, t in TEXT_EXPORTS.items()} if as_text else FILE_EXPORTS
            if file.mime not in exports:
                raise DriveError(f"{file.name} cannot be exported ({file.mime})")
            kind, suffix = exports[file.mime]
            got = await self._call("GET", f"drive/v3/files/{file.id}/export", {"mimeType": kind})
            name = file.name if file.name.endswith(suffix) else file.name + suffix
            return got.content, kind, name
        if file.size is not None and file.size > MAX_READ:
            raise DriveError(f"{file.name} is too large ({file.size // 1_048_576} MiB)")
        got = await self._call("GET", f"drive/v3/files/{file.id}", {"alt": "media"})
        return got.content, file.mime or "application/octet-stream", file.name

    async def upload(
        self, name: str, data: bytes, content_type: str, subfolder: str = "", into: str = ""
    ) -> DriveFile:
        """Into Orion's folder (or one of its subfolders), created the first time; with full
        access, into any of his folders (`into`, its id)."""
        if into:
            parent = into
        else:
            parent = await self._folder(self.folder_name, None)
            if subfolder:
                parent = await self._folder(subfolder, parent)
        metadata = json.dumps({"name": name, "parents": [parent]}).encode()
        boundary = f"orion{new_id()}"
        body = b"".join(
            [
                f"--{boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{boundary}\r\ncontent-type: {content_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        made = await self._call(
            "POST",
            "upload/drive/v3/files",
            {"uploadType": "multipart", "fields": FIELDS},
            body,
            f"multipart/related; boundary={boundary}",
        )
        return DriveFile.of(made.json())

    # -- his own files (full access) -------------------------------------------------------

    async def change(
        self,
        file: DriveFile,
        grant: str | None,
        name: str = "",
        to: str = "",
        trash: bool = False,
    ) -> DriveFile:
        """Renamed, moved (out of every folder it was in, into `to`) or put in the trash."""
        params = {"fields": FIELDS}
        if to:
            params["addParents"] = to
            if file.parents:
                params["removeParents"] = ",".join(file.parents)
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if trash:
            body["trashed"] = True
        made = await self._call(
            "PATCH",
            f"drive/v3/files/{file.id}",
            params,
            json.dumps(body).encode(),
            "application/json",
            grant,
        )
        return DriveFile.of(made.json())

    async def replace(
        self, file: DriveFile, data: bytes, content_type: str, grant: str | None
    ) -> DriveFile:
        """A new version of its content; Drive keeps the previous ones."""
        made = await self._call(
            "PATCH",
            f"upload/drive/v3/files/{file.id}",
            {"uploadType": "media", "fields": FIELDS},
            data,
            content_type,
            grant,
        )
        return DriveFile.of(made.json())

    async def make_folder(self, name: str, parent: str) -> DriveFile:
        metadata = {"name": name, "mimeType": FOLDER, "parents": [parent or "root"]}
        made = await self._call(
            "POST",
            "drive/v3/files",
            {"fields": FIELDS},
            json.dumps(metadata).encode(),
            "application/json",
        )
        return DriveFile.of(made.json())

    async def _folder(self, name: str, parent: str | None) -> str:
        """A folder Orion made, found by the mark it left on it (not by its name alone: Orion
        may write only in what it created)."""
        key = f"{parent or ''}/{name}"
        if key in self._folders:
            return self._folders[key]
        mark = "root" if parent is None else "folder"
        q = (
            f"mimeType = '{FOLDER}' and trashed = false and name = {_quoted(name)} and "
            f"appProperties has {{ key='orion' and value='{mark}' }}"
        )
        if parent is not None:
            q += f" and {_quoted(parent)} in parents"
        found = await self._list(q, 1)
        if found:
            self._folders[key] = found[0].id
            return found[0].id
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER,
            "appProperties": {"orion": mark},
        }
        if parent is not None:
            metadata["parents"] = [parent]
        made = await self._call(
            "POST",
            "drive/v3/files",
            {"fields": "id"},
            json.dumps(metadata).encode(),
            "application/json",
        )
        self._folders[key] = str(made.json()["id"])
        return self._folders[key]
