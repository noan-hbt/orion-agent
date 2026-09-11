from __future__ import annotations

from orion_config import OrionConfig
from channels import ChannelRouter
from event_handler import EventHandler


def test_telegram_configuration_round_trip():
    config = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["telegram"],
                "telegram": {
                    "enabled": True,
                    "token_env": "TELEGRAM_BOT_TOKEN",
                    "allowed_chat_ids": [123],
                    "allowed_user_ids": [456],
                    "offset_path": "data/telegram.offset",
                    "allow_all_chats": False,
                },
            }
        }
    )
    settings = config.channels.settings["telegram"]
    assert settings["allowed_chat_ids"] == [123]
    assert settings["offset_path"] == "data/telegram.offset"


def test_telegram_configuration_rejects_invalid_allowlist():
    try:
        OrionConfig.from_mapping(
            {"channels": {"enabled": ["telegram"], "telegram": {"allowed_chat_ids": ["bad"]}}}
        )
    except ValueError as exc:
        assert "allowed_chat_ids" in str(exc)
    else:
        raise AssertionError("invalid Telegram allowlist should be rejected")


def test_telegram_configuration_allows_bootstrap_owner_by_default():
    config = OrionConfig.from_mapping(
        {"channels": {"enabled": ["telegram"], "telegram": {"bootstrap_owner": True}}}
    )
    assert config.channels.settings["telegram"]["bootstrap_owner"] is True


def test_telegram_configuration_can_disable_bootstrap_owner():
    try:
        OrionConfig.from_mapping(
            {
                "channels": {
                    "enabled": ["telegram"],
                    "telegram": {"bootstrap_owner": False},
                }
            }
        )
    except ValueError as exc:
        assert "allowed_chat_ids" in str(exc)
    else:
        raise AssertionError("disabled bootstrap should require an explicit Telegram allowlist")


def test_telegram_pairing_secret_is_loaded_from_env_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_PAIRING_SECRET", "pair-secret")
    config = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["telegram"],
                "telegram": {
                    "bootstrap_owner": True,
                    "bootstrap_pairing_secret_env": "TELEGRAM_PAIRING_SECRET",
                },
            }
        }
    )
    router = ChannelRouter(EventHandler(workers=0), default_channel="telegram")
    config._configure_channels(router, usage_ledger=None)
    adapter = router.adapters["telegram"]
    assert adapter.bootstrap_pairing_secret == "pair-secret"
    assert config.channels.settings["telegram"]["bootstrap_pairing_secret_env"] == "TELEGRAM_PAIRING_SECRET"


def test_legacy_telegram_config_without_pairing_field_is_parseable_but_fail_closed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    config = OrionConfig.from_mapping(
        {"channels": {"enabled": ["telegram"], "telegram": {"bootstrap_owner": True}}}
    )
    router = ChannelRouter(EventHandler(workers=0), default_channel="telegram")
    config._configure_channels(router, usage_ledger=None)
    adapter = router.adapters["telegram"]
    assert adapter.bootstrap_pairing_secret is None
    assert adapter._owner_chat_id is None
    assert adapter.allowed_chat_ids == set()
    assert adapter.allowed_user_ids == set()

    calls = 0

    def api(method, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 2,
                            "chat": {"id": 10, "type": "private"},
                            "from": {"id": 20},
                            "text": "claim me",
                        },
                    }
                ],
            }
        adapter._stop_requested.set()
        return {"ok": True, "result": []}

    adapter._api = api
    adapter._on_message = lambda _message: None
    adapter._run()

    assert adapter._queue.empty()
    assert adapter._owner_chat_id is None
