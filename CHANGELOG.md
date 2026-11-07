# Changelog

## Unreleased

- TODO abatement round 4 (2026-11-07):
  - Bedrock usage carries its region: `UsageEntry.region` (one ledger entry per model and region) and
    `Usage.regions` for runs, steps, orchestrator decisions, agent tests and run chat; `PricedTracker` takes it from
    the region the agent's client was built for, else the priced region, else the connection's (RT-015).
  - `local_registry`: before a download of a model outside the catalog, the readiness check reads the Ollama
    registry's manifest, config and template (never the weights; not offline, not on a server) and reports missing
    tool calling as `capability_missing` with `ModelProblem.from_registry` (RT-062). A pull reads the file sizes from
    the manifest, so its total is final and `eta_scope` is `job` from the first file on (RT-064).
  - `storage_postgres.pgvector_status`: whether pgvector ranks the knowledge vectors (`active`, `installable`,
    `other_schema`, `unavailable`) with the SQL an operator runs (RT-048).
  - Checked key figures count only computing tools of the step and the steps that feed it (never lookups or parallel
    branches); `check.value_source_no_tool` asks for a computing tool; the agent test lists unbacked figures in
    `AgentTestResult.value_warnings` (RT-054).
  - The sandbox gateway inspects or removes a `nodestro-env` tag only when the image carries the lock label (RT-037);
    the built-in sandbox also denies `quotactl_fd`, `statmount`, `listmount` and `lsm_set_self_attr` (RT-018, A6).
  - When the sandbox gateway refuses the runtime image's build (429) because another build runs, the Docker backend
    stays `checking` and its self-test is tried again by itself after 5, 10, 20, 40, then every 60 seconds (for at
    most 35 minutes) instead of reporting `docker-image` for 5 minutes; a real build failure is still `docker-image`
    (`container_images.build_busy`, `docker_status.IMAGE_BUSY`; nodestro-local-docker LD-020).
  - Tests: the container reapers of two installations on a real daemon (RT-034); the MCP token search looks for
    encoded forms and raw `bytea` values and never skips silently (RT-026, issue #63).
- TODO abatement round 3 (2026-11-06):
  - `KnowledgeBase.reindex_queue` (`ReindexQueue`): with a queue attached, `start_reindex`, `cancel_reindex` and
    `drop_vectors` go through the server's job queue; `execute_reindex` builds the index for a worker and reports
    progress. `jobs.ReindexWorker` claims `reindex` jobs with a lease and heartbeat, `jobs.QueuedReindex` is the
    queue for one tenant, and `JobQueue.report` stores a running job's progress; a job whose worker stopped is
    `knowledge.reindex_interrupted` (RT-046).
  - `Workspace.background_work()` names what runs in this process (runs, tool jobs, run chats, Python installs,
    local model jobs, reindex), so a server can tell whether an idle workspace may be closed.
- TODO abatement round 2 (2026-11-06):
  - Orkexa pinned to `1b61fda` (orkexa PR #55): `ToolRegistry.refresh()`, Bedrock prompt caching, Python 3.14
    support, and compatible endpoints that refuse redirects.
  - `ToolLibrary.registry` reads tools stored by another process with `ToolRegistry.refresh()` and keeps the same
    registry object; the rebuilding workaround from #57 is gone.
  - Bedrock Claude models that support caching map `cache_control` and `cache_ttl` to cache points after the system
    prompt, the tools and the last user message (was `not_mapped`).
  - `Workflow.origin` (`WorkflowOrigin`: example id, version, language, offline and connected connections, copy
    time): where a copied example came from. It is read-only, and saving a workflow keeps it (RT-055).
  - CI and classifiers include Python 3.14; the Landlock rule struct names its packed layout, which 3.14 asks for
    (RT-022).
- TODO abatement round (2026-11-06):
  - Agents that read content they did not write (tools, server tools, memory, files, knowledge) are told in their
    system prompt that it is data, not instructions; background material is introduced as reference data (RT-002).
  - An approval decision answers the run as it goes on (`queued`), not the ended pause (RT-005).
  - `DocumentStore.put_raw_if_absent`: an atomic insert-if-absent on SQLite and PostgreSQL (RT-045).
  - Local model requests: no SDK retries, and a read timeout from the hardware (60 minutes on a CPU or unknown
    hardware, 20 minutes on a GPU), also in exported projects (RT-058, RT-068).
  - Switching the strict offline mode restarts an idle managed Ollama with the matching proxy settings (RT-059).
  - `run.agent.MaxToolRoundsError` says what the calls show (`rounds`, `calls`, `repeated`, `tool`, `looping`,
    `local`) with advice (RT-067).
  - The sandbox gateway reads each created container back and removes it when its effective limits, isolation,
    mounts, user or image differ from the request (`503`) (RT-003).
  - `list_page(since=..., equals=...)` and `list_since` filter in the store; usage entries, the cost estimate and the
    latest run of a workflow no longer read whole collections (RT-009, RT-042).
  - `Run.cause` / `RunSummary.cause` (`RunCause`: step, label, code, params): why a run failed, set with
    `run.nodes_failed` and in the `run_finished` event (RT-012).
  - `nodestro_runtime.error_codes` and `error_codes.json`: the catalog of every problem code with its parameters;
    a test fails while it is out of date (`python -m nodestro_runtime.error_codes --write`) (RT-011).
  - The Office XML guard checks the whole part with the XML parser, also UTF-16 parts (RT-044).
  - Setup advice fits the context: exported projects name the environment variable and `.env` (`where`,
    `variables`); `check.local_runtime_missing` names the models and the API route (`models`, `route`) (RT-057).
  - A tool is refused when the name it would be stored under is reserved, not only its function name (RT-025).
  - `testing.AnswerAll` finishes orchestrations (staff, delegate once, finish) (RT-056, part).
  - README: contents table and sandbox, PostgreSQL texts brought up to the code (RT-021).
- Fixed: an Ollama 400 "invalid message content type: <nil>" in multi-step agent runs with thinking models (cause and
  fix in Orkexa). A provider that refuses the conversation now fails the step with `run.provider_rejected_messages`
  (technical text ends with a content-free outline), a too-large conversation with `run.provider_context_exceeded`;
  neither is retried.
- Settings, attachments and providers (2026-11-06):
  - Output limits of Nodestro's own AI steps are configurable (`output_limits`: setting, then built-in default,
    then capped by the model and reported when lowered); run chat answers up to 4,000 tokens (was 1,500). Agent
    options follow model capabilities instead of the provider name (`model_profile`, `config.option_unsupported`,
    `config.native_tool_unsupported`, `config.max_tokens_above_model`); `AgentConfig.thinking_budget` (Gemini 2.5);
    undocumented efforts are mapped with a warning (`config.effort_adjusted`, `config.effort_dropped`);
    `check.orchestrator_model_unsupported` (RT-061).
  - Live reasoning for every provider: run events `thinking` (with `reasoning_kind`) and `thinking_status`,
    reasoning on agent test results and live run chat answers, workspace setting `reasoning_summaries`.
  - Local model server setup is a job with named steps (`setup()`); downloads report rate and remaining time only
    when reliable, resume where they stopped, survive a restart as `local.interrupted`, refuse a full disk first
    (`local.disk_full`); container and external servers are only checked, never started or stopped.
  - `local_readiness`: why a local model is not ready (cause, location, allowed actions, alternatives);
    `pull(attach=True)` follows a running download; the unattended check reads the managed model folder; Ollama
    404 becomes `run.local_model_missing`.
  - Input files can belong to an assistant conversation; `deliver()` and `files_tool()` accept their own wording.
  - `reports.REPORT_FORMATS`: one table of result file formats (extension, media type, inline view).

- Strict offline mode: `Workspace.connection` and `connect_model` refuse every provider except Ollama with
  `check.offline_cloud_model` before any request. Assistants, architects, run chat answers and orchestrator decisions
  can no longer reach a cloud model; before, only runs were refused. OpenAI-compatible and vLLM endpoints count as
  cloud models, also on localhost. `PricingSyncService.run` takes `paused`.

- Orchestrator decisions and live run chat answers set a reasoning effort only for models that support one, so
  orchestrators and team leads work with models without one, such as GPT-4o and most OpenAI-compatible and vLLM
  servers (RT-004).

- A run whose budget decision expires while it still executes now ends `failed` with `run.budget_exceeded`
  (expired), like a paused run; before, it ended `cancelled`.

- Without sqlite-vec (e.g. Windows on ARM) the semantic index reports `unavailable` with the reason in `detail`, and a
  reindex is refused with `knowledge.vectors_unavailable` instead of failing.

- Pin orkexa `e9c2147da68db01199394f51c1b7ef90067cb4f9` (orkexa #49, audit round 3: retrieval integrity on reindex,
  non-finite vectors refused, managed Ollama lifecycle with orphan reclaim, embedding identity follows the model
  digest (`reindex_required`, handled by RT3-08), safe model builds) and axentra
  `0a1d1cc72f3312140ffb9dff73184af392e40626` (axentra #41, audit round 3: an API key's rights are bounded by its
  creator's current access, `403 api_key_creator_lost_access`).

- Continuing or restarting a run (`POST /api/runs/{id}/resume`) no longer blocks the event loop on a local model
  server (audit round 3, RT3-11): the resume plan, whose pre-check may ask the server about its models, runs in a
  worker thread, as a run start does since RT3-04.

- The local model catalog's `license` is the SPDX id only (e.g. `Apache-2.0`); where it comes from is the new code
  `license_source` (`ollama`: the manifest's license layer, `upstream`: the publisher's model card because the Ollama
  manifest has none). Before, four entries carried an English note in `license`, shown untranslated in every
  language (UX round 3, B4). An exported package's model license list no longer carries that note.

- The model catalog (`GET /api/meta`, on every page load) reads the stored reference prices once instead of once
  per catalog model (UX round 3, B1): `PricingService.view()` reads and resolves the prices for many models, and a
  stored price version is parsed only once per service (a version's prices never change). With a synchronized price
  list of about 1,500 prices `catalog.models()` took 1.7 s, now 28 ms; without one 0.5 ms as before.

- The correction pass of a checked key figure no longer runs the agent's tools again (audit round 3, RT3-03): it
  gets only the built-in computing tools (e.g. `table_totals`) and the results of the first attempt's tool calls, and
  runs without MCP servers, memory, other extra tools (only the run's file delivery stays) or a resumable pause. Before, a model that
  redid the whole task called tools with effects (an own tool, a server tool) a second time.

- CI runs the PostgreSQL tests a second time as the restricted runtime role (`NODESTRO_TEST_RUNTIME_ROLE=1`, Python
  3.13 job), as a production server connects; the two migration-backup tests that changed the schema table through
  the runtime role's connection now do it as the owner (audit round 3, RT3-06).

- Cancelling a run, or its time limit, now ends a model request that waits for a local or self-hosted model server
  at once (audit round 3, RT3-05): the turn's connections made by Nodestro (`compatible`, `ollama`, `vllm`) shut
  their sockets down when the run is cancelled, so the step ends as `cancelled` (or the run with `run.time_limit`)
  instead of holding its thread until the request timeout (an hour for local servers). Orchestrator decisions too.
  Clients injected by an embedding application are left alone. Cancelling a speed measurement of a local model ends
  the job at once as well, while the model may still be loading (RT3-07).

- An embedding model that got a new digest on the server (a pull or rebuild; audit round 3, RT3-08): the knowledge
  status now says `reindex_required` (it compared only the model name and showed `ready`), and an upload is indexed
  without vectors instead of failing with an unhandled `RetrievalError` (`reindex_required`, an HTTP 500 in the API);
  a knowledge search reports `knowledge.reindex_required` (was an unhandled error); a reindex adds the vectors.
  Prepares the orkexa pin that refreshes cached embedding identities (ORK3-07).

- A run start no longer blocks the event loop on a local model server (audit round 3, RT3-04): `launch` runs the
  pre-check and the provider check in a worker thread, and a model's `/api/show` that got no answer (timeout,
  unreachable) is not asked again for 30 seconds (also `/api/ps`). Before, starting a three-agent workflow on an Ollama
  address that accepts connections but does not answer made 17 requests on the event loop, each waiting up to the
  client's 30-second timeout, so the API served nothing else meanwhile.

- A stored tool saved before a built-in tool of the same name existed (e.g. an own `table_totals`) no longer stops
  the workspace from opening ("Tool name already registered", audit round 3, RT3-02): the built-in tool wins, the
  stored files stay on disk, a warning names the tool and `ToolLibrary.shadowed` lists it.

- Checked key figures (audit round 3, RT3-01): a `table_totals` result backs only what the tool computed (totals,
  means, extremes, shares, row counts), no longer the reference total or a column or group name it repeats from its
  input; a calculator call backs nothing when it only repeats one number in another spelling (`46_3`, `0x1cf`) or
  rounds or transforms a single number (`round(463.2)`). When the step's input has the table, the table passed to
  `table_totals` must have exactly its rows (none left out, added or changed; `table_changed`), compared by the
  values of the value column when the agent renamed or left out other columns. The check still proves provenance
  only: a calculation written out from made-up numbers passes.

- Pin orkexa 22f2910 (orkexa #47): `install_runtime` prepares the Ollama runtime of another platform
  (`find_binary(root, system)`), for Nodestro's portable packages built for a different target platform.
- A checked key figure is no longer backed by a calculator call that only repeats it (expression `463`): numbers a
  call already had in its input do not count (`table_totals` results count in full). The correction tells the agent
  to write out the whole calculation from its figures. Found in a live household-budget run, where the check agent
  reported a correct surplus that no tool had computed and the correction computed only a part of it.
- Pin orkexa 6e61862 (orkexa #46): the request for the structured answer asks for the conversation's language (a
  German workflow's analysis came back in English).
- Pin orkexa 014144b (orkexa #45): an agent on a local Ollama model with tools and a structured result now calls its
  tools. Ollama compiles the result's schema into a grammar, so before, such an agent could only write JSON and
  estimated every figure itself; the live production-downtime run with `qwen3.5:4b` failed with
  `run.value_unverified` (3.41 % instead of 4.62 %). Orkexa now offers the tools without the schema and asks for
  the structured answer in one more request.
- Checked key figures: `OutputField.source` (`model`, default, or `tool`). A number field with `tool` is accepted only
  as the result of a tool call of the step or an earlier step of the run (`nodestro_runtime.value_checks`), and a
  total from `table_totals` only over the whole table of the step's input. A value that fails goes back to the agent
  once (event `values_unverified`), then the step fails with `run.value_unverified`, so a condition never decides on
  an estimated number. Found in the RT-050 measurement: `production-downtime` reported a downtime share of 3.31 % and
  2.83 % instead of 4.62 % without calling its calculator, and the second value skipped the quality branch. Workflow
  check codes `check.value_source_type` (warning) and `check.value_source_no_tool` (error).
- New built-in tool `table_totals`: exact count, total, mean, min, max, groups in Pareto order with shares and the
  share of a reference total over a column of a Markdown or CSV table; reads `1,234.5`, `1.234,5`, `14 400` and units
  in cells. Self-contained like the other built-in tools, so the sandbox probes can run it.
- `testing.AnswerAll` computes like a careful model: an agent offered the calculator first computes the sample numbers
  with it (`computing_call`, `SAMPLE_NUMBERS`), so example workflows with checked figures still run to the end.

- Fixes found by the RT-050 measurement with a real local model on a CPU: a quality judge of a local thinking model
  answers without thinking in the CPU saving mode as well (its effort is Orkexa's default, not an explicit request; the
  step is marked `cpu_saving`), and the client of a local model server waits up to `LOCAL_REQUEST_TIMEOUT` (3600 s,
  connect 10 s) instead of the OpenAI SDK's 600 s. Before, the judge thought for longer than ten minutes in one
  unstreamed answer and failed the step after three attempts (`run.provider_unreachable`), and the request of a
  parallel branch timed out while the server worked on the other one and was sent again. `endpoint_client(...,
  timeout=)`.

- CPU saving mode for local thinking models (RT-050): new setting `local_reasoning` (`auto`, default, or `full`).
  With `auto` and a local model server without a GPU, agents at effort `medium` answer without the thinking phase
  (`reasoning_effort: "none"`); effort `high` and an agent's own `reasoning_effort` are never reduced, cloud providers
  are unchanged. The step records it (`NodeRun.reasoning_reduced`, run event `reasoning_reduced`,
  `AgentTestResult.reasoning_reduced`) and the workflow check names the agents (`check.local_reasoning_saving`).
  `Connection.reasoning_saving`, `Workspace.reasoning_saving()`, `agent_builder.reasoning_reduction()`.
- Context window and hardware of the local model server (RT-051): `LocalStatus.context_length`, `context_source`
  (`configured`, `server` from `/api/ps`, `unknown`) and `accelerated`. For a server on this computer its hardware
  decides whether models run on a GPU (Ollama 0.40.14 reports `size_vram` even without a GPU device).
  `LocalModelService.advice()` (`LocalAdvice` codes `local.context_small`, `local.context_unknown`, `local.cpu_only`,
  `local.reasoning_saving`, `local.model_slow`, `local.model_too_large`, `local.memory_low`). Workflow check warnings
  `check.local_context_small` and `check.local_max_tokens`.
- `build_inputs(name)`: the pinned upstream files of a catalog model built offline (for the build dialog).
- Measured speed of local models: `LocalModelService.benchmark()` (job kind `benchmark`) stores tokens per second,
  prompt speed, context window and processor with the model (`InstalledModel.speed`, `ModelSpeed`).
- `recommend(..., require=...)`: capabilities the work needs beyond the task profile (e.g. `vision` for a tool agent
  that reads images), for the per-agent models of example copies (EX-011).
- Hybrid knowledge search weighs full-text hits by their evidence, on SQLite (through orkexa's `SQLiteIndex`) and on
  PostgreSQL (`PostgresIndex`) alike. Reciprocal rank fusion ignored how strong a full-text match is, so with a query
  in another language than the documents a file sharing one accidental word with it (`days`, `de`, a number)
  outranked the right vector hit (the offline proof's English query found `security.md` instead of `urlaub.md`). Each
  candidate now scores the min-max normalized cosine similarity plus its full-text evidence, the squared IDF-weighted
  share of the query's words it contains (`orkexa.retrieval.fuse_hybrid`; word weights count the tenant's chunks).
  On a 120-query eval (lexical queries with codes and names, paraphrases, cross-lingual queries in six languages, hard
  negatives; qwen3-embedding:0.6b) PostgreSQL hybrid recall@1 rose from 0.48 to 0.93 (fts 0.38, vector 0.89); lexical
  queries 0.98 (fts 0.95). `fts` and `vector` are unchanged; `PostgresIndex(fusion="rrf")` keeps the earlier fusion,
  `lexical_weight=` sets the full-text share (default 0.5). Hybrid hits carry the fused score in [0, 1]. Needs an orkexa
  pin that contains `fuse_hybrid` (orkexa #44, pinned at 4ec738c).
- Pin orkexa 4ec738c (orkexa #44: evidence-weighted hybrid fusion). Earlier: pin orkexa 71f8aaf (orkexa #43: `build_model` and the `MDENSEON` recipe, embedding prefixes) with its `convert`
  extra (`gguf`). New catalog entry `mdenseon:307m` (LightOn, Apache-2.0) with `source="build"`: the catalog download
  builds it from pinned upstream files and checks its reference vectors; `LocalModelService.build()` builds it from
  local files (job kind `build`). It is an alternative, suggested only with the European preference. An alternative
  of another region than the preferred one is no longer suggested.
- Fix: the managed Ollama server starts with a 32768-token context window (`OLLAMA_CONTEXT_LENGTH`, set with
  `NODESTRO_OLLAMA_CONTEXT_LENGTH`). Ollama's default without a large GPU is 4096 tokens for prompt, thinking and
  answer together, so local agents of the example templates ended with `stop_reason='length'` after about 3,500
  output tokens although `max_tokens` was 8000.
- `devstral-small-2:24b` lists Mistral AI's 256K context.
- Local thinking models at effort `low` no longer think (`reasoning_effort: "none"`): Ollama has no graded effort,
  and a 4B model on a CPU thought until `max_tokens` ran out on classification and structured-output steps of the
  example templates. `medium` and above still think; an agent's own `request_options.reasoning_effort` wins.
- European local models: `ministral-3:3b/8b/14b` and `devstral-small-2:24b` (Mistral AI) in the verified catalog as
  alternatives (`LocalModelInfo.alternative`, task-limited entries via `LocalModelInfo.tasks`). New setting
  `prefer_european_models`: the recommendation then prefers an equally suited European model (never a weaker one,
  never a cloud service); without it Qwen stays the standard. A stored model choice always wins.
- Fix: an Ollama address the client refuses (one with credentials, saved before such addresses were refused) no
  longer makes the agent check fail with a `ConfigurationError`; the capabilities stay unknown (runtime #102 asked
  the server during the check).
- Preflight checks local models while a local server answers: `check.local_model_missing`,
  `check.local_model_not_chat`, `check.local_model_no_tools` (errors). Nothing is started or downloaded for the check.
- Fix: the agent check of a local model used no capabilities its server reports, so a local agent with tools or
  persistent memory, or with an effort (the default `medium`), was refused before the run although the run would
  have accepted it. The check now uses `/api/show` like the run, and the verified catalog's capabilities while no
  local server runs (the check never starts it). Ollama's `thinking` means `reasoning: supported`; the effort of
  such a model is sent as `reasoning_effort`, and a local model that cannot reason gets no effort instead of a
  refusal. Found by running all example templates on a local model.
- Model selection preferences (`local_models.SelectionPreferences`): the languages of the work and a preferred
  publisher region decide between equally suited local models and are named in the recommendation's reasons;
  catalog entries record `vendor`, `region` and documented `languages`.
- A client injected for Ollama (`client_factory`) counts as its configured server, so preflight no longer asks
  for the local model runtime in tests and embedding applications.

Local models, embeddings and RAG (2026-11-03, see `docs/local-models.md`):
- Local models through Ollama: `local_models` (the verified catalog of `qwen3.5:4b/9b`, `qwen3.6:35b-a3b`,
  `qwen3.8:27b`, `qwen3-embedding:0.6b/4b/8b` with digests and sizes; task profiles; hardware detection;
  `recommend()`), `local_service.LocalModelService` (`Workspace.local_models`: the managed Ollama of the desktop,
  the Ollama container of Nodestro Local Docker via `NODESTRO_OLLAMA_URL`, the operator's endpoint on a server;
  status, model list with `/api/show` capabilities, pull/import/runtime/model-folder jobs). Nothing starts or downloads
  when a workspace opens.
- The Ollama provider counts as configured when Nodestro provides the server (`ProviderStatus.local_server`);
  `Connection.agent_kwargs(model)` adds the capabilities the installed model reports (declared ones still win).
- New installations default to `ollama:qwen3.5:4b` (agents) and `ollama:qwen3.5:9b` (assistants); existing
  installations keep their cloud defaults (`installation/model_defaults`, `Settings.model_defaults`). Stored
  workflows and settings are never changed.
- Knowledge retrieval modes `fts` (default), `vector`, `hybrid`: `Settings.knowledge_retrieval`,
  `Settings.embedding_model`, `KnowledgeConfig.retrieval` (`default` follows the setting). `KnowledgeBase`
  `index_status()`, `start_reindex()`, `cancel_reindex()`, `drop_vectors()`; vectors are bound to the embedding model
  identity and switched atomically. `ScopedIndex` limits searches to the agent's files before ranking (it used to
  filter after ranking). Vector and hybrid search never fall back to full text.
- PostgreSQL migration `0010_knowledge_vectors`: `nodestro_vector_spaces`, `nodestro_chunk_vectors` (tenant-scoped);
  pgvector ranks where installed (the migration installs it when available), the runtime ranks otherwise.
- Strict offline mode (`Settings.strict_offline`): preflight errors `check.offline_cloud_model`,
  `check.offline_remote_endpoint`, `check.offline_server_tool`, `check.offline_mcp`; downloads are refused. New checks
  `check.local_runtime_missing`, `check.vectors_missing`, `check.reindex_required`, `check.index_building`.
- Local usage: `Usage.local_requests`; local models are priced by Orkexa with a zero API price of source `local` (no
  external API billing; local operating costs are not recorded), so strict price mode admits them and preflight no
  longer reports them as unpriced. The catalog lists local models with `price_status: local`.

Backend audit round 2, runtime fixes (2026-11-03):
- The `dev` extra installs `moto[server]`, so the S3 backend test of the Axentra file storage
  (`tests/test_axentra_storage.py`) runs in CI instead of being skipped (found in the joint integration run).

- OPS2-01: `migrate` of a PostgreSQL database written before migration checksums (v0.2.0-preview.1) no longer
  deadlocks with its own pre-migration backup: the `checksum` column of `nodestro_schema_migrations` is added (and the
  checksums recorded) after `before_migration` returned, so `pg_dump` on another connection never waits for the
  migration transaction's ACCESS EXCLUSIVE lock. The backup stays required and is still taken first; applied
  migrations are checked by name before it.
- RT2-01: continuing a failed run asks for confirmation also for tool calls that reached the tool and then ended with
  an error (a timeout after the request was sent may hide a completed effect). Only calls refused or still waiting
  before they ran (approval state `pending`, `denied`, `rejected`, `expired`, `cancelled`) are left out.
- RT2-02: the approvals inbox (`run_views.pending_approvals`) finds paused runs through the store's indexed status
  lookup (`find("runs", "status", ("paused",))`), tenant-scoped as before, instead of scanning the 5,000 most recently
  updated runs; an old waiting run no longer drops out of the inbox.
- RT2-03: right after a database restart or failover, writes no longer fail on pooled connections the server had
  closed. Every checkout looks at the idle connection's socket (no round trip); data or end-of-stream there means the
  server closed it (or sent a notice), so it is checked and, when broken, discarded before another one is taken.
  Nothing is ever re-sent: a write that failed after it was sent still fails.
- RT2-05 (enabler for the API's run list, API2-04): `DocumentStore.list_page("runs", model, limit=, before=,
  top_level=, summary=)` is a keyset page of runs by creation (newest first, ties by id, strictly after the cursor
  `(created_at, id)`), returning `(page, more)`; `summary=True` reads only the model's top-level fields. Indexed on
  both stores: SQLite schema version 2 (backup first, as for version 1; an older release refuses the upgraded
  database) and PostgreSQL migration `0009_created_order` (function `nodestro_created_order`, two partial indexes on
  runs). `list` is unchanged.
- RT2-06: `tests/test_worker_tool_changes.py` runs the runtime sandbox's self-test once per module and skips with its
  verdict when the self-test refuses the daemon (e.g. cgroup v1) instead of failing every test with `tool.sandbox`. In
  CI (`GITHUB_ACTIONS`), with `NODESTRO_REQUIRE_DOCKER=1` or with the new `NODESTRO_REQUIRE_SANDBOX=1` a refusal is a
  failure, so CI coverage is unchanged.
- RT2-08: the billing API client of the cost reconciliation (`reconciliation.get_json`) no longer follows redirects:
  urllib repeated the request with the admin key at whatever address a redirect named, and only the first address
  was checked against the allowlist. A redirect is now an error (`ReconciliationError`).
- NSL-13: a waiting model request of a budget decision carries the node's label (`BudgetRequest.requests[].label`,
  additive, `null` for requests stored before): the label of the node in its own workflow (a child run's node in the
  child workflow), so the decision card can name the step instead of the raw node id. `agent` stays the cost
  tracker's agent name (the node id).

Orkexa pin: `bc8c4edae637b2e53f7f63f86f84846ea2e70e43` (orkexa #40, audit round 2: sandbox and tool hardening
ORK2-01..04/06/08/09, unknown provider prefixes raise (DX-02); 2026-11-03).

Axentra pin: `a87224291b561981ed8afab6131e18d8c9eecc01` (axentra #31, #32 and #38, audit round 2: schedule jobs follow the
creator's current access, webhook deadline, `/v1/me` without tenant cap, invitation re-checks, problem type URNs;
SHA-pinned CI and digest-pinned image; own-tenant overview counts, usage backdating limited to the open billing
period; 2026-11-03).

Previous Axentra pin: `2767a2eb3705161db674fb7ba887dd15d422e12e` (axentra #29, R4-1: the GET OIDC start refuses invitations and
`return_to` tokens are never adopted; 2026-11-02).

The resolve path on a real Docker engine (RT-038; 2026-11-02):

- `tests/test_resolve_real_engine.py` runs `ContainerImages.compile_in_container` against a real daemon
  (`NODESTRO_DOCKER_TESTS=1`; a new step of the CI `docker` job): the self-test with the resolve limits before any
  resolve container, the refusal paths (unproven daemon, shape and limits mismatch), digest pinning and run by image
  id, the effective limits from `docker container inspect` and from inside the container, wheel-only resolution,
  `deps.source_build_disabled` before any docker call off the desktop, timeout, cancel, cleanup and proxy credentials.
- Fix: the resolve container is checked against the shape its sandbox's self-test ran (the sandbox's limits), not
  against `_RESOLVE_LIMITS` directly, so a sandbox that proved other limits starts nothing (`sandbox.shape_mismatch`).
- Automatic mode's refusal detail says "start Docker" only when Docker is missing or unreachable; a running Docker
  that failed a check is told so instead.
- Fix: the resolve container's `/tmp` is really executable. Docker mounts a tmpfs `noexec` unless its options say
  `exec`, so dropping `noexec` had no effect; the `exec_tmp` allowance now replaces `noexec` with `exec` in
  `_resolve_args` and in `check_shape`. `/work` stays `noexec`.
- Orkexa pin `0861c1b2b79209c388a5e996035e5676a96f6d75` (orkexa #38): the self-test reports the container's own
  environment (`/proc/self/environ`), so the coerced `LC_CTYPE` of uv's resolver image (no `LANG`) no longer refuses
  every daemon. Only that file changed since `dec66eb` (plus CI and docs in #37).

Axentra pin: `f3ed8d22a8bd45faff84204786e01da32ece4034` (axentra #28, #27: secret-free OIDC `return_to`, `POST`
OIDC start, encoded-token redaction; 2026-11-02).

Security review round 4: Lows R4-3 to R4-7 handled (2026-11-02):

- A stopped tool generation job never starts an installation of libraries (R4-7): besides the job's own check before
  `gate.install`, `PythonEnvironments` refuses to start an installation (`deps.install_stopped`, nothing stored)
  while the cancel check bound to the calling thread is set; a tool job binds its stop there. The check runs under
  the lock `cancel_installs` takes, so a stop that lands between the job's check and the start (or before `prepare`
  installs an approved environment that is missing) is caught, and an installation registered before the stop is
  found by `cancel_installs`. Documented, not changed: an approval or rejection of the tool taken before the stop is
  carried out, and a job keeps working until the API's suspension check (5 s by default) sees the suspension. Tests
  in `tests/test_tool_job_cancel.py` stop a job between the library approval and the installation, and before an
  approved environment is installed again.
- `tenant_sql_check` (R4-3 to R4-6): a changing method with arguments (`.replace()` ...) is checked on any receiver
  (`self.BASE`, `Q["a"]`, `base()`, `str.replace(BASE, ...)`), with the text of its arguments when the text it
  changes is not known; a module name is resolved to its value at the point where the module reads it, and to every
  value it is ever assigned where a function reads it; SQL comments in front of an unknown part are skipped;
  fragments that start with a comment or an operator (`"/**/OR true"`, `" IS NOT NULL OR true"`) and lower-case
  conditions in parentheses (`or (1=1)`) are reported. The real runtime and API code has no new finding. Not fixed,
  listed in `docs/AXENTRA.md` ("Remaining isolation risk"): a change whose text and arguments are both unknown, and
  text other than space, parentheses and comments in front of an unknown part. Probes: `ROUND4_PROBES` and
  `ROUND4_CLEAN` in `tests/test_tenant_filters.py`.

Static tenant SQL check: the gaps of security review round 3 closed (#89; 2026-11-02):

- `tenant_sql_check` fails closed on the remaining bypasses: adjacent constants are joined without a gap (a table
  name split as `"nodestro_" + "documents"` or after a prefix constant, SR-01d); `+=` on a parameter, on `self.q`,
  `Q["a"]` or a class attribute; closures, lambdas and `nonlocal`; `"%s OR true" % q`; items of literal containers
  (`QS[0] + QS[1]`); lower-case SQL (`or true`) and comments after an unknown part; fragments such as `" OR true"`
  outside a known statement (SR-01a/b). The SQL rules also refuse an inner alias that hides an outer one, SQL inside
  strings (`DO`, `query_to_xml`), a non-parameter `tenant_id` in `INSERT`, an upsert without `tenant_id` in its
  conflict target, and `SET tenant_id`. Every review probe is a regression test; the real runtime and API code has
  no new finding. `docs/AXENTRA.md` states what the check guarantees and the remaining isolation risk without RLS.

Stopping a suspended tenant's tool generation jobs (security review round 3, NF-1; 2026-11-02):

- `ToolLibrary.cancel_jobs(code)` stops every tool job that has not ended: it is `failed` at once with `code` as its
  `message_code` (the API passes `identity.tenant_suspended`) and never changes again. A model request not sent yet
  is not sent and a late answer is not used; a running sandbox test is killed through the job's sandbox cancel check
  (a container is killed and removed); a wait for an approval or for libraries ends at once; the tool is never
  registered (a record already proposed for approval is rejected). A decision that was already taken is carried out.
  Idempotent; returns the ids stopped. Tests: `tests/test_tool_job_cancel.py`.

Local MCP containers: deadline label 15 minutes instead of 7 days (RT-037; 2026-11-02):

- `MCP_LIFETIME` is 15 minutes. Connections are per operation and capped at `2 * timeout_seconds` (at most 300 s), so
  the label only bounds a container whose relay and hub both crashed. A test ties it to the model's maximum timeout.

Adopting AI-generated tool code as one's own, desktop only (R-4, ADR-0006 amendment; 2026-11-02):

- `ToolLibrary.adopt(name, actor=, role=, expected_hash=)` makes the current version of an `untrusted` tool
  `user-authored` after a person confirmed it: only in the desktop policy (`tool.adoption_desktop_only` in the
  server and `local-container` profiles and for a server tenant's workspace, whose trust policy is unchanged), only
  for the roles `owner` and `admin` (`tool.adoption_forbidden`), only for the shown content
  (`tool_provenance.content_hash` of code and tests, `tool.adoption_stale` with `version`), never for a tool that is
  not `untrusted` (`tool.adoption_not_needed`) or has no code (`tool.adoption_not_applicable`).
- The adoption (`ToolAdoption`: name, version, content hash, actor, role, time) is stored in `tool_adoptions` and
  counts only while that version still has that content and every later version was written in the editor. A
  later AI-written version ends it: the tool is `untrusted` again, the record is dropped and
  `ToolLibrary.on_adoption_revoked` is called. Editing an adopted tool in the editor keeps it.
- `tool_adoption.adopt_tool` writes `tool_adopted` and `tool_adoption_refused` to the tool audit trail
  (`tool_approval_audit`, mirrored to Axentra with target type `tool`); the workspace writes `tool_adoption_revoked`.
- `ToolInfo.content_hash` and `ToolInfo.adoption` (new optional fields). A restore clears adoptions with the other
  provenance records; `postgres_import` never imports `tool_adoptions`. Release libraries follow the studio's
  adoption through `linked_provenance()`.
- Unchanged: AI code edited by a person stays `untrusted` until adopted, pasted code stays `user-authored`, existing
  tools keep their class (no migration), the subprocess mode still refuses `untrusted` code, and every profile's
  `SandboxPolicy` is the same. No similarity detection (see `docs/TODO.md`).

Resolve and module-check containers checked against the proven shape (R-5, RT-038; 2026-11-02):

- `container_images.check_shape` refuses a `docker run` that is not Orkexa's `run_args` (the shape the in-container
  self-test proves) up to an explicit allowlist, right before the container starts (`sandbox.shape_mismatch`,
  nothing started). The module check (`missing_modules`) must be exactly that shape. The resolve container
  (`compile_in_container`) is now built from `run_args` and may differ only in `RESOLVE_ALLOWANCES`: Docker's default
  or one named network instead of none, `/tmp` without `noexec`, proxy variables without credentials.
- The resolve container thereby gets the proven shape's noexec `/work`, sealed `/run` and `/var/tmp` and `-w /work`;
  its `/tmp` no longer sets `uid=`/`gid=`/`mode=0700` (Docker's tmpfs default applies, as for `/tmp` of every
  sandbox container).
- `StreamedSandbox(container_limits=...)`: the resolve sandbox runs Orkexa's self-test with exactly the resolve
  container's limits (open files, file size, tmpfs and `/dev/shm` sizes included), so they are proven in the
  resolver image.
- `NODESTRO_DOCKER_BUILD_NETWORK` must name a network: `host` and namespace modes (`container:<id>`, ...) are
  refused for the resolve container (`deps.build_unavailable`) before any docker call.

Source builds only on the desktop (R-1, RT-038; 2026-11-02):

- Python packages that must be built from source are resolved in a container with unfiltered network. Outside the
  desktop profile (a server tenant, Nodestro Local Docker) that is now off by default: a plan a person accepted
  reports `deps.source_build_disabled` (parameter `name`) and no container is pulled, tested or started.
  `PythonEnvironments(source_builds=...)` and `ContainerImages(source_builds=...)` default to `False`; `Workspace`
  turns it on for the desktop profile only. Wheels-only plans and image builds are unchanged. There is no switch to
  turn it on elsewhere; RT-040 and `docs/build-network.md` describe the filtered build network it would need.

Python 3.11 minimum (2026-11-02):

- Python 3.11 is now the minimum (3.10 reached end of life in October 2026): `requires-python`, classifiers, ruff
  `py311`, mypy 3.11, CI matrix 3.11/3.13.
- `mcp` is an unconditional dependency (its 3.10 marker is gone); `axentra` keeps living only in the `postgres` and
  `dev` extras, never in the base dependencies (the desktop and Local Docker do not install it). `mcp.unavailable`
  stays as an error code for an installation without the SDK.
- Removed 3.10 workarounds: the `tomllib` fallback in `python_deps.py`, the `sys.version_info` guards in
  `axentra_platform.py`, the `pytest.importorskip("mcp")` guards of the MCP tests, the mypy override for `mcp`.
  `ruff --select UP` for 3.11 (`datetime.UTC`, `TimeoutError`).
- Pin orkexa dec66eb (orkexa #36): orkexa also requires Python 3.11.
- Python 3.14 is not added yet: RT-022.
- Docs: the M12 contract step is RT-039 (RT-036 was taken twice; RT-036 stays the Docker-first follow-ups); stale
  pin and review statements in `docs/AXENTRA.md` and RT-030 are current again (the agent security review is accepted, an
  external sign-off is still outstanding).

- Pin orkexa 3364fb8 (orkexa #34, security review DS-8, DS-9, DS-10): the Docker self-test checks the mount table,
  probes seccomp and requires an enforcing AppArmor profile where the host reports AppArmor; containers get empty
  `noexec` tmpfs mounts on `/run` and `/var/tmp`, which the sandbox gateway now allows too.

Security re-review fixes (SR-01a, SR-01b, SR-03a, SR-04a; 2026-11-02):

- SR-01a: the static tenant check resolves a name where it is read, also on its own (`query += " OR ..."`;
  `db._query(query)`, `query = query + ...`, a returned name): every value it can hold there is checked, with each
  assignment and `+=` before it (and after it in a loop around it), conditional ones taken and not taken, assignments
  of functions that declare it `global`; a `+=` that a loop repeats is an unknown part, and more than 256 variants are
  reported as dynamic. The `+=` branches of `storage_postgres` (`replace_if`, `list`) are now checked.
- SR-01b: a text that starts with an unknown part (an imported fragment, `self.BASE + "..."`, a function's result,
  a parameter) followed by SQL words is reported as dynamic instead of being dropped, unless it is allowlisted with
  its reason. `str.join` of a name holding a list (an unknown part when the list is changed), `.replace` and other
  methods that change a text, and bytes literals are rebuilt too; only the known parts of a text count as naming a
  tenant table. `docs/AXENTRA.md` describes what is resolved.
- SR-03a: `RunChat.cancel_answers(code)` fails the questions this process is answering (a request not sent yet is
  not sent, a late answer is not stored), `PythonEnvironments.cancel_installs(code)` stops installations before
  their next step; the API calls both for a suspended tenant (`identity.tenant_suspended`).
  `RunChat.begin_answer` takes the question.
- SR-04a: the start's key-bytes check also decrypts samples of Axentra's own keyring columns
  (`AxentraServices.key_samples`; `secret_key_problems` and `require_readable_secrets` take `axentra_samples`), so
  other bytes under a key id that only those columns use are refused.

Security review fixes (SR-01, SR-03, SR-04, SR-05, SR-07, SR-08, SR-09):

- `tests/test_tenant_filters.py` parses every SQL statement of the runtime and the API with PostgreSQL's grammar
  (`pglast`, a dev dependency) and requires a tenant binding for each tenant table, which it reads from the
  migrations; statements it cannot build or parse fail, exceptions are listed one by one with their reason in
  `tenant_sql_allowlist.py` (new `tenant_sql_check.py`).
- `RunWorker` takes `tenant_active`: `owns()` is false for a suspended tenant's run, so it makes no further call.
- The start checks that each keyring key and `NODESTRO_SECRET_KEY` decrypts sample stored values, and counts the key
  ids of Axentra's own encrypted columns; `AxentraServices.reencrypt_columns` / `kids_in_use`.
- Run usage settlement: a run id that does not fit an idempotency key is settled under its SHA-256; a run that fails
  is quarantined and retried while the watermark moves on.
- The volume catalog of input files is rewritten under an exclusive `flock` on `catalog.lock`.
- `AxentraFiles.verify` fails on leftovers (`orphans`, new `unreferenced`), new `AxentraFiles.cleanup` removes them;
  `FileObject.used_at`; `AxentraFiles.check` creates the local storage directory.
- Every path that creates an Axentra tenant cuts its name to 200 characters (`axentra_platform.axentra_tenant_name`,
  `NAME_LIMIT`).

Run budget decisions (BUDGET-1, 2026-11-02; see `docs/BUDGET.md`):

- An attended run whose next model request does not fit its budget pauses before the request is sent instead of
  failing: run `paused` with `run.budget.pending_request_id`, node status `awaiting_budget`
  (`WAITING_NODE_STATUSES`). Agents, quality judges (a resumable refine loop), orchestrator decisions, sub-workflows
  and teams suspend through orkexa's `RequestSuspended` into a request-stage checkpoint (`AgentCheckpoint.kind
  "budget"`); no thread or worker waits. Attended runs refuse oversized requests before the call
  (`on_oversized="reject"`).
- New `nodestro_runtime.run_budget` (`BudgetRequest`, `BudgetRequests`, `RunBudgetGate`, audit trail
  `budget_audit`) and `RunManager.decide_budget`, `budget_requests`, `sweep_budget_waits`. One open request per run,
  parallel agents join it; a raise changes only the blocking limit (`run`, `subworkflow`, `team`) of this run and is
  recorded in `run.budget.raises`; compare-and-set decisions with idempotency keys; deadline
  (`budget_decision_hours`, default 24) and caps (tenant `max_run_budget_usd`, operator
  `NODESTRO_MAX_RUN_BUDGET_USD`).
- `Run.started_by`, `Run.budget` and `RunSummary.budget`; new error codes `run.budget_ended`,
  `run.budget_checkpoint_lost` and `budget.*`. Unattended runs (schedule, batch, benchmark, api) fail as before.
- New `tests/test_run_budget.py` and `tests/test_worker_budget.py` (PostgreSQL workers, tenants, racing decisions).

Axentra pin: `024da57c15b064ea3abcf4119518707fbcc4e7b5` (re-review Lows AX-R1: malformed cursors are 400
`invalid_cursor`; AX-R2: `invitation_accepted` after single sign-on). Before: `a8ea5d535feb63fda3a2ea66701c232c093849ee` (webhook backoff settings and the setup_already_completed/email_taken codes; security review fixes SR-02/SR-04/SR-05/SR-06 with migration
0011, session resolution in one transaction, P1 fixes for suspended tenants and invitation links; replaces `f6ae01d`).

Previous Axentra pin: `f6ae01d4fe5945d56b322673202860f93e2213a9` (access definitions, OIDC on host routes, inbox channel,
shared pool, runtime roles; replaces `f114920`).

Performance (Axentra migration, requirement 15, 2026-11-02; numbers in `docs/AXENTRA.md`, "Performance"):

- Fix: the identity reads of `NODESTRO_IDENTITY_BACKEND=axentra` compared `nodestro_axentra_ids` (collation `"C"`)
  with Axentra's id columns, so PostgreSQL could not use Axentra's indexes and read every membership of the product
  on each session resolution and `/api/auth/me` (22 ms at 3,500 memberships, growing). They now filter on Axentra's
  ids (`/api/auth/me` 4.2 ms). Test: `tests/test_axentra_identity_indexes.py`.
- Fix: the worker's periodic run usage pass (`NODESTRO_AXENTRA_RUN_USAGE=1`) read the mirrored events of every run
  ever settled; it now reads only its candidate runs' events by key (0.04 s instead of 0.7 s at 10,000 runs, flat).
  Test: `tests/test_axentra_run_usage_reads.py`.
- Measured with Axentra `69c06a5` (session resolution in one transaction; needs a pin bump to take effect).

Legacy cleanup (Axentra migration M12, 2026-11-02):

- `AxentraServices.start` refuses to run without the process's database pool (`axentra.unavailable`): Axentra never
  opens a second pool in a Nodestro process. The metrics show that one pool once (`pool="nodestro"`); the duplicate
  `pool="axentra"` series of the shared pool is gone.
- Removed dead code: `AxentraServices.secret_box` (use `keyring_secret_box`), `AxentraLink.backfill_tenants`
  (`nodestro-server axentra backfill-identity` links tenants) and `AxentraLink.reverse`.
- `docs/AXENTRA.md` lists every legacy path that stays, why, and its removal precondition; RT-036 tracks the
  contract step.

Security: containers that run package code (security review DS-1, DS-5, DS-7, DS-9; RT-038):

- DS-1: the module check after an image build (`ContainerImages.missing_modules`) ran the image by tag with a plain
  `docker run` (no limits, no labels, unbounded output; after 120 s only the CLI was killed and the container kept
  running). Package `.pth` files run at interpreter start, so it now runs like a tool call: Orkexa's `run_args` shape
  (no network, read-only, no capabilities, `nobody`, memory/CPU/process/file limits) with the owner and deadline
  labels, the image by its id, only after preflight and self-test (`sandbox.unavailable` otherwise), bounded output,
  and the container is killed and removed on timeout or cancel (`deps.image_failed`). A failing check is an error,
  no longer read as "all modules missing". New `StreamedSandbox.ready_image`, `shaped_run` and `run_container`.
- DS-5: the resolve container for accepted source builds (`ContainerImages.compile_in_container`) pulls uv's image by
  its digest, checks the digest and runs it by id with `--pull never`, only after preflight and self-test for that
  image; it adds private IPC, ulimits (nofile, fsize, core), no logging, the owner and deadline labels (the reaper
  removes orphans), bounded output and removal on timeout or cancel. Proxy URLs reach it without user name or
  password (`strip_credentials`; a proxy that needs credentials no longer works for the resolver). It keeps network
  for the package index; the residual risk is documented in `docs/TODO.md` (RT-038).
- DS-7: every docker CLI call of `ContainerImages` reads at most 16 MiB of stdout and the last 256 KiB of stderr
  (`bounded_run`, the new default `run`; more kills the CLI and fails with `deps.image_failed`).
- DS-9: a Python version without a pinned base image is refused with the new code `deps.python_unsupported` instead
  of building on the unpinned `python:<version>-slim` tag; `build` also refuses a custom base that is not pinned by
  digest. `tag` still names such an image (lookups answer "not built").

Local MCP servers and the sandbox gateway (security review DS-4, DS-11, 2026-11-02):

- Fix (DS-4): a local MCP stdio server's container started whenever the setting said `docker`, without Orkexa's
  preflight and self-test, and by tag. Every start now requires the preflight and the in-container self-test of that
  image in the stdio container's exact shape (`mcp.stdio.prove`, `McpService.stdio_proof`; passed self-tests are
  remembered per daemon, image id and shape) and runs the image by the checked id; `launch` refuses a tag. A daemon
  that fails (cgroup v1, unenforced limits, unconfined seccomp) starts nothing (`sandbox.isolation_required` with the
  Docker backend's `reason`). The hub runs the proof off the event loop.
- Fix (DS-4): containers of the installation's MCP owner (`nodestro-mcp-<id>`, deadline 7 days) were never reaped.
  The Docker monitor's reaper now removes them (`DockerMonitor.reap_mcp_once`): at the start of a process that holds
  the data directory lock every one started before the reaper, and every 60 s those past their deadline (a server's
  processes: only expired ones). Other installations' containers carry another owner and are never listed.
  `docker_status.MCP_LIFETIME` is the stdio containers' deadline.
- Fix (DS-11): the sandbox gateway passed up to 64 request environment variables into the container; none are passed
  now. A `nodestro-env:<key>` tag is accepted for a container only when the image carries the lock label, and the
  container is created by the checked image id. The daemon's limits and seccomp profile are confirmed again after
  `LIMITS_TTL` (5 s, the runtime's preflight interval) instead of once per gateway process.

Docker-first sandbox selection and library environments (security review DS-2, DS-3, 2026-11-02):

- DS-3: in the desktop's `auto` mode untrusted (AI-generated) code runs only in Docker once its preflight and
  self-test passed; otherwise it is refused with Docker's reason (`SandboxSelection.reason` is now e.g.
  `docker-unreachable` or `docker-checking` instead of `no-isolation`). The built-in Linux sandbox is no longer chosen
  automatically, not even when Docker is missing. New sandbox mode `linux` (`SettingsIn.sandbox`, `Settings.sandbox`,
  `SandboxSelection.mode`; additive): the only mode that uses the built-in Linux sandbox. `subprocess` never runs
  untrusted code (refused with reason `subprocess-mode` at call time, `no-isolation` in the selection); a stored
  `subprocess` choice is kept, so an installation that relied on it for the built-in sandbox has to choose `linux`.
  New `ToolLibrary.sandbox_mode` and `ToolLibrary.docker_status` (set by the workspace).
- DS-2: untrusted code's Python libraries never become a host virtual environment: no `uv pip sync` and no
  `verify_imports` on the host (whose interpreter runs the packages' `.pth` files as the user). New
  `PythonEnvironments.backend()`/`scope_trust()`: in `auto` mode such scopes are container images, built and
  import-checked in containers and refused with `sandbox.isolation_required` (Docker's `reason`) while Docker is not
  proven; in `subprocess` and `linux` mode they are refused with `sandbox.libraries_need_isolation` (status
  `unsupported`). Scopes whose code is a person's own keep host environments. A host environment built for a person's
  code is no longer handed out once AI-generated code needs it. The built-in Linux sandbox always starts Nodestro's
  own interpreter, never an environment's (whose `.pth` files would run before confinement), so untrusted code gets no
  third-party libraries in `linux` mode. The `auto` container fallback also replaces a host environment's interpreter.
  `status`, `plan`, `install`, `ensure`, `sandbox` and `ToolEnvironment` take an optional `trust` (additive); tool
  jobs plan as `untrusted`. `PythonEnvironments` takes `docker_state`.
- New `tests/test_docker_first.py`; selection, routing and built-in sandbox tests adjusted to the new policy.

Run queue delivery semantics (Axentra migration M11 / G-8, 2026-11-02):

- Decision: runs stay on Nodestro's own queue permanently (M11 closed by architecture decision; reasons and evidence
  in `docs/AXENTRA.md`, "What stays Nodestro's").
- Fix: a worker whose heartbeat stalled could still call the model or make a tool call with outside effects after
  the reaper had interrupted its run (the loss was noticed only at the next heartbeat or write). A queue execution
  now asks whether its worker still holds the run's job before every model request and every tool call
  (`RunManager.owns`, set by `RunWorker`; a failing check counts as lost).
- New `tests/test_job_delivery.py` (real PostgreSQL): SIGKILL during a model call, stalled worker, external write
  before a lost lease, parallel workers, cancel and resume, nested sub-workflows.

Identity completion (Axentra migration M5, risks 7 and 10; off by default):

- Suspended tenants: migration `0008_tenant_status` (`nodestro_tenants.status`), `IdentityStore.set_tenant_status`,
  `tenant_active`, `Tenant.status`, `Membership.tenant_status`. A suspended tenant's sessions, new sessions and setup
  links are refused with `identity.tenant_suspended`; with Axentra identity both sides are written, and in `axentra`
  mode a suspension on either side closes the tenant. Backfill copies suspensions (never resumes one), reconcile
  reports status differences.
- Versioned access definitions: Nodestro's permissions `nodestro.workspace:read|edit|run|admin` and the roles
  `editor`/`operator` are applied with Axentra's `ax.access_definitions` (`ACCESS_REVISION`, content hash pinned by a
  test) instead of creating roles at every start; a conflicting revision stops the start (`identity.access_conflict`).
  In `axentra` mode `Session.permissions` comes from Axentra's role definitions. Needs Axentra with migration `0009`.

Database roles, one connection pool, migration checksums (Axentra migration, work package dbinfra):

- `grant_runtime_role(owner_database, role)` and `inspect_role` in `storage_postgres`: a runtime role gets data
  privileges only (tables, sequences, functions, default privileges for later migrations; schema versions
  read-only); refused for the owner itself, superusers, `BYPASSRLS` and table-owning roles.
  `PostgresDatabase.role_info()`. `ServerSettings` reads `NODESTRO_DATABASE_MIGRATION_URL[_FILE]` (the owner role;
  default: `NODESTRO_DATABASE_URL`).
- `AxentraServices(database=..., migration_url=...)`: Axentra borrows the `PostgresDatabase` pool
  (`PostgresDatabase.shared_pool()`, Axentra `Database.on_pool`) and migrates with the owner's connection; its serving
  gate reads through the runtime pool. `start()` refuses a superuser, `BYPASSRLS` or table-owning role in every
  environment unless `NODESTRO_AXENTRA_ALLOW_SUPERUSER=1`, before writing anything.
- Migrations record a SHA-256 checksum per file (`nodestro_schema_migrations.checksum`, added by the runner); a changed
  applied migration is refused (`storage.migration_changed`) by `migrate` and `require_current_schema`; rows of
  earlier releases are adopted once. `migrate` no longer needs the CREATE privilege on the database when its schema
  exists.
- Tests: `tests/test_database_roles.py`, `tests/test_tenant_filters.py`, checksum cases in
  `tests/test_storage_postgres.py`; `NODESTRO_TEST_RUNTIME_ROLE=1` runs every PostgreSQL test as a runtime role
  (migrations as the owner). Measurements: `tests/bench_shared_pool.py`, `tests/bench_nodestro_rls.py`
  (`docs/AXENTRA.md`). Needs Axentra with `Database.on_pool` and the `joined`/`join` scope fix.

One container owner per installation (Docker-first plan R2 follow-up, 2026-11-02):

- A desktop and Nodestro Local Docker label their sandbox containers with their own owner,
  `docker_status.installation_owner(data_dir, installation)` = `nodestro-<12 hex digits of SHA-256 of the resolved
  data directory and the random installation id>` (`Workspace.sandbox_owner`); the self-test, the runtime-image
  sandbox, the environments' sandboxes (`ContainerImages(owner=...)`, `ContainerImages.owner`) and the reaper use it.
  The start-up reaper (`remove_all`) therefore removes only this installation's containers: two installations on one
  Docker daemon no longer remove each other's running containers. A server's tenants keep the shared owner
  `nodestro` (`OWNER`) and reap only expired containers.
- Local MCP servers: `docker_status.mcp_owner(owner)` = `nodestro-mcp-<id>` (`nodestro-mcp` for `OWNER`, still
  `MCP_OWNER`), with the week-long deadline; `mcp.stdio.launch(owner=...)` (new optional parameter).
- `monitor_for(..., owner=...)` (new optional parameter): one monitor per daemon and owner;
  `python_envs.installation_id(root)` is the installation id behind `PythonEnvironments.installation`.
- The sandbox gateway already passes on any well-formed owner label (tests added for the new owners).
- Orkexa pin moved to `e220f706611dc8f2cc7cb4c87e44306a25f93fbf` (Orkexa PR #31: container shape, stdin
  transport, preflight, self-test, reaper; and PR #32: backward-compatible budget primitives).

Docker-first sandbox status (Docker-first plan R2, 2026-11-02):

- New `nodestro_runtime.docker_status`: `DockerMonitor` checks Docker in a background thread, Orkexa's preflight at
  most every 5 s and the in-container self-test (in the runtime image) once per daemon fingerprint (a failed one again
  after 5 min or on request). `Workspace.docker_status(refresh=False) -> SandboxBackendStatus` never waits for
  `docker info` (a first settings read used to run it inline with a 10 s timeout); it reports `checking` until the
  first check finished. One monitor per daemon and process (`monitor_for`), shared by a server's tenants.
- Docker is `approved` only after preflight and self-test passed; `sandbox_policy.isolation_of` returns `container`
  only for a sandbox whose daemon passed them (`docker_status.daemon_proven`, never blocking), otherwise untrusted
  code is refused (`sandbox.isolation_required`, parameter `reason`). Docker mode, servers and Nodestro Local Docker
  report `blocked` with the Docker reason until then (they used to report `docker` regardless).
- `sandbox_selection.backends()` / `select()` take the Docker status; the earlier bool (`docker_available`) still
  works (`True` counts as proven). `Settings.docker_available` is now "preflight and self-test passed".
- `SandboxBackendStatus` (additive, all optional): `engine`, `rootless`, `cgroup_version`, `checks`
  (`SandboxCheck`: `name`, `ok`, `detail`), `tested_at`; state `checking`; reasons `docker-missing`,
  `docker-unreachable`, `docker-limits`, `docker-seccomp`, `docker-rootless-cgroup`, `docker-os-type`,
  `docker-selftest`, `docker-image`, `docker-checking`. A rootful daemon on Linux carries a note that docker group
  membership is root-equivalent (see `docs/docker-sandbox.md`).
- Reaper: a desktop and Nodestro Local Docker remove all of their `nodestro`-labelled containers at the start, then
  every process removes expired ones every 60 s (only that owner).
- macOS: Orkexa's Seatbelt sandbox was removed; `macos-seatbelt` is reported as `unavailable` with the reason
  `removed` (never probed); the `MacOSSandbox` import is gone.
- Tests: workspaces get a static "Docker missing" monitor unless a module is marked `real_docker_status`
  (`testing.StaticDockerMonitor`); `ContainerDouble` counts as self-tested. Tracked follow-ups: RT-033, RT-034.

One container shape from Orkexa (Docker-first plan R1, 2026-11-02; needs Orkexa PR #31):

- `sandbox_run_args` (same signature, new optional `owner` and `lifetime_s`) is now Orkexa's
  `orkexa.dynamic.docker.run_args`: besides the earlier flags `--ipc private`, `--shm-size 16m`,
  `--ulimit nofile=1024:1024`, `--ulimit fsize=16 MiB`, `--ulimit core=0:0`, `--log-driver none`, `nodev` tmpfs
  (`/work` with `mode=1777`) and the labels `org.orkexa.sandbox=<owner>` and `org.orkexa.deadline=<epoch>`.
  Tool containers carry the owner `nodestro` (`container_images.OWNER`), local MCP servers `nodestro-mcp`
  (`MCP_OWNER`, deadline a week ahead, so the tool reaper never removes a live server).
- `StreamedSandbox` is a thin `orkexa.dynamic.DockerSandbox` (`transport="stdin"`, owner `nodestro`, `environ` for
  every docker CLI call): every call runs the image by its id, only after Orkexa's preflight and in-container
  self-test passed; `Sandbox.cancel()` and a cancelled run (`sandbox_cancel`) kill and remove the call's container
  (Orkexa's), the runtime-local cancel watcher is gone. Containers are named `orkexa-sandbox-<hex>`.
- `RuntimeImageSandbox` resolves its tag on first use (also for `command()` and `self_test()`) and runs the image by
  id. The desktop profile now uses it too (Docker mode and the `auto` fallback); Orkexa's `orkexa-sandbox` image is
  no longer used by Nodestro.
- Environment images carry only Orkexa's sandbox runner (`runner_files()`); the image key uses
  `runner_fingerprint()` (`orkexa_fingerprint()` returns it), so every environment image is rebuilt once. `_BOOTSTRAP`
  is Orkexa's `BOOTSTRAP`.
- Sandbox gateway: `plan_create` accepts Orkexa's shape (`IpcMode` private only; `ShmSize` at most 64 MB, default
  16 MB; `Ulimits` only `nofile` <= 1024, `fsize` <= 64 MB, `core` = 0, soft = hard, defaults filled in; `LogConfig`
  only `none`; the two `org.orkexa.*` labels when well-formed, other labels dropped as before), container names
  `orkexa-selftest-*`, and images by id (`sha256:<64 hex>`) only when the daemon reports a `nodestro-env:<key>` tag and
  the lock label for exactly that id (also for `GET /images/<id>/json`; removal stays by tag). Every earlier refusal
  stays. The build context must hold exactly Orkexa's runner modules.

No package build scripts on the host (Docker-first plan R4, 2026-11-02):

- `UvRunner.compile` resolves with `--no-build` by default and `UvRunner.sync` installs host virtual environments
  with `--no-build`, so no package's build script runs on the host. `PythonEnvironments.plan` always resolves with
  `--no-build` on the host; `builds` now means that a person accepted a source build (`deps.build_required`).
- New `ContainerImages.compile_in_container`: for such an accepted plan, `uv pip compile --generate-hashes` runs in a
  container of uv's image, pinned by its multi-arch digest (`UV_IMAGE`, uv 0.9.5), with network only for that step, no bind mounts, only the
  proxy variables (by name), `--cap-drop ALL`, `no-new-privileges`, user `65534`, a read-only root and a tmpfs `/tmp`;
  requirements, the previous lock and the build CA travel on stdin, the lock comes back on stdout. An image without a
  real digest is refused (`deps.build_unavailable`); if it cannot run, the plan reports `deps.build_required` with the
  parameter `reason` (`sandbox.unavailable` without Docker). The Docker CLI 28 wording "failed to connect to the docker
  API" now also counts as Docker not running. Container image builds keep `--only-binary=:all:`.
  Tracked follow-ups: RT-032.

Environment image caps and collection (Docker-first plan R3, 2026-11-02):

- `ContainerImages.build` removes a freshly built image larger than `NODESTRO_MAX_IMAGE_GB` (default 8 GiB) and fails
  with the new code `deps.image_too_large`; an invalid value fails the build before Docker runs (`deps.image_failed`).
- New `ContainerImages.gc(max_images=20, max_bytes=20 GiB, keep=...)`: removes the least recently used environment
  images beyond either limit. Only `nodestro-env:<key>` images that carry `org.nodestro.lock-sha256` are counted or
  removed (never a foreign image), never a kept one, never one a container still uses. Last uses are recorded in
  `<data>/python/image-usage.json`; where images cannot be listed (the sandbox gateway) the record alone is used.
  `build` collects after each new image; a failed collection never fails the build.
- `PythonEnvironments` keeps the images of every environment of its store and the runtime image; with
  `remove_images=False` (a server, shared daemon) nothing is collected.
- New `nodestro_runtime.image_limits` holds the image names and limits shared with the sandbox gateway (its limits are
  unchanged: 32 images, 20 GB). Tracked follow-ups: RT-031.

Axentra platform services in the server profile (Axentra migration, 2026-11-02; everything off by default):

- Observability, notifications, run usage and secrets rollback (all off by default, needs Axentra with the inbox
  channel, migration `0010_notify_inbox`): `axentra_metrics.ServerMetrics` on Axentra's metrics registry (pools,
  jobs, leases, schema state, reconcile backlog, run cost of 24 h, operations by outcome; no tenant labels);
  `NODESTRO_AXENTRA_RUN_USAGE` settles finished runs once per run and meter (`nodestro:run:<run>:<meter>`);
  `NODESTRO_AXENTRA_NOTIFY` puts failed runs, pending approvals and budget decisions into the members' Axentra inbox
  (`axentra_notify`, idempotent per event and recipient, never e-mail); `require_readable_secrets` refuses to start
  `NODESTRO_AXENTRA=off` while `axs1` values exist or with a keyring missing a key id in use, and
  `NODESTRO_SECRETS_FORMAT=v1` re-encryption moves `axs1` values back to v1. See `docs/AXENTRA.md`.

- New `nodestro_runtime.axentra_platform` and `docs/AXENTRA.md`. `NODESTRO_AXENTRA=on` opens Axentra in the server's
  PostgreSQL database (product `nodestro`, single-product mode, keyring from `NODESTRO_AXENTRA_KEYRING[_FILE]`).
  `nodestro-runtime[postgres]` and `[dev]` install `axentra[oidc]` from a pinned commit on Python 3.11+ only; 3.10
  keeps working without it.
- Tenant secrets: `KeyringSecretBox` reads `nodestro:enc:v1:` and Axentra `axs1` values and writes the format of
  `NODESTRO_SECRETS_FORMAT` (`v1` default). `reencrypt_tenant_secrets` moves provider keys, MCP tokens, pending OAuth
  state and billing admin keys to the active key: resumable, idempotent, never overwrites an unreadable value.
- PostgreSQL migration `0007_axentra_links` (`nodestro_axentra_ids`, schema version 7) and `AxentraLink`: Nodestro
  tenants are linked to Axentra tenants in the same transaction (advisory lock, unique slugs, idempotent backfill).
- Audit and usage mirrors (`NODESTRO_AXENTRA_AUDIT`, `NODESTRO_AXENTRA_USAGE`): tool approval events, MCP events
  and usage ledger entries are mirrored into Axentra in the same transaction as Nodestro's record, in a savepoint
  that never fails Nodestro's write; `AxentraEvents.reconcile` writes what is missing, idempotently. New
  `nodestro_runtime.platform_events` (the sink protocol; never imports Axentra), `PostgresDatabase.platform_events`.
- Identity on Axentra (M5, `NODESTRO_IDENTITY_BACKEND`, default `nodestro`): new `nodestro_runtime.axentra_identity`
  with `AxentraIdentityStore` (`mirror`: Nodestro answers, Axentra written in the same transaction; `axentra`: Axentra
  answers, both written), `backfill_identity`, `reconcile_identity` and `identity_readiness`. Same session tokens on
  both sides, never a merge by e-mail, Nodestro ids and contracts unchanged. `AxentraLink.ensure_tenant` reads slugs
  in Axentra's platform scope (row-level security); `AxentraLink.tenant` caches only committed links.
- Input files in Axentra's storage (M8, `NODESTRO_FILES_BACKEND`, default `volume`): new
  `nodestro_runtime.axentra_storage` (`AxentraFiles`, `AxentraBlobs`) stores input file bytes through Axentra's
  `StorageService` (local or S3) with tenant isolation, quotas, the size limit, the G-7 content-type policy
  (`store_inert` for product `nodestro` unless it has its own), and a SHA-256 check on every read; ids and references
  unchanged, the volume is the read fallback. Resumable `migrate`, `verify`, guarded `remove_sources`
  (`NODESTRO_FILES_SOURCE_REMOVAL`, `NODESTRO_FILES_SOURCE_RETENTION_DAYS`) and `export_to_volume` (rollback).
  `InputFiles(blobs=...)`, `InputFiles.download_type`, `WorkspaceStorage.input_blobs`, `AxentraConfig.modules`.
  Tests: `tests/test_axentra_storage.py` (also against moto's S3 server when installed).
- New `tests/test_axentra_platform.py`, `tests/test_axentra_mirror.py` and `tests/test_axentra_identity.py`;
  `tests/test_identity_store.py` runs against all three identity backends.

Automatic sandbox selection (sandbox architecture Phase E, 2026-11-02):

- The desktop `sandbox` setting has a new mode `auto`, the default when nothing was chosen. Untrusted (AI-generated)
  code runs in the built-in Linux sandbox where this computer passed its checks, otherwise in a container of
  Nodestro's runtime image while Docker is running, otherwise it is refused. It never falls back to the host
  subprocess. Code a person wrote keeps running in the host subprocess. A stored `subprocess` (chosen explicitly)
  stays `subprocess` and never uses Docker; a stored `docker` stays `docker`. Server and Nodestro Local Docker
  always use containers, as before. Code of a Python environment (its own interpreter) is never moved into the
  container, whose image lacks its packages.
- New `nodestro_runtime.sandbox_selection` and settings fields `sandbox_selection` (mode, profile, the runtime's
  operating system and CPU, where untrusted and user-authored code runs, or why it is blocked) and
  `sandbox_backends` (native sandbox of this OS, Docker, subprocess: available, state `approved` / `experimental` /
  `unavailable` / `not-isolating` / `not-applicable`, reason, isolation, maturity, memory limit). The operating system
  is the runtime's, never the browser's.
- Orkexa's experimental macOS sandbox (Seatbelt) is reported as `experimental` / `not-released` and never used:
  `isolation_of` keeps returning `none` for it. Windows reports `windows-native: unavailable / not-implemented`.
- `sandbox_cancel` now uses Orkexa's cancel context (`orkexa.dynamic.lifecycle`, ORK-001), so Orkexa's subprocess and
  Docker sandboxes stop a cancelled run's call too. `LinuxSandbox` reports `capabilities()` and `probe()`.
- Orkexa pin `544ef92` (capabilities, probes, cancellation, experimental macOS sandbox; orkexa #28).
- New `tests/test_sandbox_selection.py`.

macOS and Windows start (sandbox architecture Phase A, 2026-11-02):

- The runtime imports and starts on macOS and Windows again. `sandbox_confine.py` looked up `prctl` with ctypes at
  import time (macOS: `AttributeError: dlsym(RTLD_DEFAULT, prctl): symbol not found`) and imported the POSIX-only
  `resource` module (Windows: `ModuleNotFoundError`); every import of the workspace went through it. The C library is
  now looked up on first use and only on Linux, `resource` is imported after the platform check, and `_kill` falls
  back to `Popen.kill` without `os.killpg`. The Linux confinement (Landlock, seccomp, limits, order of the steps) is
  unchanged. Off Linux the built-in sandbox reports reason `platform` and untrusted code is still refused.
- New `tests/test_platform_start.py` and workflow `Platforms` on macOS arm64 and x86-64, Windows x64 and ARM64 and
  Linux: imports every module, opens a workspace, checks the sandbox report and the refusal of untrusted code, runs
  user-authored code in the subprocess sandbox, and simulates macOS and Windows on Linux.

Live run chat fix (LRC-1, 2026-11-01):

- Questions to a team leader are answered with any provider: the answer agent no longer turns off Anthropic's prompt
  caching for other providers (that is an Anthropic-only option, so every non-Anthropic assistant model failed with
  `config.anthropic_only`). New test: a team leader on Amazon Bedrock takes an instruction at its next decision and a
  Bedrock assistant model answers a question (stubbed Converse, no AWS call).

Live run chat and steering (LRC-1, 2026-11-01):

- New `nodestro_runtime.run_chat`: a person can ask an orchestrator node of a running run read-only questions and
  give it instructions. Messages are documents of their own (collection `run_messages`, key `<root run>:<seq>`, in
  the tenant's store); `seq` is numbered under a transaction lock (one order, also across processes), status
  changes are compare-and-set. Idempotent sending (`client_id`), an open instruction with the same text is not
  stored twice; limits for length (2000), messages, instructions, questions, pending questions, questions per
  minute and a chat budget per run (0.50 USD; `NODESTRO_RUN_CHAT_BUDGET_USD`, `..._MAX_QUESTIONS`,
  `..._MAX_INSTRUCTIONS`).
- Instructions reach the orchestrator at its next decision: inside Nodestro's `decide` callback of `_orchestrate`
  they are recorded in the run (`NodeRun.guidance`, fenced save), marked `delivered` and appended to that
  decision's prompt as quoted, untrusted guidance; the recorded decision marks them `considered` (with what was
  decided); an orchestrator that ends expires the open ones. A model call that already started, running teams,
  recorded steps, approvals, budgets and the system prompt are never changed; an execution that lost its run
  delivers nothing. Without instructions the prompt is unchanged. No orkexa change.
- Questions are answered by `answer_question` from `snapshot_text` of the stored run (decisions, the decision in
  progress `NodeRun.current_decision`, team runs and working specialists): a separate tool-less model call whose
  usage goes to the usage log (new source `run_chat`), never into the run's usage or budget.
- `OrchestratorConfig.live_steering` (`attended` default, `always`, `off`): runs started by an API, schedule, batch
  or test series take instructions only with `always`. Not part of node fingerprints.
- Sanitized trace exports mask or remove the chat (`chat` next to the run); importing a trace brings its chat along.

Amazon Bedrock (2026-11-01):

- New provider `bedrock` ("Amazon Bedrock (AWS)", `ProviderInfo.auth == "aws"`; the other providers get `auth`
  `api_key`/`endpoint`, unchanged in behaviour). No API key and no base URL: settings `providers.bedrock` hold only
  `region`, `auth` (`default_chain`/`profile` on a desktop, `default_chain` in Nodestro Local, `operator_role` for
  server tenants), `profile_name` and `data_residency` (`eu_only`). Stable codes: `provider.region_invalid`,
  `provider.auth_invalid`, `provider.auth_not_allowed`, `provider.field_not_supported`, `provider.profile_name_invalid`,
  `provider.data_residency_invalid|_region|_forced`, `provider.region_not_allowed`, `provider.bedrock_not_available`.
  `ProviderStatus` gains `auth`, `region`, `profile_name`, `data_residency(_forced)`, `auth_methods`,
  `allowed_regions`, `operator_role` and `verification {state, checked_at, code}` (never an account, ARN or key).
- `Workspace.verify_provider("bedrock", model=None)`: STS `GetCallerIdentity` plus one free Bedrock control-plane read
  (`ListFoundationModels`, or `GetInferenceProfile`/`GetFoundationModel` for a given model), short timeouts, one retry,
  at most every 10 s; never invokes a model. AWS failures map to `provider.aws_*` codes.
- Server tenants never use the server's own AWS chain: `WorkspaceStorage.bedrock` (`BedrockServerPolicy`, built by
  `BedrockOperatorConfig.from_env` from `NODESTRO_BEDROCK_TENANT_ROLES[_FILE]`, `NODESTRO_BEDROCK_DATA_RESIDENCY`,
  `NODESTRO_BEDROCK_ALLOWED_REGIONS`; `tenant_storage(..., bedrock=)`) assigns one role per tenant, assumed with STS
  (`ExternalId`, `RoleSessionName=nodestro-<tenant>`, regional endpoint, 30 min, deferred refreshable credentials, a
  fresh session per agent). Options without a session are refused on a server.
- Agents: `BedrockOptions` only from the connection; request options become `additional_model_request_fields` and
  may not set region, profile, session, residency, endpoints or managed fields (`config.reserved_request_options`).
  Anthropic-only options stay refused for Claude on Bedrock. Run errors drop IAM/STS identities; residency refusals
  have the code `provider.data_residency_violation`. Pre-check codes `check.bedrock_not_configured`,
  `check.bedrock_not_available`, `check.bedrock_settings_invalid`. Exports: `environment_connection(..., settings=)`
  and `exported_settings()` (region + data residency only; `provider.region_missing`). Catalog: EU inference profile
  suggestions. Orkexa pin: `3467940` (master with the Bedrock provider).
- The workspace feeds the strict price mode the Bedrock region in effect (`PricingService.conditions_source`); the
  pre-check reports `check.model_unpriced` when a Bedrock price does not cover that region; catalog entries show
  `price_region`/`price_regions`.
- Review fixes (PR #66): a tenant's AWS session reads nothing of the operator's AWS configuration (no
  `AWS_PROFILE`, config or credentials file, `ca_bundle`, `credential_process`, credential chain or `AWS_*` settings;
  only `AWS_CA_BUNDLE` from the server's environment is honoured, for corporate TLS proxies). On a server the
  `bedrock-runtime` client is built from the tenant session in `Connection.agent_kwargs()` too, so an agent built
  without options can never sign with the server's chain. Error texts (runs, agent tests, tool jobs) drop the account
  of every ARN and bare account ids. Imports into a tenant drop Bedrock `auth`, `profile_name` and `verification`; a
  stored check result counts only for the settings and role it was made for, and an invalid stored region counts as
  not configured. Verification under a residency makes the run's static checks first. The strict price check uses
  the region an agent was built for. `NODESTRO_BEDROCK_ALLOWED_REGIONS` lists the regions where inference may run
  (destinations of cross-Region profiles included), not only the source regions.
- Studio data: Bedrock catalog entries carry Orkexa's `capabilities` (`catalog.model_capabilities(model)` gives the
  same for any typed `bedrock:` id; `None` for the other providers); `PriceEntry.regions` lists a price's
  `aws_region` tariff values (base-rate region first).

Amazon Bedrock, pricing and credential isolation:

- Bedrock is its own price source (never Anthropic's list prices). A `bedrock` price must list its AWS regions as an
  `aws_region` tariff (`pricing.region_required`, `pricing.invalid_region`); a region it does not list has an unknown
  cost, never 0. `PricingService.conditions_source` tells the strict price mode the connection's region, so a Bedrock
  request whose price does not cover it (or whose region is not known) is refused before it is sent
  (`pricing.price_unknown`). Refusals keep model ids with colons (`...-v1:0`, ARNs) whole in `params.model`.
- LiteLLM sync maps only regional Bedrock entries (`bedrock/<region>/<model id>`) into one price per model id with an
  option per region; entries without a region (`anthropic.claude-...`, `eu.`/`global.` profiles), provisioned
  throughput and ambiguous duplicates are left out (`LiteLLMSource.bedrock_unmapped`).
- Reconciliation lists providers without a billing adapter under `not_reconciled`
  (`pricing.reconciliation_not_supported` for Bedrock, whose bill also has charges usage does not report).
- The subprocess sandbox gets `HOST_AWS_ENV` (no shared AWS credential/config files, no instance metadata), so an
  AWS SDK in tool code never picks up the desktop user's AWS credentials; regression tests prove that no `AWS_*`
  variable reaches uv, the subprocess, built-in and container sandboxes or MCP stdio servers. uv keeps the user's
  shared AWS files (private package indexes through AWS CodeArtifact keep working); residual risk, unchanged from
  before: a source build runs third-party code on the host with `HOME` and could read `~/.aws`. Trace exports also
  hide temporary AWS keys, session tokens and Bedrock API keys.

Backend audit (2026-11-01):

- C-14 follow-up (re-verification found PostgreSQL reads slower than before the pool): a pooled connection is
  checked only after it sat idle for more than 2 s (`CHECK_AFTER_IDLE`), not on every checkout; a read that meets
  a lost connection anyway is retried on another one (writes are not repeated). A single write outside a
  transaction runs in autocommit, without `BEGIN`/`COMMIT` round trips. Migration 0006 makes the `status` and
  `trigger_ref` expression indexes partial (`IS NOT NULL`), so a lookup by id on a table without fresh statistics
  can no longer tie with them and scan the collection, and `migrate()` runs `ANALYZE` after migrating a database
  with data. Same-machine benchmark (5,000 runs, get + list): 1 thread ~690 ops/s (before the audit ~600, at
  `6eacec1` ~350); with fresh statistics ~1,300 vs ~1,480 before the audit (the pool's checkout costs ~0.1 ms).
- B-01: the built-in Linux sandbox and the container sandboxes (`StreamedSandbox`, `RuntimeImageSandbox`) read a
  tool call's output with Orkexa's `communicate_bounded()` / `run_bounded()`: at most `response_limit()` bytes of
  stdout and the end of stderr stay in the API or worker process. Code that wrote straight to the inherited stdout
  could make the host buffer any amount (200 MiB raised the host's memory by ~600 MiB); such a call is now stopped
  and its container removed. Orkexa pin: `6e69b9c` (master with the bounded-output fix).
- B-02: continuing a run (`resume.plan`) asks for `confirm_side_effects` for every tool call that may have run: the
  calls of child runs of sub-workflow and orchestrator nodes (recursively), of earlier loop executions, and calls that
  had started when the process stopped. Before a call the gate lets through runs, the engine writes a small note (tool,
  call id, time, SHA-256 of the arguments; never the arguments) to the new collection `tool_call_journal`
  (`nodestro_runtime.tool_journal`); a node's notes are deleted once its state is stored. Only the read-only built-in
  tools (`BUILTIN_CAPABILITIES`), failed calls and calls that never ran are not counted; extension tools now count.
- B-03: cancelling a run (also by `limits.max_duration_s`) stops its running sandbox call: the built-in Linux sandbox
  kills the call's process group, `StreamedSandbox`/`RuntimeImageSandbox` remove the call's container. Only that run's
  call is stopped (`nodestro_runtime.sandbox_cancel`); Orkexa's `SubprocessSandbox` and `DockerSandbox` calls still end
  at their timeout.
- C-01, A-03, C-14: `PostgresDatabase` keeps a pool of connections (`psycopg_pool`, `NODESTRO_DATABASE_POOL_SIZE`,
  default 8) and checks each before use, so a dropped connection is replaced instead of failing every later call until
  a restart. Reads run in parallel, so one slow request no longer stalls every tenant of the process; writes of one
  process still run one after another. Every connection has `statement_timeout`
  (`NODESTRO_DATABASE_STATEMENT_TIMEOUT`, 30 s, 0 = off) and `idle_in_transaction_session_timeout`
  (`NODESTRO_DATABASE_IDLE_IN_TRANSACTION_TIMEOUT`, 300 s); migrations have no statement limit. Session advisory locks
  use a dedicated connection, and `LeaderLock` notices a lost connection. New: `PostgresDatabase.ping()`.
- C-02, C-06: run writes are fenced across processes. An approval decision is stored with a compare-and-set, so of two
  concurrent decisions exactly one is recorded and the other is refused with `approval.not_pending`; a decision for a
  run that is no longer paused is refused (`run.not_paused`) before anything is written. A worker whose lease expired
  or whose run was interrupted meanwhile stops without overwriting the run (`run.fence_lost`).
- C-03: `DocumentStore.lock(collection, key)` serializes read-modify-write steps inside a transaction across processes.
  Workflow version numbers, `Workspace.save_workflow` and the legacy approval stamp use it.
- C-05, C-14: SQLite schema version 1 adds an index for `list()` and partial expression indexes for `find()` on
  `status` and `trigger_ref` (existing data directories are backed up first). PostgreSQL migration 0005 indexes
  `trigger_ref`, and `find()` names the field literally so the expression indexes apply.
- C-08: agents and orchestrator decisions run in the run manager's own thread pool (`NODESTRO_RUN_THREADS`, default
  32) instead of asyncio's default executor; workers renew leases and flush events on threads of their own.
- C-10: SQLite and PostgreSQL listings leave out documents that do not fit the model, log them once and count them in
  `store.damaged`, instead of failing the whole list; start-up recovery skips them. Reading one directly still raises.
- C-11: `jobs.reap` also marks top-level runs stored as queued or running without an active job as interrupted (after
  twice the lease), and `cancel` ends a stored live run that nothing executes (for example after a restore).
- A-05: on a server, OpenAI-compatible endpoints (`compatible`, `ollama`, `vllm`) get a client that never follows
  redirects, so an allowed endpoint cannot send requests to an address outside the allowlist.
- A-07: on a server, deleting a tenant's Python environment never removes its container image (operators prune unused
  images); on the desktop an image is removed only when no other environment uses it.
- D-14: `engine.PLACEHOLDER` and `memory_spaces.SPACE_PATTERN` are public; the private names remain as aliases.
- Tests: `test_worker_tool_changes.py` is skipped without a Docker daemon, except in CI or with
  `NODESTRO_REQUIRE_DOCKER=1`.
- AD-01: a provider failure during a run has its own code: `run.provider_auth` (401/403), `run.provider_unreachable`,
  `run.provider_rate_limited` (429) or `run.provider_overloaded` (503/529), with `params.provider` and `params.status`.
  Other provider errors stay `run.api_error`.
- AD-10: a graceful stop ends running runs as interrupted (`run.interrupted`, `params.cause = "shutdown"`) and they
  can be continued; it no longer looks like a cancellation by a person. After a crash the step that was running is
  marked interrupted instead of staying "waiting", and the resume plan says `resume.was_interrupted`.
- X-02: a step keeps its start time when it continues after an approval, and each orchestrator team result names its
  child run (`outcomes[].ref`).
- AD-03: runs carry `warnings` (`{code, node_id, tool, count}`) for tool calls that were rejected, expired, refused or
  failed.

Security fixes from the independent MCP re-review (R-1, R-2, two test gaps):

- R-1: a per-call approval now also covers the tool's classification (`access`, `reach`). Reclassifying a tool in
  either direction while a call waits closes the pending request (`closed_because = tool.reclassified`) and marks an
  approved, unused M6b request `stale` (`ToolApprovals.invalidate_tool`, called by `McpService.decide`). Approving a
  request whose tool is classified differently now (`ToolApprovals.decide(current=...)`, `RunManager.decide_tool_call`)
  is refused with `tool.approval_stale`. The engine compares the stored and the current classification right before
  the call in the thread path and the M6b resume path and refuses stale approvals with `tool.approval_stale`. Against
  races between that check and the request, the engine binds what the gate let the call run as
  (`tool_approvals.CallBinding`, a context variable of the call); `McpService.call` checks it against the stored tool
  before connecting and again right before sending (`McpHub.call_tool(confirm=...)`, `CallResult.refused`). A gated
  call of a changing MCP tool without a binding is refused.
- R-2: the name `memory` is reserved for the agent's system memory tool (`tool_library.RESERVED_NAMES`): saving or
  generating a tool under it is refused (`tool.name_reserved`), a tool stored under it before is never handed out
  (`ToolLibrary.tool` raises `tool.name_reserved`; it stays listed so it can be deleted), and `config.memory_name_reserved`
  is reported whenever an agent lists `memory` as a tool. The per-call gate exempts a call only when it reaches the
  real memory tool or a run's own tool (`agent_builder.run_own_tool`), in runs and agent tests.
- Tests: the agent test of a read tool asserts exactly the expected result; the PostgreSQL token search runs a real
  MCP discovery, approval and execution through an API and a worker process and searches tables, run events, MCP
  events, the approval audit, logs and the run trace.

Security fixes from the independent MCP review (H-1, M-1, M-2, L-2..L-8):

- H-1: the process-wide per-call approval switch (`NODESTRO_TOOL_APPROVALS=1`, `McpService.write_gate`) no longer opens
  MCP tools that change data on its own. `McpService.check/agent_tool/call`, `PinnedMcp`, `McpAgentTool`,
  `ToolLibrary.tool`, `AgentBuilder.build` and `execute_agent` take an explicit `gated` flag; a non-read MCP tool needs
  `write_gate and gated`. Only the run engine passes `gated=True` (it asks a person before every such call);
  assignability checks (`validate`, `usable`, `pin`) ask "could a gated run use it". The agent test applies the same
  tool rules as a run (`tool_policy.decide`): allowed calls run, a call the rules would ask about is refused with
  `tool.approval_not_in_test`, a refusal stays a refusal. The tool test panel (`ToolLibrary.run`) never runs a changing
  MCP tool (`mcp.write_not_supported`).
- M-1: server addresses that carry credentials (credential-like query names, token-like query values or path segments,
  any fragment) are refused with `mcp.url_secret` (param `where`). Stored addresses from before are cleaned at start:
  the secret part is removed, the server is switched off with `last_error_code = mcp.url_secret_removed`, and old audit
  events are redacted. Events and error texts only ever hold redacted addresses (`netguard.redact_url`).
- M-2: every MCP HTTP answer (tool calls, discovery, OAuth metadata and token answers) is limited to
  `netguard.MAX_RESPONSE_BYTES` (8 MiB) while it is read: a larger declared `Content-Length` is refused before the body,
  a longer stream stops at the limit (`mcp.response_too_large`), and compressed answers are neither asked for
  (`Accept-Encoding: identity`) nor accepted (`mcp.protocol_error`).
- L-2: loopback consent only for loopback addresses (`mcp.loopback_not_local`). L-3: approving a tool needs the reviewed
  definition hash (`mcp.hash_required`). L-4: a per-call approval no longer covers a definition that was approved while
  the call waited (`tool.approval_stale`). L-5: the external-data marker around MCP results carries a per-call nonce,
  so a result cannot fake its end. L-6: slugs of removed servers are retired and never given to a new server. L-7: a
  tool status change is dropped when the definition was approved again meanwhile. L-8: new test searching a bearer
  token in logs, the run trace, all data files (SQLite with WAL) and every PostgreSQL table.

Fix: tools on a server worker after a restart without network (S-2 pilot finding F5):

- A resolved lock named the temporary directory it was resolved in (`# via -r /tmp/nodestro-resolve-…/requirements.in`).
  The image of the runtime requirements (tool code without third-party libraries) is resolved once per process, so
  every worker start computed a new image tag and built that image again, which downloads its packages. Without
  network the build failed and tool calls failed closed with "The sandbox image is not available". uv now runs in the
  temporary directory with relative names, so the same requirements give the same lock and the existing image is
  found. Resolving still needs the package index, or `UV_OFFLINE=1` with a prepared cache.

Fix: continuing a paused run from several processes at once (Evolution review of api #40):

- A paused run is queued again by a compare-and-set from `paused` (`replace_if` on `status`). When a decision and
  the deadline sweeps of several workers continue the same run at the same moment, exactly one of them queues it
  (one job, one `resumed` audit per request); the others get `run.not_paused` and change nothing. Before, a process
  that had read the run as `paused` could write its stale copy over a run that already went on before its enqueue
  failed. The requests a continue answers are read before the run goes on, and only the process that queued it
  audits them.

Runs paused at a tool call that waits for a person (M6b, behind a second switch, default off):

- New switch `NODESTRO_TOOL_APPROVAL_CHECKPOINTS=1`, effective only together with `NODESTRO_TOOL_APPROVALS=1`. While it
  is off nothing changes: a call that asks waits in the agent's thread as before. Activating it is a separate decision.
- With it on, a call the rules ask about stores the same approval request (now `checkpointed`) and stops the agent's
  turn with an orkexa turn checkpoint; the node waits (`awaiting_approval`), the run is `paused` and frees its worker
  (no waiting slot). The checkpoint (conversation of the turn, results of calls that ran, the waiting calls, run
  memory; no tool definitions or keys) is stored per tenant in `agent_checkpoints` (module `tool_checkpoints`), never
  in the run document or its exports, carries a format version and expires with its requests.
- `RunManager.decide_tool_call` decides through the unchanged `ToolApprovals.decide` and continues the run once none
  of its calls waits; the turn resumes without asking the model again. On resume only the stored request of exactly
  that call (run, node, iteration, call id, arguments hash with tool version or MCP definition hash) answers it; the
  rules are applied again and a new deny wins. An approved call runs only within 15 minutes after its decision
  (`tool.approval_stale` otherwise) and at most once: `use` is set to `executed` by compare-and-set right before it
  runs, so a crash after the mark gives `tool.result_lost` instead of a second call. A changed agent definition ends
  the turn (`tool.checkpoint_stale`).
- `RunManager.sweep_tool_waits` (at start before anything continues, then periodically by the app): overdue questions
  expire, checkpoints of runs that are gone are dropped with their questions, decided runs continue (also after a
  restart between decision and resume). With the switch off at restart, open questions of paused runs are closed
  (`closed_because="switch_off"`) and the node ends with `tool.checkpoint_expired`; the call never runs.
- Cancelling or deleting a paused run closes its questions and deletes its checkpoints (`discard_tool_waits`).
- At most `NODESTRO_TOOL_APPROVAL_MAX_PAUSED` (default 20) runs of a tenant pause at a tool call; above it a call is
  denied at once (`tool.approval_capacity`).
- New request fields `checkpointed`, `use`, `executed_at`; new audit kinds `suspended`, `resumed`, `executed`,
  `stale`, `result_lost` and `switch_off` (never with arguments). `executed` is audited when the released call
  ends; a call a pinned tool refused because the tool changed after its release is audited `stale`
  (`tool.changed`) and is never made later. A rejected or expired request is set to `use="closed"` when its run
  continues, so `resumed` is audited once.
- Known limit: agents with a quality check keep waiting in the thread and hold a worker while they wait (a judge's
  refinement cannot be checkpointed); they still count against the waiting slots and `max_waiting`.
- Fix: the docstring of `OrchestratorSelect`.

Security fix: own and generated tools across processes (S-2 pilot finding F1):

- **Behavior change:** a tool library now reads its tool directory again whenever another process changed it (a
  cheap check of the record files' identity at every lookup). Before, every server worker kept the tools it read
  when it opened a tenant: after a tool was changed or deleted in the API it still ran the old code, and an advance
  approval or legacy exemption for the old version still let that code run, until the worker restarted.
- Agents keep the tools they got when their step started (`PinnedTool`). Before each call the tool checks, past
  every cache, that its version is still the current approved one; otherwise the call is refused ("the tool was
  changed or deleted after this step started") and does not run. This covers a change during a running workflow
  and during a waiting approval: an approval or advance approval never makes a different or deleted version run.
  The per-call rules (`tool_policy.decide`) are unchanged and decide on the current version.
- New versions are numbered from the current directory, so two processes no longer write the same version number.
- Published releases are unaffected: they run their own frozen copy of the tools.

Orchestrators: dynamic teams and approvals inside teams (Adaptive Orchestration M6b, orkexa ADR 0011):

- **Behavior change:** a team that waits for an approval step no longer gets cancelled. The orchestrator node waits
  (`awaiting_approval`, the run is `paused`), also when the approval is in a sub-workflow of a team or below a nested
  orchestrator. After the decision the same team run continues and the orchestrator goes on from its stored record;
  the model is not asked again for that delegation, and this also works after a restart. Pending approvals of team
  runs are listed with the run (`approvals_of_tree`). A rejection that stops a team's run (`on_reject="stop"`) is
  recorded as `rejected` and stops the orchestrated run like a sub-workflow, so no other team can get around it.
  Cancelling a waiting run cancels its waiting teams and closes the record (`stop_code: run.cancelled`).
- Teams with approval steps are selected by capability again; the preflight note
  `check.orchestrator_member_approval` is now an `info`.
- New `OrchestratorConfig` options, all off by default: `staffing` + `max_team` (the orchestrator assembles its team
  from the pool while it runs and may change it), `max_parallel` (up to 5 teams per decision at the same time) and
  `allow_values` (the orchestrator fills a team's start form fields except files; checked like a manual start).
  The pool stays pinned at the run's start; creating teams or agents is still impossible.
- The record (`NodeRun.orchestration`) is protocol version 2: an `outcomes` entry per delegated team
  (`succeeded`, `failed`, `rejected`, `cancelled`, `expired`) and `pending` while a team waits. New events
  `orchestration_waiting` and `orchestration_resumed`; `orchestration_delegated` carries `step` and `slot`.
- Requires orkexa with orchestration protocol version 2.

MCP resources (WP-10):

- A server's listed resources become one tool, `resources/read`, approved and classified like any other tool and
  called through the same policy, per-call approval and audit. Its `uri` is an enum of the listed addresses (no
  templates), its hash covers the list (a changed list needs a new review), text results are marked as external data
  and binary content is not passed on. Server tools named `resources/read` are skipped at discovery.
- Tests prove that Nodestro's client declares no elicitation, sampling or roots capability (threat model T16).

Per-call tool approvals, warning before an unattended start (S-2, risk R1 of the security acceptance):

- New `tool_preflight.unattended_tool_issues(ctx, workflow, trigger=..., trigger_ref=...)`: the tool calls a
  schedule, a batch, a test series or a published API would have refused, found before the start or the publication.
  It asks the existing `tool_policy.decide` with the same call context as the engine, so classification, tool version,
  legacy exemptions and pre-grants count exactly as at call time. Warnings only (codes `tool.unattended_*`: no
  pre-grant, outdated pre-grant, batch not started yet, published API, destructive, MCP); it never grants anything
  and the engine still refuses at every call. Empty while `NODESTRO_TOOL_APPROVALS` is off.
- `ToolGate.call_context(...)` and `tool_approvals.tool_facts(...)`: the call context and the tool facts of the
  engine's gate, now shared with the warning (no behavior change in the engine).

Per-call tool approvals, security acceptance fixes (S-2, decision D10b):

- **Fix:** running the activation step again (`Workspace.stamp_legacy_tool_bindings`) used to stamp every binding
  without an entry, so a tool assigned after the first run, a new workflow or a new version became a legacy
  exemption. The step now runs once per workspace: `LegacyReport` records `stamped_at`, `stamped_by` and
  `publications_stamped_at`; a repeated call stamps nothing and is audited (`legacy_stamped`, `repeated: true`).
  `record_legacy_tool_bindings(..., publications_stamped_at=)` records the API's part.
- **Fix:** `keep_only_inherited` also drops legacy entries of tools the agent no longer has, so removing a tool and
  assigning it again (or sending the old entry back) never revives an exemption.
- Regression tests for both, and a test that a new tool version and the expiry end a pre-grant.

MCP tools in releases (ADR-0007, work package WP-9):

- `ReleaseBundle.mcp_tools` freezes each MCP tool a release uses as a `release.McpToolRef` (server id and slug, approved
  hash, classification, definition); never an address or a token. Left out of the digest while empty, so existing
  bundles keep their digest. `tool_marker` names the frozen hash.
- `McpService.pin(name)` builds the reference (only for tools usable now). `nodestro_runtime.mcp.pinned.PinnedMcp`
  stands in for the service in a release's tool library: only frozen tools, and each lookup and call checks the live
  policy plus the frozen hash and classification (`mcp.release_tool_changed`, `mcp.tool_not_in_release`).

Adaptive Orchestration M5 (pool selection):

- New `OrchestratorConfig.select` (`capabilities`, up to 10 tags; `max_teams` 1..20, default 5; `done`, set once a snapshot selected): saved workflows
  whose capabilities match join the pool. The selection happens when a run or a published release pins its
  workflows, so it is fixed and visible in the run's snapshot: each selected `OrchestratorMember` has `matched`
  (the tags it shares). Order: number of matching tags, then name and id; no score is invented. Workflows with
  errors or approval steps, explicit members and the workflow itself are left out; a selected team that is already
  running is not offered. `RunManager.team_candidates()` lists the saved teams; `graph.workflow_refs(node)` names
  every workflow a node calls (sub-workflow or orchestrator teams).
- Pre-check: `check.orchestrator_selected` (info: the teams the selection takes now) and
  `check.orchestrator_no_match` (error without explicit members, else warning). At run time an empty pool after
  selection fails with `run.orchestrator_no_match` (`node`, `capabilities`).
- `NodeRun.decision_usage`: the orchestrator's own requests; usage per model counts them for its model and the
  teams' requests for their agents' models.

Adaptive Orchestration M4 (orchestrator node):

- New node type `orchestrator` (`OrchestratorConfig`: `members` (up to 20 team workflows, optionally pinned to a
  version), `task_template`, `instructions`, `model`, `max_decisions` (1..50, default 8), `max_per_member`). An AI
  model chooses step by step which team of its pool gets which task and finishes with an answer, following the
  `orkexa.orchestration` protocol (ADR 0010 in orkexa; orkexa pin b91d0b7). The model has no tools, cannot create
  teams, agents or tools and has no permissions of its own: every team runs as a linked child run
  (`NodeRun.child_run_ids`) with its own budget share, limits and tool approvals. Refused decisions (unknown team,
  limits, repeated task, invalid answer) are recorded with their code and shown to the model.
- Every decision, verdict, result and reason is stored in `NodeRun.orchestration` as it happens (events
  `orchestration_delegated`, `orchestration_step`), so a run can be inspected and replayed.
- A team that stops at an approval step is cancelled and reported to the orchestrator as a failure (pausing a
  run inside an orchestrator is not supported yet). An orchestrator that uses all its decisions without
  finishing fails with `run.orchestration_limit` (`node`, `max_decisions`); a missing team fails with
  `run.orchestrator_member_missing`.
- Workflow check: `workflow.orchestrator_no_members`, `workflow.orchestrator_member_duplicate`,
  `workflow.orchestrator_self`, `workflow.orchestrator_limits`. Pre-check: `check.orchestrator_member_missing`,
  `check.orchestrator_cycle`, `check.orchestrator_member_invalid` (errors) and `check.orchestrator_member_approval`
  (warning). Runs pin the team versions like sub-workflows; resume fingerprints cover the teams.

Adaptive Orchestration M3 (teams: output contracts and capabilities):

- New `OutputNodeConfig.contract` (list of `OutputField`, like an agent's structured output): the fields a workflow
  promises to deliver. The result node checks its input against it (an object, or JSON text of one), keeps only
  the contract's fields and fails with `run.output_contract` (`fields`) when a required field is missing or has
  the wrong type. A parent that runs such a workflow as a sub-workflow therefore receives a checked object, and
  decisions after it can rely on its fields. New module `nodestro_runtime.contracts` (`contract_of(workflow)`).
- Workflow check: `workflow.contract_field_name` and `workflow.contract_field_duplicate` block a run. Pre-check
  warnings: `check.contract_text_source` (an agent before the result answers in plain text) and
  `check.contract_field_missing` (no structured agent before the result delivers a required field).
- New `WorkflowIn.capabilities`: up to 20 tags (at most 40 characters each, trimmed, unique ignoring case) that
  say what a workflow can do as a team. They travel with the workflow and grant nothing.
- `WorkflowSummary` gains `capabilities` and `contract` (the field names of the output contract), so lists can
  offer teams without loading every workflow.

Local MCP servers over stdio in an isolated container (ADR-0007, work package WP-8):

- `McpServer.transport` (`http` or `stdio`) and `McpServer.stdio` (`StdioCommand`: plain package requirements, a
  module path, arguments, memory). A stdio server's address is `stdio:<module>`; it takes no token or sign-in.
- Its packages go through the Python environments' approval (new scope kind `mcp`, `needed_for_requirements`,
  `McpService.plan_environment` / `environment`, `PythonEnvironments.image`). Each connection runs `python -m <module>`
  in a container of the approved image with the sandbox's flags (new `container_images.sandbox_run_args`, shared with
  `StreamedSandbox`): no network, read-only, no capabilities, `nobody`, limits, `--pull never`, removed afterwards.
- No host fallback: without the Docker sandbox a stdio server is refused (`sandbox.isolation_required`), without
  installed packages it does not start (`mcp.environment_not_ready`). A relay (`nodestro_runtime.mcp.stdio`) bounds the
  line length (8 MB) and removes the container; stderr is discarded. New codes `mcp.stdio_invalid`, `mcp.stdio_failed`.
- Removing a stdio server forgets its environment; orphaned `mcp:` environments are cleaned up.
- Tests: fake-docker end-to-end tests, and a real container test with `mcp-server-time` from PyPI through the sandbox
  gateway (CI job `docker`).

MCP OAuth 2.1 (ADR-0007, work package WP-6):

- New module `nodestro_runtime.mcp.oauth`: sign-in for MCP servers with `auth="oauth"` following the MCP
  authorization specification, built on the official SDK (metadata models, discovery URLs, issuer checks, token
  parsing, the refreshing `OAuthClientProvider`). `McpService.begin_oauth` / `abegin_oauth` discover the protected
  resource metadata (RFC 9728) and the authorization server metadata (RFC 8414, issuer checked), register a client
  (RFC 7591 or a client ID metadata document) and return the authorization URL (PKCE `S256` required, `state`,
  `resource` per RFC 8707). `complete_oauth` / `acomplete_oauth` check `state`, expiry (10 minutes) and the issuer of
  the response (RFC 9207), exchange the code once and store the credentials sealed (`SecretBox` per tenant or the
  desktop keychain key; otherwise for the session only). `sign_out` forgets them.
- Calls present the access token only to the MCP server and refresh it at the discovered token endpoint; one provider
  per server serializes refreshes (rotating refresh tokens); refreshed tokens are stored sealed again. A lost grant
  fails with `mcp.oauth_reauthorize`. Every request of the flow goes through the guarded HTTP client (address
  checks, operator endpoint list). New error codes: `mcp.oauth_failed`, `mcp.oauth_not_required`,
  `mcp.oauth_not_configured`, `mcp.oauth_state_invalid`, `mcp.oauth_expired`, `mcp.oauth_redirect_invalid`,
  `mcp.oauth_registration_unsupported`, `mcp.oauth_token_input`, `mcp.oauth_reauthorize`; new events
  `oauth_started`, `oauth_failed`.

Adaptive Orchestration M2 (step limit):

- New `WorkflowLimits.max_steps` (1 to 10,000): the most node executions of a run. Every executed node counts,
  start and result nodes included, and every round of a loop counts again. It maps to the kernel's
  `max_invocations`; when the next execution would exceed it, the run fails with `run.step_limit` (`max_steps`).
  Continued runs count the reused steps of the earlier run.

Sandbox gateway hardening (Nodestro Local Docker, internal cross-check K1, K2, K4, D2, D4):

- **Image limits (K1).** A build is refused with 507 once the daemon holds `NODESTRO_SANDBOX_MAX_IMAGES` (32)
  environment images or `NODESTRO_SANDBOX_MAX_IMAGES_GB` (20) of them. Rebuilding an existing tag is still allowed.
- **Connections and queues (K2).**
  - At most `NODESTRO_SANDBOX_MAX_CONNECTIONS` (32) client connections; more are refused with 503.
  - A request head must arrive within 10 s and a body within 120 s (408).
  - A second image build is refused with 429 on its head, before its body is read, instead of waiting in memory.
- **Build contexts.** Only uncompressed tar archives are accepted, with at most 2000 entries, and they may not unpack
  to more than their own size.
- **Daemon security (D2).** Besides the resource limits (#37), the gateway requires the daemon's seccomp profile.
  `SecurityOptions` without seccomp, or with `profile=unconfined`, means no sandbox container is created (503).
- **Certificates (D4).** `ca.pem` in a build context must consist of X.509 certificates in PEM (comments allowed),
  each of which `ssl` can load. Before, only its first line was checked.
- **Base images (K4).** Environment images build on `python:<v>-slim` pinned by the digest of its multi-platform index
  (`container_images.BASE_IMAGES`, Python 3.10 to 3.14). The gateway builds only on pinned bases. Existing
  environment images get a new tag once and are rebuilt.

Adaptive Orchestration M2 (time and parallel limits):

- New `Workflow.limits` (`WorkflowLimits`): `max_parallel` (1 to 32 nodes of a run executing at the same time;
  further ready nodes wait for a free slot) and `max_duration_s` (the longest active execution time of a run, at
  most 7 days). Both default to `None` (no limit, as before) and are enforced by the `orkexa.flow` kernel.
- The kernel's clock is the run's active time, stored as `Run.active_seconds`: waiting for an approval does not
  count. When the time limit is reached, the kernel stops the flow, running nodes are cancelled and the run fails
  with `run.time_limit` (`max_duration_s`). Known limitation: a model request or tool call that is already running
  finishes first (agents and tools stop at their next cancellation check), so the run can end later than the limit
  by the length of that call.

Adaptive Orchestration M2 (budget reservations, AO-12):

- **Behavior change: requests reserve their most expensive outcome before they are sent** (Orkexa
  `BudgetTracker` reservations). Branches, sub-workflows and agents that run at the same time can no longer
  spend past a run's budget together; a request that does not fit next to the requests in flight waits for
  them. Sub-workflow trackers chain their reservations to the parent (`_ChildTracker` now uses Orkexa's
  `parent=`), so neither budget can be bypassed through the other.
- A request whose most expensive outcome is larger than the rest of the budget runs alone in standard price
  mode and is refused before it is sent in strict mode (`run.budget_exceeded`).
- New `Usage.uncapped_requests` (requests admitted without a capped cost: no price, native tools, images,
  binary documents, or too large and admitted alone), `Run.budget_usd` (the budget the run started with) and
  `Run.budget_hard_maximum` (`True` only when every request was capped and priced; `None` without a budget).
  Unknown costs are never reserved as 0.
- Orkexa pin: `b85133d53d58616dfba164d45ccdc035766d4144` (orkexa #15).

Adaptive Orchestration M2 (sub-workflow budgets):

- **Behavior change: the `budget_usd` of a sub-workflow is now enforced.** Before, a child run only counted against
  the parent's budget and its own limit was ignored. Now the child stops at whichever comes first, its own remaining
  budget or the parent's; the parent's limit still covers everything the child spends. The child uses the parent's
  price mode, so strict mode refuses unpriced models in the child as well. Known limitation: in standard price mode a
  request without a price is still sent and its cost stays unknown (not zero), so it is not counted against either
  limit; the reservations of M2 make this visible.

MCP operations (ADR-0007, work package WP-7):

- `McpLimits` (`NODESTRO_MCP_MAX_SERVERS`, `_MAX_PARALLEL`, `_QUEUE_SECONDS`, `_CALLS_PER_MINUTE`,
  `_BREAKER_FAILURES`, `_BREAKER_SECONDS`; invalid values raise `mcp.limits_invalid`): servers per tenant
  (`mcp.too_many_servers`), parallel calls per tenant with a bounded wait (`mcp.overloaded`), a rate limit per server
  (`mcp.rate_limited`) and a circuit breaker that rests a server after repeated transport failures
  (`mcp.cooling_down`; tool errors do not count, a successful discovery resets it). `McpHub.stats()` reports active
  calls, limits and per-server health; `McpHub.close()` cancels open work and stops the hub's loop thread.
- The definition hash is now compared on the same connection before the request is sent: a tool whose definition
  changed after approval never receives the arguments (`CallResult.sent` is `False`).
- `McpService.write_gate` is a plain flag (default `False`): `write`/`destructive` tools stay blocked until the
  shared per-call approval (D10, S-2b) turns it on; there is no MCP-specific approval path.

Adaptive Orchestration M1 (loop placeholders in the check before a run):

- `preflight` accepts `{{iteration}}`, `{{previous}}` and `{{feedback}}` in the tasks of nodes inside a loop; outside
  loops they still get `check.placeholder_unknown`, because they stay as they are there.

Sandbox gateway: no tool code without enforced resource limits (Nodestro Local Docker):

- **Fixed: sandbox containers could run without memory, CPU and process limits.** Docker drops limits it cannot
  apply and only warns, for example rootless Docker without cgroup v2 delegation (found in the rootless Docker end-to-end
  run). The gateway now asks the daemon's `/info` before the first container create or image build and refuses both
  with 503 until `MemoryLimit`, `PidsLimit`, `CpuCfsQuota` and `CpuCfsPeriod` are confirmed.

Sandbox gateway security fixes (Nodestro Local Docker):

- **Fixed: request smuggling to the Docker daemon.** A header value with a bare line feed (`User-Agent: x\n\nGET
  /containers/json ...`) was passed on; the daemon's parser ended the header there and ran the rest as a second,
  unchecked request, so a compromised studio container could reach the whole Docker API (host root on rootful
  Docker). Request heads are now parsed strictly (`parse_head`): no control characters, bare CR or LF, folded
  headers, unknown methods, fragments or non-path targets, and not `Content-Length` with `Transfer-Encoding`.
- Every target sent to the daemon is rebuilt from the checked parts (ping, version and info without a query, image
  inspect and remove from the checked name); before, those were passed through as written.
- An attach passes the client's raw bytes on only after the daemon switched protocols (101); on any other answer
  the connection ends, so a kept-alive daemon connection can never carry further unchecked requests.
- The body limit is chosen by route before the body is read (1 MB, 64 MB for builds), and image builds get the
  sandbox's memory and CPU limits. Sandbox containers state `IpcMode: private` and empty binds, mounts and devices
  instead of relying on daemon defaults.
- Adversarial tests: 12 ambiguous request heads, smuggling and target rewriting through the gateway, a
  non-switched attach with a smuggled request, and the same raw attempts against a real daemon.

Tool capabilities and per-call approvals, groundwork (evolution S-1/S-2, decision D10):

- New module `nodestro_runtime.tool_policy`: `ToolCapability` (`access` read/write/destructive, `reach`
  internal/external, `source` nodestro/author/admin/derived/default) for every tool kind, the fixed statements of the
  built-in and the provider's server tools, and `capability_of(...)`. A missing or invalid statement is `write` +
  `external`; a stored statement can never claim another source; MCP tools always reach out; own and generated tools
  in a sandbox without network (container, built-in Linux sandbox) count as `internal` (`derived`). Description only:
  no behavior changes yet.
- `DocumentStore.replace_if(collection, key, model, *, field, expected, live_until=None)`: replaces a document only
  while its field still has the expected value and, with `live_until`, only while the time in that field lies in the
  future (PostgreSQL: one conditional `UPDATE` against the database clock; SQLite: inside `BEGIN IMMEDIATE`). Exactly
  one of several concurrent writers wins, also across processes and connections. Tool approval decisions will use it,
  so that they never live in (and are never overwritten by saves of) the run document.
- `ToolInfo.capability` is now a `ToolCapability` and set for every tool (it was a plain dict, for MCP tools only; the
  JSON shape is unchanged): built-in tools carry Nodestro's statement, own and generated tools `write` + `external`
  until their author states otherwise, `internal` (`derived`) when the library's sandbox has no network, MCP tools the
  administrator's class. `ServerToolInfo.capability` shows what the provider's server tools do. `McpTool.capability()`
  returns a `ToolCapability`; `ToolLibrary.isolation(trust)` tells how the library's sandbox isolates a trust class.
- Per-call approval rules, not wired into runs yet: `tool_policy.ToolBinding` (`ask` `auto`/`always`/`never`; `never`
  needs a reason for `write` tools, never applies to `destructive` tools; legacy exemptions hold only for the bound
  tool version), `ToolPreGrant` (one tool version, one schedule, batch or test series, expiry, revocation; not for
  destructive or MCP tools), `CallContext` and `decide(...)` -> allow / deny (`tool.not_granted`,
  `tool.approval_unattended`) / ask. Orchestrator grants only narrow; unattended runs deny what would ask unless a
  pre-grant matches; MCP tools unattended only as read-only with D-7.
- `nodestro_runtime.tool_approvals.ToolApprovals`: stored approval requests (collection `tool_approvals`, key
  `<run id>:<request id>`, a new id per question; node, loop iteration and call id recorded) bound to the exact
  arguments and the tool version or definition hash
  (`arguments_hash`); decisions, expiry and cancellation are compare-and-set from `pending` with the deadline
  checked in the same step (at most 15 minutes); the waiter reads the store, so a decision from another process
  reaches it; `expire_run` for stopped runs. New codes `tool.approval_stale`, `tool.approval_expired`,
  `tool.approval_decided`, `tool.approval_not_found`.
- Per-call approvals in runs, **off by default** (`NODESTRO_TOOL_APPROVALS=1` switches them on; activation is a
  separate step after the security acceptance). While on, `RunManager.tool_gate` (`ToolGate`) applies `decide` in
  the engine's `before_tool` to every tool call except Nodestro's own run tools (memory, files, knowledge
  proposals): a call that asks stores a request and waits in the agent's thread for a stored decision (the run stays
  `running`; 15 minutes at most), then runs unchanged in its sandbox or is denied (`tool.approval_rejected` with the
  comment, `tool.approval_timeout`); unattended runs deny it (`tool.approval_unattended`); a full waiting capacity
  denies it at once (`tool.approval_capacity`; `WaitingSlots`, 3 runs per desktop process). Cancelling a waiting run
  cancels its question; `recover()` and `interrupt()` expire the questions of interrupted runs. New events
  `tool_approval_requested`, `tool_approval_decided`, `tool_denied`. Additive schemas: `AgentConfig.tool_policies`
  (tool name -> `ToolBinding`), `ToolCallRecord.approval` (`ToolCallApproval`). Not yet: stamping existing bindings as
  legacy exemptions (required before any activation), stored pre-grants.
- Server workers hold up to `max_runs` runs (default 2), of which up to `max_waiting` (default 1) may wait for a tool
  approval (`RunWorker(max_runs=, max_waiting=)`, validated by `jobs.check_capacity`: `max_runs >= 1`,
  `0 <= max_waiting < max_runs`). A worker claims only while it holds fewer than `max_runs` runs and none of them
  executes, so without questions it still executes one run at a time; an approved run continues in its own slot
  (never more than `max_runs` per worker); a run that would wait beyond `max_waiting` is denied at once
  (`tool.approval_capacity`). The worker's waiting slots are shared by the gates of all tenants it serves. `serve`
  waits for its held runs when stopped.
- A question that an interruption expired (the reaper after a lost lease, a restart) stops the waiting execution
  instead of letting it go on (`ToolApprovalRequest.closed_because`, `run.interrupted`).
- Legacy exemptions (D10b), `nodestro_runtime.tool_legacy`: `Workspace.stamp_legacy_tool_bindings()` is the
  activation step (an administrator runs it; nothing calls it on its own). It writes every existing tool binding of
  every stored workflow and workflow version as `ask: "never"`, `legacy: true`, bound to the tool's current version,
  stores the report (`tool_approvals_activation/legacy`), and is idempotent. `save_workflow` drops legacy entries the
  stored workflow did not already have for the same node, tool and version, so saving, copying, importing or an API
  client never creates one; a new tool version ends an exemption at the next call.
- Stored pre-grants (D10c): `tool_approvals.ToolPreGrants` (collection `tool_pre_grants`) grants one tool version for
  one schedule, batch or test series, with who and when, an optional expiry and revocation; refused for MCP and
  `destructive` tools and for tools without a version (`tool.pre_grant_*`). The workspace's gate reads them per
  call, so a revocation applies at the next call.
- Approval audit trail (S-2b, D10): collection `tool_approval_audit` (`tool_approvals.ToolApprovalEvent`,
  `record_audit`, `audit_events`, newest 5000 kept) records every decision, every refused decision with its problem
  code, every pre-grant and revocation and the activation step, with actor and time, never the call's arguments
  (only their hash). `ToolApprovals.open()` lists the tenant's open requests of all runs (the approval inbox),
  `ToolPreGrants.all()` the pre-grants; `ToolPreGrant.revoked_by` and `ToolPreGrants.revoke(..., by=)` name who
  revoked; `Workspace.legacy_tool_bindings()` reads the activation report and `stamp_legacy_tool_bindings(by=)`
  names who activated. `LegacyBinding.release` names a published release stamped by the API's activation step.
  Fix: running the activation step again no longer replaces the stored report with an empty one;
  `record_legacy_tool_bindings` adds to it, so earlier exemptions stay listed.
- Changing MCP tools behind the gate (WP-5, D10c): with `NODESTRO_TOOL_APPROVALS=1` the workspace sets
  `McpService.write_gate`, so approved `write`/`destructive` MCP tools become usable, and every such call is asked,
  whatever the binding says (no `never`, no legacy exemption, no pre-grant). The approval binds the arguments and the
  approved definition hash; unattended runs still deny them. Without the switch they stay refused as before.

MCP client (ADR-0007, work packages WP-1 and WP-2):

- New package `nodestro_runtime.mcp`: remote MCP servers over Streamable HTTP through the official SDK (`mcp` 2.x,
  Python 3.11+, dependency with an environment marker; 3.10 reports `mcp.unavailable`). The SDK negotiates the
  protocol version (2026-07-28 `server/discover`, fallback to the 2025-11-25/06-18/03-26 handshake); each operation
  uses its own short connection. `Workspace.mcp` (`McpService`) stores servers, discovered tools, approvals and audit
  events per tenant (`mcp_servers`, `mcp_tools`, `mcp_events`).
- Network protection (`mcp.netguard`): the connection step checks the resolved address it connects to (no gap after
  DNS, rebinding caught), IPv4/IPv6 with embedded IPv4, metadata endpoints, `https` only, redirects re-checked,
  proxy variables ignored, TLS verifies the host name, operator list `NODESTRO_MCP_ENDPOINTS` (required on servers).
- Tokens (`mcp.vault`): sealed with the tenant's `SecretBox` on servers and with `NODESTRO_VAULT_KEY` (from the OS
  keychain, passed by the desktop app) on desktops; otherwise kept in memory for the session only.
- Policy: tools are approved one by one by an administrator, classified `read`/`write`/`destructive` (annotations are
  hints), pinned by definition hash, checked when an agent is built and right before every call (the client lists the
  tools first); changed definitions are blocked (`mcp.tool_changed`); only `read` tools until per-call approvals
  exist; unattended runs need `NODESTRO_MCP_UNATTENDED=1`. Timeouts, output limit, parallel-call limit, cancel with
  the run; results are marked as external data.
- Additive: `ToolInfo.kind` can be `mcp` (with `server_id`, `server_name`, `mcp_name`, `definition_hash`,
  `capability`); `ToolCallRecord.provenance` (MCP calls carry `external_cost: "unknown"`); `AgentBuilder.build(...,
  cancelled=, attended=)`; `ToolLibrary.tool(..., cancelled=)`. New error codes `mcp.*`.

Adaptive Orchestration M1-4 (loop connections):

- A connection can carry `loop` (`max_iterations` 2..20, required; `on_exhausted` `fail` or `continue`): it leads from
  a decision or approval back to an earlier node, and the nodes in between run again (quality loops, revise after a
  rejected approval). Loop connections are not dependencies: they are ignored for the order and for cycle detection,
  and `check_workflow` reports `workflow.loop_unguarded`, `workflow.loop_target`, `workflow.loop_overlap`,
  `workflow.loop_shared` and `workflow.loop_invalid`. A connection that closes a cycle without `loop` is still
  `workflow.cycle`.
- The kernel counts the iterations: `Run.flow_state` stores the kernel's flow state (needed to continue a paused run
  inside a loop, also in another process). A node that runs again keeps its earlier results in `NodeRun.earlier`;
  `NodeRun.iteration` names the iteration (one entry per loop around the node). Node events carry `iteration`, a new
  `loop_iteration` event marks each new round. A loop that reaches its limit fails the run with `run.loop_exhausted`
  (`node`, `max_iterations`) or, with `continue`, follows the other branches of the decision.
- Task templates of nodes in a loop know `{{iteration}}` (1-based), `{{previous}}` (the node's own result of the
  previous round) and `{{feedback}}` (what arrived over the loop connection); `-` in the first round, unchanged
  outside loops. Continuing a failed run runs a whole loop again (`resume.in_loop`).

Adaptive Orchestration M1-2 (the engine on the `orkexa.flow` kernel):

- The engine no longer has a scheduler of its own: `RunManager` executes the nodes that the `orkexa.flow` kernel
  (Orkexa ADR 0009) dispatches and applies what the kernel settles (skipped, blocked, cancelled). New module
  `nodestro_runtime.flow_driver` maps a workflow to a flow (branching nodes declare their ports; a connection
  without a valid port of a decision or approval gets the port `~unconnected`, which never opens) and derives the
  flow state from the run's node states, so stored runs (also paused runs of earlier versions) continue without a
  migration. The rules of `graph.py` are unchanged; `_settle` and the per-node `asyncio.gather` scheduler are gone.
- Orkexa pin `456b92f8f0fe83aaaacf2c634b95e354064e315d` (adds `orkexa.flow`).
- Tests: golden traces of 77 scenarios recorded with the previous scheduler (17 handwritten, 60 random workflows with
  decisions, approvals, failures, cancellation and sub-workflows; statuses, error codes, outputs, prompts, decisions
  and the events of every node in order) pass unchanged; every finished trace is a fixed point of the kernel; an
  architecture test keeps the engine free of its own scheduling.

Nodestro Local Docker (the `local-container` profile and the sandbox gateway):

- New operating profile `local-container` (`NODESTRO_PROFILE=local-container`, also `local_container`): one person
  and SQLite like the desktop, for a runtime that runs in a container and is used from the host's browser. Tool code
  that needs isolation runs only in containers there, as on a server: every trust class except `trusted` needs a
  container, a host subprocess sandbox cannot be created (`sandbox.unavailable`), and the built-in Linux sandbox does
  not count as isolating inside the container (its self-test could pass in a Debian 13 image, but it was validated for
  a person's own computer, not for a container that holds the data and keys). Tool code without libraries runs in the
  runtime image, environments become images (`RuntimeImageSandbox`, no bind mounts). `builtin_sandbox.reason` is
  `server` (containers only) with its own detail text. `SINGLE_USER_PROFILES` names the profiles of one person.
- New `nodestro_runtime.sandbox_gateway` (`python -m nodestro_runtime.sandbox_gateway --listen SOCKET`): a Docker
  Engine API endpoint on a UNIX socket that holds the real daemon's socket and forwards only what Nodestro's
  sandboxes need, in their safe form. Containers are created from image, command and working directory only; the
  gateway sets no network, a read-only root file system, no capabilities, `no-new-privileges`, user 65534, memory,
  CPU and process limits up to its maxima, two `noexec` tmpfs and its label, and refuses requests for anything else
  (privileged, mounts, devices, host namespaces, ports, other images, unmasked paths). Only containers it created
  can be attached to, started, waited for, killed, inspected or removed; it removes them after a maximum lifetime and
  at start and stop, and limits how many run at once. Images can only be built from exactly the runtime's Dockerfile,
  with a lock of exact versions with hashes, the gateway's own Orkexa and the tag the runtime computes; only
  `nodestro-env:<key>` images can be inspected or removed. Everything else (exec, archives, pulls, volumes,
  networks, events, BuildKit sessions) is refused. Limits: `NODESTRO_SANDBOX_MAX_CONTAINERS`, `_MAX_MEMORY_MB`,
  `_MAX_CPUS`, `_MAX_PIDS`, `_MAX_LIFETIME`, `_BUILD_NETWORK`, `_INSTANCE`. `ContainerImages` passes `DOCKER_BUILDKIT` to
  the docker CLI (0 selects the classic builder, which the gateway checks). Tests: the policy on the requests docker
  29 really sends and on hostile ones, the protocol (including attach with half-close) against a stand-in daemon,
  and with `NODESTRO_DOCKER_TESTS=1` a real image build, a tool call without network or writable files, and refused
  escape attempts through the docker CLI.

Request options cannot add tools or a provider-hosted MCP server (security fix, MCP-0 finding F-1):

- An agent's `request_options` could switch on a provider's own MCP connector and add tools past the tool library:
  Anthropic `mcp_servers` with an MCP beta, `extra_body` with `tools` (e.g. an `mcp_toolset`) and an
  `anthropic-beta` header, or OpenAI `extra_body.tools` with an `mcp` tool. Confirmed with SDK requests to a
  loopback stub. `providers.check_request_options` now refuses `mcp_servers` and objects of an `mcp*` type wherever
  they appear, MCP betas, the `anthropic-beta` header in `extra_headers`, and the fields that define the model, the
  conversation or the tools inside any `extra_body`. Other options stay allowed (e.g. `extra_body` with provider
  parameters, other headers, other betas). It runs in `AgentBuilder.build` (every run, agent test, published and
  exported release) and in `Connection.options`; the inspector shows it. Refusals use the existing code
  `config.reserved_request_options` with the refused paths in `keys`, so the studio's text and the Workflow
  Architect's automatic fix apply unchanged.

Price refusals with their own codes (PRICING-1, strict price mode):

- A request refused because its model has no known price (Orkexa's `UnpricedModelError`) now fails with
  `pricing.price_unknown`, one whose price failed the price policy (`UnreliablePriceError`) with
  `pricing.price_unreliable`, both with the parameter `model`. Before, both read `run.budget_exceeded`, which said
  the budget was used up when it was not. `problems.price_problem` maps them for runs and for agent tests.

Built-in Linux sandbox (replaces the bubblewrap prototype of PR-13; ADR-0006 point 7):

- New `nodestro_runtime.builtin_sandbox.LinuxSandbox`: runs tool code isolated without Docker, Bubblewrap or any
  installed tool. The sandbox process confines itself before it reads any tool code
  (`nodestro_runtime.sandbox_confine`): Landlock (ABI 4 or newer) allows reading only the interpreter, the system
  libraries and the call's files, and writing only the call's private `tmp`; seccomp refuses new processes,
  `execve`, every socket, `ptrace`, System V IPC, signals and limit changes aimed at other processes, namespaces,
  mounts and other kernel interfaces; resource limits cover memory, CPU, file size, open files and processes; the
  environment is empty. The host adds the wall-clock timeout, a size limit for `tmp`, cancel and cleanup. Each run
  verifies the confinement from the inside; if any step fails, no tool code runs.
- It needs no user namespaces, so Ubuntu 24.04's AppArmor restriction of unprivileged user namespaces does not
  affect it (bubblewrap does not work there without an administrator's AppArmor profile, measured on a clean
  Ubuntu 24.04 machine). A root user is refused.
- `LinuxSandbox.status()` checks the computer once: Linux x86-64, a tested distribution (`TESTED_SYSTEMS`: Ubuntu
  24.04 and Debian 13), the kernel features and a confined self-test call. The reason is reported when it is not
  available (`platform`, `untested-system`, `landlock`, `seccomp`, `root`, `paths`, `verify`, `failed`).
- The sandbox policy counts it as isolating (`isolation_of` → `"kernel"`) only in the desktop profile and only where
  its status is available. A server keeps tenants' code in containers.
- On the desktop, code that needs isolation and was given a host subprocess (the default sandbox or a Python
  environment's interpreter) runs in the built-in sandbox automatically (`ToolLibrary._require`). Without it, such
  code is refused with `sandbox.isolation_required`, whose message and new `reason` parameter say why; there is no
  fallback to a host subprocess. User-authored code keeps the configured sandbox. Untrusted code may now use
  approved third-party libraries where the built-in sandbox is available (`PythonEnvironments.isolation()` →
  `"kernel"`).
- New `python -m nodestro_runtime.sandbox_probes`: the security probes (files, data directory, keys and
  environment, network, processes, foreign processes, kernel interfaces, limits, timeout, cancel, cleanup,
  manipulated paths, fail-closed) and compatibility checks (standard library, the examples' built-in tools,
  generated tool tests, repeated, parallel and long calls, timeouts, a compiled library, overhead). A probe passes
  only if its code ran in the sandbox and every attempt was refused. CI runs them as a normal user.
- Tests see a computer without the built-in sandbox unless a module is marked `real_sandbox_status`
  (`tests/conftest.py`), so the rules for computers without isolation stay tested everywhere.

Comparing estimates with the providers' bills (PRICING-1, phase N7, optional; runtime part):

- New `nodestro_runtime.reconciliation.CostReconciliationService`. It fetches the providers' cost reports and
  compares them with Nodestro's estimates per UTC day, provider and model. Adapters: `OpenAICostsAdapter`
  (`/v1/organization/costs`, admin key, optionally limited to projects) and `AnthropicCostAdapter`
  (`/v1/organizations/analytics/cost`, admin or `read:analytics` key, amounts in cents, 31-day windows,
  `data_refreshed_at` as the watermark, optionally limited to workspaces). Admin keys are passed in by the caller
  and are never logged or stored by the runtime.
- `nodestro_estimates(workspace, start, end)` sums the estimates of workflow runs (without imported traces and
  sub-workflow runs, which count in their parent) and of the usage ledger.
- A deviation (`warning`) needs both thresholds: more than 5 % and more than 1 USD (`ReconciliationSettings`).
  A day the provider has not finalized yet is `pending`, a day with unknown costs is `incomplete` (never a
  deviation), billed lines without a model are `unattributed`, and a model the report does not name is
  `not_comparable`. Dated snapshot names in reports (`gpt-5-2026-08-07`) match the alias Nodestro used. A failing
  provider is reported and does not hide the others.

Costs priced with the workspace's catalog (PRICING-1, phases N5 and N6, runtime part):

- New `nodestro_runtime.cost_tracking.PricedTracker`: the `BudgetTracker` of workflow runs, sub-workflows and
  agent tests, and `Workspace.tracker(budget)` for the API's other AI operations. It hands Orkexa the
  workspace's resolver, so overrides and synced prices apply everywhere. It remembers which requests were
  priced with a stale price. Costs are calculated once, when a request ends; later price changes never alter
  stored costs.
- `Usage` gained price references: `known_cost_usd` (a lower bound when incomplete), `cost_complete`,
  `unpriced_requests`, `stale_price_requests`, `missing_cost_components`, `pricing_versions`, `pricing_sources`
  and `pricing_quality` (the lowest involved). `usage_of(summary, records)` fills them; `add_usage` merges them.
  Usage stored by earlier releases reads unchanged (`known_cost_usd` and `cost_complete` are `None`); in sums
  an older unknown cost counts as unpriced and an older known cost as known.
- `UsageLog.record(..., tracker=...)` stores the same references for operations outside runs.
- New setting `price_mode`: `standard` (default; unknown and stale prices warn, as before) or `strict` (a
  request without a known price, or with a price not checked for seven days (`pricing.stale_price`), is not
  sent; a request whose cost turned out unknown stops the operation after it was recorded). Existing budget
  settings are unchanged.
- The pre-check (`check.model_unpriced`) uses the workspace's prices instead of Orkexa's built-in table.

Automatic price updates (PRICING-1, phase N3):

- New `nodestro_runtime.pricing_sync.PricingSyncService`. It asks each price source for a candidate and hands it
  to `PricingService.submit`, which validates it and activates what may be activated. A failing source changes
  nothing; the last valid prices stay active and the error is recorded (`PricingState.last_error`).
- Sources: `OrkexaBuiltinSource` (Orkexa's verified Anthropic prices, no network), `SnapshotFileSource` (a
  reviewed snapshot file an operator supplies), and `LiteLLMSource` (LiteLLM's community-maintained cost map:
  OpenAI, Anthropic and Gemini text models with cache rates, long-context tiers and the flex, priority and batch
  service tiers; quality `unverified`). The official price pages of the providers are HTML pages without a
  stable machine-readable interface and are not scraped.
- Downloads are HTTPS only, from allowed hosts, with timeouts, a 32 MB limit and three attempts with
  exponential backoff. One sync runs at a time per process, and an optional lock covers several processes
  (the server's worker). `due()` follows the refresh interval (24 hours); `run()` is the background loop: it
  syncs at start when prices are due and then whenever they become due. Model requests never trigger a download.

Managed model prices (PRICING-1, phases N1, N2 and N4):

- New `nodestro_runtime.pricing.PricingService`, available as `Workspace.pricing`. It holds the workspace's
  price catalog: versioned reference snapshots in Orkexa's `orkexa.price-snapshot/1` format (collection
  `price_snapshots`, one active version, switched atomically, old versions kept), individual prices in their
  own collection (`price_overrides`), and a history (`price_events`). `resolver()` returns the Orkexa
  `PriceResolver` with the overrides applied. The resolver is the same instance across changes and is
  republished, so running requests keep their pinned prices.
- Without a stored snapshot the catalog starts from Orkexa's built-in prices, so an offline or new installation
  still prices Claude models. The last activated snapshot survives restarts. Nothing here touches the network.
- `submit(candidate)` validates prices (provider and model ids, USD only, implausible rates, missing source)
  and drops invalid ones. It rejects a candidate that is empty, mostly invalid or truncated, and the active
  prices stay as they are. Rate changes above 20 % without official confirmation (quality `verified` or
  `contract`) and models that vanish from their source are held back as `pending_review`; everything else is
  activated. An unconfirmed price never replaces an officially confirmed price of another source.
  `approve()`/`reject()` decide a review.
- Every price has a status (`current`, `stale`, `unknown`, `pending_review`, `custom`) and, separately,
  Orkexa's quality and its source. Freshness defaults: a warning after 48 hours without a successful check and
  `critical` after seven days (`PricingSettings`).
- On a server, `WorkspaceStorage.price_reference` hands tenants the server's reference prices read-only; each
  tenant's overrides and events stay in its own store. Without it, a tenant sees Orkexa's built-in prices.
- `catalog.models(pricing)` takes prices, status, quality, source and version from the catalog. A model without
  a price keeps `None` prices and gets the status `unknown`. No schema migration: the new collections use the
  existing document tables of SQLite and PostgreSQL.

Orkexa pricing engine (PRICING-1, phase N0):

- Orkexa pinned to `76883f700e68f10240f744a4af3ed37f5ec54674` (orkexa PR #13: versioned price snapshots, price
  resolver, itemized cost breakdowns, price policies for budgets). It also removes Orkexa's claude-agent
  compatibility layer, which Nodestro never used.
- Nodestro still uses only `resolve_pricing` and `MODEL_PRICING`, which keep their behavior. Prices change where
  Orkexa corrected them: Claude Sonnet 5.5 cache reads cost $0.10 per million tokens (was $0.20), and Anthropic
  fast mode and US data residency are now priced. OpenAI and compatible responses with a non-standard
  `service_tier`, and requests with unpriced fees (for example hosted web search), now have an unknown cost
  instead of a standard-rate one.

Built-in sandbox prototype (M6 PR-13, ADR-0006 point 7):

- `nodestro_runtime.builtin_sandbox.BubblewrapSandbox`: a Linux x86-64 candidate built on bubblewrap. Each call gets
  new user, PID, network, IPC, UTS and cgroup namespaces, a root file system with only the interpreter and its
  libraries, an empty environment, a small private `/tmp`, and a seccomp filter. The filter refuses new processes,
  non-`AF_UNIX` sockets, `ptrace`, mounts, namespaces and similar kernel interfaces.
- `tests/test_builtin_sandbox.py` runs the acceptance tests of ADR-0006 against it; the new CI job
  `builtin-sandbox` runs them with bubblewrap installed and fails instead of skipping.
  `python -m nodestro_runtime.builtin_sandbox` prints the timings.
- **Not isolating.** `isolation_of` still returns `"none"` for it, so untrusted code is refused exactly as before,
  and no setting selects it. Nothing changes for the desktop or the server.

Runs executed by a worker (M5/M6 PR-12, found by the server's end-to-end tests):

- A run is stored as `running` when its execution starts, so other processes (the API of a server) no longer show
  it as `queued` until it ends.

Model endpoints on a server (M5/M6 PR-12):

- `WorkspaceStorage.endpoints` / `tenant_storage(..., endpoints=...)`: a tenant's endpoint providers (`compatible`,
  `ollama`, `vllm`) reach only the origins the operator allows; others are refused when stored and when used
  (`provider.endpoint_not_allowed`), so a tenant cannot make the server reach internal services. The desktop is
  unchanged. New helpers `providers.endpoint_origin` and `providers.check_endpoint_allowed`.

Server settings, tenant secrets and containers without bind mounts (M5/M6 PR-12, runtime part):

- `profile.server_unavailable` is gone. `RuntimeProfile.validate(settings)` accepts the server profile with
  `server_settings.ServerSettings`; without them it is refused with `profile.invalid`, and so is a server
  `Workspace` without tenant storage. Nothing about the desktop changes.
- New `server_settings`: `ServerSettings.from_env` (database, public origin, OpenID Connect issuer, client and
  secret, secret key; `…_FILE` variants; https required outside loopback; all problems listed at once) and
  `SecretBox` (AES-256-GCM, per-tenant HKDF key, bound to tenant and field). New error code `secrets.unreadable`.
- `WorkspaceStorage.secrets` / `tenant_storage(..., secrets)`: tenant provider keys are stored encrypted; plain-text
  or undecryptable keys count as missing; the server's environment keys are not used for tenants
  (`provider_status(..., environ)`).
- `postgres_import` leaves provider keys behind and says so in `ImportReport.not_imported`.
- `container_images.StreamedSandbox`: images carry Orkexa (part of the image key, `orkexa_fingerprint`), tool files
  travel on stdin into a `tmpfs`; no bind mounts, so a remote Docker daemon works. `LocalImageSandbox` is an alias.
- `RuntimeImageSandbox` and `PythonEnvironments.runtime_sandbox/runtime_image`: on a server, tool code without
  libraries runs in an image of the runtime requirements, built on first use. `make_sandbox(..., container=...)`.
- The `postgres` extra adds `cryptography`.

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
- MUP-ONA (security and database backend plan; kept as one group at the end of this section):
  - tests: legacy fixtures and migration checksum list. `tests/fixtures/migration_checksums.json` fixes the
    SHA-256 of migrations 0001-0010 (T-MIG-1); the PostgreSQL dumps FX-PG-0 and FX-PG-0v (two tenants, files,
    chunks, an active and a building vector space, a zero vector, memory files) with their generator
    `make_legacy_pg.py` and a restore test (T-FX-1); the ranking corpus FX-RANK-0 with its PostgreSQL results
    (`tests/fixtures/ranking/`, T-RANK-0); every CI pytest job compares its skipped tests with
    `tests/ci/skipped-<job>.txt` (`scripts/check_skips.py`, T-SKIP-1).
  - Price sync (A7, VB-1): LiteLLM's cost map is read at a fixed commit and must match the SHA-256 shipped with
    the release (`LITELLM_COMMIT`, `LITELLM_SHA256`); another file is not imported and a warning is logged.
    Price downloads no longer follow redirects. See `docs/pricing-sync.md`.
  - tests/ci: the tenant SQL check is anchored in CI (A11). `check_sql`/`scan_package` take `allow_any` (default
    off): `tenant_id = ANY(<param>)` no longer binds a reference and needs an allowlist entry with its reason
    (no statement of the runtime or the API uses it). The API scan tests carry the marker `tenant_api_scan` and
    read `NODESTRO_API_SRC`; `NODESTRO_REQUIRE_API_SCAN=1` fails instead of skipping. The new job
    `tenant-sql-api` scans a public checkout of nodestro-api with this commit's checker, including a planted
    leak. `CONTRIBUTING.md`: a checker or migration change needs a green nodestro-api `tenant-sql` run.
  - Security (A8, VB-7): the MCP egress check refuses deprecated IPv6 site-local addresses (`fec0::/10`, reason
    `site_local`), which `ipaddress` counts as global; Axentra's webhook check already refused them (found by the
    shared security test vectors).
  - Security (A8, VB-7): credential redaction (`sanitize`: trace exports, `redact_secrets`) also removes
    `Authorization: Basic` credentials (also `Proxy-Authorization` and header dicts), `Cookie:` / `Set-Cookie:`
    header values up to the line end, and the password in a URL's user information
    (`postgresql://app:<password>@db`); the prefix stays readable. Axentra's redaction already removed them (found by
    the shared security test vectors).
  - tests/ci: shared security test vectors (A8, VB-7). `tests/vectors/security-vectors.json` (version 1) is the
    canonical corpus of egress, ID token and redaction cases with the documented `known_differences` between
    Nodestro and Axentra; `tests/test_security_vectors.py` runs the egress cases against `netguard` and the
    redaction cases against `sanitize` (T-VEC-1). Axentra and nodestro-api keep checksummed copies. The new job
    `security-vectors-sync` checks out axentra anonymously (no secret) and compares its copy and `SOURCE.md` with
    the canonical file (T-VEC-4, `NODESTRO_AXENTRA_SRC`, `NODESTRO_REQUIRE_VECTOR_SYNC=1`); the main test matrix
    lists the comparison as skipped with that reason. Maintenance: `tests/vectors/README.md`.
  - A1 (PS-1): PDF text is read with limits in a separate process. Knowledge uploads, input files
    (`file_formats.extract`) and adoption previews read PDFs through `documents.pdf_pages`, that is orkexa's
    `pdf_pages_isolated`; the runtime no longer imports orkexa's private `_pdf_pages`, and a knowledge upload no longer
    reads a PDF a second time in the API process (`Index.add_file`, `pdf_has_text`; `documents.pdf_has_text` is
    removed). New settings `NODESTRO_PDF_TIMEOUT_SECONDS` (30), `NODESTRO_PDF_MAX_PAGES` (2000),
    `NODESTRO_PDF_MAX_CHARS` (5,000,000) and `NODESTRO_PDF_MAX_MEMORY_MB` (512, Linux only), read by
    `documents.pdf_limits`; an invalid value is `document.limits_invalid`. Behavior change: a PDF over a limit is
    refused with the new code `document.too_complex` (`name`, `reason`) and nothing is stored; a PDF with text only
    after page 20 is no longer marked as a scan. Orkexa pinned to `f89102c` (orkexa PR #59, MUP-ONA A1, which also
    brings A10: SQLite integrity checks, the FTS5 race fix and search limits).
  - B0: one Axentra commit for every extra. The `postgres` and `dev` extras pin Axentra `273e675` (Axentra A4, A6,
    A7 and B0; it includes axentra #42, IP and request id on login and setup audit events). No runtime code change.
  - B1 (runtime part): `PostgresIndex`, `PostgresFileStore` and `PostgresMemory` report `capabilities` (orkexa's
    storage contracts: `partitioned` and `concurrent_writers`; the index adds `vectors` and `reindex` with an
    embedding model), and `tests/test_orkexa_conformance.py` runs orkexa's public conformance suite
    (`orkexa.testing.conformance`) against them, with the index ranking vectors in the database (pgvector) and in
    the runtime. The ten documented PostgreSQL drifts are strict expected failures (`MUP-DRIFT-1` to `-10`, fixed by
    MP-B3); every partition case passes. Through orkexa's `Index` contract the PostgreSQL index now refuses a query
    over 1000 characters or 64 distinct words with `RetrievalError` `query.too_long`, as SQLite does since A10.
    New catalog code `run.agent.VectorError` (orkexa's vector check). Orkexa pinned to `b3d6fae` (orkexa #63, the B1
    conformance suite).

## 0.2.0 (2026-10-29)

- First version as a repository of its own: extracted from `nodestro` 0.1.0 (commit 15e2076, now
  `nodestro-legacy`) without new features and without behavior changes. The origin of every file is listed in
  PROVENANCE.md.
- Dependencies pinned to exact commits (Orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); the tested
  combination is recorded in `nodestro-studio/platform/versions.json`.
- CI with the repository secret `NODESTRO_READ`.
- New: `Workspace` as the assembly of the runtime for one data directory, `extensions` (contract 1 for
  integrations), `testing` (simulated provider clients). OCR moved to `nodestro-integrations`.
