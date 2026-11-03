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

## Which model

`local_models.CATALOG` holds the seven suggested models with registry digest, download size, listed capabilities,
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

A recommendation is a suggestion: callers store the chosen model explicitly. New installations use
`ollama:qwen3.5:4b` for agents and `ollama:qwen3.5:9b` for assistants; existing installations keep the cloud
defaults they had (`Settings.model_defaults`), so an update changes no model.

While a local server answers, preflight checks each local model a workflow uses: `check.local_model_missing` (not
installed), `check.local_model_not_chat` (an embedding model as an agent) and `check.local_model_no_tools` (an agent
with tools on a model whose `/api/show` lacks `tools`). The check never starts the managed server or downloads
anything; without a running server the run itself reports a missing model.

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
continues where it stopped. Files uploaded while the model server is down are indexed for full text and reported as
`incomplete` until the next reindex embeds them. Vector and hybrid search never fall back to full text: a missing
index is a preflight error.

An agent's view (`ScopedIndex`) limits the candidates to the agent's files *before* ranking, in every mode, so
other files never take their places. On PostgreSQL every query is tenant-scoped; pgvector ranks the vectors where
installed (migration `0010` installs it when available), the runtime ranks the same rows otherwise.

## Offline

The strict offline mode (`Settings.strict_offline`) makes preflight refuse cloud models (also fallbacks, judges,
orchestrators), a model server that is not local, server tools and remote MCP servers; downloads are refused and the
managed server runs without proxy variables. Local models have no external API billing: Orkexa prices them at zero
with source `local`, and usage counts them in `Usage.local_requests`. Local operating costs (hardware, energy) are
not recorded, so an interface shows "local, no API billing" instead of a total of 0.
