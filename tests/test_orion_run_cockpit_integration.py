from __future__ import annotations

import io
import threading
from types import SimpleNamespace

import orion_run
from channel_adapters import CLIAdapter
from channels import InboundMessage
from openrouter_client import OpenRouterConfigurationError


class _Router:
    def __init__(self, adapters):
        self._adapters = dict(adapters)

    @property
    def adapters(self):
        return dict(self._adapters)

    def unregister(self, name):
        self._adapters.pop(name, None)

    def register(self, adapter):
        self._adapters[adapter.name] = adapter


class _LegacyCLI:
    name = "cli"

    def __init__(self):
        self.starts = 0

    def start(self, _on_message):
        self.starts += 1


class _Cockpit:
    name = "cli"
    instances = []

    def __init__(self, backend):
        self.backend = backend
        self.starts = 0
        self.loops = 0
        self.stops = 0
        self._exit_handler = None
        type(self).instances.append(self)

    def set_exit_handler(self, handler):
        self._exit_handler = handler

    def start(self, _on_message):
        self.starts += 1

    def loop(self):
        assert self.starts == 1
        self.loops += 1

    def stop(self):
        self.stops += 1


class _Application:
    def __init__(self, adapters):
        self.channels = _Router(adapters)
        self.events = SimpleNamespace(on_error=None)
        self.runtime = SimpleNamespace(
            on_error=None,
            current_task=None,
            run_context=None,
            state=SimpleNamespace(value="sleep"),
            wake_count=0,
            pending_events=0,
            pending_events_during_run=0,
            reflection_engine=None,
            last_error=None,
            task_store=SimpleNamespace(list=lambda: []),
            _tool_definitions=lambda: [],
        )
        self.llm = SimpleNamespace(model="test/model")
        self.subagents = None
        self.starts = 0
        self.stops = 0
        self.run_forever_calls = 0

    def start(self):
        self.starts += 1
        for adapter in self.channels.adapters.values():
            starter = getattr(adapter, "start", None)
            if callable(starter):
                starter(lambda _message: None)

    def stop(self):
        self.stops += 1

    def run_forever(self, _shutdown):
        self.run_forever_calls += 1


def _patch_process_helpers(monkeypatch):
    monkeypatch.setattr(orion_run, "CockpitCLIAdapter", _Cockpit)
    monkeypatch.setattr(orion_run, "CockpitBackend", lambda application: application)
    monkeypatch.setattr(orion_run, "_install_signal_handlers", lambda shutdown, **kwargs: {})
    monkeypatch.setattr(orion_run, "_restore_signal_handlers", lambda previous: None)


def test_interactive_run_replaces_legacy_cli_before_start_and_owns_stdin(monkeypatch):
    _Cockpit.instances.clear()
    legacy = _LegacyCLI()
    application = _Application({"cli": legacy})
    _patch_process_helpers(monkeypatch)
    monkeypatch.setattr(orion_run, "load_orion", lambda _path: application)

    assert orion_run.run("orion.toml", stop_event=threading.Event()) == orion_run.EXIT_OK

    cockpit = _Cockpit.instances[0]
    assert application.channels.adapters["cli"] is cockpit
    assert legacy.starts == 0
    assert cockpit.starts == 1
    assert cockpit.loops == 1
    assert application.starts == 1
    assert application.stops == 1
    assert application.run_forever_calls == 0


