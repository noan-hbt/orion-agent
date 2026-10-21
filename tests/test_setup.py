"""`orion setup`: a few questions, keys checked online, the owner found by his message (D92)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from orion.config import load_config
from orion.setup import CORE_ENV, GUARDIAN_ENV, Setup, load_env_files, read_env, write_env

OWNER_ID = 8491079293


def fake_services(sent: list[dict[str, object]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            good = request.headers["authorization"] == "Bearer sk-good"
            return httpx.Response(200 if good else 401, json={"data": {}})
        token, method = request.url.path.removeprefix("/bot").split("/")
        if token != "123:abc":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "orion_bot"}})
        if method == "getUpdates":
            message = {
                "chat": {"id": OWNER_ID, "type": "private"},
                "from": {"id": OWNER_ID, "first_name": "Noan"},
                "text": "salut",
            }
            return httpx.Response(
                200, json={"ok": True, "result": [{"update_id": 5, "message": message}]}
            )
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    return httpx.MockTransport(handle)


def answers(*given: str) -> Iterator[str]:
    yield from given


def wizard(
    tmp_path: Path, asked: Iterator[str], secrets: Iterator[str], sent: list[dict[str, object]]
) -> Setup:
    return Setup(
        tmp_path / "orion.toml",
        tmp_path,
        ask=lambda _prompt: next(asked),
        secret=lambda _prompt: next(secrets),
        say=lambda _text: None,
        http=httpx.Client(transport=fake_services(sent)),
    )


def test_a_fresh_install_asks_little_and_checks_every_key(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    setup = wizard(
        tmp_path,
        # name, timezone, guardian, telegram, is it you
        answers("Noan", "", "o", "", ""),
        # a bad key is asked again; then a bad token, then the right one
        answers("sk-bad", "sk-good", "nope", "123:abc"),
        sent,
    )
    assert setup.run() == 0
    config = load_config(tmp_path / "orion.toml")
    assert config.memory.owner_names == ["Noan"]
    assert config.guardian.enabled and config.channels.telegram.enabled
    assert config.channels.telegram.owner_id == OWNER_ID  # read from his message, not typed
    # The core's key for the core; the bot's token for the guardian, never both.
    assert read_env(tmp_path / CORE_ENV) == {"OPENROUTER_API_KEY": "sk-good"}
    assert read_env(tmp_path / GUARDIAN_ENV) == {"TELEGRAM_BOT_TOKEN": "123:abc"}
    assert (tmp_path / CORE_ENV).stat().st_mode & 0o777 == 0o600
    assert "api_key" not in (tmp_path / "orion.toml").read_text("utf-8")
    assert sent and sent[0]["chat_id"] == OWNER_ID  # the bot told him it knows him


def test_run_again_it_keeps_what_is_there(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []
    (tmp_path / "orion.toml").write_text(
        '# mon Orion\ninstance = "orion-main"\n[guardian]\nenabled = false\n'
        f"[channels.telegram]\nenabled = true\nowner_id = {OWNER_ID}\n",
        "utf-8",
    )
    write_env(tmp_path / CORE_ENV, {"OPENROUTER_API_KEY": "sk-good", "OTHER": "x"})
    write_env(tmp_path / CORE_ENV, {"TELEGRAM_BOT_TOKEN": "123:abc"})
    # name, timezone, no guardian, telegram, keep the owner
    setup = wizard(tmp_path, answers("", "", "", "", ""), answers("", ""), sent)
    assert setup.run() == 0
    text = (tmp_path / "orion.toml").read_text("utf-8")
    assert text.startswith("# mon Orion")
    assert load_config(tmp_path / "orion.toml").channels.telegram.owner_id == OWNER_ID
    env = read_env(tmp_path / CORE_ENV)
    assert env["OTHER"] == "x" and env["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert not (tmp_path / GUARDIAN_ENV).exists()
    assert sent == []  # nothing asked of him again


def test_the_core_never_loads_the_guardians_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env(tmp_path / CORE_ENV, {"ORION_TEST_CORE": "c"})
    write_env(tmp_path / GUARDIAN_ENV, {"ORION_TEST_GUARDED": "g"})
    monkeypatch.delenv("ORION_TEST_CORE", raising=False)
    monkeypatch.delenv("ORION_TEST_GUARDED", raising=False)
    load_env_files(tmp_path, "serve")
    assert os.environ.get("ORION_TEST_CORE") == "c"
    assert "ORION_TEST_GUARDED" not in os.environ
    monkeypatch.delenv("ORION_TEST_CORE")
    load_env_files(tmp_path, "guardian")
    assert os.environ.get("ORION_TEST_GUARDED") == "g"
    assert "ORION_TEST_CORE" not in os.environ
    monkeypatch.setenv("ORION_TEST_GUARDED", "already")
    load_env_files(tmp_path, "doctor")
    assert os.environ["ORION_TEST_GUARDED"] == "already"  # never overridden
