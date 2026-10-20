"""What Orion could do with more setup is in its system message, so it can say so (D64)."""

from __future__ import annotations

from orion.app import constitution, offers
from orion.config import OrionConfig


def _config(**sections: object) -> OrionConfig:
    return OrionConfig.model_validate(sections)


def test_a_module_that_is_off_offers_itself() -> None:
    listed = offers(_config())
    assert any("[modules.mail]" in o for o in listed)
    assert any("[modules.web] enabled = true" in o for o in listed)
    assert not any("orion login" in o for o in listed)  # the browser itself is off

    (browser,) = [o for o in listed if o.startswith("Un navigateur à toi")]
    assert "session" in browser and "gardien" in browser  # its accounts need more than this

    on = offers(_config(modules={"web": {"enabled": True}}))
    assert not any("[modules.web]" in o for o in on)


def test_a_module_that_is_on_names_what_it_still_lacks() -> None:
    lent = offers(_config(modules={"computer": {"enabled": True}}))
    (sessions,) = [o for o in lent if "orion login" in o]
    assert "[guardian] enabled = true" in sessions  # sessions are lent through the guardian

    calendar = {"enabled": True, "accounts": [{"name": "perso", "url": {"env": "ICS"}}]}
    read_only = offers(_config(modules={"calendar": calendar}))
    assert any("orion google auth" in o for o in read_only)


def test_offers_close_the_constitution_and_are_stable() -> None:
    config = _config()
    first = constitution([], offers(config))
    assert first == constitution([], offers(config))  # same config, same bytes (cache)
    section = first[first.index("# Ce que tu pourrais faire de plus") :]
    assert "Une\nseule fois par sujet" in section and "- " in section
    assert "# Ce que tu pourrais faire de plus" not in constitution([], [])


def test_telegram_says_what_it_rules_out() -> None:
    assert not any("Telegram" in o for o in offers(_config()))
    listed = offers(_config(channels={"telegram": {"enabled": True, "owner_id": 1}}))
    (telegram,) = [o for o in listed if "Telegram" in o]
    assert "gardien" in telegram and "session" in telegram