def test_install_cockpit_preserves_constructor_supported_cli_presentation_and_handlers(monkeypatch):
    output = io.StringIO()
    usage = object()
    legacy = CLIAdapter(
        prompt="configured ❯ ",
        output=output,
        style=False,
        banner=False,
        name="Configured Orion",
        model="configured/model",
        history_path=None,
        markdown=False,
        timestamps=False,
        slow_request_seconds=7.5,
        usage_provider=usage,
    )
    def status_provider():
        return {"configured": True}

    legacy.set_status_provider(status_provider)
    application = _Application({"cli": legacy})

    class PresentationCockpit:
        name = "cli"
        instance = None

        def __init__(
            self,
            backend,
            *,
            input=None,
            output=None,
            prompt=None,
            style=None,
            banner=None,
            name=None,
            model=None,
            markdown=None,
            timestamps=None,
            slow_request_seconds=None,
            usage_provider=None,
        ):
            self.backend = backend
            self.settings = {
                "input": input,
                "output": output,
                "prompt": prompt,
                "style": style,
                "banner": banner,
                "display_name": name,
                "model": model,
                "markdown": markdown,
                "timestamps": timestamps,
                "slow_request_seconds": slow_request_seconds,
                "usage_provider": usage_provider,
            }
            self.status_provider = None
            type(self).instance = self

        def set_status_provider(self, provider):
            self.status_provider = provider

        def start(self, _on_message):
            return None

        def stop(self):
            return None

        def send(self, output):
            return output

    monkeypatch.setattr(orion_run, "CockpitCLIAdapter", PresentationCockpit)

    cockpit = orion_run._install_cockpit(application)

    assert cockpit is PresentationCockpit.instance
    assert cockpit.settings == {
        "input": legacy.console.input_stream,
        "output": output,
        "prompt": "configured ❯ ",
        "style": False,
        "banner": False,
        "display_name": "Configured Orion",
        "model": "configured/model",
        "markdown": False,
        "timestamps": False,
        "slow_request_seconds": 7.5,
        "usage_provider": usage,
    }
    assert cockpit.status_provider is status_provider


def test_cockpit_compat_bridge_preserves_requests_stop_retry_resume_and_jobs(monkeypatch):
    legacy = CLIAdapter(output=io.StringIO(), banner=False, history_path=None)
    legacy.set_threads_provider(
        lambda: [
            {"id": "thread-1", "intent": "inspect"},
            {"id": "thread-2", "intent": "report"},
        ]
    )
    legacy.set_trace_provider(
        lambda: [
            {"id": "thread-1", "event": "context.loaded"},
            {"id": "thread-1", "event": "context.loaded"},
            {"id": "thread-2", "event": "context.saved"},
        ]
    )
    application = _Application({"cli": legacy})
    application.subagents = SimpleNamespace(
        list_jobs=lambda limit=20: [
            {"id": "job-1", "objective": "inspect logs", "status": "running"},
            {"id": "job-2", "objective": "write report", "status": "done"},
        ]
    )

    class TrackingCockpit:
        name = "cli"

        def __init__(self, backend, **_settings):
            self.backend = backend
            self.callback = None
            self.sent = []

        def start(self, on_message):
            self.callback = on_message

        def stop(self):
            return None

        def send(self, output):
            self.sent.append(output)
            return output

    monkeypatch.setattr(orion_run, "CockpitCLIAdapter", TrackingCockpit)
    cockpit = orion_run._install_cockpit(application)
    assert cockpit is not None

    published = []

    def publish(message):
        published.append(message)
        return SimpleNamespace(id=f"event-{len(published)}")

    cockpit.start(publish)
    assert callable(cockpit.callback)
    cockpit.callback(
        InboundMessage(
            channel="cli",
            source="cli",
            payload={"text": "first request"},
            correlation_id="cli-natural",
            text="first request",
        )
    )

    requests = cockpit.backend.execute("/requests")["data"]
    assert len(requests) == 1
    first_id = requests[0]["request_id"]
    assert requests[0]["state"] == "running"
    assert requests[0]["text"] == "first request"

    stopped = cockpit.backend.execute(f"/stop {first_id[:8]}")
    assert stopped["data"]["stopped"] == [first_id]
    assert cockpit.backend.execute(f"/requests {first_id[:8]}")["data"][0]["state"] == "canceled"

    late_output = SimpleNamespace(
        event_id="event-1",
        correlation_id="cli-natural",
        output_id=None,
        idempotency_key=None,
        metadata={},
        content="late answer",
    )
    assert cockpit.send(late_output) is late_output
    assert cockpit.sent == []

    retried = cockpit.backend.execute(f"/retry {first_id[:8]}")
    retry_id = retried["data"]["request_id"]
    assert published[-1].text == "first request"
    assert retry_id != first_id

    retry_message = published[-1]
    failed_output = SimpleNamespace(
        event_id="event-2",
        correlation_id=retry_message.correlation_id,
        output_id=None,
        idempotency_key=None,
        metadata={"error": True},
        content="provider failed",
    )
    cockpit.send(failed_output)
    assert cockpit.sent == [failed_output]
    assert cockpit.backend.execute(f"/requests {retry_id[:8]}")["data"][0]["state"] == "failed"

    resumed = cockpit.backend.execute(f"/resume {retry_id[:8]}")
    assert resumed["data"]["state"] == "running"
    assert published[-1].text == "first request"
    assert published[-1].metadata["parent_request_id"] == retry_id

    jobs = cockpit.backend.execute("/jobs job-1")
    assert jobs["data"] == [{"id": "job-1", "objective": "inspect logs", "status": "running"}]

    threads = cockpit.backend.execute("/threads thread-1")
    assert threads["data"] == [{"id": "thread-1", "intent": "inspect"}]

    trace = cockpit.backend.execute("/trace thread-1")
    assert trace["data"] == [{"id": "thread-1", "event": "context.loaded"}]

    debug = cockpit.backend.execute("/debug")
    assert debug.get("error") is None
    assert isinstance(debug["data"], dict)

    thread_help = cockpit.backend.execute("/help threads")
    assert thread_help.get("error") is None
    assert thread_help["data"]["command"] == "threads"
    assert thread_help["data"]["usage"].startswith("/threads")
    assert thread_help["data"]["description"]
    assert thread_help["data"]["available"] is True

    assert {
        "requests",
        "stop",
        "retry",
        "resume",
        "jobs",
        "threads",
        "trace",
        "debug",
    }.issubset(cockpit.backend.commands())


