"""The owner's paired devices (D125): a one-time code, then a passkey, then sessions.

`orion app pair <device>` gives a code (ten minutes, once). The app trades it for a passkey made
on the phone, with user verification required (Face ID, or the phone's code): the public key is
journaled, never anything that could sign. From then on, a passkey signature opens a session (a
cookie signed with a key only Orion holds) and says yes or no to an action. A device is cut off
with `orion app revoke <device>`.

Each signature is checked here: the challenge Orion drew, the origin and domain of the app, user
verification, and a signature counter that must not go back (a cloned key shows up).

    mobile.code.created  mobile.paired  mobile.signed_in  mobile.revoked
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.private import write_private
from orion.core.projections import ProjectionRunner
from orion.mobile.push import b64url, unb64url

CODE_CREATED = "mobile.code.created"
PAIRED = "mobile.paired"
SIGNED_IN = "mobile.signed_in"
REVOKED = "mobile.revoked"
CODE_TTL = timedelta(minutes=10)
CEREMONY_SECONDS = 300.0
"""How long the phone has to answer a challenge (Face ID, then the signature)."""


class DeviceError(ValueError):
    """A pairing or a signature that does not hold: refused, and said why in the log."""


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


@dataclass
class Device:
    name: str
    credential_id: str
    public_key: str
    sign_count: int


@dataclass
class DeviceState:
    name: str = "mobile-devices"
    codes: dict[str, tuple[str, datetime]] = field(default_factory=dict[str, tuple[str, datetime]])
    """code hash → (device name, expiry), while unused."""
    devices: dict[str, Device] = field(default_factory=dict[str, Device])
    """by device name."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.codes.clear()
        self.devices.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == CODE_CREATED:
            expires = datetime.fromisoformat(str(payload["expires_at"]))
            self.codes[str(payload["code_hash"])] = (str(payload["device"]), expires)
        elif event.kind == PAIRED:
            self.codes.pop(str(payload["code_hash"]), None)
            name = str(payload["device"])
            self.devices[name] = Device(
                name,
                str(payload["credential_id"]),
                str(payload["public_key"]),
                int(payload["sign_count"]),
            )
        elif event.kind == SIGNED_IN:
            device = self.devices.get(str(payload["device"]))
            if device is not None:
                device.sign_count = max(device.sign_count, int(payload["sign_count"]))
        elif event.kind == REVOKED:
            self.devices.pop(str(payload["device"]), None)

    def by_credential(self, credential_id: str) -> Device | None:
        return next((d for d in self.devices.values() if d.credential_id == credential_id), None)


@dataclass(frozen=True)
class Ceremony:
    """A challenge Orion drew, for one purpose: pairing with a code, opening a session, or
    answering one approval with one answer."""

    challenge: bytes
    purpose: str
    expires: float
    code_hash: str = ""
    device: str = ""


