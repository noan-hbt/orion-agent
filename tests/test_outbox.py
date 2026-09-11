import pytest
from outbox import Outbox


def test_idempotency_claim_ack(tmp_path):
    q = Outbox(tmp_path / "o.db")
    ident = q.enqueue("telegram", {"text": "hi"}, "k")
    assert ident == q.enqueue("telegram", {"text": "hi"}, "k")
    with pytest.raises(ValueError, match="idempotency_key reused"):
        q.enqueue("telegram", {"text": "changed"}, "k")
    item = q.claim()[0]
    assert item.payload == {"text": "hi"} and item.attempts == 1
    assert q.ack(item.id) and q.metrics()["sent"] == 1


def test_fail_retry_and_expired_lease(tmp_path):
    q = Outbox(tmp_path / "o.db", base_backoff=0)
    ident = q.enqueue("email", {"x": 1})
    first = q.claim(lease_seconds=0.1, worker_id="worker-old")[0]
    assert q.fail(first.id, "temporary", worker_id="worker-old")
    assert q.claim(worker_id="worker-new")[0].id == ident
    assert q.renew(first.id, worker_id="worker-old") is False


def test_release_replay_metrics(tmp_path):
    q = Outbox(tmp_path / "o.db")
    ident = q.enqueue("webhook", {})
    first = q.claim()[0]
    assert q.release(first.id)
    assert q.claim()[0].id == ident
    q.ack(ident)
    assert q.replay(include_sent=True) == 1
    assert q.metrics()["pending"] == 1


def test_expired_stale_claim_cannot_mutate_reclaimed_item(tmp_path):
    now = [100.0]
    q = Outbox(tmp_path / "o.db", clock=lambda: now[0], base_backoff=0)
    ident = q.enqueue("email", {"x": 1})
    stale = q.claim(lease_seconds=1, worker_id="worker-old")[0]

    now[0] = 102.0
    reclaimed = q.claim(lease_seconds=10, worker_id="worker-new")[0]
    assert reclaimed.id == ident == stale.id

    assert q.renew(ident, worker_id="worker-old") is False
    assert q.fail(ident, "stale failure", worker_id="worker-old") is False
    assert q.ack(ident, worker_id="worker-old") is False
    assert q.ack(ident, worker_id="worker-new") is True