def test_cockpit_compat_bridge_preserves_threads_trace_debug_and_help_metadata(monkeypatch):
    legacy = CLIAdapter(output=io.StringIO(), banner=False, history_path=None)
    legacy.set_threads_provider(
        lambda: [
            {"id": "thread-1", "channel": "cli", "intent": "audit"},
            {"id": "thread-2", "channel": "telegram", "intent": "ops"},
        ]
    )
    legacy.set_trace_provider(
        lambda: [
            {"id": "trace-1", "event": "intent", "detail": "audit"},
            {"id": "trace-1", "event": "intent", "detail": "audit"},
            {"id": "trace-2", "event": "message", "detail": "done"},
        ]
    )
    application = _Application({"cli": legacy})

    class TrackingCockpit:
        name = "cli"

        def __init__(self, backend, **_settings):
            self.backend = backend
            self.callback = None

        def start(self, on_message):
            self.callback = on_message

        def stop(self):
            return None

        def send(self, output):
            return output

    monkeypatch.setattr(orion_run, "CockpitCLIAdapter", TrackingCockpit)
    cockpit = orion_run._install_cockpit(application)
    assert cockpit is not None

    threads = cockpit.backend.execute("/threads thread-1")
    assert threads["data"] == [{"id": "thread-1", "channel": "cli", "intent": "audit"}]

    trace = cockpit.backend.execute("/trace")
    assert trace["data"] == [
        {"id": "trace-1", "event": "intent", "detail": "audit"},
        {"id": "trace-2", "event": "message", "detail": "done"},
    ]

    debug = cockpit.backend.execute("/debug")
    assert debug["data"] == {"Requêtes en attente": 0, "Arrêt demandé": False}

    published = []

    def publish(message):
        published.append(message)
        return SimpleNamespace(id="event-debug")

    cockpit.start(publish)
    cockpit.callback(
        InboundMessage(
            channel="cli",
            source="cli",
            payload={"text": "pending"},
            correlation_id="cli-debug",
            text="pending",
        )
    )
    assert cockpit.backend.execute("/debug")["data"]["Requêtes en attente"] == 1
    cockpit.stop()
    assert cockpit.backend.execute("/debug")["data"]["Arrêt demandé"] is True

    help_item = cockpit.backend.execute("/help threads")["data"]
    assert help_item == {
        "command": "threads",
        "usage": "/threads [thread-id]",
        "description": "Afficher les conversations et intentions persistantes.",
        "available": True,
    }
    help_commands = cockpit.backend.execute("/commands")["data"]["commands"]
    help_by_name = {item["command"]: item for item in help_commands}
    assert help_by_name["trace"]["available"] is True
    assert help_by_name["debug"]["available"] is True
    assert {"threads", "trace", "debug"}.issubset(cockpit.backend.commands())

    assert cockpit.backend.execute("/threads --help")["data"]["usage"] == "/threads [thread-id]"
    assert cockpit.backend.execute("/trace one two")["error"] == "Usage: /trace [thread-id]"
    assert cockpit.backend.execute("/debug unexpected")["error"] == "Usage: /debug"


