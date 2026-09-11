from __future__ import annotations

import json
import threading

from prompt_context import ConversationJournal, SQLiteConversationJournal


def _message(text: str):
    return [{"role": "user", "content": text}]


def test_jsonl_recent_and_dedupe_use_hot_indexes_without_rescan(tmp_path, monkeypatch):
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path, recent_cache_messages=8)
    for i in range(5):
        journal.append(
            event_id=f"e-{i}", task_id=None, conversation_id="chat",
            messages=_message(f"m-{i}"),
        )

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("hot recent/dedupe path must not rescan JSONL")

    monkeypatch.setattr(type(path), "read_text", fail_read_text)
    assert [m["content"] for m in journal.recent_messages(conversation_id="chat", limit=3)] == [
        "m-2", "m-3", "m-4"
    ]
    duplicate = journal.append(
        event_id="e-4", task_id=None, conversation_id="chat",
        messages=_message("replacement"),
    )
    assert duplicate.messages[0]["content"] == "m-4"


def test_jsonl_external_append_is_detected_and_reindexed(tmp_path):
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path)
    journal.append(event_id="one", task_id=None, conversation_id="chat", messages=_message("one"))
    raw = {
        "id": 99,
        "event_id": "external",
        "task_id": None,
        "messages": [{"role": "assistant", "content": "external"}],
        "created_at": "2026-01-01T00:00:00Z",
        "source": "orion",
        "channel": None,
        "conversation_id": "chat",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(raw) + "\n")

    assert journal.append(
        event_id="external", task_id=None, conversation_id="chat", messages=_message("retry")
    ).id == 99
    assert journal.recent_messages(conversation_id="chat", limit=1)[0]["content"] == "external"


def test_jsonl_multi_instance_writers_allocate_unique_cursor_ids(tmp_path):
    path = tmp_path / "conversation.jsonl"
    first = ConversationJournal(path)
    second = ConversationJournal(path)
    barrier = threading.Barrier(2)

    # Force both pre-patch writers to refresh their stale next-id before either
    # append can proceed.  With the interprocess lock, only the first writer can
    # reach this point; it times out, appends id=1, and the second then refreshes
    # to id=2.  This makes the old duplicate-id race deterministic.
    def synchronize_refresh(journal):
        original = journal._refresh_indexes_if_changed

        def wrapped():
            original()
            try:
                barrier.wait(timeout=0.15)
            except threading.BrokenBarrierError:
                pass

        journal._refresh_indexes_if_changed = wrapped

    synchronize_refresh(first)
    synchronize_refresh(second)
    results = []
    errors = []

    def append(journal, text):
        try:
            results.append(
                journal.append(
                    event_id=None,
                    task_id=None,
                    conversation_id="chat",
                    messages=_message(text),
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(first, "one")),
        threading.Thread(target=append, args=(second, "two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(entry.id for entry in results) == [1, 2]

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sorted(row["id"] for row in rows) == [1, 2]
    reader = ConversationJournal(path)
    first_page = reader.after(0, limit=1)
    second_page = reader.after(first_page[-1].id, limit=1)
    assert [entry.id for entry in first_page] == [1]
    assert [entry.id for entry in second_page] == [2]


def test_sqlite_recent_is_bounded_and_preserves_last_message_order(tmp_path):
    journal = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        for i in range(80):
            journal.append(
                event_id=f"e-{i}", task_id=None, conversation_id="chat",
                messages=[
                    {"role": "user", "content": f"u-{i}"},
                    {"role": "assistant", "content": f"a-{i}"},
                ],
            )
        recent = journal.recent_messages(conversation_id="chat", limit=5)
        assert [m["content"] for m in recent] == ["a-77", "u-78", "a-78", "u-79", "a-79"]
        plan = journal._db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM journal WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            ("chat", 5),
        ).fetchall()
        assert any("idx_journal_conversation_id_id" in str(row["detail"]) for row in plan)
    finally:
        journal.close()


def test_sqlite_append_dedupe_keeps_original_payload(tmp_path):
    journal = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        first = journal.append(event_id="same", task_id=None, conversation_id="chat", messages=_message("first"))
        second = journal.append(event_id="same", task_id=None, conversation_id="chat", messages=_message("second"))
        assert second.id == first.id
        assert second.messages[0]["content"] == "first"
    finally:
        journal.close()
