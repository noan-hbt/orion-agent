"""`orion hand`: the program on the owner's machine.

It dials out to Orion's server, so nothing on the machine is reachable from outside, and does
only what its own configuration allows, whatever the server asks:

- `notify`: a desktop notification;
- `open_url`: open an http(s) address in the default browser;
- `find_files`: file names matching a query, inside the allowed folders only;
- `read_file`: the text of one file inside the allowed folders (size capped);
- `get_file`: one file inside the allowed folders, byte for byte, for Orion to send to the
  owner (10 MiB at most).

Paths are resolved (symlinks included) and must stay inside an allowed folder. The
configuration (`~/.config/orion/hand.json`, mode 0600) holds the server, the device's secret,
the folders and the actions; edit it to give more or less.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import structlog
import websockets

log = structlog.get_logger(__name__)

CONFIG = Path.home() / ".config" / "orion" / "hand.json"
MAX_READ = 200_000
MAX_FILE = 10 * 1024 * 1024
"""What one iMessage carries."""
MAX_FOUND = 50
MAX_VISITED = 20_000
"""Entries looked at per search: a shared home folder must not keep the call waiting."""
SKIPPED = frozenset({"node_modules", "__pycache__", "Library"})
DEFAULT_ACTIONS = ("notify", "find_files", "read_file", "get_file")


@dataclass
class HandConfig:
    server: str
    device: str
    secret: str
    roots: list[str] = field(default_factory=list[str])
    actions: list[str] = field(default_factory=lambda: list(DEFAULT_ACTIONS))

    @classmethod
    def load(cls, path: Path = CONFIG) -> HandConfig:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path = CONFIG) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created 0600: the device's secret is never readable by another user, even briefly.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(json.dumps(self.__dict__, indent=2))
        path.chmod(0o600)

    def socket_url(self) -> str:
        parts = urlsplit(self.server)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/hands/connect"


class Refused(ValueError):
    """Not allowed on this machine."""


class Hand:
    def __init__(self, config: HandConfig) -> None:
        self.config = config
        self._roots = [Path(r).expanduser().resolve() for r in config.roots]

    def _inside(self, path: str) -> Path:
        target = Path(path).expanduser().resolve()
        if not any(target == root or root in target.parents for root in self._roots):
            raise Refused(f"{path} is outside the allowed folders")
        return target

    async def do(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action not in self.config.actions:
            raise Refused(f"{action} is not allowed on this machine")
        if action == "notify":
            return self.notify(str(args.get("text", ""))[:300])
        if action == "open_url":
            return self.open_url(str(args.get("url", "")))
        if action == "find_files":
            return await asyncio.to_thread(self.find_files, str(args.get("query", "")))
        if action == "read_file":
            return await asyncio.to_thread(self.read_file, str(args.get("path", "")))
        if action == "get_file":
            return await asyncio.to_thread(self.get_file, str(args.get("path", "")))
        raise Refused(f"unknown action {action}")

    def notify(self, text: str) -> dict[str, Any]:
        system = platform.system()
        if system == "Darwin":
            script = f'display notification {json.dumps(text)} with title "Orion"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "Orion", text], check=False, timeout=10)
        else:
            print(f"[Orion] {text}")
        return {"content": "notified"}

    def open_url(self, url: str) -> dict[str, Any]:
        if urlsplit(url).scheme not in ("http", "https"):
            raise Refused("only web addresses")
        system = platform.system()
        if system == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
        else:
            opener = "open" if system == "Darwin" else "xdg-open"
            subprocess.run([opener, url], check=False, timeout=10)
        return {"content": f"opened {url}"}

    def find_files(self, query: str) -> dict[str, Any]:
        words = [w.casefold() for w in query.split() if w]
        if not words:
            raise Refused("empty query")
        found: list[str] = []
        visited = 0
        for root in self._roots:
            # Hidden and bulky folders are pruned rather than walked; links are not followed.
            for folder, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIPPED)
                for name in sorted(files):
                    visited += 1
                    if visited > MAX_VISITED:
                        return {"content": "\n".join(found), "truncated": True}
                    if not name.startswith(".") and all(w in name.casefold() for w in words):
                        found.append(os.path.join(folder, name))
                        if len(found) >= MAX_FOUND:
                            return {"content": "\n".join(found), "truncated": True}
                visited += len(dirs)
        return {"content": "\n".join(found)}

    def read_file(self, path: str) -> dict[str, Any]:
        target = self._inside(path)
        if not target.is_file():
            raise Refused(f"{path} is not a file")
        with target.open("rb") as file:  # never the whole of a huge file, only what is read
            data = file.read(MAX_READ)
        if b"\x00" in data[:4096]:
            raise Refused(f"{path} is not text")
        return {"content": data.decode("utf-8", errors="replace"), "path": str(target)}

    def get_file(self, path: str) -> dict[str, Any]:
        target = self._inside(path)
        if not target.is_file():
            raise Refused(f"{path} is not a file")
        with target.open("rb") as file:
            data = file.read(MAX_FILE + 1)  # the size as read, not as it was a moment before
        if len(data) > MAX_FILE:
            raise Refused(f"{path} is too large to send (10 MiB at most)")
        return {
            "content": f"{target.name}, {len(data)} bytes",
            "name": target.name,
            "data": base64.b64encode(data).decode(),
        }

    # -- the connection -----------------------------------------------------------------------

    async def session(self) -> None:
        """One connection, until it drops."""
        async with websockets.connect(
            self.config.socket_url(),
            additional_headers={"Authorization": f"Bearer {self.config.secret}"},
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "actions": self.config.actions,
                        "roots": [str(r) for r in self._roots],
                    }
                )
            )
            log.info("hand.connected", server=self.config.server, device=self.config.device)
            async for raw in socket:
                try:
                    loaded: Any = json.loads(raw)
                except ValueError:
                    log.warning("hand.unreadable_message", size=len(raw))
                    continue
                if not isinstance(loaded, dict):
                    continue
                message = cast(dict[str, Any], loaded)
                if message.get("type") != "call":
                    continue
                call_id = str(message.get("id"))
                try:
                    result = await self.do(
                        str(message.get("action")), dict(message.get("args", {}))
                    )
                    reply: dict[str, Any] = {"type": "result", "id": call_id, "ok": True, **result}
                except (Refused, OSError, ValueError, subprocess.SubprocessError) as exc:
                    # ValueError: a path with a NUL byte, arguments of the wrong shape…
                    log.info("hand.refused", action=message.get("action"), reason=str(exc))
                    reply = {"type": "result", "id": call_id, "ok": False, "error": str(exc)}
                await socket.send(json.dumps(reply))

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.session()
                delay = 1.0
            except websockets.InvalidStatus as exc:
                if exc.response.status_code == 403:
                    log.warning("hand.revoked", device=self.config.device)
                    raise Refused("this machine is no longer paired: pair it again") from exc
                log.warning("hand.disconnected", error=repr(exc), retry_in=delay)
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("hand.disconnected", error=repr(exc), retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def pair(server: str, code: str, roots: list[str], actions: list[str]) -> HandConfig:
    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.post(server.rstrip("/") + "/hands/pair", json={"code": code})
    if response.status_code != 200:
        raise Refused(f"the server refused the code ({response.status_code})")
    data = response.json()
    return HandConfig(server.rstrip("/"), str(data["device"]), str(data["secret"]), roots, actions)
