from __future__ import annotations

from orion_config import OrionConfig


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
