"""A passkey in software, for tests: what an iPhone hands the app, signed the same way."""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers.encode_cbor import encode_cbor

from orion.mobile.push import b64url

UP, UV, AT = 0x01, 0x04, 0x40


class FakePasskey:
    def __init__(self, origin: str, rp_id: str, verify_user: bool = True) -> None:
        self.origin = origin
        self.rp_id = rp_id
        self.verify_user = verify_user
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.count = 0

    def _flags(self) -> int:
        return UP | (UV if self.verify_user else 0)

    def _client_data(self, kind: str, options: str) -> bytes:
        challenge = json.loads(options)["challenge"]
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}
        ).encode()

    def create(self, options: str) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        cose = encode_cbor(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags() | AT])
            + struct.pack("!I", self.count)
            + bytes(16)
            + struct.pack("!H", len(self.credential_id))
            + self.credential_id
            + cose
        )
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(self._client_data("webauthn.create", options)),
                "attestationObject": b64url(attestation),
            },
        }

    def sign(self, options: str, count: int | None = None) -> dict[str, Any]:
        self.count = self.count + 1 if count is None else count
        client_data = self._client_data("webauthn.get", options)
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags()])
            + struct.pack("!I", self.count)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
            },
        }
