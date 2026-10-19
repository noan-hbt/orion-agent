"""iMessage channel, through an Inkbox managed line.

Inbound messages arrive by webhook or by polling; both go through `receive`, which deduplicates
by message id and journals the message before anything else happens (a webhook is acknowledged
only once that is done). Only the configured owner number is obeyed; anyone else is journaled
as third-party content and gets no answer.

Owner messages go to the unified inbox (which coalesces bursts and runs the turn); this channel
shows a read receipt and a typing indicator while Orion thinks, and sends the reply through the
outbox, split into bubbles at paragraph breaks.

Approvals are asked here too: Orion sends the exact action and waits for a tapback (👍/👎) or a
one-word answer. No answer before the timeout means no.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import unicodedata
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from orion.adaptation.signals import FEEDBACK_REACTION, reaction_event
from orion.channels.inbox import Inbound as InboxItem
from orion.channels.inbox import OwnerInbox
from orion.channels.inkbox import (
    IMessage,
    InkboxClient,
    InkboxError,
    MessageReaction,
    ReactionEvent,
    WebhookEvent,
)
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.outbox import Outbox, SendError
from orion.guardian.policy import ActionProposal, Approval

log = structlog.get_logger(__name__)

CHANNEL = "imessage"
INBOUND = "imessage.inbound"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"

_YES = {"oui", "ouais", "ok", "okay", "vas-y", "vas y", "go", "yes", "yep", "valide", "👍"}
_NO = {"non", "nan", "no", "nope", "stop", "annule", "refuse", "👎"}
REACTION_YES = {"like", "love", "emphasize"}
REACTION_NO = {"dislike"}


def normalize_number(number: str) -> str:
    return re.sub(r"[^\d+]", "", number)


def answer_word(text: str) -> bool | None:
    """'oui' → True, 'non' → False, anything else → None (not an answer)."""
    word = unicodedata.normalize("NFKC", text).strip().lower().rstrip(" .!")
    if word in _YES:
        return True
    if word in _NO:
        return False
    return None


def split_bubbles(text: str, max_bubbles: int = 4) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(parts) > max_bubbles:
        parts = [*parts[: max_bubbles - 1], "\n\n".join(parts[max_bubbles - 1 :])]
    return parts


class ApprovalDesk(Protocol):
    """The guardian process, which writes and records approval requests itself (D28)."""

    async def request_approval(
        self, action: str, params: dict[str, Any], reason: str, conversation_id: str
    ) -> tuple[str, str]: ...


@dataclass(frozen=True)
class IMessageSettings:
    owner_number: str
    identity_id: str | None = None
    approval_timeout_seconds: float = 900.0
    typing_refresh_seconds: float = 20.0
    max_bubbles: int = 4
    reaction_poll_every: int = 15
    """In poll mode, read tapbacks on Orion's recent messages every N polls."""


@dataclass(frozen=True)
class Inbound:
    message_id: str
    conversation_id: str
    text: str


@dataclass
class ChannelState:
    """Which inbound messages were seen and handled, and the owner's conversation. Rebuilt from
    the journal, so a restart neither loses nor repeats a message."""

    name: str = "imessage-channel"
    seen: set[str] = field(default_factory=set[str])
    reactions: set[str] = field(default_factory=set[str])
    handled: set[str] = field(default_factory=set[str])
    owner_inbound: dict[str, Inbound] = field(default_factory=dict[str, Inbound])
    owner_conversation: str | None = None
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.seen.clear()
        self.reactions.clear()
        self.handled.clear()
        self.owner_inbound.clear()
        self.owner_conversation = None
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == INBOUND:
            message_id = str(payload["message_id"])
            self.seen.add(message_id)
            if payload.get("is_owner"):
                conversation = str(payload["conversation_id"])
                self.owner_conversation = conversation
                self.owner_inbound[message_id] = Inbound(
                    message_id, conversation, str(payload.get("text") or "")
                )
        elif event.kind == FEEDBACK_REACTION:
            self.reactions.add(str(payload.get("reaction_id")))
        elif event.kind in ("message.received", APPROVAL_DECIDED):
            self.handled.update(str(ref) for ref in payload.get("refs", []))

    def unhandled(self) -> list[Inbound]:
        return [m for i, m in self.owner_inbound.items() if i not in self.handled]


