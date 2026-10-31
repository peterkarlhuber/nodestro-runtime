# Nodestro Runtime

The workflow engine of [Nodestro](https://github.com/peterkarlhuber/nodestro-studio) as a standalone Python
library, without a server and without a user interface. It runs multi-agent workflows (agents, decisions,
human approvals, sub-workflows, result files), builds every agent with
[Orkexa](https://github.com/peterkarlhuber/orkexa) and stores everything in one SQLite database inside a data
directory.

The runtime is part of the Nodestro platform; [PLATFORM.md](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/PLATFORM.md)
gives an overview of all repositories. The studio and the HTTP API (`nodestro-api`) are built on top of this
package; the runtime itself depends on neither.

## Contents

| Area | Modules |
| --- | --- |
| Workflow model | `schemas`, `graph`, `conditions`, `forms`, `catalog`, `versions`, `release` |
| Execution | `engine` (DAG, parallel branches, live events, budget, cancellation), `launcher`, `preflight`, `resume`, `run_views`, `usage_log`, `traces` |
| Agents | `agent_builder` (`AgentConfig` → `orkexa.Agent`), `agent_test`, `providers` (keys, endpoints, capabilities) |
| Tools | `tool_library`, `builtin_tools`, `tool_drafts`, `python_deps`, `python_envs`, `python_tools` |
| Knowledge, memory, files | `knowledge`, `memory_spaces`, `documents`, `input_files`, `file_formats`, `file_delivery`, `office`, `reports`, `adoption`, `sanitize`, `preview` |
| Assembly | `workspace` (`Workspace`: every service of one data directory), `storage` (`DocumentStore`, `SQLiteStore`), `sqlite_tables` |
| Extensions | `extensions` (contract for integrations, entry point group `nodestro.integrations`) |
| Testing | `testing` (simulated provider clients and uv; never a real request) |

**Public API.** `workspace.Workspace`, the models in `schemas`, `problems` (`NodestroError`, `Problem`),
`extensions` and `testing` are the stable interface for Python code. The other modules are used by the
Nodestro API and studio and may change between versions.

## Installation

Requires Python 3.10 or newer (CI tests 3.10 and 3.13).

```bash
pip install "nodestro-runtime @ git+https://github.com/peterkarlhuber/nodestro-runtime.git@<commit>"
```

Orkexa is installed with it, pinned to commit `b85133d53d58616dfba164d45ccdc035766d4144` (Orkexa has no
tags). Both repositories are private, so pip needs read access, for example through
`git config url."https://<token>@github.com/peterkarlhuber/".insteadOf "https://github.com/peterkarlhuber/"`.
The tested combination of all Nodestro components is recorded in `nodestro-studio/platform/versions.json`.

## Quickstart (no provider account needed)

This example runs completely offline: `ScriptedClient` from `nodestro_runtime.testing` answers in place of
the provider, so it costs nothing and always gives the same result.

```python
import asyncio
import tempfile

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

# Each request is answered from the script whose key occurs in the agent's system prompt.
client = ScriptedClient({"OFFER": [text("Offer: 10 units, delivered Friday, 1,200 EUR.")]})

workflow = WorkflowIn.model_validate(
    {
        "name": "Offer",
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "writer",
                "type": "agent",
                "label": "Writer",
                "agent": {"name": "Writer", "system_prompt": "OFFER: write a short offer."},
            },
            {"id": "out", "type": "output", "label": "Result"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "writer"},
            {"id": "e2", "source": "writer", "target": "out"},
        ],
    }
)


async def main() -> None:
    data_dir = tempfile.mkdtemp()
    async with Workspace(data_dir, client_factory=lambda _provider: client, integrations=[]) as workspace:
        saved = workspace.save_workflow(workflow)
        run = await workspace.run(saved, RunIn(input="10 units by Friday"))
        done = await workspace.wait(run.id, timeout=60)
        print(done.status, done.output)  # succeeded Offer: 10 units, delivered Friday, 1,200 EUR.


asyncio.run(main())
```

To use a real model, leave out `client_factory` and provide the provider's key. A key from the environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`) wins over one stored in the settings
of the data directory (`workspace.update_settings(...)`). OpenAI-compatible endpoints, Ollama and vLLM are
configured with a base URL instead. Models are named `provider:model`, for example `openai:gpt-5.5`; a name
without a prefix is an Anthropic model. The same data directory (default `~/.nodestro`) can be opened by the
studio and the API.

## Using the runtime

**Lifecycle.** A `Workspace` owns one data directory; open one per process and directory. The workspace holds
a lock on `<data_dir>/nodestro.lock` until it is closed: a second workspace on the same directory, in this or
another process, is refused with `workspace.in_use` (it would mark the first one's runs as interrupted and run
its schedules a second time). Runs execute as
tasks of the asyncio event loop that started them, so start runs from async code. `async with Workspace(...)`
(or `await workspace.aclose()`) cancels the runs of this process, waits until each one has stored its final
state and closes the database. `close()` does the same without waiting. Paused runs are not affected: they
wait in the database for their approval and continue after the next start. Runs that were executing when the
process stopped are marked as interrupted (`run.interrupted`) when the data directory is opened again and can
be continued.

**Workers (server, ADR-0004).** A workspace on handed-in tenant storage takes no directory lock and recovers
nothing when it opens, because API and worker processes open the same tenant at once. `nodestro_runtime.jobs`
coordinates them through PostgreSQL: `QueuedRuns(queue, tenant, workspace.runs).attach()` makes an API process
store and queue runs instead of executing them; `RunWorker(queue, workspaces).serve(stop)` claims run jobs
(`FOR UPDATE SKIP LOCKED`), executes them, renews their leases and writes their events to `nodestro_run_events`
(`PostgresEventLog`); `reap(queue, workspaces)` marks the runs of jobs whose lease expired as interrupted. Such a
run is never executed again by itself. `LeaderLock` and `ScheduleFirings` let exactly one process fire each
schedule slot once. Only runs go through the queue so far.

**Operating profile.** A workspace serves one of two profiles (`nodestro_runtime.profile`): `desktop`, the
default and the behavior described here, or `server` (many users, PostgreSQL, OpenID Connect, workers, container
sandbox). The server starts only with complete `server_settings.ServerSettings` (read from the environment by
`ServerSettings.from_env`; `RuntimeProfile.validate(settings)`), and its workspaces are its tenants, opened on
`tenant_storage(...)`; a server profile without settings, or a server workspace without tenant storage, is refused
with `profile.invalid`. `Workspace(data_dir)` always opens the desktop profile, whatever the environment says. `Workspace(profile=RuntimeProfile.from_env())` takes the profile from
`NODESTRO_PROFILE` (`desktop` or `server`, default `desktop`) and `NODESTRO_DATA_DIR` (default `~/.nodestro`);
unknown values are refused with `profile.invalid` instead of falling back to a default. Given both a data
directory and a profile, they must name the same directory. Every refusal happens before a file or the database is
touched.

**Running.** `await workspace.run(workflow, RunIn(...))` checks the workflow first (the same pre-check the
editor shows, `nodestro_runtime.preflight`), refuses a run that would fail for a known reason and returns the
queued run. `await workspace.wait(run_id)` returns it once it finished or paused. `workspace.cancel(run_id)`
stops a running or paused run.

**Approvals.** An approval node pauses the run (`status == "paused"`, the node is `awaiting_approval`) until
a person decides with `workspace.decide(run_id, node_id, "approved" | "rejected", comment)`. Nothing is ever
approved automatically, a decision is recorded once, and a pending approval survives restarts. `decide`,
`cancel` and starting a run may also be called from another thread while the event loop runs; they are
carried out on that loop.

**Continuing.** `await workspace.resume(run_id)` continues a failed or cancelled run as a new linked run. A
step's earlier result is reused only when its fingerprint (configuration, connections, tool code, inputs and
files, sub-workflow version) is unchanged; reused steps cost nothing again. Steps that run again after they
had called own tools need `confirm=True`.

**Errors.** Expected failures raise `nodestro_runtime.problems.NodestroError` (a `ValueError`) with a stable
`code`, `params` and an English message, for example `api_key_missing`, `workflow.blocked` or
`approval.not_pending`. Failures during a run are recorded on the run and its nodes (`error`, `error_code`,
`error_params`) instead of being raised. Unknown ids raise `KeyError`.

**Events.** `workspace.runs.subscribe(run_id)` returns the events so far and a queue of the next ones (`None`
ends the stream). Event numbers (`seq`) increase by one without gaps for the whole life of a run in a process.
Text and thinking deltas beyond 5,000 events are published but not kept for late subscribers, and a
subscriber that falls 10,000 events behind is ended (it reads the run again).

**Tools.** Built-in tools run in the process. Custom and AI-generated tools are checked statically and run in
a sandbox: a separate Python process with an empty environment, time and memory limits (`subprocess`), or a Docker
container without network (`docker`). The process sandbox limits what a tool can do by accident; it is not a
security boundary against hostile code. Third-party Python packages are installed only after a person approved
the exact versions of every package (`python_envs`), into a separate environment per workflow or tool.

**Sandbox policy (ADR-0006).** Every stored tool has a trust class (`ToolInfo.trust`): `trusted` (built in or from an
installed integration), `user-authored` (every version of it was written by a person in the tool editor) or
`untrusted` (AI-generated, including approved generated tools and tool drafts, or of unknown provenance).
`nodestro_runtime.sandbox_policy.SandboxPolicy` decides before any code runs: untrusted code runs only in an
isolating sandbox (Docker); in the process sandbox it is refused with `sandbox.isolation_required`, and nothing is
executed. There is no setting, environment variable or confirmation that lifts this. Consequences without Docker:
tool generation does not start, tool drafts cannot be tested or approved, and agents that use untrusted tools fail
the workflow check before a run starts. Provenance is stored per tool (`tool_provenance`); editing an untrusted tool,
re-creating it under the same name or copying its code into a new tool keeps it untrusted, tools stored before this
release are untrusted, and a restored backup makes every restored tool untrusted. In the process sandbox, untrusted
tools cannot use third-party libraries (`sandbox.libraries_need_isolation`), because the library environments run as
host processes. In the Docker sandbox they can: there an approved environment is a container image (see below). In
the server profile, only containers run tool code.

**Server settings and tenant secrets (plan step PR-12).** `ServerSettings.from_env` reads `NODESTRO_DATABASE_URL`,
`NODESTRO_PUBLIC_ORIGIN` (`https://`; `http://` only for loopback), `NODESTRO_OIDC_ISSUER`,
`NODESTRO_OIDC_CLIENT_ID`, `NODESTRO_OIDC_CLIENT_SECRET` and `NODESTRO_SECRET_KEY` (32 random bytes, base64); each
secret may come from a file (`…_FILE`), never from both. Every missing or unsafe value is listed at once
(`profile.invalid`); secrets never appear in `repr` or errors. A tenant's provider keys are stored encrypted
(`SecretBox`: AES-256-GCM with a per-tenant key derived by HKDF-SHA256, bound to tenant and field, so a value copied
elsewhere does not decrypt); a key that cannot be decrypted or was stored in plain text counts as missing. The
server's own environment variables (`ANTHROPIC_API_KEY`, …) are never used for a tenant. Endpoint providers
(`compatible`, `ollama`, `vllm`) reach only the model endpoints the operator allows (`WorkspaceStorage.endpoints`,
origins); any other base URL is refused with `provider.endpoint_not_allowed`, so a tenant cannot make the server
reach internal services. The desktop import leaves
provider keys behind. Changing `NODESTRO_SECRET_KEY` makes stored keys unreadable (no rotation yet). Needs the
`postgres` extra (psycopg, cryptography).

**Containers without bind mounts.** Images built by `container_images` contain Orkexa (`/opt/orkexa/orkexa`, part
of the image key), and `StreamedSandbox` sends the tool's files on stdin into a `tmpfs` work directory instead of
mounting host paths, so the Docker daemon may run on another host or in another container (`DOCKER_HOST`). On a
server, tool code without libraries runs in the runtime image (Nodestro's runtime requirements only), built on first
use (`RuntimeImageSandbox`); no image has to be built by hand. The desktop keeps Orkexa's `orkexa-sandbox` image for
tools without libraries.

**Libraries in the Docker sandbox (ADR-0006 point 6).** With the Docker sandbox, `nodestro_runtime.container_images`
turns an approved lock file into an image `nodestro-env:<key>`, where the key is the SHA-256 of the lock, the Python
version and the base image (`python:<version>-slim`). Locks are compiled with `--generate-hashes`. The build
downloads exactly the locked wheels with `pip download --require-hashes --only-binary=:all:` (no source builds, so
no package code runs; a package without a Linux wheel fails with `deps.wheels_required`), then installs them
offline with `--no-index --require-hashes` into a final image that runs as `nobody` and keeps no download cache,
certificate or proxy setting. Installing an environment means building its image and checking in it, without
network, that every needed module can be found; no host virtual environment is created. Tool calls run in the
image with Orkexa's container flags (no network, read-only file system, no capabilities, resource limits) and
`docker run --pull never`, so a missing image is never looked for in a registry. If Docker or the image is missing,
the call fails; there is never a fallback to a host process. An environment approved before this release has no
hashes and must be approved again (reason `hashes`). The download stage gets `HTTP(S)_PROXY`/`NO_PROXY` as build
arguments and the certificate authority in `NODESTRO_BUILD_CA_FILE` (or `SSL_CERT_FILE`);
`NODESTRO_DOCKER_BUILD_NETWORK` chooses the build network. The docker CLI gets no provider keys.

**Storage.** Every component stores its documents through the `DocumentStore` protocol
(`nodestro_runtime.storage`); `SQLiteStore` (still importable as `Store`) implements it on the table
`nodestro_documents`. The SQL Nodestro runs on Orkexa's own tables (knowledge chunks, memory) is in
`sqlite_tables`; no other module opens or uses a database connection. The database records its schema version in
`PRAGMA user_version` (0, unchanged since Nodestro 0.1). When a later release needs a migration, the workspace
copies the database to `<data_dir>/backups/pre-migration-<from>-to-<to>-<time>.db` first (or calls
`Workspace(before_migration=...)`), then runs every step in one transaction; a failing backup or step leaves the
database unchanged (`storage.backup_failed`, `storage.migration_failed`), and a database of a newer release is
refused (`storage.schema_too_new`).

**PostgreSQL (server profile, preview).** `nodestro_runtime.storage_postgres` implements the same protocol on
PostgreSQL; it needs the optional dependency `pip install "nodestro-runtime[postgres]"` (psycopg 3) and is not
used by the desktop. `PostgresDatabase(conninfo, schema=...)` owns one connection; `database.store(tenant_id)` gives
the documents of one tenant. Every row carries `tenant_id` and every query filters on it, so one tenant's store
never reads or changes another tenant's documents. `database.migrate(before_migration=...)` applies the numbered SQL
files in `nodestro_runtime/migrations/postgres` under an advisory lock (several processes may start at once) and
records them in `nodestro_schema_migrations`; a database that already has a schema needs a backup first (operators:
`pg_dump`), and the same error codes as on SQLite apply. Credentials come from the connection string, the `PG*`
variables or `~/.pgpass`. The tests run the storage contract on PostgreSQL when `NODESTRO_TEST_POSTGRES` names a
test database (each test uses its own schema). Knowledge files, their search index and memory spaces have
PostgreSQL backends too (`nodestro_runtime.postgres_tables`), and `nodestro_runtime.postgres_import` moves a
desktop database into an empty tenant (without tool provenance: imported tools are untrusted).

### MCP servers (external tools)

`workspace.mcp` connects agents to the tools of remote MCP servers over Streamable HTTP (ADR-0007 in the studio
repository; package `nodestro_runtime.mcp`). The official MCP SDK negotiates the protocol version: `server/discover`
at 2026-07-28, otherwise the `initialize` handshake (2025-11-25 and older). It needs Python 3.11 or later; on 3.10
servers cannot be reached (`mcp.unavailable`).

```python
from nodestro_runtime.mcp.models import McpServerIn

server = workspace.mcp.add(McpServerIn(name="Docs", url="https://mcp.example.com/mcp", auth="bearer", token="..."))
workspace.mcp.discover(server.id)  # lists the tools; each one starts as "new"
workspace.mcp.decide(server.id, "search", approve=True, access="read", actor="admin")
# agents can now list "mcp__docs__search" in AgentConfig.tools
```

Access is the intersection of three decisions: the server is enabled, an administrator approved this exact tool
definition (pinned by its hash) and classified it, and the agent lists the tool. The check runs when an agent is built
and right before every call, against the definition the server lists at that moment; a changed definition is blocked
(`mcp.tool_changed`) until an administrator accepts it. Annotations such as `readOnlyHint` are shown as hints only.
Tools classified as `write` or `destructive` are usable only while the shared per-call approval is active
(`NODESTRO_TOOL_APPROVALS=1`, decision D10): every such call waits for a person; otherwise they are refused
(`mcp.write_not_supported`). Runs without a
person (schedules, batches, test series, published APIs) may use MCP tools only when the operator sets
`NODESTRO_MCP_UNATTENDED=1` after the MCP security acceptance (`mcp.unattended_not_enabled`).

Calls have a timeout (default 60 s), an output limit (default 20,000 characters), a limit of parallel calls per
server, and stop when the run is cancelled. Results reach the model marked as external data. Each call in a run
records its provenance (`ToolCallRecord.provenance`: server, qualified name, definition hash, access) and
`external_cost: "unknown"`: the cost of an external service is not known to Nodestro and is never counted as zero.

Servers that use OAuth 2.1 (`auth="oauth"`) are signed in from a browser in two steps, so the callback may reach
another process than the one that started it:

```python
url = workspace.mcp.begin_oauth(server.id, "https://nodestro.example.com/api/mcp/oauth/callback", actor="admin")
# the person opens url, signs in, and the authorization server redirects to the callback with code, state and iss
workspace.mcp.complete_oauth(state, code, iss)
```

`begin_oauth` asks the MCP server without credentials, discovers its protected resource metadata (RFC 9728) and the
authorization server's metadata (RFC 8414, issuer checked), registers Nodestro (dynamic client registration, or a
client ID metadata document) and builds the authorization URL with PKCE `S256`, `state` and the `resource` indicator
(RFC 8707). `complete_oauth` checks `state` and the response's issuer (RFC 9207), exchanges the code once and stores
the credentials sealed like every other token. Calls present the access token only to the MCP server and refresh it
at the discovered token endpoint (rotating refresh tokens, one refresh per server at a time); when the grant is gone
they fail with `mcp.oauth_reauthorize` instead of waiting for a browser. `sign_out` forgets the credentials; a new
address or authentication kind forgets them as well.

Operational limits (WP-7) come from the environment: `NODESTRO_MCP_MAX_SERVERS` (25 per tenant),
`NODESTRO_MCP_MAX_PARALLEL` (8 parallel calls per tenant), `NODESTRO_MCP_QUEUE_SECONDS` (10),
`NODESTRO_MCP_CALLS_PER_MINUTE` (120 per server), `NODESTRO_MCP_BREAKER_FAILURES` (5) and
`NODESTRO_MCP_BREAKER_SECONDS` (60): after that many transport failures in a row a server rests
(`mcp.cooling_down`). `workspace.mcp.hub.stats()` reports active calls, the limits and each server's health.

Published releases (WP-9) freeze the MCP tools their agents use: `McpService.pin(name)` gives a `McpToolRef`
(server id and slug, approved hash, classification, definition; no address, no token) for `ReleaseBundle.mcp_tools`.
A release's tool library uses `PinnedMcp(workspace.mcp, bundle.mcp_tools)` instead of the service: only the frozen
tools exist, and every lookup and call checks the live policy and that the approval still has the frozen hash and
classification (`mcp.release_tool_changed`, `mcp.tool_not_in_release`). Tokens stay in the tenant's configuration.
Runs of published APIs are unattended, so they also need `NODESTRO_MCP_UNATTENDED=1` (D-7).

Local MCP servers over stdio (WP-8) run only in an isolated container, never on the host (decision D-5, ADR-0006).
An administrator names the server's Python packages and its module; the packages go through the same review as a
tool's libraries (hash-pinned lock, approval, image build), and each connection starts `python -m <module> <args>` in
a container of that image:

```python
from nodestro_runtime.mcp.models import McpServerIn, StdioCommand

server = workspace.mcp.add(
    McpServerIn(
        name="Time",
        transport="stdio",
        stdio=StdioCommand(requirements=["mcp-server-time==2025.9.25", "mcp>=1.10,<2"], module="mcp_server_time"),
    )
)
plan = workspace.mcp.plan_environment(server.id)  # the versions and hashes to review
workspace.python.approve(plan.id, wait=True)  # an administrator approves; the image is built
workspace.mcp.discover(server.id)  # then tools are reviewed and approved as for HTTP servers
```

The container has no network, a read-only root file system, no capabilities, runs as `nobody` with memory, CPU and
process limits, is never pulled from a registry and is removed when the connection ends; on a server it goes through
the sandbox gateway like every sandbox container. Without the Docker sandbox a stdio server is refused
(`sandbox.isolation_required`); without approved and installed packages it does not start
(`mcp.environment_not_ready`). A local server takes no token and no sign-in. Servers that need network access, and
servers that are not Python packages (for example npm packages), are not supported. A relay between the SDK and the
container ends a connection whose server writes a line over 8 MB, and the server's stderr is discarded.

## Security and data protection

- Everything is stored locally in `<data_dir>/nodestro.db` (SQLite, WAL mode) and below the data directory.
  Prompts, inputs and files that agents receive are sent to the configured model provider.
- API keys are never returned by display methods and are given only to agents of their own provider. Error
  texts stored with runs and the logs of package installations are cleaned of anything that looks like a
  credential.
- `uv` (package resolution and installation) runs with an allowlist of environment variables: system paths,
  locale, temporary directories, proxy and certificate settings, `UV_*` and compiler settings. Provider keys,
  tokens and other secrets are not passed on. Resolving a package that has no wheel builds it from source,
  which runs that package's build script before the approval overview is shown. Nodestro therefore resolves
  the libraries of generated tools with `--no-build` (such a package is reported as `deps.build_required`);
  only a plan a person asks for (*Review and approve*) may build from source. uv is not a sandbox: a build
  script runs with the rights of the Nodestro process.
- Workflows can contain approval nodes for human oversight; every decision is stored with the run, as are
  the prompts, tool calls, results and costs of every step, and each run records the workflow version it ran.
- Sanitized exports (`sanitize`) replace credentials and personal data by placeholders.
- MCP connections (threat model T7 in the studio repository): `https` only (loopback `http` only on the desktop and
  only with per-server consent); the connection step resolves the host name, checks every address and connects only
  to a checked one, so DNS rebinding cannot slip in between; private, loopback, link-local, shared, multicast,
  reserved and cloud metadata addresses are refused for IPv4 and IPv6 (embedded IPv4 included); TLS verifies the
  configured host name; redirects stay on the endpoint's origin and are checked again; proxy environment variables
  are ignored. A server profile reaches only the origins in `NODESTRO_MCP_ENDPOINTS` (empty: none). The same checks
  apply to every request of an OAuth sign-in (metadata, registration, token endpoint), so on a server the list must
  name the authorization server's origin too.
- MCP tokens are stored only encrypted: on a server with the tenant's key derived from `NODESTRO_SECRET_KEY`, on a
  desktop with the keychain key the desktop app passes in `NODESTRO_VAULT_KEY`. Without such a key a token lives
  only in memory for the session; there is no plain-text fallback. Tokens are bound to their server's origin and
  never appear in API views, events, logs or exports. OAuth credentials (tokens, client registration) and a started
  sign-in (its PKCE verifier) are sealed the same way.

These are technical measures. They do not by themselves make an application compliant with the GDPR, the EU
AI Act or ISO/IEC 42001; that depends on how an operator uses and documents the system.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

The tests never call a paid provider, need no API key and no network. `tests/test_architecture.py` checks that the runtime
imports neither the API nor the studio and uses nothing of claude-agent; `tests/test_readme.py` runs the
quickstart above.

`tests/test_container_images.py` builds a real image and runs a tool in it when `NODESTRO_DOCKER_TESTS=1` (needs
Docker and network); `NODESTRO_REQUIRE_DOCKER=1` makes a missing Docker fail instead of skip, as in the CI job
`docker`.

CI needs the repository secret `NODESTRO_READ` (read access to Orkexa and the Nodestro repositories).

## Provenance and license

Extracted from `nodestro` 0.1 (15e2076); see [PROVENANCE.md](PROVENANCE.md) and [CHANGELOG.md](CHANGELOG.md).
MIT, see [LICENSE](LICENSE).
