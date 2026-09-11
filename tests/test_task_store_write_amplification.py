import threading

import pytest

import tasks as tasks_module
from tasks import JsonTaskStore


def test_identical_save_skips_rewrite_and_fsync(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    task = store.create("stable")

    replaces = 0
    fsyncs = 0
    real_replace = tasks_module.os.replace
    real_fsync = tasks_module.os.fsync

    def counted_replace(source, destination):
        nonlocal replaces
        replaces += 1
        return real_replace(source, destination)

    def counted_fsync(fd):
        nonlocal fsyncs
        fsyncs += 1
        return real_fsync(fd)

    monkeypatch.setattr(tasks_module.os, "replace", counted_replace)
    monkeypatch.setattr(tasks_module.os, "fsync", counted_fsync)

    for _ in range(100):
        store.save(task)

    assert replaces == 0
    assert fsyncs == 0


def test_failed_replace_does_not_mark_snapshot_durable(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    task = store.create("first")
    task.current_state["phase"] = "updated"

    real_replace = tasks_module.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(tasks_module.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.save(task)

    # The failed candidate must not become the store's in-memory durable view.
    in_memory = store.get(task.id)
    assert in_memory is not None
    assert in_memory.current_state == {}

    store.save(task)
    assert attempts == 2
    restored = JsonTaskStore(path).get(task.id)
    assert restored is not None
    assert restored.current_state == {"phase": "updated"}


def test_concurrent_identical_saves_coalesce_to_one_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    task = store.create("shared")
    task.current_state["version"] = 1

    real_replace = tasks_module.os.replace
    replace_count = 0
    count_lock = threading.Lock()

    def counted_replace(source, destination):
        nonlocal replace_count
        with count_lock:
            replace_count += 1
        return real_replace(source, destination)

    monkeypatch.setattr(tasks_module.os, "replace", counted_replace)

    errors = []
    barrier = threading.Barrier(20)

    def worker():
        try:
            barrier.wait()
            store.save(task)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert replace_count == 1
    restored = JsonTaskStore(path).get(task.id)
    assert restored is not None
    assert restored.current_state == {"version": 1}
