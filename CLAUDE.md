# Orion — notes for coding agents

Orion is an always-on, proactive personal agent. Read `docs/architecture/04-architecture.md`
before changing structure, and `docs/DECISIONS.md` for why things are the way they are. Docs are
in French; code, identifiers and comments are in English.

## Commands

- `uv sync` — install
- `uv run pytest -q tests/test_x.py` — tests (only the files touched; a stuck test fails after
  60 s). On Windows, `uv` is in `~/.local/bin` (add it to PATH if not found)
- `uv run ruff format . && uv run ruff check .` — format and lint
- `uv run pyright --pythonplatform Linux` — strict type check (must stay at 0 errors; Linux
  like CI and the server, even from Windows)
- `uv run orion doctor` — check config and storage (`uv tool install --editable .` puts `orion`
  on PATH; without `--config` it reads `./orion.toml`, else `~/.orion/orion.toml`, D180)
- `uv run python -m evals` — behavioural evaluations on the real model (costs cents; `--model`,
  `--case`, `--repeat`, `--offline`). Add a case when Orion gets something wrong for real.

## Rules

- The journal (`orion.core.journal`) is append-only. Never add an update path; corrections are
  new events, forgetting goes through `redact`.
- Projections must be rebuildable from the journal alone.
- Every piece of content carries `Provenance` and `Trust`; derived trust is the minimum of inputs.
- Fail closed. Never swallow an exception: log it with context or let it propagate.
- No secrets in config files or code: use `SecretRef`.
- New capabilities (channels, sources, skills) are modules (`orion.modules`, D24), not code
  wired into `app.py` or `serve.py`. The core stays small.
- Everything that leaves Orion goes through `guardian.outbox.Outbox` (journaled, idempotent).
  Orion speaks first only through the initiative engine, which journals every decision.
- Tests: few, and on behaviour. A new feature gets one or two tests of what the owner would
  notice, not one per branch; a fix gets the test that would have caught it. Never assert exact
  wording, layout or counts that are not the point: they break on every change. Run only the
  test files you touched (`uv run pytest -q tests/test_x.py`), never the whole suite. A test
  that breaks for a reason unrelated to the change is simplified or deleted, not patched again.
- Prompt-cache contract (`tests/test_runtime_cache.py`): each model request must extend the
  previous one byte for byte. Nothing volatile (time, retrieved memory) goes in the system
  message; journaled messages are replayed exactly as sent; the tool registry is fixed and
  sorted; every request carries a stable `session_id`. Never break these tests.
- Parallel sessions: if another coding agent session is already working in this checkout
  (uncommitted changes you did not make, a branch you did not create), do not touch it. Work in
  your own git worktree (`git worktree add ../orion-<topic> -b <branch>`) and remove it once
  merged.

## Work tracking (Jira)

The work list lives in Jira: project `ORION` on `nerb-solutions.atlassian.net` (cloudId
`c72251bd-3128-4f30-b749-98fc18ae2206`), through the Atlassian MCP connector. It replaces
`docs/A-FAIRE.md`. Audits (ChatGPT or others) file their findings there as tickets; a ticket is
the unit of work. Jira holds the work, `docs/DECISIONS.md` still holds the why.

Statuses, and who moves a ticket into each:

- `Backlog` — proposed (audits, agents). Not to be worked on.
- `Selected for Development` — accepted by the owner, ready. Pick from here only, highest
  priority first, then lowest key.
- `En cours` — an agent is on it (assigned).
- `En revue` — PR open, waiting for the owner.
- `Terminé(e)` — the owner, after merge. Never an agent.

Labels: `pret-agent` (self-contained, an agent can take it), `a-decider` (needs an owner
decision first: never code it), `proprietaire` (the owner does it by hand), `recurrent` (never
closed, comment each run), plus an area (`memoire`, `veille`, `initiative`, `navigateur`, `web`,
`gardien`, `evals`, `ops`...). Epics group the work (P1, P2, Exploitation).

- Start: read the ticket, its epic and its links; assign it to yourself; move it to `En cours`.
- A ticket without a clear "Fait quand", or that contradicts a decision (Dxxx): comment the
  question on the ticket, add `a-decider`, move it back to `Backlog`, and stop. Do not guess.
- One ticket = one branch `ORION-<n>-<topic>` = one PR whose title starts with `ORION-<n>`.
- PR open: comment on the ticket with the link, what was done, what was not, and what was
  verified (tests run, pyright). Move it to `En revue`.
- Work found along the way goes into a new `Backlog` ticket linked to the current one, not into
  the current PR. Tickets you create: Contexte / À faire / Fait quand / Références, in French.
