# Local models, embeddings and semantic knowledge search

Nodestro runs agents, assistants and knowledge search on local models through [Ollama](https://ollama.com),
without a cloud API key. The model API itself comes from Orkexa (`orkexa.local`, `orkexa.embeddings`); this runtime
decides where the server comes from, which model suits which work, and how knowledge is searched.

## Where the server comes from

| Profile | Server | Configured by |
|---|---|---|
| Desktop / native | A managed Ollama: a child process on a free loopback port, own model folder (`<data dir>/local-models/models` or a chosen folder), own home, `OLLAMA_NO_CLOUD=1` | The runtime bundled with the desktop app (`NODESTRO_OLLAMA_BINARY`), or installed on request from the pinned, SHA-256-verified release archive (download or a local file) |
| Nodestro Local Docker | A separate Ollama container on an internal network | `NODESTRO_OLLAMA_URL` (e.g. `http://ollama:11434`), `NODESTRO_OLLAMA_MODELS_DIR` (the shared model volume, for offline imports) |
| Server | No local server | The operator's endpoint: the Ollama provider's base URL, allowed by `NODESTRO_MODEL_ENDPOINTS` |

An Ollama the user already runs is detected (`LocalStatus.detected_external`) but used only when they save its
address as the Ollama provider's base URL; Nodestro never starts, stops or reconfigures it. Nothing starts or
downloads when the runtime is imported or a workspace opens: the API starts the managed server when the
application starts (`LocalModelService.start_on_launch`, unless switched off) or when an agent first needs it.

`Workspace.local_models` (`LocalModelService`) gives the status (`status()`), the installed models with the
capabilities they report in `/api/show`, recommendations, and jobs with progress and cancel: `pull` (network),
`install_runtime` (download or archive), `import_package` (an offline model package, SHA-256 checked),
`import_gguf` (a GGUF file and an optional vision projector), `set_models_dir` (use a folder as it is, or copy the
models there, check them and switch; the old folder is kept).

## Setup and download progress

Every long operation is a job (`LocalModelService.jobs()`, `job(id)`, `cancel(id)`) with named steps. The steps
mirror what the code really does, and each step is `pending`, `running`, `done`, `skipped` (with a note why) or
`failed` (`local_progress.py`):

| Job | Steps |
|---|---|
| `setup` (`setup(archive=None, download=False, start=True)`, also behind `install_runtime`) | `check`, `prerequisites`, `download`, `verify`, `install`, `configure`, `start`, `reachable`, `hardware`, `ready` |
| `pull` | `check` (free space), `manifest`, `download`, `verify`, `finalize` |
| `build` | `template`, `weights`, `convert`, `create`, `check`, `install` |

What the setup does per mode: the managed mode installs (or uses the bundled or installed runtime and says so),
configures (`OLLAMA_MODELS`, `OLLAMA_CONTEXT_LENGTH`, `OLLAMA_NO_CLOUD`, shown in the step's detail), starts the
child process and checks it. In the container and the external mode it only checks the server and the hardware;
the other steps are skipped with a note. An Ollama the user runs is reported in `check` and is never started,
stopped or reconfigured; `POST /api/local-models/stop` refuses every mode but the managed one (`local.not_managed`).

**Measured or not.** `completed`/`total` describe the running step only when it can be measured (a download, the
SHA-256 check, a model build's conversion). Without a measure `total` is null and the interface shows an
indeterminate bar. Nothing is estimated:

- `rate` (bytes per second) is set only after 3 s of samples, over a 10 s window, and only for transfers (a checksum
  has a percentage but no rate).
- `eta_seconds` is set only once the rate is steady: at least 5 s of samples, and the last 3 s within 0.5 to 2 times
  the window's rate. `eta_scope` is `job` when the total is final and `file` during a model download.
- `stalled_seconds` is set once no new byte arrived for 5 s. Rate and ETA then fall back to null.
- `resumed_bytes` gives what an earlier attempt left. For the runtime this is the partial archive. For a model it is
  Ollama's own `-partial-N` records in the models folder, read before the pull starts, so the value is exact.

**Model downloads (Ollama 0.40).** `/api/pull` names one file (layer) at a time and gives its size only when that
file starts, so Ollama never reports the overall total up front. Before the pull the runtime therefore reads the
model's manifest from Ollama's public registry (`local_registry.download_sizes`: one HTTPS request of a few
kilobytes, logged as `manifest: N files, B bytes (registry)`). With it `PullProgress` has the size of every file from
the first one on: `total` is the whole download, `total_final=True`, and `eta_scope` is `job`; files that are already
in the model folder count as done. As soon as Ollama names a file the manifest does not list, or with another size
(another registry, a tag that moved meanwhile), the manifest is dropped for the rest of the job (logged). Without the
manifest (no internet access here, a model of another host such as `hf.co/...`) `PullProgress` sums the files seen
so far with `total_final=False`, lists each in `layers`, gives the remaining time of the current file
(`eta_scope=file`) and sets `total_final=True` at `verifying sha256 digest`, when all files are known. Ollama's own
report stays the source of `completed` in both cases.

**Errors and retry.** Error codes are kept from the bottom up:

- `local.network`: no connection, a proxy or DNS failure, or Ollama's "max retries exceeded" after about a minute of
  its own retries.
- `local.disk_full`: refused before the download when the free space is below the remaining bytes, or "no space left
  on device" while writing.
- `local.checksum_mismatch`, `local.model_missing`, `local.cancelled`, `local.interrupted`, `local.install_running`,
  `local.job_running`.

`retryable` says whether starting again makes sense. Codes that a retry cannot fix (strict offline mode,
unsupported platform, a missing file, a server profile) are not retryable. A retry continues where the job stopped:
the runtime archive with an HTTP `Range` request (`<home>/runtime/.downloads/`), a model through Ollama's partial
files. A checksum failure deletes the partial archive.

**One at a time.** The same model downloads once at a time (`local.job_running` with the running job's id). There
is one setup at a time. Across processes, an advisory file lock guards the runtime install
(`runtime/.install.lock`, `local.install_running`).

**After a restart.** Jobs are kept in the store (collection `local_jobs`, document `recent`, the last 30 finished
ones). They are written at most every 5 s, at once on a step change, and at the end. `close()` records running jobs
as `local.interrupted` with the bytes they reached (not as canceled). A crash leaves them `running` in the store,
and the next start turns them into `local.interrupted`. Both are retryable and continue from the partial files.

The interface (`frontend/src/components/Progress.tsx`, `pages/LocalModelJobs.tsx`) polls the jobs every second while
one runs and every 10 s otherwise. A job started elsewhere therefore appears without a reload.

## Missing local models (`local_readiness`)

`nodestro_runtime.local_readiness` answers one question for every caller: can this local model be used now, and if
not, why and what may this person do? `diagnose(ws, model, action=, needs=, can_install=, switchable=)` returns
`None` for a ready (or non-local) model, else a `ModelProblem`:

- `cause`, never lumped together as "missing": `model_missing`, `runtime_missing`, `server_stopped`,
  `server_unreachable`, `server_missing`, `model_name_invalid` (also Ollama cloud names), `disk_full` (catalog size
  against the free space of the model folder, partial files counted), `capability_missing` (`ModelNeeds`: kind chat
  or embedding, capabilities such as `tools` or `structured_output` from `/api/show`), `hardware_insufficient`.
- Before a download: for a model that is neither installed nor in the catalog and an action that needs capabilities,
  `local_registry.registry_facts` reads the registry manifest, config and prompt template (never the weights; not in
  the strict offline mode, not on a server, only names of Ollama's public registry; cached for an hour, failures for
  ten minutes). A capability it proves missing is reported as `capability_missing` with `from_registry=true` and no
  download is offered. Only `tools` can be proven missing there: no built-in parser (`parser`/`renderer` in the
  config) and no `.Tools` in the template, which is how Ollama derives it. Vision, thinking, completion and
  embedding need the model file's GGUF metadata and stay unknown until after the download; any registry failure is
  "unknown", never an error.
- `location` (`this_computer`, `container`, `remote` with `host`, `server` for the server profile) and `mode`.
- `actions`, computed here and only here (`_offered`): `download`, `setup_server`, `retry`, `choose_model`,
  `ask_admin`, `open_settings`. `blocked` says why no download is offered: `permission` (no install permission),
  `operator` (server profile: the operator installs), `offline` (strict offline mode).
- Facts for the dialog: catalog download and memory size, free and partial bytes, `fits`, and the running download
  (`job`, followed instead of starting another) or the last stopped one (`previous_job`, resumable).

`alternatives(ws, needs, exclude)` lists ready models that can do the step: installed local models and the allowed
models of configured cloud providers (none in the strict offline mode; a cloud model whose capability is known to be
`unsupported` is left out). `require_ready()` raises `NodestroError("model.local_unavailable", problem=...,
alternatives=[...])`; `check_override()` accepts a per-action model only if it is among those alternatives
(`model.choice_invalid`). Nothing here starts a download: with `start=False` and a stopped managed server the answer
comes from the model folder (`scan_store`), so unattended paths never start the server either.

An injected client (`client_factory` answering for `ollama`) means there is no local model server: `diagnose` returns
`None` then, as the run does not need one.

Unattended runs: `preflight._local_model_issues` reads the managed model folder when the server is not running, so a
schedule, batch or published API start is refused with `check.local_model_missing` (`params.model`) before anything
starts; nothing downloads. A run that still reaches a missing model (an external server lost it meanwhile) maps
Ollama's 404 "model ... not found" to `run.local_model_missing` (`engine.provider_problem`) instead of an API error.

`LocalModelService.pull(name, attach=True)` returns the running download of the same model (also when another
request started it a moment earlier), so a second click, tab or dialog never starts a second download.

## Which model

`local_models.CATALOG` holds the eleven suggested models with registry digest, download size, listed capabilities,
minimum Ollama version and license (checked on 2026-11-03). `recommend(task, hardware, installed)` picks the model
for a task profile (`classify`, `extract`, `write`, `document_vision`, `tool_agent`, `analysis`, `coding`, `judge`,
`orchestrator`, `architect`, `assistant`, `embedding`):

* only models with the needed capabilities (the installed model's `/api/show` values win over the listed ones);
* only models that fit the hardware: GPU memory, or RAM on a CPU (the dense 27B model is left out on a CPU);
* an installed model that is good enough wins over a better one that needs a download; the result says so
  (`quality`: `recommended`, `acceptable`, `limited`, `insufficient`; `better`; `reasons`).

`SelectionPreferences` add what the person prefers: the languages of the work (models that document all of them
come first; `languages_unverified` when none does) and a publisher region (`region`, e.g. `eu`), which decides only
between models of the same suitability and is named in the reasons (`preferred_region`,
`preferred_region_unavailable`). The region is the publisher's origin, not where data goes: every catalog model runs
on this computer. Each catalog entry records `vendor`, `region` and the documented `languages`.

European models (approved 2026-11-03): Mistral AI's `ministral-3:3b`, `ministral-3:8b`, `ministral-3:14b` and
`devstral-small-2:24b` are catalog entries marked `alternative`. Qwen stays the general standard: without a region
preference an alternative is never the choice. The setting "Prefer European models" (`Settings.prefer_european_models`,
`LocalModelService.preferences()`) sets `region="eu"`; the recommendation then suggests the best suited European model
of the same suitability, even when it still needs a download, and keeps an installed model as the first alternative.
It never chooses a weaker model, never a cloud service (every catalog model runs locally), and never overrides a model
stored on an agent: a manual choice always wins, and the strict offline mode still refuses downloads. Entries may limit
themselves to task profiles (`tasks`): `devstral-small-2:24b` is a coding model and needs a GPU that holds it. Its
context length is Mistral AI's 256K (the 393,216 some sources give is the YaRN maximum); `ministral-3:14b` is listed but not yet
verified in a run here (`notes`). The embedding standard stays `qwen3-embedding:0.6b`.

LightOn's `mdenseon:307m` (Apache-2.0, 768 dimensions, 8192 tokens) is the optional European embedding model. It is
not in the Ollama registry: `source="build"` entries are built by an orkexa recipe (`orkexa.local.MDENSEON`) from
pinned upstream files (1.2 GB weights and a 6 MB template header, each checked by SHA-256) into a reproducible GGUF,
and the model is kept only if it reproduces the recipe's reference vectors. A download in the catalog
(`LocalModelService.pull`) runs that build with `download=True`; `LocalModelService.build(name, weights=...,
template=...)` builds from files on this computer (air gap). The document and query prefixes the model was trained
with (`"document: "`, `"query: "`) come from orkexa's `KNOWN_PREFIXES` and are part of the index identity. It is
suggested only with the European preference and only while no embedding model is installed: switching an existing
index to another model means rebuilding it (`Settings.embedding_model`, then rebuild the index).

A recommendation is a suggestion: callers store the chosen model explicitly. New installations use
`ollama:qwen3.5:4b` for agents and `ollama:qwen3.5:9b` for assistants; existing installations keep the cloud
defaults they had (`Settings.model_defaults`), so an update changes no model.

While a local server answers, preflight checks each local model a workflow uses: `check.local_model_missing` (not
installed), `check.local_model_not_chat` (an embedding model as an agent) and `check.local_model_no_tools` (an agent
with tools on a model whose `/api/show` lacks `tools`). The check never starts the managed server or downloads
anything; without a running server the run itself reports a missing model.

A local model whose server reports `thinking` gets the agent's effort as `reasoning_effort`. Ollama's thinking
models have no graded effort (any level thinks, without a limit), so at effort `low` Nodestro sends
`reasoning_effort: "none"` and the model answers without thinking; `medium` (the default) and above think. Thinking
counts against `max_tokens`: on a CPU a small model can spend thousands of tokens and many minutes thinking about one
step, so fast steps (sorting, extraction, short structured answers) should run at `low`. An agent's own
`request_options.reasoning_effort` wins.

### Context window

Ollama gives a model 4096 tokens of context unless it runs on a large GPU, and the prompt, the thinking and the answer
share them: an agent then ends with `stop_reason='length'` long before its `max_tokens`. The managed server of the
desktop starts with `OLLAMA_CONTEXT_LENGTH=32768` (`NODESTRO_OLLAMA_CONTEXT_LENGTH`, 2048 to 262144); with
`qwen3.5:4b` that is about 4.8 GB of memory instead of 3.7 GB. Nodestro Local Docker sets the same in its Ollama
container. An Ollama you run yourself keeps its own setting: start it with `OLLAMA_CONTEXT_LENGTH=32768`.

`LocalStatus.context_length` says which window agents get: `configured` (the managed server and the Local Docker
container, from `NODESTRO_OLLAMA_CONTEXT_LENGTH`), `server` (an external server, read from a chat model it has loaded,
`/api/ps`) or `unknown` (an external server before it has loaded a model). The overview's advice
(`LocalModelService.advice`, `GET /api/local-models` → `advice`) warns with `local.context_small` below 16,384 tokens;
the workflow check adds `check.local_context_small` and, per agent, `check.local_max_tokens` when the answer limit does
not fit the window. Ollama's OpenAI-compatible endpoint ignores a per-request context size, so the setting belongs to
the server.

### Computers without a GPU: the CPU saving mode

Without a GPU a 4B model writes about 5 tokens per second (4 vCPU), and a thinking model at effort `medium` can think
for many minutes per step. The workspace setting `local_reasoning` decides:

- `auto` (default): when the local model server has no GPU, thinking models answer at effort `medium` without the
  thinking phase (`reasoning_effort: "none"`). Effort `high` and an agent's own `request_options.reasoning_effort` are
  never reduced, and cloud providers are never touched. The step records it (`NodeRun.reasoning_reduced =
  "cpu_saving"`, run event `reasoning_reduced`; the agent test's `reasoning_reduced`), and the workflow check names
  the agents in advance (`check.local_reasoning_saving`, info). To keep the thinking for one agent, set its effort to
  `high`; to keep it everywhere, set `local_reasoning` to `full`.
- `full`: every agent thinks as its effort says.

A quality judge (`quality.enabled`) of a local thinking model counts like an agent at effort `medium`: its effort is
Orkexa's default, not a request for thoroughness, so in the CPU saving mode it also answers without thinking, and the
step is marked even when the agent itself keeps thinking at effort `high`. A judge's answer comes in one piece (it is
not streamed), so a judge that thinks for many minutes on a CPU used to run into the client timeout.

Whether the server has a GPU: for a server on this computer (managed, Local Docker, a loopback address) this
computer's hardware decides (`detect_hardware`), because Ollama 0.40.14 reports part of a model in GPU memory
(`size_vram`) even on a computer without any GPU device. For a server on another computer `/api/ps` decides: nothing in
GPU memory means CPU, (almost) all of it means GPU, a split or no loaded model means unknown, and an unknown server never
saves. `LocalStatus.accelerated` shows the result.

Measured on 4 vCPU without a GPU (Ollama 0.40.14, `qwen3.5:4b` Q4_K_M, 32k context), one real `production-downtime`
run (German, one test case, no AI judge of the test series): with `full` 6535 s, 6 requests, 28191 output tokens;
with `auto` 893 s, 4 requests, 5157 output tokens (7.3 times faster). Both runs finished. The deterministic checks
failed in both on the same agent, which computed the downtime share without its calculator (3.31 % and 2.83 %
instead of 4.6 %); one run per mode does not show a quality difference between the modes.

Speeds are measured, never estimated: `LocalModelService.benchmark` (`POST /api/local-models/benchmark`) asks an
installed chat model one short fixed question without thinking and stores tokens per second, prompt speed, context
window and processor with the model (`InstalledModel.speed`); below 8 tokens per second the advice says
`local.model_slow`. Other hints: `local.cpu_only`, `local.memory_low` (below 8 GB), `local.model_too_large` (a model
bigger than this computer's memory).

Parallel model runs: Ollama 0.40.14 answers one request per model at a time (`OLLAMA_NUM_PARALLEL=1`) and queues the
others, and it loads another model next to it while memory allows. Parallel teams of an orchestrator therefore wait for
each other on one CPU instead of competing for it, which is what a CPU needs; Nodestro does not raise the setting.
A waiting request receives nothing until its turn comes, so the client of a local model server waits up to
`LOCAL_REQUEST_TIMEOUT` (3600 seconds) for an answer or for the next part of a streamed one, instead of the OpenAI
SDK's 600 seconds: with the SDK's default the request of the second of two parallel agents timed out after ten minutes
while the server was busy with the first and had to be sent again, and after three such attempts a step fails with
`run.provider_unreachable`. The run's own limits (its duration, budget and cancellation) still apply.

## Knowledge search

| Mode | Ranking | Needs |
|---|---|---|
| `fts` (default) | Full text (SQLite FTS5, PostgreSQL `tsvector`) | Nothing |
| `vector` | Embeddings, exact cosine similarity | The semantic index |
| `hybrid` | Both, reciprocal rank fusion | The semantic index |

The workspace setting (`knowledge_retrieval`, `embedding_model`, default model `qwen3-embedding:0.6b`) applies to
every agent whose `KnowledgeConfig.retrieval` is `default`. The semantic index exists only after a person built it
(`KnowledgeBase.start_reindex`); it belongs to one embedding model identity (model, digest, dimensions,
instructions). Choosing another model shows `reindex_required`; searches keep using the complete current vectors
until the reindex to the new model finished and switched in one transaction. A reindex can be cancelled and
continues where it stopped. On the desktop it runs in a thread of the process that started it; on a server with a
job queue the API process only queues it (`jobs.QueuedReindex`, job kind `reindex`, at most one per tenant on the
whole server) and a worker process runs it with a lease (`jobs.ReindexWorker`), reporting its progress in the job;
every API process reads the same job, and a worker that dies leaves it `failed` with
`knowledge.reindex_interrupted` (started again, it continues). A suspended tenant's reindex does not start, or stops. Files uploaded while the model server is down are indexed for full text and reported as
`incomplete` until the next reindex embeds them. Vector and hybrid search never fall back to full text: a missing
index is a preflight error. The index state is `unavailable` only when the storage cannot hold vectors (`detail` says
why): a SQLite installation without sqlite-vec (no wheel for Windows on ARM), where a reindex is refused with
`knowledge.vectors_unavailable`, or a PostgreSQL schema without the vector tables. PostgreSQL without pgvector is not
`unavailable` (the runtime ranks the vectors itself).

An agent's view (`ScopedIndex`) limits the candidates to the agent's files *before* ranking, in every mode, so
other files never take their places. On PostgreSQL every query is tenant-scoped; pgvector ranks the vectors where
installed (migration `0010` installs it when available), the runtime ranks the same rows otherwise.

**pgvector installed later.** Migration `0010` installs pgvector only if the PostgreSQL server has it at that moment
and the migrating role may create it (pgvector is not a trusted extension). `storage_postgres.pgvector_status(database)`
tells an operator where a database stands (read-only): `active` (pgvector ranks), `installable` (the server has
pgvector, this database does not use it: run `CREATE EXTENSION vector SCHEMA "<schema>";` as a superuser),
`other_schema` (installed in a schema the connection's `search_path` does not include, e.g. `public` while the server
runs with `NODESTRO_DATABASE_SCHEMA`: `ALTER EXTENSION vector SET SCHEMA "<schema>";`, unless other applications of
that database use it there) or `unavailable` (install the pgvector package on the PostgreSQL server first). It
returns the `command` and an English `message`. Each knowledge index checks pgvector once, so the API and worker
processes rank with it after their next restart (`PostgresIndex.refresh()` makes an open index check again; the
API does not call it yet).
The vectors themselves need no reindex: they are stored as `real[]` either way.

**Scores and query words (the same on SQLite and PostgreSQL).** Both backends meet orkexa's index contract
(`orkexa.testing.conformance`, run against the PostgreSQL classes by `tests/test_orkexa_conformance.py`):

- `vector`: the score is the cosine similarity rounded to 6 digits, with or without pgvector and whether or not
  the model's vectors are normalized; equal scores go to the lower chunk id. `fts`: the score is `1 / (60 + rank)`
  (the full-text engine only orders the hits). `hybrid`: the index's fusion of both (`weighted` or `rrf`).
- A model that returns NaN, infinity or a vector of another length is refused (`VectorError`: `vector.invalid`,
  `vector.dimension`); nothing is stored. A zero vector has no direction: it is stored but never ranked, and a zero
  query vector finds nothing.
- Vectors that searches leave out (NaN or infinity stored by older releases, zero vectors) stay in the database;
  PostgreSQL's `vector_status()` counts them (`excluded`) and marks such a space `legacy` until a reindex replaces
  them.
- Query words are runs of letters, digits and `_`, matched as prefixes in lower case; a word with `_` finds its
  parts in sequence (`order_id` finds "order id", not "order" alone). Diacritics are folded for documents and
  queries alike (`Café` finds "cafe"). A query over 1000 characters or 64 distinct words is refused
  (`query.too_long`); a search limited to sources accepts at most 50,000.
- An index with an embedding model searches `hybrid` when no mode is given (the knowledge base always passes the
  workspace's or the agent's mode).
- A chunk deleted while a reindex embeds its batch gets no vector; the reindex completes.

## Offline

The strict offline mode (`Settings.strict_offline`) makes preflight refuse cloud models (also fallbacks, judges,
orchestrators), a model server that is not local, server tools and remote MCP servers; downloads are refused and the
managed server runs without proxy variables. Outside runs, `Workspace.connection`/`connect_model` refuse a cloud
model with the same code `check.offline_cloud_model` before any request, so assistants, the architects, run chat
answers and orchestrator decisions cannot reach a cloud provider either (also when the mode is switched on during a
run). Only Ollama counts as local: an OpenAI-compatible or vLLM endpoint is treated as a cloud model even on
`localhost`, because Nodestro cannot tell where such a server sends the data. The background price check makes no
request while the mode is on (checked each round). The managed server is not restarted when the mode is switched:
it keeps the proxy variables it started with until Nodestro restarts. Local models have no external API billing: Orkexa prices them at zero
with source `local`, and usage counts them in `Usage.local_requests`. Local operating costs (hardware, energy) are
not recorded, so an interface shows "local, no API billing" instead of a total of 0.
