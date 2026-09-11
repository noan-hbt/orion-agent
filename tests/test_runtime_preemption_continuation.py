import json

import pytest

from event_handler import Event
from runtime import AgentRuntime, RunContext
from tasks import InMemoryTaskStore, JsonTaskStore, RunStatus, TaskStatus


class InterruptControlLLM:
    def __init__(self, control_name, control_arguments):
        self.control_name = control_name
        self.control_arguments = control_arguments
        self.calls = []
        self.runtime = None
        self.interrupt_task_id = None
        self.preempted_context = None

    def tool_definitions(self):
        return []

    def complete(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        call_index = len(self.calls)
        if call_index == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.control_name}",
                                    "type": "function",
                                    "function": {
                                        "name": self.control_name,
                                        "arguments": json.dumps(self.control_arguments),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if call_index == 2:
            # This is the text-only finalization after the interrupting task's
            # control tool. The preempted context must still be untouched here.
            assert self.runtime is not None
            assert self.runtime._run_context is not None
            assert self.runtime._run_context.task is not None
            assert self.runtime._run_context.task.id == self.interrupt_task_id
            assert self.preempted_context is not None
            assert self.preempted_context.task.id != self.interrupt_task_id
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "interrupt finalized"}}
                ]
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "preempted resumed"}}
            ]
        }


def _build_preempted_runtime(control_name, control_arguments):
    store = InMemoryTaskStore()
    outputs = []
    client = InterruptControlLLM(control_name, control_arguments)
    runtime = AgentRuntime(
        llm_client=client,
        task_store=store,
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=outputs.append,
    )
    client.runtime = runtime

    original_event = Event("message", {"text": "original work"}, priority=20)
    original_task = store.create("original durable task")
    original_run = original_task.start_run(original_event.id)
    store.save(original_task)
    original_context = RunContext(
        event=original_event,
        task=original_task,
        run_id=original_run.id,
        loaded_state={"owner": "original"},
        turn=1,
        messages=[
            {"role": "system", "content": "PREEMPTED-CONTEXT-MARKER"},
            {"role": "user", "content": "continue original task"},
        ],
    )
    client.preempted_context = original_context

    interrupt_event = Event("interrupt", {"token": "urgent"}, priority=40)
    interrupt_task = store.create("interrupting durable task", priority=40)
    interrupt_task.wait_for(
        event_type="interrupt",
        payload_equals={"token": "urgent"},
        description="wait for urgent interrupt",
    )
    store.save(interrupt_task)
    client.interrupt_task_id = interrupt_task.id

    runtime._last_event = original_event
    runtime._current_task = original_task
    runtime._run_context = original_context
    runtime._run_in_progress = True
    runtime._pause_active_run(interrupt_event)

    # A preemption request may arrive while a tool is still unwinding. The
    # active bindings must remain on A until that execution has yielded.
    assert runtime._current_task.id == original_task.id
    assert runtime._run_context is original_context
    assert original_context.task.id == original_task.id
    runtime._run_in_progress = False

    return (
        runtime,
        store,
        client,
        outputs,
        original_task,
        original_context,
        interrupt_task,
        interrupt_event,
    )


def _run_interrupt_then_resume(runtime, interrupt_event):
    runtime._wake(interrupt_event)
    resume_event = runtime.wake_queue.get(timeout=0.2)
    try:
        assert resume_event.type == runtime._RESUME_PREEMPTED_EVENT
        with runtime._execution_lock:
            runtime._queued_event_ids.discard(resume_event.id)
        runtime._wake(resume_event)
    finally:
        runtime.wake_queue.task_done()


def test_interrupting_complete_task_does_not_overwrite_preempted_context_and_resumes():
    (
        runtime,
        store,
        client,
        _outputs,
        original_task,
        original_context,
        interrupt_task,
        interrupt_event,
    ) = _build_preempted_runtime("complete_task", {"summary": "urgent work done"})

    _run_interrupt_then_resume(runtime, interrupt_event)

    saved_interrupt = store.get(interrupt_task.id)
    saved_original = store.get(original_task.id)
    assert saved_interrupt.status is TaskStatus.COMPLETED
    assert saved_original.status is TaskStatus.RUNNING
    assert runtime.preempted_runs == 0
    assert runtime._run_context is original_context
    assert original_context.task.id == original_task.id
    assert original_context.loaded_state == {"owner": "original"}
    assert "PREEMPTED-CONTEXT-MARKER" in json.dumps(client.calls[2]["messages"])
    original_run = next(run for run in saved_original.runs if run.id == original_context.run_id)
    assert original_run.status is RunStatus.COMPLETED


