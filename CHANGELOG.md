# Changelog

## Unreleased

Reliability round: execution, streaming, recovery, storage and security. No data migration; stored workflows,
runs, settings and memory stay compatible, and the public Python API is unchanged (only additions).

- The event history of a finished run is released: a long-running server (schedules, batches, published
  APIs) kept up to 5,000 events per run in memory. CI uses current actions (Node 24) and checks out without
  stored credentials.
- Events: every event of a run gets a strictly increasing `seq`. Before, the number was derived from the
  length of the kept history, so after 5,000 events text deltas and the following events shared numbers and
  a reconnecting client could skip events.
- Events: a subscriber that falls 10,000 events behind is ended instead of buffering without limit. A paused
  run keeps only its event counter (its numbering continues on resume), not its history.
- Lifecycle: runs remember their own event loop; `start`, `resume`, `decide` and `cancel` called from another
  thread are carried out on that loop; calling them without any event loop raises `run.no_event_loop` before
  anything is stored (before, a run could be stored as queued, or an approval recorded, and then fail).
- Lifecycle: `RunManager.shutdown()` waits for all runs together and stores a run that does not stop in time as
  cancelled (before, it stayed `running` in the database). New `Workspace.aclose()`, `async with Workspace(...)`
  and `with Workspace(...)`; `Workspace.close()` is idempotent and cancels runs that are still executing.
- Recovery: interrupted runs are found by status, however many runs are stored (before, only the 5,000 most
  recently updated runs were checked). Runs executing in this process are never marked as interrupted.
- Recovery: interrupted Python environment installations are repaired at start: the last working environment
  is restored when the swap was interrupted, staging directories are removed, and an installation still marked
  as running is marked as failed (`deps.install_interrupted`) so it can be started again.
- Budgets: a paused run that continues counts the known part of its earlier cost against the budget even when
  its total cost is unknown.
- Storage: `Store.transaction()` groups writes. Saving a workflow and recording its version is one
  transaction, and two saves at the same time no longer get the same version number. `Store.find()` selects
  documents by a top-level field.
- Workflow check: duplicate node ids are refused (`workflow.node_duplicate`); before, one of the nodes was
  silently dropped from the run.
- Security: `uv` runs with an allowlist of environment variables (system, locale, temp, proxy, certificates,
  `UV_*`, compiler settings). Before, every variable except `ANTHROPIC_API_KEY` and a few Python variables
  reached uv and the build scripts of packages built from source (`OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `GITHUB_TOKEN`, cloud credentials ...). uv's output and the error texts stored with runs are redacted.
- Python environments: approving or installing the same environment twice at the same time is refused
  atomically.
- Tool jobs: a decision is taken once; a second decision can no longer override the first.
- Integrations: an installed integration that does not fit (other contract version, a tool name that is
  taken, also by a built-in tool) is logged and left out instead of stopping the runtime. Integrations passed
  explicitly to `Workspace(integrations=...)` are still checked strictly.
- Knowledge: an agent's search widens the search in the shared index until it finds its own files; before, in a
  large knowledge base, other files could fill all candidates and the agent found nothing.
- Documents: worksheets with a non-numeric row reference are read instead of failing.
- Documentation, CI and provenance in English; README with an offline quickstart that the tests run.

## 0.2.0 (2026-10-29)

- First version as a repository of its own: extracted from `nodestro` 0.1.0 (commit 15e2076, now
  `nodestro-legacy`) without new features and without behavior changes. The origin of every file is listed in
  PROVENANCE.md.
- Dependencies pinned to exact commits (Orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); the tested
  combination is recorded in `nodestro-studio/platform/versions.json`.
- CI with the repository secret `NODESTRO_READ`.
- New: `Workspace` as the assembly of the runtime for one data directory, `extensions` (contract 1 for
  integrations), `testing` (simulated provider clients). OCR moved to `nodestro-integrations`.
