from datetime import datetime, timedelta, timezone

import pytest

import approvals as approvals_module
from approvals import ApprovalStore


def test_memory_store_keeps_schema_and_data_across_calls():
    store = ApprovalStore(":memory:")

    created = store.create("tester", "deploy", {"version": 1})

    assert store.get(created["id"])["payload"] == {"version": 1}
    assert [item["id"] for item in store.pending()] == [created["id"]]


def test_expired_approval_cannot_be_approved_and_is_not_pending(monkeypatch):
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(approvals_module, "_now", lambda: clock["now"].isoformat())
    store = ApprovalStore(":memory:")
    created = store.create(
        "tester",
        "deploy",
        expires_at=(clock["now"] + timedelta(seconds=30)).isoformat(),
    )
    clock["now"] += timedelta(seconds=31)

    with pytest.raises(ValueError):
        store.approve(created["id"], decided_by="owner")

    assert store.get(created["id"])["status"] == "expired"
    assert store.pending() == []


def test_file_backed_store_still_persists_across_connections(tmp_path):
    path = str(tmp_path / "approvals.sqlite3")
    first = ApprovalStore(path)
    created = first.create("tester", "deploy")

    second = ApprovalStore(path)

    assert second.get(created["id"])["status"] == "pending"
    assert second.approve(created["id"])["status"] == "approved"
