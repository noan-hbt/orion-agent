from __future__ import annotations

import threading
import time

from event_handler import EventHandler
from teams import TeamBus


def _bus(path, *, instance_id: str, claim_timeout: float = 0.2, poll_interval: float = 0.5, events=None):
    return TeamBus(
        path,
        instance_id=instance_id,
        team="ops",
        claim_timeout=claim_timeout,
        poll_interval=poll_interval,
        event_handler=events,
    )


def test_active_delivery_heartbeat_prevents_second_instance_reclaim(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    events = EventHandler(workers=0)
    first = _bus(
        path,
        instance_id="worker",
        claim_timeout=0.18,
        poll_interval=0.5,
        events=events,
    )
    second = _bus(path, instance_id="worker", claim_timeout=0.18, poll_interval=0.5)
    try:
        message = sender.send("worker", "long running job", kind="job")
        first.start()

        deadline = time.monotonic() + 2.0
        while message.id not in first._published_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert message.id in first._published_ids

        # Wait for more than two original lease periods.  The second process
        # must still see no recoverable work because the active owner heartbeats.
        time.sleep(0.42)
        assert second.inbox() == []
        assert first.acknowledge_delivery(message.id) is True
        assert second.inbox() == []
    finally:
        first.close()
        second.close()
        sender.close()


def test_stop_quiesces_polling_but_keeps_inflight_claim_alive_until_ack(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    events = EventHandler(workers=0)
    first = _bus(
        path,
        instance_id="worker",
        claim_timeout=0.18,
        poll_interval=0.5,
        events=events,
    )
    second = _bus(path, instance_id="worker", claim_timeout=0.18, poll_interval=0.5)
    try:
        message = sender.send("worker", "drain safely")
        first.start()
        deadline = time.monotonic() + 2.0
        while message.id not in first._published_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert message.id in first._published_ids

        # OrionApplication stops TeamBus intake before EventHandler/runtime
        # drain. The heartbeat must remain active during that interval.
        first.stop()
        assert not first.running
        time.sleep(0.42)
        assert second.inbox() == []
        assert first.acknowledge_delivery(message.id) is True
    finally:
        first.close()
        second.close()
        sender.close()


def test_expired_claim_is_recovered_but_stale_owner_cannot_ack(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    stale = _bus(path, instance_id="worker", claim_timeout=0.1)
    replacement = _bus(path, instance_id="worker", claim_timeout=1.0)
    try:
        message = sender.send("worker", "recover me")
        assert stale._claim_for_poll(message.id) is True

        # Simulate a dead process deterministically by expiring its durable
        # lease without running its poll/heartbeat thread.
        with stale._lock:
            stale._connection.execute(
                "UPDATE team_messages SET claim_expires_at=? WHERE id=?",
                (time.time() - 1.0, message.id),
            )
            stale._connection.commit()

        # Fencing is strict: an expired owner may not revive its lease even
        # before the replacement process executes recovery.
        assert stale._renew_delivery_claim(message.id) is False

        assert [item.id for item in replacement.inbox()] == [message.id]
        assert replacement._claim_for_poll(message.id) is True
        assert stale.acknowledge_delivery(message.id) is False
        assert replacement.acknowledge_delivery(message.id) is True
    finally:
        replacement.close()
        stale.close()
        sender.close()


def test_restart_recovers_crashed_claim_after_expiry(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    crashed = _bus(path, instance_id="worker", claim_timeout=0.1)
    try:
        message = sender.send("worker", "resume after restart", kind="job")
        assert crashed._claim_for_poll(message.id) is True
        old_owner = crashed._claim_owner

        with crashed._lock:
            crashed._connection.execute(
                "UPDATE team_messages SET claim_expires_at=? WHERE id=?",
                (time.time() - 1.0, message.id),
            )
            crashed._connection.commit()

        restarted = _bus(path, instance_id="worker", claim_timeout=1.0)
        try:
            assert restarted._claim_owner != old_owner
            recovered = restarted.inbox()
            assert [item.id for item in recovered] == [message.id]
            assert restarted._claim_for_poll(message.id) is True
            assert restarted.acknowledge_delivery(message.id) is True
        finally:
            restarted.close()
    finally:
        crashed.close()
        sender.close()


def test_claim_scope_isolation_survives_recovery(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender_ops = _bus(path, instance_id="sender")
    ops = _bus(path, instance_id="worker", claim_timeout=0.1)
    other_team = TeamBus(
        path,
        instance_id="worker",
        team="other",
        claim_timeout=1.0,
        poll_interval=0.5,
    )
    try:
        message = sender_ops.send("worker", "ops only")
        assert ops._claim_for_poll(message.id) is True
        with ops._lock:
            ops._connection.execute(
                "UPDATE team_messages SET claim_expires_at=? WHERE id=?",
                (time.time() - 1.0, message.id),
            )
            ops._connection.commit()

        assert other_team.inbox() == []
        assert other_team.acknowledge_delivery(message.id) is False
        assert [item.id for item in ops.inbox()] == [message.id]
    finally:
        other_team.close()
        ops.close()
        sender_ops.close()


def test_stale_owner_cannot_complete_job_after_reclaim(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    stale = _bus(path, instance_id="worker", claim_timeout=0.1)
    replacement = _bus(path, instance_id="worker", claim_timeout=1.0)
    try:
        message = sender.send("worker", "fenced completion", kind="job")
        assert stale._claim_for_poll(message.id) is True
        stale_fence = stale._claim_fences[message.id]

        with stale._lock:
            stale._connection.execute(
                "UPDATE team_messages SET claim_expires_at=? WHERE id=?",
                (time.time() - 1.0, message.id),
            )
            stale._connection.commit()

        assert [item.id for item in replacement.inbox()] == [message.id]
        assert replacement._claim_for_poll(message.id) is True
        replacement_fence = replacement._claim_fences[message.id]
        assert replacement_fence > stale_fence

        try:
            stale.complete_job(message.id, "stale result")
        except PermissionError:
            pass
        else:
            raise AssertionError("stale completion unexpectedly succeeded")

        current = replacement.get(message.id)
        assert current is not None
        assert current.status == "in_progress"
        assert current.result is None

        completed = replacement.complete_job(message.id, "fresh result")
        assert completed.status == "completed"
        assert completed.result == "fresh result"
    finally:
        replacement.close()
        stale.close()
        sender.close()


def test_delivery_replay_reuses_stable_event_idempotency_key(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = _bus(path, instance_id="sender")
    events = EventHandler(workers=0)
    first = _bus(path, instance_id="worker", events=events, claim_timeout=1.0, poll_interval=0.01)
    try:
        message = sender.send("worker", "publish once", kind="job")
        first.start()
        deadline = time.monotonic() + 2.0
        while message.id not in first._published_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert message.id in first._published_ids

        original = events.queue.get_nowait()
        events.queue.task_done()
        expected_key = f"team:delivery:{message.id}:v0"
        assert original.idempotency_key == expected_key

        # Model a process crash after EventHandler accepted the event but before
        # runtime acknowledgement. Releasing the claim makes the durable row
        # replayable while EventHandler still remembers the accepted identity.
        first.close()
        restarted = _bus(
            path,
            instance_id="worker",
            events=events,
            claim_timeout=1.0,
            poll_interval=0.01,
        )
        try:
            restarted.start()
            deadline = time.monotonic() + 2.0
            while message.id not in restarted._published_ids and time.monotonic() < deadline:
                time.sleep(0.01)
            assert message.id in restarted._published_ids
            assert events.queue.qsize() == 0
        finally:
            restarted.close()
    finally:
        if not first._closed:
            first.close()
        sender.close()


def test_completion_notification_replay_reuses_stable_event_identity(tmp_path):
    path = tmp_path / "teams.sqlite3"
    events = EventHandler(workers=0)
    sender = _bus(path, instance_id="sender", events=events)
    worker = _bus(path, instance_id="worker")
    try:
        message = sender.send("worker", "complete me", kind="job")
        completed = worker.complete_job(message.id, "done")
        assert completed.status == "completed"

        sender._replay_completion_notifications()
        original = events.queue.get_nowait()
        events.queue.task_done()
        assert original.idempotency_key is not None
        assert original.idempotency_key.startswith("team:completion:")

        sender.close()
        restarted = _bus(path, instance_id="sender", events=events)
        try:
            # Constructor replay uses the same durable completion key. The
            # EventHandler keeps the original receipt instead of enqueuing a
            # distinct second runtime event.
            assert events.queue.qsize() == 0
        finally:
            restarted.close()
    finally:
        if not sender._closed:
            sender.close()
        worker.close()


def test_completion_notification_has_single_claim_across_sender_incarnations(tmp_path):
    path = tmp_path / "teams.sqlite3"
    events_a = EventHandler(workers=0)
    events_b = EventHandler(workers=0)
    sender_a = _bus(path, instance_id="sender", events=events_a)
    sender_b = _bus(path, instance_id="sender", events=events_b)
    worker = _bus(path, instance_id="worker")
    try:
        message = sender_a.send("worker", "complete once", kind="job")
        worker.complete_job(message.id, "done")

        barrier = threading.Barrier(3)

        def replay(bus):
            barrier.wait()
            bus._replay_completion_notifications()

        first = threading.Thread(target=replay, args=(sender_a,))
        second = threading.Thread(target=replay, args=(sender_b,))
        first.start()
        second.start()
        barrier.wait()
        first.join(timeout=1.0)
        second.join(timeout=1.0)
        assert not first.is_alive() and not second.is_alive()
        assert events_a.queue.qsize() + events_b.queue.qsize() == 1

        owner = sender_a if events_a.queue.qsize() else sender_b
        payload = owner.pending_completions()[0]
        key = payload["completion_key"]
        assert owner.acknowledge_completion(key) is True
    finally:
        sender_a.close()
        sender_b.close()
        worker.close()


def test_failed_completion_receipt_is_rearmed_under_same_stable_key(tmp_path):
    path = tmp_path / "teams.sqlite3"
    events = EventHandler(
        workers=0,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )

    def fail(event):
        raise RuntimeError("runtime completion intake failed")

    events.register("handoff.completed", fail)
    sender = _bus(path, instance_id="sender", events=events)
    worker = _bus(path, instance_id="worker")
    try:
        message = sender.send("worker", "complete me", kind="job")
        worker.complete_job(message.id, "done")
        sender._replay_completion_notifications()
        original_receipt = events._durable_store.list_events()[0]
        stable_key = original_receipt.idempotency_key
        assert stable_key is not None and stable_key.startswith("team:completion:")
        events.dispatch_one()
        assert any(
            item.idempotency_key == stable_key
            for item in events._durable_store.list_events(status="failed")
        )

        events.unregister("handoff.completed", fail)
        delivered: list[str] = []
        events.register("handoff.completed", lambda event: delivered.append(event.id))
        sender._replay_completion_notifications()
        assert events.queue.qsize() == 1
        replayed_receipt = next(
            item
            for item in events._durable_store.list_events(status="queued")
            if item.idempotency_key == stable_key
        )
        assert replayed_receipt.event_id == original_receipt.event_id
        events.dispatch_one()
        assert delivered == [original_receipt.event_id]

        key = sender.pending_completions()[0]["completion_key"]
        assert sender.acknowledge_completion(key) is True
    finally:
        sender.close()
        worker.close()
        events.close()


def test_permanent_completion_delivery_failure_is_bounded(tmp_path):
    path = tmp_path / "teams.sqlite3"
    events = EventHandler(
        workers=0,
        default_max_attempts=1,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )
    events.register(
        "handoff.completed",
        lambda _event: (_ for _ in ()).throw(RuntimeError("permanent failure")),
    )
    sender = _bus(path, instance_id="sender", events=events)
    worker = _bus(path, instance_id="worker")
    try:
        message = sender.send("worker", "complete me", kind="job")
        worker.complete_job(message.id, "done")
        sender._replay_completion_notifications()
        events.dispatch_one()
        key = sender.pending_completions()[0]["completion_key"]

        for expected in range(1, sender._MAX_DOWNSTREAM_RECOVERIES + 1):
            sender._replay_completion_notifications()
            with sender._lock:
                row = sender._connection.execute(
                    "SELECT delivery_recoveries FROM completion_notifications WHERE key=?",
                    (key,),
                ).fetchone()
            assert int(row["delivery_recoveries"]) == expected
            assert events.queue.qsize() == 1
            events.dispatch_one()

        sender._replay_completion_notifications()
        with sender._lock:
            row = sender._connection.execute(
                "SELECT delivery_recoveries, delivery_failed FROM completion_notifications WHERE key=?",
                (key,),
            ).fetchone()
        assert int(row["delivery_recoveries"]) == 3
        assert int(row["delivery_failed"]) == 1
        assert events.queue.qsize() == 0
        sender._replay_completion_notifications()
        assert events.queue.qsize() == 0
    finally:
        sender.close()
        worker.close()
        events.close()


def test_permanent_team_message_delivery_failure_is_bounded(tmp_path):
    path = tmp_path / "teams.sqlite3"
    events = EventHandler(
        workers=0,
        default_max_attempts=1,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )
    events.register(
        "team.message",
        lambda _event: (_ for _ in ()).throw(RuntimeError("permanent failure")),
    )
    sender = _bus(path, instance_id="sender")
    recipient = _bus(path, instance_id="worker", events=events)
    try:
        message = sender.send("worker", "deliver me")
        assert recipient._claim_for_poll(message.id) is True
        claimed = recipient.get(message.id)
        assert claimed is not None
        payload = claimed.to_dict()
        payload["delivered_at"] = None
        stable_key = recipient._delivery_idempotency_key(claimed)
        events.publish(
            "team.message",
            payload,
            source=f"team:{claimed.sender}",
            metadata={"team_message_id": claimed.id, "team": claimed.team, "internal_event": True},
            max_attempts=1,
            idempotency_key=stable_key,
        )
        recipient._published_ids.add(message.id)
        events.dispatch_one()

        for expected in range(1, recipient._MAX_DOWNSTREAM_RECOVERIES + 1):
            assert recipient._recover_failed_published_deliveries() == 1
            with recipient._lock:
                row = recipient._connection.execute(
                    "SELECT delivery_recoveries FROM team_messages WHERE id=?",
                    (message.id,),
                ).fetchone()
            assert int(row["delivery_recoveries"]) == expected
            assert events.queue.qsize() == 1
            events.dispatch_one()

        assert recipient._recover_failed_published_deliveries() == 0
        current = recipient.get(message.id)
        assert current is not None
        assert current.status == "failed"
        with recipient._lock:
            row = recipient._connection.execute(
                "SELECT delivery_recoveries, delivery_failed FROM team_messages WHERE id=?",
                (message.id,),
            ).fetchone()
        assert int(row["delivery_recoveries"]) == 3
        assert int(row["delivery_failed"]) == 1
        assert message.id not in recipient._published_ids
        assert events.queue.qsize() == 0
    finally:
        recipient.close()
        sender.close()
        events.close()
