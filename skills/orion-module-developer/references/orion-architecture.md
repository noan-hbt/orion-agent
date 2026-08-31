# Orion architecture reference

This reference describes the current repository shape. Verify names and
signatures in source before implementing because Orion is under active
development.

## Flow of an interaction

```text
ChannelAdapter
    ↓ InboundMessage
ChannelRouter.receive()
    ↓ Event
EventHandler priority queue
    ↓
AgentRuntime.receive_event()
    ↓
SLEEP → EVENT → WAKE → RUN
    ↓
load task / match waiting task / consider preemption
    ↓
LLM reflection
    ↓
tool call → tool observation → next turn
    ↓
answer or control action (wait, schedule, complete)
    ↓
ChannelRouter.route(AgentOutput)
    ↓
SLEEP
```

The runtime's LLM loop is in `runtime.py`. Runtime tools are defined there;
external tools are registered on `OpenRouterClient` and merged into the
definitions sent to the model. A tool call is executed by the runtime so task
actions and the action ledger can be updated consistently.

## Main modules

| Module | Responsibility |
|---|---|
| `openrouter_client.py` | OpenRouter HTTP client, tool registration and direct tool loop |
| `event_handler.py` | Thread-safe prioritized event queue and dispatch |
| `channels.py` | Channel-neutral inbound/outbound contracts and router |
| `channel_adapters.py` | CLI, Telegram, email, HTTP/webhook and Discord integrations |
| `runtime.py` | Wake/run/sleep lifecycle, task control, tool orchestration and preemption |
| `tasks.py` | Durable task, plan, run, action, wait and history models/stores |
| `scheduler.py` | Future wake-up persistence and event generation |
| `action_ledger.py` | Persistent idempotency and duplicate-effect protection |
| `prompt_context.py` | Core prompt layers, journal and optional memory maintenance |
| `context_assembler.py` | Per-component projection, compaction, caching and context limits |
| `tool_manager.py` | Installable package discovery and dynamic registration |
| `orion_config.py` | TOML configuration and application bootstrap |

## Persistence

- `data/tasks.json`: durable tasks, plans, runs, actions, artifacts and history;
- `data/conversations.jsonl`: unified channel-aware conversation journal;
- `data/schedules.json`: scheduled wake-ups;
- `data/action_ledger.sqlite3`: idempotency records for side effects;
- `data/prompt_context.json`: mutable profile, preferences and durable memory;
- `data/installed_tools.json`: installed package metadata and update sources.

Do not use one of these stores for another concern without a migration plan.
Avoid returning their complete contents to the model.

## Prompt and model layers

`ORION_CORE.md` is the immutable core. `PromptContextStore` and
`PromptComposer` add personality, methodology, user profile, preferences and
memory according to configuration. `ContextAssembler` handles dynamic event,
task and conversation components. The main model is `[llm].model`; context
compaction uses `[context].compactor_model`; memory extraction uses
`[memory].model`.

## Integration invariants

1. Adapters translate; the core reasons.
2. Events wake the agent; they do not decide task creation.
3. Tools describe capabilities to the model and return observations.
4. Side effects are recorded and deduplicated before execution.
5. Waiting and scheduling wake Orion later instead of polling.
6. Task plans remain mutable.
7. Context and persisted action results must be bounded and non-recursive.
