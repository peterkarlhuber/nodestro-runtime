# Changelog

## Unreleased

Operating profiles (desktop and server plan, M1 PR-1). No behavior change for existing callers:

- New `nodestro_runtime.profile`: an immutable `RuntimeProfile` with the profiles `desktop` (default) and
  `server`, built with `RuntimeProfile.desktop(data_dir)` or `RuntimeProfile.from_env()` (`NODESTRO_PROFILE`,
  `NODESTRO_DATA_DIR`; empty means default, anything unknown is refused). Stable codes: `profile.invalid` (every
  problem listed in `params["problems"]`) and `profile.server_unavailable` (`params["missing"]`).
- `Workspace` takes an optional `profile`. `Workspace(data_dir, ...)` works as before and always opens the desktop
  profile, also when `NODESTRO_PROFILE` is set. A data directory that contradicts the profile, and the server
  profile (which cannot start until authentication, PostgreSQL, tenant isolation, workers and the container sandbox
  exist), are refused before the data directory is created, locked or opened. `workspace.profile` tells which
  profile is open.

Platform integration fixes (found while testing all six repositories together):

- Approvals: an approval's comment reaches later agents and sub-workflows as before, but no longer result
  nodes or decisions. Before, a comment became an extra entry of the result node (the result changed shape)
  and ended up in result files (Markdown, PDF, DOCX ...) and published API answers; a decision right after an
  approval with a comment evaluated a dictionary instead of the result. The comment stays in the approval
  record and the `approval_decided` event.
- Knowledge: a damaged, truncated, empty or password-protected PDF is refused before it is stored
  (`document.unreadable`, new `document.encrypted`) instead of failing with an unhandled error and leaving a
  half-stored entry. Any other failure while indexing a new file removes the file again.
- Events: the event counter of a paused run is stored (collection `run_event_seq`), so its numbering continues
  after a restart instead of starting at 1 again; clients reconnecting with `Last-Event-ID` no longer miss events.
- Python libraries: the libraries of a generated tool are resolved with `uv --no-build`, so no package build
  script runs before a person has seen the plan; a package without a wheel is reported as
  `deps.build_required`. Plans a person asks for resolve as before.
- New check `check.input_form_only`: an agent that reads a start node with a form through `{{input}}` gets the
  form values only, without the additional free text; the check recommends `{{run.input}}`.
- The OCR note says "1 page" and "1 word" in the singular.
- One process per data directory is now enforced: a `Workspace` locks `<data_dir>/nodestro.lock` while it is open,
  and a second one is refused with `workspace.in_use`. Before, starting `nodestro` and `nodestro-api` on the same
  data directory marked the other process's running runs as interrupted, removed its installation staging
  directories and ran every schedule twice.

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
