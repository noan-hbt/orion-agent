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
    assert not any("`browser` connect" in o for o in listed)  # the browser itself is off

    (browser,) = [o for o in listed if o.startswith("Un navigateur à toi")]
    assert "session" in browser and "gardien" in browser  # its accounts need more than this

    on = offers(_config(modules={"web": {"enabled": True}}))
    assert not any("[modules.web]" in o for o in on)


def test_a_module_that_is_on_names_what_it_still_lacks() -> None:
    lent = offers(_config(modules={"computer": {"enabled": True}}))
    (accounts,) = [o for o in lent if "`browser` hand_over" in o]
    assert "téléphone" in accounts  # he signs in from his phone, in Orion's browser (D116)
    assert "public_url" in accounts  # the hand-over page is the console's

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


def test_no_offer_assumes_a_mac() -> None:
    """20/10: « sur ton Mac », to an owner who has none."""
    every = {name: {"enabled": True} for name in ("computer", "hands")}
    for listed in (offers(_config()), offers(_config(modules=every))):
        assert not any("son Mac" in o or "pair mac" in o for o in listed)


def test_orion_knows_his_name_from_the_start() -> None:
    """20/10: « tu sais déjà comment je m'appelle » — it did not; it is in the config."""
    assert "Ton propriétaire s'appelle Noan." in constitution([], [], ["Noan"])
    assert "s'appelle" not in constitution([], [])