class IMessageChannel:
    """Inbound handling, outbound transport, inbox outlet and approver for iMessage."""

    name = CHANNEL

    def __init__(
        self,
        client: InkboxClient,
        journal: EventStore,
        inbox: OwnerInbox,
        settings: IMessageSettings,
        desk: ApprovalDesk | None = None,
    ) -> None:
        self._client = client
        self._desk = desk
        self._journal = journal
        self._inbox = inbox
        self._settings = settings
        self._owner = normalize_number(settings.owner_number)
        self.outbox = Outbox(journal, {CHANNEL: self})
        self.state = ChannelState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._approvals: dict[str, asyncio.Future[tuple[bool, str, list[str]]]] = {}
        self._polls = 0
        inbox.register(self)

    async def start(self) -> None:
        """Catch up, finish sends interrupted by a crash, requeue unanswered owner messages."""
        await self._runner.catch_up()
        await self.outbox.resume()
        for message in self.state.unhandled():
            log.info("imessage.requeue", message_id=message.message_id)
            self._inbox.submit(self._inbound(message))

    @staticmethod
    def _inbound(message: Inbound) -> InboxItem:
        return InboxItem(
            CHANNEL, (message.message_id,), message.text, context=message.conversation_id
        )

    # -- outbound: transport for the outbox, outlet for the inbox ------------------------------

    async def transmit(self, to: str, text: str, idempotency_key: str) -> str:
        try:
            sent = await self._client.send(to, text, idempotency_key, self._settings.identity_id)
        except InkboxError as exc:
            raise SendError(str(exc), retryable=exc.retryable or exc.status is None) from exc
        return sent.id

    async def available(self) -> bool:
        await self._runner.catch_up()
        return self.state.owner_conversation is not None

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        await self._send_to_owner(text, correlation_id)

    @asynccontextmanager
    async def working(self, item: InboxItem) -> AsyncGenerator[None]:
        """Read receipt, then a typing indicator kept alive until the answer is ready."""
        conversation = item.context or self.state.owner_conversation
        if conversation is None:
            yield
            return
        await self._best_effort(self._client.mark_read(conversation), "mark_read")
        typing = asyncio.create_task(self._keep_typing(conversation))
        try:
            yield
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing

    async def _send_to_owner(self, text: str, correlation_id: str | None = None) -> list[str]:
        conversation = self.state.owner_conversation
        if conversation is None:
            raise SendError("the owner has not written to Orion on iMessage yet", retryable=False)
        return [
            await self.outbox.send(CHANNEL, conversation, bubble, correlation_id)
            for bubble in split_bubbles(text, self._settings.max_bubbles)
        ]

    # -- inbound ------------------------------------------------------------------------------

    async def handle_webhook(self, event: WebhookEvent) -> None:
        if event.event_type == "imessage.received" and event.data.message is not None:
            await self.receive(event.data.message)
        elif event.event_type == "imessage.reaction_received" and event.data.reaction is not None:
            await self.receive_reaction(event.data.reaction)
        else:
            log.info("imessage.webhook_ignored", event_type=event.event_type, event_id=event.id)

    async def receive(self, message: IMessage) -> None:
        if message.direction != "inbound" or message.is_group:
            return
        await self._runner.catch_up()
        if message.id in self.state.seen:
            return
        sender = message.sender or ""
        is_owner = bool(self._owner) and normalize_number(sender) == self._owner
        text = message.content or ""
        await self._journal.append(
            [
                NewEvent(
                    kind=INBOUND,
                    payload={
                        "message_id": message.id,
                        "conversation_id": message.conversation_id,
                        "sender": sender,
                        "text": text,
                        "is_owner": is_owner,
                        "service": message.service,
                        "created_at": message.created_at,
                    },
                    provenance=Provenance(
                        source=f"imessage:{message.conversation_id}",
                        actor="owner:main" if is_owner else f"contact:{sender}",
                    ),
                    trust=Trust.OWNER if is_owner else Trust.THIRD_PARTY,
                    correlation_id=new_id(),
                )
            ]
        )
        await self._runner.catch_up()
        if not is_owner:
            log.info("imessage.third_party_ignored", conversation=message.conversation_id)
            return
        inbound = Inbound(message.id, message.conversation_id, text)
        if self._approvals and self._resolve_by_word(inbound):
            return
        self._inbox.submit(self._inbound(inbound))

    async def receive_reaction(self, reaction: ReactionEvent) -> None:
        if reaction.direction != "inbound":
            return
        if normalize_number(reaction.remote_number or "") != self._owner:
            return
        if reaction.target_message_id in self._approvals:
            self._resolve_by_reaction(reaction.target_message_id, reaction.reaction)
            return
        await self._feedback(
            reaction.target_message_id,
            reaction.id,
            reaction.reaction,
            reaction.custom_emoji,
            reaction.conversation_id,
        )

    async def _feedback(
        self,
        target: str,
        reaction_id: str,
        reaction: str,
        emoji: str | None,
        conversation: str,
    ) -> None:
        """A tapback on one of Orion's messages: a signal about how Orion is doing."""
        await self._runner.catch_up()
        if reaction_id in self.state.reactions:
            return
        await self._journal.append(
            [reaction_event(target, reaction, emoji, reaction_id, f"imessage:{conversation}")]
        )
        await self._runner.catch_up()

    async def _poll_reactions(self) -> None:
        conversation = self.state.owner_conversation
        if conversation is None:
            return
        for message in await self._client.recent(conversation, self._settings.identity_id):
            if message.direction != "outbound" or message.id in self._approvals:
                continue
            for reaction in message.reactions or []:
                if self._is_owner_reaction(reaction):
                    await self._feedback(
                        message.id,
                        reaction.id,
                        reaction.reaction,
                        reaction.custom_emoji,
                        conversation,
                    )

    def _is_owner_reaction(self, reaction: MessageReaction) -> bool:
        if reaction.direction != "inbound":
            return False
        return not reaction.remote_number or normalize_number(reaction.remote_number) == self._owner

    async def poll_once(self) -> None:
        for message in sorted(
            await self._client.unread(self._settings.identity_id), key=lambda m: m.created_at
        ):
            await self.receive(message)
        for message_id in list(self._approvals):
            message = await self._client.message(message_id, self._settings.identity_id)
            for reaction in message.reactions or []:
                if reaction.direction == "inbound":
                    self._resolve_by_reaction(message_id, reaction.reaction)
        self._polls += 1
        if self._polls % self._settings.reaction_poll_every == 0:
            await self._poll_reactions()

    async def poll_forever(self, interval: float) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(interval)

    async def _keep_typing(self, conversation: str) -> None:
        while True:
            await self._best_effort(self._client.typing(conversation), "typing")
            await asyncio.sleep(self._settings.typing_refresh_seconds)

    async def _best_effort(self, call: Awaitable[None], what: str) -> None:
        """Cosmetic calls (typing, read receipts) must never break a turn; failures are logged."""
        try:
            await call
        except InkboxError:
            log.warning("imessage.cosmetic_failed", call=what, exc_info=True)

    # -- approvals ----------------------------------------------------------------------------

    async def approve(self, proposal: ActionProposal) -> Approval:
        conversation = self.state.owner_conversation
        if conversation is None:
            log.warning("imessage.approval_unreachable", tool=proposal.tool.name)
            return Approval(False)
        approval_id = f"a_{new_id()}"
        arguments = proposal.arguments.model_dump(mode="json")
        grant: str | None = None
        await self._approval_event(
            APPROVAL_REQUESTED,
            {"approval_id": approval_id, "tool": proposal.tool.name, "arguments": arguments},
        )
        if self._desk is not None:
            # The guardian writes the question itself and will check the answer itself.
            grant, message_id = await self._desk.request_approval(
                proposal.tool.name, arguments, proposal.ask_reason, conversation
            )
        else:
            [message_id, *_] = await self._send_to_owner(
                f"Avant que je le fasse : {proposal.describe()}\n"
                + (f"Je demande parce que {proposal.ask_reason}.\n" if proposal.ask_reason else "")
                + "👍 pour oui, 👎 pour non (ou réponds oui / non)."
            )
        future: asyncio.Future[tuple[bool, str, list[str]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._approvals[message_id] = future
        try:
            approved, how, refs = await asyncio.wait_for(
                future, self._settings.approval_timeout_seconds
            )
        except TimeoutError:
            approved, how, refs = False, "timeout", []
        finally:
            self._approvals.pop(message_id, None)
        await self._approval_event(
            APPROVAL_DECIDED,
            {
                "approval_id": approval_id,
                "approved": approved,
                "how": how,
                "refs": refs,
                **({"grant": grant} if grant else {}),
            },
        )
        return Approval(approved, grant if approved else None)

    def _resolve_by_word(self, inbound: Inbound) -> bool:
        answer = answer_word(inbound.text)
        if answer is None:
            return False
        message_id = next(reversed(self._approvals))
        future = self._approvals[message_id]
        if not future.done():
            future.set_result((answer, "word", [inbound.message_id]))
        return True

    def _resolve_by_reaction(self, message_id: str, reaction: str) -> None:
        future = self._approvals.get(message_id)
        if future is None or future.done():
            return
        if reaction in REACTION_YES:
            future.set_result((True, f"reaction:{reaction}", []))
        elif reaction in REACTION_NO:
            future.set_result((False, f"reaction:{reaction}", []))

    async def _approval_event(self, kind: str, payload: dict[str, object]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="orion:guardian", actor="orion:guardian"),
                    trust=Trust.OWNER if kind == APPROVAL_DECIDED else Trust.SYSTEM,
                    correlation_id=new_id(),
                )
            ]
        )
        await self._runner.catch_up()
