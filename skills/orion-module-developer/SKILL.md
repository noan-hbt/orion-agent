---
name: orion-module-developer
description: Develop or update Orion modules, installable tools, channel adapters, runtime integrations, and configuration while respecting Orion's event-driven agent architecture.
metadata:
  short-description: Build modules for Orion
---

# Orion module developer

Use this skill whenever a user asks to add a capability, tool, integration,
channel, scheduler behavior, task behavior, memory behavior, or other module to
Orion. The objective is not merely to make code run: the resulting module must
fit Orion's lifecycle, be understandable by the agent, be configurable, and be
installable or updatable when it is an external tool.

## First rule: inspect the actual repository

Before changing code, read the relevant current files. Orion evolves quickly;
this skill describes its invariants and public contracts, not every current
implementation detail. Treat the source code as authoritative.

At minimum, inspect:

- `README.md` and `ORION_CORE.md`;
- `runtime.py`, `event_handler.py`, `tasks.py`;
- `openrouter_client.py`, `tool_manager.py`;
- `channels.py`, `channel_adapters.py`;
- `orion_config.py`, `orion.toml`;
- `prompt_context.py` and `context_assembler.py` when context, memory, or
  prompt behavior is involved.

Read [references/orion-architecture.md](references/orion-architecture.md) for
the current architecture map and [references/tool-packages.md](references/tool-packages.md)
for the installable tool contract.

## Orion's governing model

Orion is an event-driven, persistent agent. Its base cycle is:

```text
SLEEP → EVENT → WAKE → RUN → SLEEP
```

The event wakes Orion; the event itself does not decide whether a durable task
must be created or changed. During `RUN`, Orion decides whether to answer,
call one or more tools, create or update a task, wait for an event, schedule a
wake-up, or complete an objective.

The useful distinction is:

- an **event** is something that happened or arrived;
- a **task** is an objective that may outlive the current message;
- a **run** is one execution turn for a task or an untracked request;
- an **action** is a durable record of an attempted operation;
- an **observation** is information returned after an action;
- a **plan** is mutable guidance, never a rigid script;
- a **wait** is a declared condition that lets Orion sleep until a matching
  event arrives.

Never put task policy in an event adapter. The agent must retain the autonomy
to answer a greeting without creating a task, and to create a task for a large
or future objective even when the originating event is just a message.

## Choose the right extension point

Use an installable **tool package** for an external capability such as an API,
file operation, search provider, notification service, or domain action. This
is the default choice for new user-facing capabilities.

Use a **channel adapter** only when integrating a source or destination of
messages. It translates inbound data into an event and routes outbound
`AgentOutput`; it must not contain agent reasoning or task policy.

Change the **runtime/core** only when the behavior is a general Orion
invariant: scheduling, waiting, preemption, task transitions, context assembly,
or the agent loop itself. Avoid putting a one-service integration in the core.

Use **configuration** for paths, model choices, limits, feature flags, channel
selection, and non-secret operational behavior. Use `.env` for secrets; never
write secrets into TOML, task JSON, conversation logs, tool manifests, or
assistant output.

## Implementation workflow

For every module request:

1. Restate the intended capability and classify it as a tool, channel, runtime
   change, persistence change, or combination. Identify what must happen now,
   what must persist, and what should wake Orion later.
2. Inspect the current contracts and locate the narrowest extension point.
3. Define the contract before coding: input schema, output shape, failure
   behavior, side effects, idempotency/deduplication key, required secrets,
   configuration, persistence, and lifecycle (`start`, `stop`, or one-shot).
4. Implement the smallest coherent module. Preserve existing public APIs and
   unrelated user changes. Use Python 3.10-compatible syntax and type hints.
5. Register and expose the capability. A module that exists on disk but is not
   loaded into the active runtime is incomplete.
6. Add the required TOML configuration and `.env` variable names, without
   embedding secret values. Ensure installation does not erase existing user
   configuration or data.
7. Update the relevant README or module documentation with installation,
   configuration, examples, and limitations.
8. Verify proportionally: compile changed Python files and run focused,
   non-destructive checks. Do not generate a large test suite for every small
   change, but do verify imports, registration, schemas, and important
   persistence or lifecycle invariants.

When a request is ambiguous, make the smallest assumption that preserves the
architecture and state it in the handoff. Ask only when the ambiguity would
change the module boundary, cause an external side effect, or risk data loss.

## Contract for Orion tools

The existing client contract is the stable base:

```python
def register(client, context=None):
    client.register_tool(
        "tool_name",
        handler,
        description="What the tool does and when to use it.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        side_effect=False,
    )
```

Handlers should return JSON-serializable, concise data. They should raise a
clear exception on failure. They must not silently claim that an external
operation succeeded.

For tools that send, create, modify, delete, schedule, or otherwise cause an
external effect:

- set `side_effect=True`;
- provide stable arguments and a meaningful target so the action ledger can
  detect duplicates;
- make retries safe or explicitly report uncertainty;
- return an operation identifier and a concise outcome when available;
- do not perform the action during module import or registration.

Tool results are inserted into the next model turn and persisted in action
records. Never return a complete `Task.to_dict()` from an action, never put a
full conversation or unbounded API response in a result, and never create a
result that contains its own previous results. Return a compact observation;
store durable detail in the appropriate persistence layer and expose it via a
separate read operation.

The agent must know the capability exists. A new tool therefore needs a
precise name, description, parameter schema, and useful error messages. If the
capability has non-obvious limitations, include them in the tool description
or the relevant Orion prompt documentation; capability without discoverability
is not a usable feature.

## Task, wait, schedule, and context rules

- Let the model decide whether to call `create_task`, `bind_task`, or neither.
- Use the task store for durable objectives; do not use conversation history as
  a task database.
- Use `wait_for_event` instead of polling. Use `schedule_wakeup` for a future
  time. A waiting task should allow the runtime to sleep.
- Preserve task status, plan mutability, current state, actions, observations,
  and history when changing task behavior.
- A task can be paused by a higher-priority event and resumed later. Do not
  discard the preempted task or confuse interruption with completion.
- Keep the unified conversation journal channel-aware. Do not make CLI,
  Telegram, email, or another adapter own a separate agent memory unless that
  separation is explicitly required by configuration.
- Keep context bounded per component. Prefer projections and recent useful
  facts over injecting complete historical objects. Compaction is a safety
  mechanism, not permission to persist unlimited data.

## Installation and security

An installable tool package contains a `tool.toml` manifest and an entrypoint
module. The package manager currently supports local directories, `.zip`
archives, and URLs to `.zip` archives. Installation records the source so the
same source can be used by `update`. Users can restrict loading with
`[tools].enabled` and `[tools].disabled`.

Treat every tool package as executable code. Do not add automatic execution of
arbitrary installation scripts, and do not install dependencies or grant
permissions implicitly. If a module needs a new dependency, document it and
extend the installer deliberately. Do not load secrets into a tool context
unless the configuration explicitly defines the required secret boundary.

## Handoff requirements

When finishing module work, report:

- what capability was added and which extension point was chosen;
- files changed and configuration keys introduced;
- installation or activation commands;
- side effects, secrets, permissions, and deduplication behavior;
- verification performed and any remaining limitation.

Keep the final result actionable for a user who will copy the module into a
separate Orion installation.
