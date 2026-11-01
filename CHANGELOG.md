# Changelog

## Unreleased

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
  `stale`, `result_lost` and `switch_off` (never with arguments). Agents with a quality check keep waiting in the
  thread (a judge's refinement cannot be checkpointed).
- Fix: the docstring of `OrchestratorSelect`.

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

## 0.2.0 (2026-10-29)

- First version as a repository of its own: extracted from `nodestro` 0.1.0 (commit 15e2076, now
  `nodestro-legacy`) without new features and without behavior changes. The origin of every file is listed in
  PROVENANCE.md.
- Dependencies pinned to exact commits (Orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); the tested
  combination is recorded in `nodestro-studio/platform/versions.json`.
- CI with the repository secret `NODESTRO_READ`.
- New: `Workspace` as the assembly of the runtime for one data directory, `extensions` (contract 1 for
  integrations), `testing` (simulated provider clients). OCR moved to `nodestro-integrations`.
