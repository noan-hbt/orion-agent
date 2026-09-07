import time
from outbox import Outbox

def test_idempotency_claim_ack(tmp_path):
    q = Outbox(tmp_path / "o.db")
    assert q.enqueue("telegram", {"text": "hi"}, "k") == q.enqueue("telegram", {"text": "changed"}, "k")
    item = q.claim()[0]
    assert item.payload == {"text": "hi"} and item.attempts == 1
    assert q.ack(item.id) and q.metrics()["sent"] == 1

def test_fail_retry_and_expired_lease(tmp_path):
    q = Outbox(tmp_path / "o.db", base_backoff=0)
    item = q.claim() if False else None
    ident = q.enqueue("email", {"x": 1})
    first = q.claim(lease_seconds=.1)[0]
    assert q.fail(first.id, "temporary")
    assert q.claim()[0].id == ident
    assert q.renew(first.id) is False

def test_release_replay_metrics(tmp_path):
    q = Outbox(tmp_path / "o.db")
    ident = q.enqueue("webhook", {})
    first = q.claim()[0]; assert q.release(first.id)
    assert q.claim()[0].id == ident
    q.ack(ident); assert q.replay() == 1; assert q.metrics()["pending"] == 1
