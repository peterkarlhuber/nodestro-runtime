# Runtime ADR-0001: Built-in tool specs and service connections

- Status: Accepted (2026-11-13). The Tabrion program (CR-DB) confirmed the connection contract with conditions
  C-1 to C-3, which are part of the text below. Owner decisions: NAL plan v1.1 approved (E-1), one shared connection model
  provided by NAL-A1 and reused by CR-DB (E-2), both on 2026-11-13.
- Repositories: nodestro-runtime (models, storage, checks, tool binding), nodestro-api and nodestro-sdk (management
  interface), nodestro-studio (user interface). orkexa: unchanged.
- Related: studio [ADR-0003](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/adr/0003-identity-and-tenants.md)
  (tenants), studio ADR-0006 (sandbox policy), studio ADR-0007 (MCP client, token vault D-3), the shared tool
  capability model S-1/S-2 (`tool_policy.py`, design `docs/evolution/s-tool-approvals.md` in nodestro-studio),
  Tabrion planning document 14 (CR-DB, NOD-CR-2 `DatabaseConnection`).
- Numbering: this is the first ADR kept in nodestro-runtime itself; the platform-wide ADRs stay in nodestro-studio.

## Context

Built-in tools are five pure computations (`builtin_tools.py`). Their capability lives in a fixed table
(`tool_policy.BUILTIN_CAPABILITIES`); nothing says whether a tool needs the network, works in the strict offline
mode, needs a service, has parameters, costs money or how long it may run. Tools that reach services (web search,
mail, webhooks in later NAL packages; database tools in CR-DB) need credentials. Today credentials exist in three
places with different rules: model provider keys (`workspace.py`), MCP tokens (`mcp/vault.py`, sealed) and the
server's tenant secrets (`server_settings.SecretBox`). CR-DB planned a fourth model (`DatabaseConnection`).

## Decision

### D-1 Tool specs

Every built-in tool has a `ToolSpec` (`tool_spec.py`): `id`, `version`, `category`, `capability`, `network`,
`offline`, `connection_type`, `params_schema`, `cost_unit`, `modes`, `timeout_seconds`, `max_calls_per_run`,
`i18n_key`.

- The capability stays in `BUILTIN_CAPABILITIES` (security-reviewed); the spec takes it from there. There is no
  second capability or permission model.
- `network="external"` implies `offline=False` and `reach="external"` (enforced by the model).
- At run time a built-in tool is wrapped (`bind_tool`): it runs under `timeout_seconds` (error text names
  `tool.timeout`), counts against `max_calls_per_run` per agent run (`tool.call_limit`; the model gets a tool error
  and can answer) and sees its binding through `current_tool_context()`. The model's view of the tool (name,
  description, input schema) is unchanged and never contains connection data or parameters.
- Tools of integrations without a spec keep an unknown capability and get a time limit of 300 seconds.
- Releases record `id@version` of every built-in tool (`ReleaseBundle.builtin_versions`).
- New built-in tool names may not collide with the provider server tool keys (`web_search`, `web_fetch`,
  `code_execution`); the names `search_web` and `fetch_web_page` are reserved now.

### D-2 Binding settings

`AgentConfig.tool_settings: dict[str, ToolSettings]` with `ToolSettings(connection_id, params)`. Parameters are
validated against the spec's `params_schema` (JSON Schema). Settings for tools without a spec, for tools the agent
does not use, or with a connection of another type are reported by the agent check; a tool that needs a connection
and has none is an error (`check.connection_missing`).

### D-3 Service connections (the shared contract)

One model, `ServiceConnection` (`connections.py`), for every access to an outside service. CR-DB uses it for
databases; there is no `DatabaseConnection` next to it.