def test_run_configures_legacy_providers_before_cockpit_replacement(monkeypatch):
    legacy = CLIAdapter(output=io.StringIO(), banner=False, history_path=None)
    application = _Application({"cli": legacy})
    application.threads = [{"id": "thread-runtime", "intent": "continue"}]
    application.context_trace = [{"id": "trace-runtime", "event": "context"}]

    class RunCockpit:
        name = "cli"
        instance = None

        def __init__(self, backend, **_settings):
            self.backend = backend
            self.starts = 0
            type(self).instance = self

        def start(self, _on_message):
            self.starts += 1

        def loop(self):
            assert self.starts == 1

        def stop(self):
            return None

        def send(self, output):
            return output

    monkeypatch.setattr(orion_run, "CockpitCLIAdapter", RunCockpit)
    monkeypatch.setattr(orion_run, "CockpitBackend", lambda app: app)
    monkeypatch.setattr(orion_run, "load_orion", lambda _path: application)
    monkeypatch.setattr(orion_run, "_install_signal_handlers", lambda shutdown, **kwargs: {})
    monkeypatch.setattr(orion_run, "_restore_signal_handlers", lambda previous: None)

    assert orion_run.run("orion.toml", stop_event=threading.Event()) == orion_run.EXIT_OK

    cockpit = RunCockpit.instance
    assert cockpit is not None
    assert cockpit.backend.execute("/threads")["data"] == [
        {"id": "thread-runtime", "intent": "continue"}
    ]
    assert cockpit.backend.execute("/trace")["data"] == [
        {"id": "trace-runtime", "event": "context"}
    ]


def test_headless_run_does_not_install_cockpit_or_take_over_process_loop(monkeypatch):
    _Cockpit.instances.clear()
    application = _Application({"telegram": SimpleNamespace(name="telegram")})
    _patch_process_helpers(monkeypatch)
    monkeypatch.setattr(orion_run, "load_orion", lambda _path: application)

    assert orion_run.run("orion.toml", stop_event=threading.Event()) == orion_run.EXIT_OK

    assert _Cockpit.instances == []
    assert set(application.channels.adapters) == {"telegram"}
    assert application.starts == 0
    assert application.stops == 0
    assert application.run_forever_calls == 1


def test_once_and_command_modes_do_not_enter_interactive_run(monkeypatch):
    interactive = []
    monkeypatch.setattr(orion_run, "run", lambda *args, **kwargs: interactive.append(True) or 99)
    monkeypatch.setattr(orion_run, "run_once", lambda *args, **kwargs: 17)
    monkeypatch.setattr(orion_run, "run_command", lambda *args, **kwargs: 23)

    assert orion_run.main(["--once", "hello"]) == 17
    assert orion_run.main(["--command", "status"]) == 23
    assert interactive == []


def test_signal_handler_sets_shutdown_and_stops_interactive_owner(monkeypatch):
    installed = {}
    monkeypatch.setattr(orion_run.signal, "getsignal", lambda signum: f"previous-{signum}")
    monkeypatch.setattr(
        orion_run.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    shutdown = threading.Event()
    stops = []

    previous = orion_run._install_signal_handlers(
        shutdown,
        on_shutdown=lambda: stops.append(True),
    )

    handler = installed[next(iter(installed))]
    handler(None, None)
    assert shutdown.is_set()
    assert stops == [True]
    assert previous


def test_main_maps_openrouter_configuration_error_to_runtime_exit(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise OpenRouterConfigurationError("missing local configuration")

    monkeypatch.setattr(orion_run, "run", fail)

    assert orion_run.main([]) == orion_run.EXIT_RUNTIME
    stderr = capsys.readouterr().err
    assert "OpenRouterConfigurationError" in stderr
    assert "missing local configuration" in stderr
