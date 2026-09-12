from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prompt_context import (
    DEFAULT_CORE,
    DEFAULT_METHODOLOGY,
    ConversationJournal,
    MemoryExtractor,
    MemoryMaintenance,
    PromptContextStore,
    SQLiteConversationJournal,
)


class _ExtractionClient:
    def __init__(self) -> None:
        self.payloads: list[list[dict]] = []

    def complete(self, messages, **_kwargs):
        self.payloads.append(json.loads(messages[-1]["content"]))
        return {
            "text": json.dumps(
                {
                    "user_profile": {},
                    "preferences": [],
                    "memories": [],
                    "forget": [],
                }
            )
        }

    @staticmethod
    def text_from_response(response):
        return response["text"]


def _append_entries(journal, count: int, *, content_size: int = 40) -> None:
    for index in range(count):
        journal.append(
            event_id=f"event-{index + 1}",
            task_id=None,
            conversation_id="chat",
            messages=[
                {
                    "role": "user",
                    "content": f"entry-{index + 1}-" + ("x" * content_size),
                }
            ],
        )


def test_extractor_large_batch_advances_only_through_serialized_prefix(tmp_path):
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    _append_entries(journal, 6, content_size=650)
    store = PromptContextStore(":memory:")
    client = _ExtractionClient()
    extractor = MemoryExtractor(client, store, max_input_chars=1400)
    maintenance = MemoryMaintenance(
        journal,
        extractor,
        batch_size=6,
        min_entries=1,
        max_batches_per_run=1,
        tail_max_age=0,
    )

    processed = maintenance.run_once()

    assert 0 < processed < 6
    assert store.journal_cursor == processed
    assert [item["id"] for item in client.payloads[0]] == list(
        range(1, processed + 1)
    )

    while store.journal_cursor < 6:
        maintenance.run_once()

    seen = [item["id"] for payload in client.payloads for item in payload]
    assert seen == [1, 2, 3, 4, 5, 6]
    assert store.journal_cursor == 6


def test_maintenance_drains_backlog_up_to_configured_bound(tmp_path):
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    _append_entries(journal, 10)
    store = PromptContextStore(":memory:")
    client = _ExtractionClient()
    maintenance = MemoryMaintenance(
        journal,
        MemoryExtractor(client, store, max_input_chars=20_000),
        batch_size=2,
        min_entries=2,
        max_batches_per_run=3,
        tail_max_age=0,
    )

    assert maintenance.run_once() == 6
    assert store.journal_cursor == 6
    assert maintenance.run_once() == 4
    assert store.journal_cursor == 10


def test_tail_below_min_entries_is_processed_when_old_enough(tmp_path):
    now = datetime.now(timezone.utc)
    path = tmp_path / "journal.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": 1,
                "event_id": "old",
                "task_id": None,
                "messages": [{"role": "user", "content": "old fact"}],
                "created_at": (now - timedelta(hours=2)).isoformat(),
                "source": "test",
                "channel": None,
                "conversation_id": "chat",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal = ConversationJournal(path)
    client = _ExtractionClient()
    store = PromptContextStore(":memory:")
    maintenance = MemoryMaintenance(
        journal,
        MemoryExtractor(client, store),
        batch_size=5,
        min_entries=5,
        tail_max_age=3600,
    )

    assert maintenance.run_once() == 1
    assert store.journal_cursor == 1


