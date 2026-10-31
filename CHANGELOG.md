# Changelog

## Unreleased

Libraries in the Docker sandbox (M6 PR-11, ADR-0006 point 6):

- New `container_images.ContainerImages`: an approved lock file becomes the image `nodestro-env:<key>` (key: SHA-256
  of lock, Python version and base image). Two-stage build: `pip download --require-hashes --only-binary=:all:
  --no-deps`, then an offline `pip install --no-index --require-hashes`; the image runs as `nobody` and carries the
  label `org.nodestro.lock-sha256`. Proxy and certificate settings reach the download stage only; the docker CLI
  gets no provider keys. Build failures are explained as `deps.wheels_required`, `deps.hash_mismatch`,
  `sandbox.unavailable` or `deps.image_failed`.
- `PythonEnvironments` with the Docker sandbox: the installation builds the image and verifies the needed modules in
  it (`deps.module_missing` removes the image); `sandbox()` returns a container sandbox for the image
  (`LocalImageSandbox`, `docker run --pull never`) and never a host process. `deps.docker_unsupported` is gone:
  untrusted tools may use libraries in the Docker sandbox. `isolation()` is `container` there.
- Locks are compiled with `--generate-hashes`; `parse_lock` reads multi-line entries. An environment approved
  without hashes needs a new approval in the Docker sandbox (reason `hashes`, error `deps.lock_without_hashes`).
- Removing an environment also removes its image.
- CI job `docker`: builds a real image and runs a tool in it without network.

Following worker runs from API processes (M5 PR-10, ADR-0004). Nothing changes for the desktop:

- `RunManager.remote_events` (set by `jobs.QueuedRuns.attach`): `subscribe` and `is_live` follow a run that a
  worker executes through `nodestro_run_events`, and `wait` waits for its stored state. A stream ends with the run's
  `run_finished` or `run_paused` event, or when the run stops being queued or running (withdrawn, interrupted).
- In queue execution, a paused run whose decision was recorded is stored as `queued` again until a worker takes it.

Jobs and workers (M5 PR-10, ADR-0004). Nothing changes for the desktop:

- Migration `0004`: `nodestro_jobs` (at most one queued or running job per piece of work), `nodestro_run_events`
  (event JSON as text, so U+0000 survives) and `nodestro_schedule_firings`.
- `jobs.JobQueue`: enqueue, claim with `FOR UPDATE SKIP LOCKED` and a lease, heartbeat, finish, cancel (a queued
  job is withdrawn, a running one gets `cancel_requested`), reaping of expired leases (`interrupted`) and cleanup.
- `jobs.RunWorker` executes claimed runs, renews leases, passes cancel requests to the run, stops a run whose lease
  was lost, and writes the run's events through the buffered `jobs.PostgresEventLog`. `jobs.reap` marks the runs
  of dead workers as `run.interrupted`; they are never executed again by themselves.
- `jobs.QueuedRuns` connects an API process's `RunManager` to the queue. New `RunManager` hooks: `dispatch`,
  `remote_cancel` and `event_sink`, plus `execute_claimed`, `interrupt` and `cancel_unclaimed`.