class Devices:
    def __init__(
        self,
        journal: EventStore,
        public_url: str,
        session_key: bytes,
        session_days: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parts = urlsplit(public_url)
        if not parts.hostname:
            raise DeviceError(f"not an address: {public_url!r}")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.rp_id = parts.hostname
        self._journal = journal
        self._key = session_key
        self._session_seconds = session_days * 86400
        self.clock = clock
        self.state = DeviceState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._ceremonies: dict[str, Ceremony] = {}

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def any_paired(self) -> bool:
        await self.sync()
        return bool(self.state.devices)

    # -- the code -----------------------------------------------------------------------------

    async def new_code(self, device: str) -> str:
        """Eight characters, easy to type on a phone, without look-alikes (0/O, 1/I)."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        await self._append(
            CODE_CREATED,
            {
                "code_hash": _hash(code),
                "device": device,
                "expires_at": (self.clock() + CODE_TTL).isoformat(),
            },
        )
        return code

    # -- pairing ------------------------------------------------------------------------------

    async def pairing_options(self, code: str) -> tuple[str, str]:
        """(ceremony id, the options to create a passkey, as JSON) for a valid code."""
        await self.sync()
        found = self.state.codes.get(_hash(code))
        if found is None or self.clock() >= found[1]:
            raise DeviceError("unknown or expired code")
        device = found[0]
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="Orion",
            user_name=device,
            user_id=device.encode(),
            user_display_name=f"Orion sur {device}",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=unb64url(d.credential_id))
                for d in self.state.devices.values()
            ],
        )
        ceremony = self._open(Ceremony(options.challenge, "pair", 0, _hash(code), device))
        return ceremony, options_to_json(options)

    async def pair(self, ceremony_id: str, credential: dict[str, Any]) -> str:
        """The device's name, once its new passkey is checked and kept."""
        ceremony = self._take(ceremony_id, "pair")
        await self.sync()
        found = self.state.codes.get(ceremony.code_hash)
        if found is None or self.clock() >= found[1]:
            raise DeviceError("the code was used or expired meanwhile")
        try:
            verified = verify_registration_response(
                credential=json.dumps(credential),
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            raise DeviceError(f"passkey refused: {exc}") from exc
        await self._append(
            PAIRED,
            {
                "device": ceremony.device,
                "code_hash": ceremony.code_hash,
                "credential_id": b64url(verified.credential_id),
                "public_key": b64url(verified.credential_public_key),
                "sign_count": verified.sign_count,
            },
        )
        return ceremony.device

    # -- signatures (a session, an answer) ----------------------------------------------------

    async def signing_options(self, purpose: str) -> tuple[str, str]:
        """(ceremony id, options for a passkey signature) for `purpose`: `session`, or
        `approval:<id>:<yes|no>` (a yes and a no never share a challenge)."""
        await self.sync()
        if not self.state.devices:
            raise DeviceError("no device is paired")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=unb64url(d.credential_id))
                for d in self.state.devices.values()
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._open(Ceremony(options.challenge, purpose, 0)), options_to_json(options)

    async def verify(self, ceremony_id: str, purpose: str, credential: dict[str, Any]) -> str:
        """The device that signed, if the signature holds for this very purpose."""
        ceremony = self._take(ceremony_id, purpose)
        await self.sync()
        device = self.state.by_credential(str(credential.get("id", "")))
        if device is None:
            raise DeviceError("unknown passkey")
        try:
            verified = verify_authentication_response(
                credential=json.dumps(credential),
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origin,
                credential_public_key=unb64url(device.public_key),
                credential_current_sign_count=device.sign_count,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            raise DeviceError(f"signature refused: {exc}") from exc
        await self._append(
            SIGNED_IN, {"device": device.name, "sign_count": verified.new_sign_count}
        )
        return device.name

    def _open(self, ceremony: Ceremony) -> str:
        now = time.monotonic()
        self._ceremonies = {k: c for k, c in self._ceremonies.items() if c.expires > now}
        ceremony_id = secrets.token_urlsafe(16)
        self._ceremonies[ceremony_id] = Ceremony(
            ceremony.challenge,
            ceremony.purpose,
            now + CEREMONY_SECONDS,
            ceremony.code_hash,
            ceremony.device,
        )
        return ceremony_id

    def _take(self, ceremony_id: str, purpose: str) -> Ceremony:
        """Once only, and only for what it was drawn for."""
        ceremony = self._ceremonies.pop(ceremony_id, None)
        if ceremony is None or ceremony.expires <= time.monotonic():
            raise DeviceError("unknown or expired challenge")
        if not hmac.compare_digest(ceremony.purpose, purpose):
            raise DeviceError("that challenge was drawn for something else")
        return ceremony

    # -- sessions -----------------------------------------------------------------------------

    def session(self, device: str) -> str:
        expires = int(self.clock().timestamp()) + self._session_seconds
        body = f"{b64url(device.encode())}.{expires}"
        return f"{body}.{self._sign(body)}"

    async def session_device(self, cookie: str | None) -> str | None:
        """The device a session cookie belongs to, if it is valid and the device still paired."""
        if not cookie or cookie.count(".") != 2:
            return None
        name, expires, signature = cookie.split(".")
        body = f"{name}.{expires}"
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        if not expires.isdigit() or int(expires) <= self.clock().timestamp():
            return None
        try:
            device = unb64url(name).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        await self.sync()
        return device if device in self.state.devices else None

    def _sign(self, body: str) -> str:
        return b64url(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    # -- revocation ---------------------------------------------------------------------------

    async def revoke(self, device: str) -> bool:
        await self.sync()
        if device not in self.state.devices:
            return False
        await self._append(REVOKED, {"device": device})
        return True

    async def _append(self, kind: str, payload: dict[str, Any]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="mobile", actor="owner:main"),
                    trust=Trust.OWNER,
                    correlation_id=new_id(),
                )
            ]
        )
        await self.sync()


def session_key(folder: Path) -> bytes:
    """The key that signs the app's sessions, made once; deleting it signs every device out."""
    path = folder / "session.key"
    if not path.is_file():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Made meanwhile by another process: use that one.
        with contextlib.suppress(FileExistsError):
            write_private(path, secrets.token_bytes(32), exclusive=True)
    return path.read_bytes()