| Field | Type | Rule |
|---|---|---|
| `id` | string `conn_<12 hex>` | generated, never reused |
| `contract` | int | contract version, currently `1` |
| `type` | string | a registered connection type: `search`, `smtp`, `webhook`; `database` reserved for CR-DB |
| `name` | string, 1–80 | unique per tenant (case-insensitive) |
| `provider` | string `[a-z0-9][a-z0-9_-]{0,39}` | one of the type's providers (`generic` when the type has none) |
| `description` | string, ≤ 500 | free text |
| `settings` | object | validated by the type's settings model; unknown fields are refused |
| `egress` | list of `host:port` | derived from type, provider and settings; never typed in |
| `secret_storage` | `none` \| `vault` \| `session` | where the one secret lives |
| `secret_sealed` | string or null | the sealed secret; never leaves the runtime |
| `check` | `{status, checked_at, code, kind}` | `unchecked`, `ok` or `failed`; `kind` is `configuration` or `service` |
| `tenant_id` | string | the owning tenant (`local` on desktop and Local Docker) |
| `created_at`, `updated_at`, `created_by`, `updated_by` | time, actor | audit fields |

Types are registered with `register_connection_type(ConnectionType(...))`: name, settings model, providers,
whether a secret is required, the secret's label, the `egress` derivation, optional own address rules
(`addresses`), an optional `check` and the modes.
Settings models forbid unknown fields, so a secret cannot be stored as a setting by mistake.

**Secret rules.**

1. One secret per connection, written only (`set_secret`; an empty value removes it). No read, view, list, event,
   log line, error text, backup in plain text or export contains it; tools get it in memory through `resolve()`,
   whose `repr` hides it.
2. Stored only sealed, in the same token vault as MCP tokens (`TokenVault`, namespace `connection`; the core sees it
   through the `SecretVault` protocol, so exported packages need no MCP code): the tenant's
   `SecretBox` (AES-GCM, one HKDF key per tenant) on a server, the desktop app's keychain key
   (`NODESTRO_VAULT_KEY`) on a desktop. The encryption context contains tenant, namespace and connection id: a
   sealed value copied to another tenant, connection or to the MCP namespace cannot be opened.
3. Without a key store the secret lives only in the process's memory (`session`); there is no plain-text fallback.
4. Changing the secret, the provider or the settings resets `check` to `unchecked`.

**Tenant binding.** A connection belongs to the tenant of the store it is in (one store per tenant). It also
carries `tenant_id`; a document whose `tenant_id` is not the service's tenant is treated as missing, and its secret
cannot be opened with another tenant's key.

