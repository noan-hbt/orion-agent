"""Web Push to the owner's phone (D125): what lets Orion speak first when the app is closed.

The phone subscribes with the browser's push service (Apple's, for an iPhone) and hands Orion an
endpoint and two keys. Each notification is encrypted for that subscription (RFC 8291,
`aes128gcm`, RFC 8188): the push service only carries ciphertext. The request is signed with
Orion's VAPID key (RFC 8292), which the push service checks against the public key the app
subscribed with. The private key is Orion's own, made on first start and kept beside the data
(0600), like the console's session key.

Only endpoints on known push services are called: a session that leaked must not make Orion
send requests anywhere it likes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orion.core.private import write_private

PUSH_HOSTS = (
    "web.push.apple.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "notify.windows.com",
)
"""Where a subscription may point: Apple's service (iPhone, Safari), and the others in case the
owner pairs another browser. Anything else is refused."""
RECORD_SIZE = 4096
TTL_SECONDS = 12 * 3600


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PushError(RuntimeError):
    def __init__(self, message: str, gone: bool = False) -> None:
        super().__init__(message)
        self.gone = gone
        """The subscription no longer exists (the app was removed, or unsubscribed)."""


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    p256dh: str
    """The phone's public key (uncompressed P-256 point, base64url)."""
    auth: str
    """Its 16-byte authentication secret (base64url)."""


def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == known or host.endswith("." + known) for known in PUSH_HOSTS
    )


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(
    plaintext: bytes,
    subscription: Subscription,
    sender: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The request body for one notification (RFC 8291). `sender` and `salt` are fresh for
    each message; they are parameters only for the RFC's test vector."""
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    ua_public = unb64url(subscription.p256dh)
    auth_secret = unb64url(subscription.auth)
    as_public = sender.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), peer)
    prk_key = hmac.new(auth_secret, shared, hashlib.sha256).digest()
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_public + as_public, 32)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise PushError("notification too large for one record")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


class Vapid:
    """Orion's VAPID key pair, made once and kept in `path`."""

    def __init__(self, path: Path, subject: str) -> None:
        self.path = path
        self.subject = subject
        """Who to contact about these pushes (RFC 8292): Orion's own address."""
        if path.is_file():
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                raise ValueError(f"{path} is not a P-256 key")
            self._key = loaded
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(
                path,
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )

    @property
    def public_key(self) -> str:
        """The `applicationServerKey` the app subscribes with (base64url)."""
        return b64url(
            self._key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )

    def authorization(self, endpoint: str, now: float | None = None) -> str:
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int((now or time.time()) + TTL_SECONDS),
            "sub": self.subject,
        }
        head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
        body = b64url(json.dumps(claims, separators=(",", ":")).encode())
        signed = f"{head}.{body}".encode()
        r, s = decode_dss_signature(self._key.sign(signed, ec.ECDSA(hashes.SHA256())))
        signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={head}.{body}.{signature}, k={self.public_key}"


class Pusher:
    def __init__(self, vapid: Vapid, http: httpx.AsyncClient) -> None:
        self.vapid = vapid
        self._http = http

    async def send(self, subscription: Subscription, message: dict[str, object]) -> None:
        if not allowed_endpoint(subscription.endpoint):
            raise PushError(f"not a known push service: {subscription.endpoint!r}", gone=True)
        body = encrypt(json.dumps(message, ensure_ascii=False).encode(), subscription)
        try:
            response = await self._http.post(
                subscription.endpoint,
                content=body,
                headers={
                    "authorization": self.vapid.authorization(subscription.endpoint),
                    "content-encoding": "aes128gcm",
                    "content-type": "application/octet-stream",
                    "ttl": str(TTL_SECONDS),
                    "urgency": "high",
                },
            )
        except httpx.HTTPError as exc:
            raise PushError(f"push service unreachable: {exc}") from exc
        if response.status_code in (404, 410):
            raise PushError("the subscription is gone", gone=True)
        if response.status_code >= 400:
            raise PushError(f"push refused ({response.status_code}): {response.text[:200]}")
