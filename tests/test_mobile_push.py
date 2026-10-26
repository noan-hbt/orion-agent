"""Web Push for the app (D125): RFC 8291 encryption, VAPID signature, known services only."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from orion.mobile.push import (
    Pusher,
    PushError,
    Subscription,
    Vapid,
    allowed_endpoint,
    b64url,
    encrypt,
    unb64url,
)


def test_encryption_matches_the_rfc_8291_example() -> None:
    sender = ec.derive_private_key(
        int.from_bytes(unb64url("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    subscription = Subscription(
        "https://push.example.net/push/JzLQ3raZJfFBR0aqvOMsLrt54w4rJUsV",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
    )
    body = encrypt(
        b"When I grow up, I want to be a watermelon",
        subscription,
        sender=sender,
        salt=unb64url("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
        "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQex"
        "SgSxsj_Qulcy4a-fN"
    )


def test_only_known_push_services() -> None:
    assert allowed_endpoint("https://web.push.apple.com/QGuQyavXutnMH")
    assert not allowed_endpoint("http://web.push.apple.com/x")
    assert not allowed_endpoint("https://127.0.0.1:8790/console/")
    assert not allowed_endpoint("https://web.push.apple.com.evil.example/x")


def test_the_vapid_key_is_made_once_and_signs(tmp_path: Path) -> None:
    path = tmp_path / "mobile" / "vapid.pem"
    vapid = Vapid(path, "https://orion.example")
    assert path.stat().st_mode & 0o777 == 0o600
    assert Vapid(path, "https://orion.example").public_key == vapid.public_key
    header = vapid.authorization("https://web.push.apple.com/abc", now=1_000_000)
    token, key = header.removeprefix("vapid t=").split(", k=")
    head, body, signature = token.split(".")
    claims = json.loads(unb64url(body))
    assert claims == {"aud": "https://web.push.apple.com", "exp": 1_043_200,
                      "sub": "https://orion.example"}  # fmt: skip
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(key))
    raw = unb64url(signature)
    public.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{body}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


async def test_a_push_is_sent_encrypted_and_a_gone_subscription_said(tmp_path: Path) -> None:
    phone = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    subscription = Subscription(
        "https://web.push.apple.com/abc",
        b64url(
            phone.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        ),
        b64url(b"0123456789abcdef"),
    )
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201 if len(seen) == 1 else 410)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        pusher = Pusher(Vapid(tmp_path / "vapid.pem", "https://orion.example"), http)
        await pusher.send(subscription, {"title": "Orion", "body": "Le 7 h 04."})
        with pytest.raises(PushError) as gone:
            await pusher.send(subscription, {"title": "Orion", "body": "re"})
        assert gone.value.gone
        with pytest.raises(PushError):
            await pusher.send(
                Subscription("https://10.0.0.1/x", subscription.p256dh, subscription.auth), {}
            )
    first = seen[0]
    assert first.headers["content-encoding"] == "aes128gcm"
    assert first.headers["authorization"].startswith("vapid t=")
    assert b"7 h 04" not in first.content  # only ciphertext leaves
    assert len(seen) == 2  # the unknown endpoint was never called