- `jobs.LeaderLock` (session advisory lock, released when the leader's connection ends) and
  `jobs.ScheduleFirings` (each schedule slot fires once per tenant).
- A workspace on tenant storage no longer takes the data directory lock or recovers runs and Python installations
  when it opens: other processes of the server work on the same tenant at the same time.
- Only runs go through the queue so far. Batches, test series, assistant drafts and Python environment
  installations still run in the process that started them.

Tenant workspaces and identity records (M3 PR-9, ADR-0003). Nothing changes for the desktop:

- `Workspace(data_dir, storage=WorkspaceStorage(...))` runs a workspace on the stores of one tenant instead of its
  own SQLite file; `postgres_tables.tenant_storage(database, tenant)` builds them. The data directory then holds only
  that tenant's files. `Workspace.tenant_id` is `local` on the desktop. `Workspace.store` is typed as
  `DocumentStore`; the SQLite file of the desktop is `Workspace.sqlite_store` (`storage.not_sqlite` for a tenant).
- A workspace on handed-in storage uses the server sandbox policy: tool code runs only in containers, a stored
  `subprocess` setting (for example from an imported desktop) is ignored, and changing it is refused before
  anything is stored (`sandbox.unavailable`).
- Migration `0003`: tenants, users (OpenID Connect issuer and subject), memberships with the roles `owner`,
  `admin`, `editor`, `operator` and `viewer`, sessions, login states and bootstrap tokens.
- `identity_store.IdentityStore`: creates tenants and users (no default user, no passwords), keeps at least one
  owner per tenant, issues sessions that end after a fixed time, after idling and with the membership, takes OIDC
  login states once, and binds a tenant's first owner with a one-time bootstrap token. Session tokens, login states
  and bootstrap tokens are stored only as SHA-256 hashes.

Knowledge, search and memory on PostgreSQL, and the desktop-to-server import (M3 PR-8, ADR-0002). Nothing changes
for the desktop:

- Migration `0002`: tables `nodestro_files`, `nodestro_chunks` (full-text search with a `tsvector` over the words
  of title and text, lower case and without diacritics) and `nodestro_memory_files`, each keyed by tenant.
- `postgres_tables`: `PostgresFileStore`, `PostgresIndex` and `PostgresMemory` implement Orkexa's `FileStore`,
  `Index` and `MemoryBackend` for one tenant; `knowledge_tables(database, tenant)` and
  `PostgresMemoryTables(database, tenant)` plug them into `KnowledgeBase` and `MemorySpaces`, which now accept
  `KnowledgeTables` and `MemoryTables` beside a SQLite path. `MemorySpaces.open` returns a `BrowsableMemory`.
- Search on PostgreSQL matches any query word or word prefix like on SQLite, ranked with `ts_rank` instead of
  BM25; vector search is not available there yet. PostgreSQL text cannot hold U+0000: indexed text keeps U+FFFD in
  its place, and memory files with it are refused.
- `postgres_import.import_desktop_database(path, database, tenant)` copies a desktop `nodestro.db` into an empty
  tenant in one transaction (`storage.import_target_not_empty`, `storage.import_source`). Tool provenance is not
  imported, so imported tools are untrusted; files beside the database (`tools`, `python`, `inputs`) stay behind
  and are listed in the report.
- Knowledge and memory tests run on both backends (`tests/test_knowledge_memory_backends.py`).

PostgreSQL document store (desktop and server plan, M3 PR-7, ADR-0002). Nothing changes for the desktop:

- New `storage_postgres`: `PostgresDatabase` (one connection, reentrant transactions, schema migrations) and
  `PostgresStore`, a `DocumentStore` for one tenant. Table `nodestro_documents` with `tenant_id` in the primary key;
  every query filters on the tenant. Tenant ids are checked (`[a-z0-9][a-z0-9_-]{0,62}`). Documents may hold any
  character: `jsonb` cannot store U+0000, so such a document also keeps its exact JSON text.
- Migrations are numbered SQL files in `nodestro_runtime/migrations/postgres` (package data), applied under an
  advisory lock and recorded in `nodestro_schema_migrations`; backup required for a database with a schema, newer
  databases refused, failing steps leave the database unchanged (`storage.*` codes as on SQLite).
- Optional dependency `nodestro-runtime[postgres]` (psycopg 3); without it `storage.postgres_unavailable`.
- The storage contract tests run on PostgreSQL too (`NODESTRO_TEST_POSTGRES`); CI uses a PostgreSQL 16 service and
  fails when the database is missing (`NODESTRO_REQUIRE_POSTGRES=1`).

Sandbox policy and trust classes (desktop and server plan, M1 PR-4, ADR-0006). **Changed behavior on purpose:**

- New `sandbox_policy` (`TrustClass`, `SandboxPolicy`, `isolation_of`) and `tool_provenance` (records of the tool
  versions a person wrote in the editor; collection `tool_provenance`). `ToolInfo.trust` reports the class.
- Untrusted code (AI-generated tools, also after approval; tool drafts; tool job tests; tools of unknown
  provenance) runs only in an isolating sandbox. Only `DockerSandbox` counts as isolating. Otherwise
  `ToolLibrary.tool`, `run`, `check` and `save_custom` refuse with `sandbox.isolation_required` and execute nothing.
  `ToolLibrary.start_job` refuses before a job starts. The agent check reports the code on the `tools` field, so
  the workflow check blocks a run before it starts.
- Without Docker on the desktop, these features are unavailable: generating tools, testing and approving tool
  drafts, and running AI-generated tools or tools stored before this release (their provenance is unknown, also
  for hand-written ones; copying their code into a new tool keeps it untrusted). With Docker they keep running.
  Tools written in the editor from now on run in the process sandbox as before.
- Untrusted code with third-party libraries is refused (`sandbox.libraries_need_isolation`); tool jobs ask the
  Coding Agent for a standard-library version and never resolve or install the libraries.
- `make_sandbox(kind, policy)` refuses the process sandbox in the server profile (`sandbox.unavailable`).
- `Workspace.reload_after_restore` forgets all provenance records: restored tools are untrusted.
- `ReleaseBundle.tool_trust` records the class of each tool at publish time (left out of the digest when empty);
  `ReleaseBundle.provenance()` and `ToolLibrary.linked_provenance()` give release libraries their provenance.
- `testing.ContainerDouble`: a test-only stand-in for Docker (not isolated).

Storage seams (desktop and server plan, M1 PR-3). No change of data, schema, SQL statements or behavior:

- `nodestro_runtime.storage.DocumentStore`: a typed protocol with the existing operations (`put`, `put_raw`, `get`,
  `get_raw`, `list`, `find`, `items`, `list_prefix`, `delete_prefix`, `delete`, `transaction`, `close`, `closed`)
  and the new `search_raw(collections, text)`. The SQLite class is now `SQLiteStore`; `Store` stays as an alias, so
  imports and type hints keep working. Runtime components declare `DocumentStore`.
- No module outside `storage` and the new `sqlite_tables` uses a database connection any more (a test checks it):
  input-file references use `search_raw` (same `LIKE` query), knowledge chunk counts, the file id of chunks and the
  indexed text use `sqlite_tables.KnowledgeChunks`, memory totals and clearing a space use
  `sqlite_tables.memory_totals`/`clear_memory`, each with the statement used before.
- Schema versions: `PRAGMA user_version` (0, as every existing database) is checked when a workspace opens.
  `SQLiteStore.migrate()` runs the steps of `storage.MIGRATIONS` (none in this release) after a mandatory backup
  (`Workspace(before_migration=...)`, default: a copy of the database in `<data_dir>/backups/`), all in one
  transaction. New codes: `storage.schema_too_new`, `storage.migration_missing`, `storage.backup_required`,
  `storage.backup_failed`, `storage.migration_failed`. A refused workspace releases the data directory again.
- `SQLiteStore.snapshot(path)` (online backup) and `SQLiteStore.maintenance()` (the connection with the store held)
  give the API's backup and restore a public way to do what they did with private attributes (follow-up PR in
  nodestro-api).
- Contract tests (`tests/test_storage_contract.py`) for every store operation, transactions, concurrent writers,
  WAL readers, migrations with failing steps and backups, FTS5 search after attaching file ids, memory totals, and
  a data directory written like Nodestro 0.1/0.2 that opens without any change.

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
