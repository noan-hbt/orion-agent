import pytest

import event_handler as event_handler_module
from event_handler import DuplicateEventError, Event, EventHandler
from orion_config import OrionConfig


def test_dispatch_one_default_timeout_receives_event_not_priority_tuple():
    handler = EventHandler(workers=0)
    seen: list[Event] = []
    handler.register("regression", seen.append)

    published = handler.publish("regression", {"value": 1})
    dispatched = handler.dispatch_one()

    assert dispatched is published
    assert seen == [published]


def test_dedupe_duplicate_and_conflict_contract_within_ttl(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(event_handler_module.time, "monotonic", lambda: now[0])
    handler = EventHandler(workers=0, dedupe_ttl=30.0, dedupe_max_entries=10)

    first = handler.publish("message", {"text": "same"}, message_id="message-1")
    duplicate = handler.publish("message", {"text": "same"}, message_id="message-1")

    assert duplicate.id == first.id
    assert duplicate.created_at == first.created_at
    assert handler.queue.qsize() == 1
    with pytest.raises(DuplicateEventError):
        handler.publish("message", {"text": "changed"}, message_id="message-1")


def test_dedupe_reaccepts_key_after_ttl_expiration(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(event_handler_module.time, "monotonic", lambda: now[0])
    handler = EventHandler(workers=0, dedupe_ttl=10.0, dedupe_max_entries=10)

    first = handler.publish("message", {"text": "first"}, message_id="message-1")
    now[0] = 110.1
    second = handler.publish("message", {"text": "changed"}, message_id="message-1")

    assert second.id != first.id
    assert handler.queue.qsize() == 2


def test_dedupe_capacity_is_lru_bounded(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(event_handler_module.time, "monotonic", lambda: now[0])
    handler = EventHandler(workers=0, dedupe_ttl=60.0, dedupe_max_entries=2)

    handler.publish("message", {"text": "a"}, message_id="a")
    handler.publish("message", {"text": "b"}, message_id="b")
    handler.publish("message", {"text": "a"}, message_id="a")
    handler.publish("message", {"text": "c"}, message_id="c")

    assert list(handler._dedupe) == ["a", "c"]
    assert len(handler._dedupe) == 2


def test_dedupe_records_do_not_retain_payload_and_expired_records_are_pruned(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(event_handler_module.time, "monotonic", lambda: now[0])
    handler = EventHandler(workers=0, dedupe_ttl=5.0, dedupe_max_entries=10)
    large_payload = {"blob": "x" * 100_000}

    handler.publish("message", large_payload, message_id="large")
    record = handler._dedupe["large"]
    assert not hasattr(record, "event")
    assert not hasattr(record, "payload")

    now[0] = 105.1
    handler.publish("heartbeat", {"ok": True})
    assert "large" not in handler._dedupe


def test_event_dedupe_settings_are_configurable():
    config = OrionConfig.from_mapping(
        {"events": {"dedupe_ttl": 12.5, "dedupe_max_entries": 321}}
    )

    assert config.events.dedupe_ttl == 12.5
    assert config.events.dedupe_max_entries == 321
    config.validate()
