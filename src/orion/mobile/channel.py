"""The app as a channel (D125, D88): what the owner writes from his phone, and what Orion sends.

It does what the Telegram channel does, with Orion as its own provider:

- a message from the app is journaled first (`mobile.inbound`, the owner's own words, with the
  files he attached kept under the data directory), then handed to the inbox; a restart
  requeues one never answered;
- what Orion sends goes through the channel's outbox and becomes `mobile.outbound`: the app
  reads the conversation from the journal, and gets each new message live while it is open;
  closed, it gets a notification (Web Push, encrypted for the phone);
- while Orion thinks, the app shows it;
- an action is put to the owner in the app; his answer counts only signed by a paired passkey,
  after Face ID, for this approval and this answer; no answer before the timeout means no;
- his reactions on Orion's messages are signals for adaptation.

    mobile.inbound  mobile.outbound  mobile.subscribed  mobile.unsubscribed
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import re
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from orion.adaptation.signals import FEEDBACK_REACTION, reaction_event
from orion.channels.imessage import APPROVAL_DECIDED, APPROVAL_REQUESTED
from orion.channels.inbox import Attachment, OwnerInbox
from orion.channels.inbox import Inbound as InboxItem
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.private import write_private
from orion.core.projections import ProjectionRunner
from orion.guardian.outbox import Outbox, SendError
from orion.guardian.policy import ActionProposal, Approval
from orion.mobile.devices import Devices
from orion.mobile.push import PushError, Subscription, allowed_endpoint

log = structlog.get_logger(__name__)

CHANNEL = "app"
INBOUND = "mobile.inbound"
OUTBOUND = "mobile.outbound"
SUBSCRIBED = "mobile.subscribed"
UNSUBSCRIBED = "mobile.unsubscribed"
MAX_FILE_BYTES = 25 * 1024 * 1024
PUSH_PREVIEW = 180
REACTIONS = {"👍": "like", "❤️": "love", "👎": "dislike", "😂": "laugh", "❓": "question"}


@dataclass(frozen=True)
class Kept:
    """A file in the conversation, kept under the channel's folder."""

    path: str
    """Relative to the channel's folder."""
    name: str
    content_type: str
    size: int


@dataclass(frozen=True)
class Said:
    """One message of the conversation, as the app shows it."""

    seq: int
    ref: str
    who: str
    """`owner` or `orion`."""
    text: str
    at: str
    files: tuple[Kept, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ref": self.ref,
            "who": self.who,
            "text": self.text,
            "at": self.at,
            "files": [
                {"ref": f"{self.ref}/{i}", "name": f.name, "type": f.content_type, "size": f.size}
                for i, f in enumerate(self.files)
            ],
        }


def _kept(raw: object) -> tuple[Kept, ...]:
    items = cast(list[dict[str, Any]], raw or [])
    return tuple(
        Kept(str(f["path"]), str(f["name"]), str(f["content_type"]), int(f["size"])) for f in items
    )