def test_interrupting_wait_for_event_does_not_overwrite_preempted_context_and_resumes():
    (
        runtime,
        store,
        client,
        _outputs,
        original_task,
        original_context,
        interrupt_task,
        interrupt_event,
    ) = _build_preempted_runtime(
        "wait_for_event",
        {
            "event_type": "later",
            "payload_equals": {"token": "resume-interrupt-task"},
            "description": "wait for later",
        },
    )

    _run_interrupt_then_resume(runtime, interrupt_event)

    saved_interrupt = store.get(interrupt_task.id)
    saved_original = store.get(original_task.id)
    assert saved_interrupt.status is TaskStatus.WAITING
    assert saved_interrupt.waiting_for[0].event_type == "later"
    assert saved_original.status is TaskStatus.RUNNING
    assert runtime.preempted_runs == 0
    assert runtime._run_context is original_context
    assert original_context.task.id == original_task.id
    assert original_context.loaded_state == {"owner": "original"}
    assert "PREEMPTED-CONTEXT-MARKER" in json.dumps(client.calls[2]["messages"])


@pytest.mark.parametrize("control_name", ["complete_task", "wait_for_event"])
def test_resume_preempted_task_refuses_mid_run_context_swap(control_name):
    arguments = (
        {"summary": "done"}
        if control_name == "complete_task"
        else {"event_type": "later", "payload_equals": {"job_id": "job-1"}}
    )
    runtime, _, _, _, original_task, original_context, _, interrupt_event = (
        _build_preempted_runtime(control_name, arguments)
    )
    interrupt_task = runtime.task_store.find_waiting_task(interrupt_event)
    interrupt_task.resume_from_wait(interrupt_event.id)
    interrupt_run = interrupt_task.start_run(interrupt_event.id)
    interrupt_context = RunContext(
        event=interrupt_event,
        task=interrupt_task,
        run_id=interrupt_run.id,
        loaded_state={},
    )
    runtime._current_task = interrupt_task
    runtime._run_context = interrupt_context
    runtime._run_in_progress = True

    assert runtime.resume_preempted_task() is None
    assert runtime._current_task.id == interrupt_task.id
    assert runtime._run_context is interrupt_context
    assert original_context.task.id == original_task.id


def test_preempted_resume_replay_after_second_crash_rebinds_durable_run(tmp_path):
    event_path = tmp_path / "runtime-events.sqlite3"
    task_path = tmp_path / "tasks.json"
    tasks = JsonTaskStore(task_path)
    original = Event("message", {"text": "original"}, priority=20)
    task = tasks.create("preempted durable task")
    run = task.start_run(original.id)
    task.pause(run_id=run.id, reason="urgent", interrupted_by="urgent-event")
    tasks.save(task)

    resume = Event(
        AgentRuntime._RESUME_PREEMPTED_EVENT,
        {"task_id": task.id, "run_id": run.id, "interrupted_by": "urgent-event"},
        priority=20,
        source="runtime",
        metadata={"internal_event": True},
        id=f"resume:{run.id}:urgent-event",
    )
    first = AgentRuntime(
        task_store=tasks,
        durable_path=str(event_path),
        action_ledger_path=":memory:",
        history_enabled=False,
    )
    first.receive_event(resume)

    # Fault injection: the resume mutation reached task storage, then the
    # process died before the durable resume receipt could be ACKed.
    persisted = tasks.get(task.id)
    assert persisted is not None
    persisted.resume(run_id=run.id, event_id=resume.id)
    tasks.save(persisted)
    first._durable_store.close()

    restarted_tasks = JsonTaskStore(task_path)
    restarted = AgentRuntime(
        task_store=restarted_tasks,
        durable_path=str(event_path),
        action_ledger_path=":memory:",
        history_enabled=False,
    )
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None and processed.id == resume.id
        saved = restarted_tasks.get(task.id)
        assert saved is not None and saved.status is TaskStatus.RUNNING
        restored_run = next(item for item in saved.runs if item.id == run.id)
        assert restored_run.status is RunStatus.RUNNING
        assert restarted.wake_context is not None
        assert restarted.wake_context.task_id == task.id
        assert restarted._durable_store.list_events()[0].status == "acked"
    finally:
        restarted._durable_store.close()
