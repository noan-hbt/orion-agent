"""Instance identity: an Ed25519 key pair. The private key lives in the guardian process."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(value: Any) -> bytes:
    """The bytes that are signed: sorted keys, no spaces, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(public_key: str) -> str:
    """Short, human-comparable: read it aloud to check you paired with the right Orion."""
    digest = hashlib.sha256(unb64(public_key)).hexdigest()[:16]
    return " ".join(digest[i : i + 4] for i in range(0, 16, 4))


def generate_secret() -> str:
    """A new private key, to store in the guardian's environment."""
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    key = Ed25519PrivateKey.generate()
    return b64(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


class Identity:
    def __init__(self, secret: str) -> None:
        try:
            self._key = Ed25519PrivateKey.from_private_bytes(unb64(secret))
        except ValueError as exc:
            raise ValueError("not an Ed25519 private key (use `orion federation keygen`)") from exc
        self.public_key = b64(self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def sign(self, data: bytes) -> str:
        return b64(self._key.sign(data))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)


def verify(public_key: str, data: bytes, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(unb64(public_key)).verify(unb64(signature), data)
    except (InvalidSignature, ValueError):
        return False
    return True