@dataclass
class MobileState:
    name: str = "mobile-channel"
    said: list[Said] = field(default_factory=list[Said])
    owner_messages: dict[str, Said] = field(default_factory=dict[str, Said])
    handled: set[str] = field(default_factory=set[str])
    sent: set[str] = field(default_factory=set[str])
    """Outbox keys already turned into messages (a resumed send is not shown twice)."""
    subscriptions: dict[str, tuple[str, Subscription]] = field(
        default_factory=dict[str, tuple[str, Subscription]]
    )
    """endpoint → (device, subscription)."""
    reactions: set[str] = field(default_factory=set[str])
    reacted: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """ref → the emoji he put on that message of Orion's, from the app."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.said.clear()
        self.owner_messages.clear()
        self.handled.clear()
        self.sent.clear()
        self.subscriptions.clear()
        self.reactions.clear()
        self.reacted.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind in (INBOUND, OUTBOUND):
            said = Said(
                event.seq,
                str(payload["ref"]),
                "owner" if event.kind == INBOUND else "orion",
                str(payload.get("text") or ""),
                str(payload["at"]),
                _kept(payload.get("files")),
            )
            self.said.append(said)
            if event.kind == INBOUND:
                self.owner_messages[said.ref] = said
            else:
                self.sent.add(said.ref)
        elif event.kind == SUBSCRIBED:
            subscription = Subscription(
                str(payload["endpoint"]), str(payload["p256dh"]), str(payload["auth"])
            )
            self.subscriptions[subscription.endpoint] = (str(payload["device"]), subscription)
        elif event.kind == UNSUBSCRIBED:
            self.subscriptions.pop(str(payload["endpoint"]), None)
        elif event.kind == FEEDBACK_REACTION:
            reaction_id = str(payload.get("reaction_id"))
            self.reactions.add(reaction_id)
            if reaction_id.startswith("app:"):
                self.reacted.setdefault(str(payload["remote_id"]), []).append(
                    str(payload.get("emoji") or "")
                )
        elif event.kind in ("message.received", APPROVAL_DECIDED):
            self.handled.update(str(ref) for ref in payload.get("refs", []))

    def unhandled(self) -> list[Said]:
        return [m for ref, m in self.owner_messages.items() if ref not in self.handled]

    def find(self, ref: str) -> Said | None:
        return next((s for s in reversed(self.said) if s.ref == ref), None)

    def public(self, said: Said) -> dict[str, Any]:
        """What the app shows of a message: the message, and his reactions on it."""
        return {**said.public(), "reactions": self.reacted.get(said.ref, [])}


class Sender(Protocol):
    """What sends a notification to one subscription (`orion.mobile.push.Pusher`)."""

    async def send(self, subscription: Subscription, message: dict[str, object]) -> None: ...


class Hub:
    """The app's open connections: each gets every live event (a message, « en train
    d'écrire », a question), in order."""

    def __init__(self) -> None:
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def listening(self) -> bool:
        return bool(self._listeners)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in self._listeners:
            queue.put_nowait(event)

    @asynccontextmanager
    async def listen(self) -> AsyncGenerator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._listeners.add(queue)
        try:
            yield queue
        finally:
            self._listeners.discard(queue)


@dataclass
class Asked:
    approval_id: str
    question: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    expires_at: datetime
    future: asyncio.Future[tuple[bool, str]]


