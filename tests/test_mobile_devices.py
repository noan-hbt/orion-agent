"""Pairing the owner's phone (D125): a one-time code, a passkey, sessions, signatures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.mobile.devices import DeviceError, Devices, DeviceState, session_key
from tests.fake_passkey import FakePasskey

URL = "https://orion.example"


def devices(
    journal: SqliteJournal,
    tmp_path: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Devices:
    return Devices(journal, URL, session_key(tmp_path / "mobile"), clock=clock)


async def paired(desk: Devices, phone: FakePasskey) -> str:
    code = await desk.new_code("iphone")
    ceremony, options = await desk.pairing_options(code)
    return await desk.pair(ceremony, phone.create(options))


async def test_a_code_becomes_a_passkey_then_sessions(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    desk = devices(journal, tmp_path)
    phone = FakePasskey(URL, "orion.example")
    assert not await desk.any_paired()
    assert await paired(desk, phone) == "iphone"
    assert await desk.any_paired()

    ceremony, options = await desk.signing_options("session")
    assert await desk.verify(ceremony, "session", phone.sign(options)) == "iphone"
    cookie = desk.session("iphone")
    assert await desk.session_device(cookie) == "iphone"
    assert await desk.session_device(cookie[:-2] + "xx") is None  # forged

    # Only public keys reach the journal, and the projection rebuilds the same devices.
    [paired_event] = [e for e in await journal.read() if e.kind == "mobile.paired"]
    assert set(paired_event.payload) == {
        "device",
        "code_hash",
        "credential_id",
        "public_key",
        "sign_count",
    }  # a public key and a hash: nothing that could sign or pair again
    rebuilt = DeviceState()
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert rebuilt.devices == desk.state.devices

    assert await desk.revoke("iphone")
    assert await desk.session_device(cookie) is None  # cut off at once


async def test_a_code_works_once_and_not_after_ten_minutes(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    now = [datetime(2026, 10, 26, 12, 0, tzinfo=UTC)]
    desk = devices(journal, tmp_path, clock=lambda: now[0])
    code = await desk.new_code("iphone")
    ceremony, options = await desk.pairing_options(code)
    await desk.pair(ceremony, FakePasskey(URL, "orion.example").create(options))
    with pytest.raises(DeviceError):
        await desk.pairing_options(code)  # used

    late = await desk.new_code("ipad")
    now[0] += timedelta(minutes=11)
    with pytest.raises(DeviceError):
        await desk.pairing_options(late)
    with pytest.raises(DeviceError):
        await desk.pairing_options("NOTACODE")


async def test_a_signature_needs_face_id_the_right_site_and_the_right_purpose(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    desk = devices(journal, tmp_path)
    phone = FakePasskey(URL, "orion.example")
    await paired(desk, phone)

    # The challenge for a « no » cannot carry a « yes ».
    ceremony, options = await desk.signing_options("approval:a_1:no")
    with pytest.raises(DeviceError, match="something else"):
        await desk.verify(ceremony, "approval:a_1:yes", phone.sign(options))
    # And a challenge is good once.
    ceremony, options = await desk.signing_options("session")
    signed = phone.sign(options)
    await desk.verify(ceremony, "session", signed)
    with pytest.raises(DeviceError):
        await desk.verify(ceremony, "session", signed)

    # Without user verification (no Face ID), from another site, or a cloned key: refused.
    for liar in (
        FakePasskey(URL, "orion.example", verify_user=False),
        FakePasskey("https://evil.example", "orion.example"),
    ):
        liar.key, liar.credential_id = phone.key, phone.credential_id
        ceremony, options = await desk.signing_options("session")
        with pytest.raises(DeviceError):
            await desk.verify(ceremony, "session", liar.sign(options))
    ceremony, options = await desk.signing_options("session")
    with pytest.raises(DeviceError):
        await desk.verify(ceremony, "session", phone.sign(options, count=1))  # went back


async def test_pairing_without_face_id_is_refused(journal: SqliteJournal, tmp_path: Path) -> None:
    desk = devices(journal, tmp_path)
    code = await desk.new_code("iphone")
    ceremony, options = await desk.pairing_options(code)
    with pytest.raises(DeviceError):
        await desk.pair(
            ceremony, FakePasskey(URL, "orion.example", verify_user=False).create(options)
        )
    assert not await desk.any_paired()
