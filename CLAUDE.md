# Orion — notes for coding agents

Orion is an always-on, proactive personal agent. Read `docs/architecture/04-architecture.md`
before changing structure, and `docs/DECISIONS.md` for why things are the way they are. Docs are
in French; code, identifiers and comments are in English.

## Commands

- `uv sync` — install
- `uv run pytest -q` — tests
- `uv run ruff format . && uv run ruff check .` — format and lint
- `uv run pyright` — strict type check (must stay at 0 errors)
- `uv run orion doctor` — check config and storage

## Rules

- The journal (`orion.core.journal`) is append-only. Never add an update path; corrections are
  new events, forgetting goes through `redact`.
- Projections must be rebuildable from the journal alone.
- Every piece of content carries `Provenance` and `Trust`; derived trust is the minimum of inputs.
- Fail closed. Never swallow an exception: log it with context or let it propagate.
- No secrets in config files or code: use `SecretRef`.
- Every behaviour change comes with a test.
- Prompt-cache contract (`tests/test_runtime_cache.py`): each model request must extend the
  previous one byte for byte. Nothing volatile (time, retrieved memory) goes in the system
  message; journaled messages are replayed exactly as sent; the tool registry is fixed and
  sorted; every request carries a stable `session_id`. Never break these tests.