def test_apply_extraction_supersedes_keyed_facts_and_forgets_all_categories(tmp_path):
    path = tmp_path / "prompt-context.json"
    store = PromptContextStore(path)
    store.apply_extraction(
        {
            "_provenance": "journal:1-2",
            "_observed_at": "2026-09-11T12:00:00+00:00",
            "user_profile": {"city": "Paris"},
            "preferences": [{"value": "Prefers coffee", "key": "drink"}],
            "memories": [{"value": "Lives in Paris", "key": "residence"}],
        }
    )
    store.apply_extraction(
        {
            "user_profile": {"city": "Lyon"},
            "preferences": [
                {
                    "value": "Prefers tea",
                    "key": "drink",
                    "contradicts": ["coffee"],
                }
            ],
            "memories": [{"value": "Lives in Lyon", "key": "residence"}],
        }
    )

    snapshot = store.snapshot()
    assert snapshot.user_profile == {"city": "Lyon"}
    assert snapshot.preferences == ["Prefers tea"]
    assert snapshot.memories == ["Lives in Lyon"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["memory_metadata"]["preferences"]["prefers tea"]["key"] == "drink"
    assert raw["memory_metadata"]["memories"]["lives in lyon"]["observed_at"]

    store.apply_extraction(
        {
            "forget": {
                "user_profile": ["city"],
                "preferences": ["drink"],
                "memories": ["residence"],
            }
        }
    )
    snapshot = store.snapshot()
    assert snapshot.user_profile == {}
    assert snapshot.preferences == []
    assert snapshot.memories == []


def test_structured_supersedes_replaces_legacy_string_fact():
    store = PromptContextStore(":memory:")
    store.apply_extraction({"memories": ["User lives in Paris"]})

    store.apply_extraction(
        {
            "memories": [
                {
                    "value": "User lives in Lyon",
                    "key": "residence",
                    "supersedes": ["User lives in Paris"],
                }
            ]
        }
    )

    assert store.snapshot().memories == ["User lives in Lyon"]


def test_two_store_instances_merge_sequential_stale_writes(tmp_path):
    path = tmp_path / "prompt-context.json"
    first = PromptContextStore(path)
    stale = PromptContextStore(path)

    first.apply_extraction(
        {
            "user_profile": {"first": "A"},
            "memories": ["M1"],
        },
        journal_cursor=5,
    )
    stale.apply_extraction(
        {
            "user_profile": {"second": "B"},
            "memories": ["M2"],
        },
        journal_cursor=3,
    )

    fresh = PromptContextStore(path)
    snapshot = fresh.snapshot()
    assert snapshot.user_profile == {"first": "A", "second": "B"}
    assert snapshot.memories == ["M1", "M2"]
    assert fresh.journal_cursor == 5


def test_stale_store_snapshot_and_cursor_refresh_external_writes(tmp_path):
    path = tmp_path / "prompt-context.json"
    reader = PromptContextStore(path)
    writer = PromptContextStore(path)

    writer.apply_extraction({"memories": ["M1"]}, journal_cursor=4)
    assert reader.snapshot().memories == ["M1"]
    assert reader.journal_cursor == 4

    writer.apply_extraction({"memories": ["M2"]}, journal_cursor=7)
    assert reader.snapshot().memories == ["M1", "M2"]
    assert reader.journal_cursor == 7


@pytest.fixture(params=["jsonl", "sqlite"])
def _journal(request, tmp_path):
    if request.param == "jsonl":
        yield ConversationJournal(tmp_path / "conversation.jsonl")
        return
    journal = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        yield journal
    finally:
        journal.close()


def test_interrupted_attempt_is_superseded_by_success_with_fresh_cursor(_journal):
    first = _journal.append(
        event_id="same-event",
        task_id=None,
        conversation_id="chat",
        messages=[
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "Le RUN a été interrompu avant sa réponse finale. Phase atteinte : decision.",
            },
        ],
    )
    success = _journal.append(
        event_id="same-event",
        task_id=None,
        conversation_id="chat",
        messages=[
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "done"},
        ],
    )

    assert success.id > first.id
    assert [entry.id for entry in _journal.after(first.id)] == [success.id]
    assert [entry.id for entry in _journal.after(0)] == [success.id]
    assert [m["content"] for m in _journal.recent_messages(conversation_id="chat")] == [
        "do it",
        "done",
    ]
    duplicate = _journal.append(
        event_id="same-event",
        task_id=None,
        conversation_id="chat",
        messages=[{"role": "assistant", "content": "different retry"}],
    )
    assert duplicate.id == success.id
    assert duplicate.messages[-1]["content"] == "done"


def test_journal_preserves_tool_call_links_for_history(_journal):
    _journal.append(
        event_id="tool-event",
        task_id=None,
        conversation_id="chat",
        messages=[
            {"role": "user", "content": "look it up"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "web", "arguments": '{"q":"orion"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "web",
                "content": "result",
            },
        ],
    )

    history = _journal.recent_messages(conversation_id="chat", limit=10)
    assistant = next(item for item in history if item["role"] == "assistant")
    tool = next(item for item in history if item["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["function"]["name"] == "web"
    assert tool["tool_call_id"] == "call-1"
    assert tool["name"] == "web"


def test_default_methodology_does_not_require_persistence_for_ephemeral_requests():
    combined = f"{DEFAULT_CORE}\n{DEFAULT_METHODOLOGY}".casefold()
    assert "demande ephemere n'impose aucune" in combined
    assert "optional continuity aids" in combined
