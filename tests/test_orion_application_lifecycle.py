import threading

from event_handler import Event, EventHandler
from orion_config import OrionApplication, OrionConfig


class _Producer:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def start(self) -> None:
        self.calls.append(f"{self.name}.start")

    def stop(self) -> None:
        self.calls.append(f"{self.name}.stop")


class _RuntimeProbe:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.running = False
        self.pending: list[Event] = []
        self.processed: list[Event] = []
        self.action_ledger = None
        self.task_store = None
        self.context_registry = None
        self.retrieval_store = None
        self.conversation_journal = None

    def start(self) -> None:
        self.running = True
        self.calls.append("runtime.start")

    def receive_event(self, event: Event) -> None:
        self.calls.append("runtime.receive")
        self.pending.append(event)

    def stop(self) -> None:
        self.calls.append("runtime.stop")
        self.processed.extend(self.pending)
        self.pending.clear()
        self.running = False


class _Closable:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def close(self) -> None:
        self.calls.append(f"{self.name}.close")


class _TeamBus(_Producer):
    def close(self) -> None:
        self.calls.append("team_bus.close")


def test_stop_drains_already_accepted_event_before_runtime_stops() -> None:
    calls: list[str] = []
    entered_blocker = threading.Event()
    release_blocker = threading.Event()

    events = EventHandler(workers=1)
    runtime = _RuntimeProbe(calls)

    def block_dispatch(_event: Event) -> None:
        entered_blocker.set()
        assert release_blocker.wait(timeout=2.0)

    events.register("message", block_dispatch)
    events.register("message", runtime.receive_event)

    original_events_stop = events.stop

    def draining_events_stop(*, wait: bool = True, drain: bool = True) -> None:
        calls.append("events.stop")
        release_blocker.set()
        original_events_stop(wait=wait, drain=drain)

    events.stop = draining_events_stop  # type: ignore[method-assign]

    app = OrionApplication(
        events=events,
        llm=_Closable("llm", calls),
        runtime=runtime,
        scheduler=_Producer("scheduler", calls),
        subagents=_Producer("subagents", calls),
        team_bus=_TeamBus("team_bus", calls),
        channels=_Producer("channels", calls),
        gateway_ledger=_Closable("ledger", calls),
    )
    app.start()

    accepted = events.publish("message", {"text": "accepted before shutdown"})
    assert entered_blocker.wait(timeout=2.0)

    app.stop()

    assert runtime.processed == [accepted]
    assert calls.index("channels.stop") < calls.index("events.stop")
    assert calls.index("scheduler.stop") < calls.index("events.stop")
    assert calls.index("subagents.stop") < calls.index("events.stop")
    assert calls.index("team_bus.stop") < calls.index("events.stop")
    assert calls.index("events.stop") < calls.index("runtime.stop")
    assert calls.index("runtime.stop") < calls.index("team_bus.close")
    assert calls.index("runtime.stop") < calls.index("llm.close")
    assert calls.index("runtime.stop") < calls.index("ledger.close")

    # stop() remains idempotent and does not close components twice.
    snapshot = list(calls)
    app.stop()
    assert calls == snapshot


def test_stop_closes_runtime_owned_resources_once_after_runtime_drain() -> None:
    calls: list[str] = []
    events = EventHandler(workers=0)
    runtime = _RuntimeProbe(calls)
    shared_store = _Closable("shared_store", calls)
    journal = _Closable("journal", calls)

    runtime.action_ledger = shared_store
    runtime.context_registry = shared_store
    runtime.retrieval_store = _Closable("retrieval_store", calls)
    runtime.conversation_journal = journal

    app = OrionApplication(
        events=events,
        llm=_Closable("llm", calls),
        runtime=runtime,
        gateway_ledger=journal,
    )
    app.start()

    app.stop()

    runtime_stop = calls.index("runtime.stop")
    assert runtime_stop < calls.index("shared_store.close")
    assert runtime_stop < calls.index("retrieval_store.close")
    assert runtime_stop < calls.index("journal.close")
    assert calls.count("shared_store.close") == 1
    assert calls.count("journal.close") == 1
    assert calls.index("journal.close") < calls.index("llm.close")

    snapshot = list(calls)
    app.stop()
    assert calls == snapshot


def test_application_exposes_owned_services_without_duplicate_storage() -> None:
    calls: list[str] = []
    runtime = _RuntimeProbe(calls)
    task_store = object()
    registry = object()
    retrieval = object()
    journal = object()
    usage = object()
    tools = object()
    runtime.task_store = task_store
    runtime.context_registry = registry
    runtime.retrieval_store = retrieval
    runtime.conversation_journal = journal
    llm = _Closable("llm", calls)
    llm.usage_ledger = usage

    app = OrionApplication(
        events=EventHandler(workers=0),
        llm=llm,
        runtime=runtime,
        tool_manager=tools,
    )

    assert app.task_store is task_store
    assert app.tool_manager is tools
    assert app.context_registry is registry
    assert app.retrieval_store is retrieval
    assert app.conversation_journal is journal
    assert app.usage_ledger is usage


def test_config_build_exposes_cockpit_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()

    config = OrionConfig.from_mapping({
        "reflection": {"enabled": False},
        "context": {"reflection_enabled": False},
        "subagents": {"enabled": False},
        "scheduler": {"enabled": False},
        "memory": {"enabled": False},
        "context_os": {
            "enabled": True,
            "registry_path": "data/context.sqlite3",
            "memory_path": "data/memory.sqlite3",
            "state_path": "data/thread.json",
        },
        "tools": {"directory": "tools"},
    })
    config.config_path = tmp_path / "orion.toml"

    app = config.build()
    try:
        assert app.tool_manager is not None
        assert app.task_store is app.runtime.task_store
        assert app.context_registry is app.runtime.context_registry
        assert app.context_registry is not None
        assert app.retrieval_store is app.runtime.retrieval_store
        assert app.retrieval_store is not None
        assert app.conversation_journal is app.runtime.conversation_journal
        assert app.usage_ledger is app.llm.usage_ledger
    finally:
        app.stop()
