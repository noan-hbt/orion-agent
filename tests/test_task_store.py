import json
import threading

import pytest

import tasks as tasks_module
from tasks import JsonTaskStore, TaskStoreConflict


def test_json_task_store_atomic_save_preserves_previous_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    store.create("first")
    before = path.read_bytes()
    real_replace = tasks_module.os.replace

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(tasks_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.create("second")

    assert path.read_bytes() == before
    assert [task.objective for task in store._tasks.values()] == ["first"]
    assert list(tmp_path.glob(".tasks.json.*.tmp")) == []

    # A later successful mutation must not accidentally persist the failed
    # "second" task from RAM.
    monkeypatch.setattr(tasks_module.os, "replace", real_replace)
    store.create("third")
    assert [task.objective for task in JsonTaskStore(path).list()] == ["first", "third"]


def test_json_task_store_concurrent_mutations_reload_consistently(tmp_path):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    count = 40
    barrier = threading.Barrier(count)
    errors = []

    def create_task(index):
        try:
            barrier.wait()
            store.create(f"task-{index}")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create_task, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["tasks"]) == count

    restored = JsonTaskStore(path)
    tasks = restored.list()
    assert len(tasks) == count
    assert {task.objective for task in tasks} == {f"task-{index}" for index in range(count)}
    assert [task.id for task in tasks] == list(range(1, count + 1))


def test_json_task_store_two_instances_do_not_collide_or_lose_create(tmp_path):
    path = tmp_path / "tasks.json"
    first = JsonTaskStore(path)
    second = JsonTaskStore(path)
    barrier = threading.Barrier(2)
    created = []
    errors = []

    def create(store, objective):
        try:
            barrier.wait()
            created.append(store.create(objective))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=create, args=(first, "from-first")),
        threading.Thread(target=create, args=(second, "from-second")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(task.id for task in created) == [1, 2]
    restored = JsonTaskStore(path).list()
    assert [task.id for task in restored] == [1, 2]
    assert {task.objective for task in restored} == {"from-first", "from-second"}


def test_json_task_store_stale_same_task_save_fails_closed(tmp_path):
    path = tmp_path / "tasks.json"
    seed = JsonTaskStore(path)
    task_id = seed.create("shared").id
    first = JsonTaskStore(path)
    second = JsonTaskStore(path)
    first_task = first.get(task_id)
    second_task = second.get(task_id)
    assert first_task is not None and second_task is not None

    first_task.current_state["owner"] = "first"
    first.save(first_task)
    second_task.current_state["owner"] = "second"

    with pytest.raises(TaskStoreConflict, match="changed in another store instance"):
        second.save(second_task)

    restored = JsonTaskStore(path).get(task_id)
    assert restored is not None
    assert restored.current_state == {"owner": "first"}


def test_json_task_store_save_and_reload_preserves_existing_format(tmp_path):
    path = tmp_path / "tasks.json"
    store = JsonTaskStore(path)
    task = store.create("persist me")
    task.current_state["step"] = "done"
    store.save(task)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == ["tasks"]
    assert payload["tasks"][0]["objective"] == "persist me"

    restored = JsonTaskStore(path).get(task.id)
    assert restored is not None
    assert restored.current_state == {"step": "done"}