def _safe_name(name: str, content_type: str) -> str:
    plain = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .") or "fichier"
    if "." not in plain:
        plain += mimetypes.guess_extension(content_type) or ""
    return plain[:120]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MobileChannel:
    """Inbound handling, outbound transport, inbox outlet and approver for the app."""

    name = CHANNEL

    def __init__(
        self,
        journal: EventStore,
        inbox: OwnerInbox,
        devices: Devices,
        folder: Path,
        pusher: Sender | None = None,
        approval_timeout_seconds: float = 900.0,
    ) -> None:
        self._journal = journal
        self._inbox = inbox
        self.devices = devices
        self.folder = folder
        self._pusher = pusher
        self._timeout = approval_timeout_seconds
        self.outbox = Outbox(journal, {CHANNEL: self})
        self.state = MobileState()
        self._runner = ProjectionRunner(journal, [self.state])
        self.hub = Hub()
        self.typing = False
        """Whether Orion is working on his message right now (an app that opens shows it)."""
        self.asked: dict[str, Asked] = {}
        inbox.register(self)

    async def start(self) -> None:
        """Catch up, finish sends interrupted by a crash, requeue unanswered owner messages."""
        await self._runner.catch_up()
        await self.outbox.resume()
        for said in self.state.unhandled():
            log.info("mobile.requeue", ref=said.ref)
            self._inbox.submit(self._inbound(said))

    async def sync(self) -> None:
        await self._runner.catch_up()

    def path(self, kept: Kept) -> Path:
        return self.folder / kept.path

    # -- inbound ------------------------------------------------------------------------------

    async def receive(
        self, device: str, text: str, files: list[tuple[str, str, bytes]] | None = None
    ) -> Said:
        """A message the owner wrote in the app: journaled, then answered."""
        ref = f"m_{new_id()}"
        kept: list[Kept] = []
        for index, (name, content_type, data) in enumerate(files or []):
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"{name}: too large ({len(data)} bytes)")
            relative = f"in/{ref}/{index}-{_safe_name(name, content_type)}"
            target = self.folder / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            await asyncio.to_thread(write_private, target, data)
            kept.append(Kept(relative, Path(relative).name, content_type, len(data)))
        await self._append(
            INBOUND,
            {
                "ref": ref,
                "device": device,
                "text": text,
                "at": _now(),
                "files": [f.__dict__ for f in kept],
            },
            Trust.OWNER,
            Provenance(source=f"app:{device}", actor="owner:main"),
        )
        said = self.state.owner_messages[ref]
        self._inbox.submit(self._inbound(said))
        return said

    def _inbound(self, said: Said) -> InboxItem:
        return InboxItem(
            CHANNEL,
            (said.ref,),
            said.text,
            media=tuple(self._attachment(f) for f in said.files),
            sent_at=datetime.fromisoformat(said.at),
        )

    def _attachment(self, kept: Kept) -> Attachment:
        path = self.path(kept)

        async def fetch() -> bytes:
            return await asyncio.to_thread(path.read_bytes)

        return Attachment(f"app:{kept.path}", kept.content_type, kept.size, fetch)

    async def react(self, device: str, ref: str, emoji: str) -> bool:
        await self.sync()
        target = self.state.find(ref)
        if target is None or target.who != "orion":
            return False
        reaction_id = f"app:{ref}:{emoji}"
        if reaction_id in self.state.reactions:
            return True
        await self._journal.append(
            [
                reaction_event(
                    ref, REACTIONS.get(emoji, "custom"), emoji, reaction_id, f"app:{device}"
                )
            ]
        )
        await self.sync()
        return True

    async def subscribe(self, device: str, endpoint: str, p256dh: str, auth: str) -> None:
        if not allowed_endpoint(endpoint):
            raise ValueError("not a known push service")
        await self._append(
            SUBSCRIBED,
            {"device": device, "endpoint": endpoint, "p256dh": p256dh, "auth": auth},
            Trust.OWNER,
            Provenance(source=f"app:{device}", actor="owner:main"),
        )

    # -- outbound: transport for the outbox, outlet for the inbox ------------------------------

    async def transmit(self, to: str, text: str, idempotency_key: str) -> str:
        return await self._show(text, idempotency_key, ())

    async def transmit_file(self, to: str, text: str, path: str, idempotency_key: str) -> str:
        source = Path(path)
        name = _safe_name(source.name, mimetypes.guess_type(source.name)[0] or "")
        relative = f"out/{idempotency_key}/{name}"
        target = self.folder / relative
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, source, target)
            target.chmod(0o600)
        except OSError as exc:
            raise SendError(f"the file cannot be read: {exc}", retryable=False) from exc
        kept = Kept(relative, name, mimetypes.guess_type(name)[0] or "", target.stat().st_size)
        return await self._show(text, idempotency_key, (kept,))

    async def _show(self, text: str, key: str, files: tuple[Kept, ...]) -> str:
        """The message joins the conversation (once, whatever a resumed send does), goes to
        the open app, or else to the phone's notifications."""
        await self.sync()
        if key in self.state.sent:
            return key
        await self._append(
            OUTBOUND,
            {"ref": key, "text": text, "at": _now(), "files": [f.__dict__ for f in files]},
            Trust.SYSTEM,
            Provenance(source="orion:app", actor="orion:core"),
        )
        said = self.state.find(key)
        assert said is not None
        self.hub.publish({"type": "message", "message": self.state.public(said)})
        if not self.hub.listening:
            body = text or (f"📎 {files[0].name}" if files else "")
            await self._notify({"title": "Orion", "body": body[:PUSH_PREVIEW], "tag": key})
        return key

    async def _notify(self, message: dict[str, object]) -> None:
        if self._pusher is None:
            return
        await self.sync()
        for endpoint, (device, subscription) in list(self.state.subscriptions.items()):
            try:
                await self._pusher.send(subscription, message)
            except PushError as exc:
                log.warning("mobile.push_failed", device=device, error=str(exc))
                if exc.gone:
                    await self._append(
                        UNSUBSCRIBED,
                        {"endpoint": endpoint, "why": str(exc)},
                        Trust.SYSTEM,
                        Provenance(source="orion:app", actor="orion:core"),
                    )

    async def available(self) -> bool:
        return await self.devices.any_paired()

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        await self.outbox.send(CHANNEL, "owner", text.strip(), correlation_id)

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None:
        await self.outbox.send(CHANNEL, "owner", caption, correlation_id, media=str(path))

    @asynccontextmanager
    async def working(self, item: InboxItem) -> AsyncGenerator[None]:
        self.typing = True
        self.hub.publish({"type": "typing", "on": True})
        try:
            yield
        finally:
            self.typing = False
            self.hub.publish({"type": "typing", "on": False})

    # -- approvals ----------------------------------------------------------------------------

    async def approve(self, proposal: ActionProposal) -> Approval:
        if not await self.devices.any_paired():
            return Approval(False, reached=False)
        approval_id = f"a_{new_id()}"
        arguments = proposal.arguments.model_dump(mode="json")
        await self._approval_event(
            APPROVAL_REQUESTED,
            {"approval_id": approval_id, "tool": proposal.tool.name, "arguments": arguments},
        )
        question = f"Avant que je le fasse : {proposal.describe()}" + (
            f"\nJe demande parce que {proposal.ask_reason}." if proposal.ask_reason else ""
        )
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        expires = datetime.now(UTC).timestamp() + self._timeout
        asked = Asked(
            approval_id,
            question,
            proposal.tool.name,
            arguments,
            proposal.ask_reason,
            datetime.fromtimestamp(expires, UTC),
            future,
        )
        self.asked[approval_id] = asked
        self.hub.publish({"type": "approval", "approval": self.public(asked)})
        await self._notify(
            {
                "title": "Orion demande ton accord",
                "body": question[:PUSH_PREVIEW],
                "tag": approval_id,
            }
        )
        try:
            approved, how = await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            approved, how = False, "timeout"
        finally:
            self.asked.pop(approval_id, None)
            self.hub.publish({"type": "approval_closed", "id": approval_id})
        await self._approval_event(
            APPROVAL_DECIDED,
            {"approval_id": approval_id, "approved": approved, "how": how, "refs": []},
        )
        return Approval(approved, reached=True)

    @staticmethod
    def public(asked: Asked) -> dict[str, Any]:
        return {
            "id": asked.approval_id,
            "question": asked.question,
            "tool": asked.tool,
            "arguments": asked.arguments,
            "reason": asked.reason,
            "expires_at": asked.expires_at.isoformat(),
        }

    def refuse(self, approval_id: str, device: str) -> None:
        """No, from a signed-in app: refusing needs no Face ID, it is what silence does too."""
        asked = self.asked.get(approval_id)
        if asked is None or asked.future.done():
            raise LookupError("no such question pending")
        asked.future.set_result((False, f"session:{device}"))

    async def answer(
        self, approval_id: str, yes: bool, ceremony: str, credential: dict[str, Any]
    ) -> str:
        """The owner's answer, signed for this approval and this answer, by a paired passkey:
        the device that signed (`DeviceError` otherwise)."""
        asked = self.asked.get(approval_id)
        if asked is None or asked.future.done():
            raise LookupError("no such question pending")
        device = await self.devices.verify(
            ceremony, f"approval:{approval_id}:{'yes' if yes else 'no'}", credential
        )
        if not asked.future.done():
            asked.future.set_result((yes, f"passkey:{device}"))
        return device

    async def _approval_event(self, kind: str, payload: dict[str, Any]) -> None:
        await self._append(
            kind, payload, Trust.SYSTEM, Provenance(source="app", actor="orion:core")
        )

    async def _append(
        self, kind: str, payload: dict[str, Any], trust: Trust, provenance: Provenance
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=new_id(),
                )
            ]
        )
        await self.sync()

    async def close(self) -> None:
        for asked in list(self.asked.values()):
            with contextlib.suppress(asyncio.InvalidStateError):
                asked.future.set_result((False, "closed"))