**Egress.** `egress` lists the only hosts a connection may reach, derived from its settings (e.g. the SMTP host and
port, the webhook URL's host and port, a self-hosted search endpoint) or from the provider's adapter. `egress` may be empty (e.g. a
local SQLite file; its path rules belong to that type). Addresses are checked when saved. URL-based types (the NAL
types) use the same rules as MCP server addresses (`net_address.py`, shared by both): no user information, no credential in query, path or fragment (`url_secret_problem`), https
only (http for loopback outside the server profile), address literals must be global unicast (loopback outside the
server profile only). A type may supply its own address rules instead (`addresses`; e.g. `database`: a TCP
`host:port` without a scheme, address literals checked with the `net_address.py` rules). Host names are resolved and checked against private ranges at call time by the network guard
of the tool that uses the connection (NAL-A2 for web tools; CR-DB for databases, which must also block internal
databases per NOD-CR-2).

**Checks.** `check()` runs the type's check with the secret in memory and stores only status and an error code,
never the service's message. Until the network guard of NAL-A2 exists, the checks of the NAL types contact no
service: search
connections check only the offline `test` provider, SMTP and webhook connections are configuration checks
(`kind="configuration"`). The `database` type's check may contact the database through CR-DB's own call-time
guard (internal Nodestro database blocked, `NODESTRO_DB_ENDPOINTS`).

**Audit.** Creating, changing, deleting, setting or removing the secret and checking a connection write a
`connection_events` record (actor, connection id and type, changed field names; never values). With the Axentra
audit mirror switched on, the event is mirrored in the same transaction (`nodestro.connection.<kind>`).

**Export.** Connections are never exported. A release lists what its built-in tools need
(`ReleaseBundle.required_connections`, `{connection_id: "type:provider"}`); an exported package asks for the
connection when it is set up (`configure`) and keeps it on the target computer only (`StaticConnections`, read from
`<DATA_DIR>/secrets/connections.json` with owner-only permissions; `NODESTRO_CONNECTION_<ID>_SECRET` replaces the
secret). A package whose required connections are missing or invalid refuses to start agents (`connection.missing`).

**Versioning.** `contract` changes only when stored connections or callers would break. Additive changes (an optional
field, a new type, a new provider) keep version 1 and are announced to both programs (NAL and CR-DB). A reader
ignores unknown fields. A breaking change needs a new ADR, a migration and both programs' agreement.

### D-4 Integration contract v2

`nodestro.integrations` contract 2: `Integration.tools` may hold `(ToolSpec, Tool)` pairs. Contract 1 stays
accepted unchanged. A spec from an integration is the author's statement (`capability.source="author"`), shown in
the catalog only; the effective capability of an integration tool stays unknown (`write` + `external`), so an
integration can never make its tool look harmless to the approval rules.

### D-5 Checks

- Strict offline mode: a built-in tool with `offline=False` is refused (`check.offline_tool`).
- A tool not available in the installation's mode: `check.tool_mode_unsupported`.
- A bound connection not checked successfully: warning `check.connection_unchecked`.
- Automatic starts (schedules, batches, test series) list every tool except Nodestro's own built-in tools that reach
  nothing outside; integration tools are listed now (before they were left out).

## Consequences

- CR-DB registers the `database` type (providers = engines `sqlite`, `postgresql`; settings model with host, port,
  database name, TLS mode, access mode and limits; secret = the password only, never a connection string, which
  would carry the location past the settings-derived egress; egress = the database host, empty for SQLite; its own
  address rules and check) and references this ADR instead of
  defining `DatabaseConnection`. Its tenant binding, egress allow-list and "block internal databases" rule are the
  ones above plus its own call-time check.
- Agreed additive extensions for CR-DB (contract stays 1): per-provider modes (SQLite off on the server); a check
  `ok` with a warning code (e.g. the role is superuser, owner or `BYPASSRLS`); a secret reference `env:NAME` as a
  `secret_storage` value (Tabrion profile file); the export file `secrets/connections.json` replaces CR document
  14's `databases.json`.
- Model provider keys stay where they are; moving desktop provider keys into the vault is a separate follow-up.
- MCP servers stay their own objects (with OAuth, discovery and tool approvals); they share the vault, not the model.
- Cost booking of `cost_unit` into the run budget arrives with the first priced tool (NAL-A2); the spec field and the
  connection's price setting exist now.

## Amendment NAL-A2 (2026-11-14): additive, contract stays 1

- `ConnectionType.keyless_providers`: providers usable without a secret although the type needs one (`search`:
  `ddgs`, `searxng`); `needs_secret(provider)` replaces direct reads of `secret_required` (the `test` search provider
  still needs a secret, so the A1 secret path stays covered).
- `ConnectionType.provider_modes` and `offers(provider, mode)`: the per-provider modes agreed for CR-DB above
  (`ddgs` is never offered on a server; SQLite can use the same field). Creating, changing, resolving or configuring
  a connection of a provider not offered in the mode fails with `connection.provider_unsupported_mode`.
- `add_search_provider(name, host=, keyless=, modes=)`: a search provider's fixed API host becomes its egress
  (`api.search.brave.com:443`) when no `endpoint` is set; connections stored before keep their stored egress.
- The search check stays offline: the `test` provider answers, every other provider gets a configuration check
  (secret, SearXNG endpoint). A check that sends a real search needs the owner's approval (E-3) and is not built.
- Cost booking: a priced built-in tool books `units x price` through `tool_spec.RunServices.book` into the run's
  tracker as one token-free usage record (`model = "<cost_unit>:<provider>"`, counted in `web_search_requests`).

## Tests

`tests/test_connections_contract.py` (contract: fields, secret rules, tenant binding, egress, versioning, reserved
type), `tests/test_tool_spec.py` (specs, binding, time and call limits, offline and mode checks, integration
contract v2, release refs). PostgreSQL tenant separation runs when `NODESTRO_TEST_POSTGRES` is set.
