# Access grants (access model v2, N-06)

Status: **in development, off by default.** The switch is `NODESTRO_ACCESS_GRANTS=1`, and it only takes effect together
with `NODESTRO_TOOL_APPROVALS=1`. Activating it is a separate owner decision (gate G-2). While it is off, the tool
gate decides exactly as before: `ToolBinding`, `ToolPreGrant` and the S-2 rules are unchanged.

The access model v2 extends the per-call approvals of S-2 (`tool_policy`, `tool_approvals`). It is not a second
approval system. It adds three things:

- stored **grants** and **denies** about a resource,
- **global safety limits** that are code, not data,
- one evaluation order that reports *why* an access is refused.

## Records

Both records live in the tenant's document store (collections `access_grants` and `access_denies`, one document per
record, key = record id). On PostgreSQL they are rows of `nodestro_documents` with the tenant id, like
`tool_pre_grants`. No schema migration is needed. Records are never deleted: a revocation sets `revoked_at`.

| Field | `Grant` | `Deny` |
|---|---|---|
| `resource` | `type` (`tool`, `folder`, `workflow`, `mcp_server`, `connection`, `cloud_provider`) and `id` | same |
| `actions` | `read`, `write`, `move`, `trash`, `delete`, `execute`, `send`, `submit` (one or more) | same |
| `scope`, `agents` | `agent` (one), `agents` (a list) or `all` | same |
| `duration` | `once`, `conversation` (with `conversation_id`), `until` (with `expires_at`), `until_revoked` | `until`, `until_revoked` |
| `mode` | `supervised` (a person watches) or `unattended` (also covers background calls) | `supervised`, `unattended` or none (both) |
| `tool` | for a tool grant: the tool as it was granted (`ToolFacts`: version, `access`, `reach`, MCP definition hash) | none (a deny holds for every version) |
| `status` | `active`, `used` (a `once` grant after its use) or `revoked` | `active` or `revoked` |
| who, when | `granted_by`, `created_at`, `revoked_at`, `revoked_by` | `denied_by`, same |

All times carry a time zone. Using a `once` grant (`AccessRules.use`) and revoking any record are each one
compare-and-set of `status` from `active`. A record stored before `status` existed is revoked by a direct write;
nothing can use it, since `use` also compares `status`. So exactly one of them wins, a revoked or expired grant is never used, and
a revocation is never lost.

Desktop-to-server imports (`postgres_import`) skip both collections: grants never move silently into another trust
domain.

**Only a person writes records.** `AccessRules.grant`, `deny`, `revoke_grant` and `revoke_deny` take a `Person`.
`Person` refuses machine principals (`system`, API keys `api:<id>`, anything with a `:`). The writer also refuses to
run inside a tool call (`access.in_tool_call`), because code that runs there acts for a model. No tool, MCP, engine
or orchestrator module references the writer; a test checks this.

The writer also refuses:

| Code | Why |
|---|---|
| `access.person_required` | The principal is not a `Person` |
| `access.in_tool_call` | Called while a tool call runs |
| `access.grant_destructive` | A grant for `delete`: deleting for good needs a confirmation every time |
| `access.tool_facts_required` | A tool grant without `tool` (`ToolFacts.of(capability, version=..., definition_hash=...)`), or `tool` on another resource |
| `access.unattended_scope` | An unattended grant for more than `read` for all agents: it must name its agents |
| `access.unattended_too_long` | An unattended grant for more than `read` that lasts longer than `MAX_UNATTENDED_GRANT` (30 days; param `days`) |
| `access.deny_not_enforced` | A deny on one of the run's own tools (`RUN_OWN_TOOLS`), which never pass the gate (param `tool`) |
| `access.expiry` | `until` without `expires_at`, an expiry with another duration, an expiry in the past or without a time zone |
| `access.conversation_required` | `conversation` without `conversation_id`, or a conversation id with another duration |
| `access.unattended_expiry` | An unattended grant for more than `read` that is not `until` or `once` |
| `access.folder_path` | A folder that is not an absolute path |
| `access.invalid` | The scope does not fit the agent list, or another field is invalid |
| `access.not_found` | Revoking a record that does not exist (param `id`) |

Every change writes one audit event (`tool_approval_audit`, kinds `access_granted`, `access_grant_revoked`,
`access_denied`, `access_deny_revoked`). The audit event also goes to the activity log (`tool_approval.access_*`,
resource type `access_grant` or `access_deny`, resource id = record id). Audit events name the resource type. They
never name a folder path.

## Evaluation

`tool_policy.evaluate(request, grants=..., denies=..., at=...)` does no I/O and checks in this order:

1. **Safety limit** (`SAFETY_LIMITS`):
   - `offline`: an external effect or a cloud provider while strict offline is on.
   - `destructive_unattended`: a destructive action with nobody watching.
   - `protected_folder`: a folder in `system_folders()` or in the caller's `protected_folders` (Nodestro's own data
     folder; required for every folder request).
     - `system_folders()` is `SYSTEM_FOLDERS` plus the Windows folders the environment names (`SystemRoot`,
       `windir`, `ProgramFiles`, `ProgramFiles(x86)`, `ProgramW6432`, `ProgramData`).
     - An administrative share of any host (`\\host\C$`) counts as its drive here.
   - `unresolvable_path`: a folder that cannot be compared by name: not absolute, leading or trailing white space, a
     Windows device path (`\\.\`), an 8.3 short name (`PROGRA~1`) or a `:` in a component.
2. **Deny**: an active deny that addresses the request.
3. **Destructive actions**: no grant covers them. The result is a missing grant with source
   `limit:destructive_per_call`, so only a confirmation of the one call allows them.
4. **Grant**: an active grant that addresses the request. Standing grants come first and `once` grants last.
5. **Defaults of spec 7.2**: a `read_external` tool without shared content in the conversation, or a cloud model
   without shared content.
6. Otherwise a **missing grant**.

The result (`AccessDecision`) has `allowed`, a `kind` of refusal (spec 7.6: `missing_grant`, `denied`,
`safety_limit`; codes `access.missing_grant`, `access.denied`, `access.safety_limit`) and a `source`: `grant:<id>`,
`deny:<id>`, `limit:<name>`, `default:<rule>` or `none`. The source is for the person. The model never gets it
(N-07).

Further rules:

- **Revocation and expiry** are checked at every evaluation, so a change applies at the next access, also for a
  running background task.
- **Unattended** requests need a grant with mode `unattended`. Beyond `read_internal`, that grant must also:
  - be dated (`until`) or `once`,
  - name its agents,
  - last at most `MAX_UNATTENDED_GRANT` (spec 7.2, D10).
- **Tool grants** match only the tool as it was granted. A new version, a reclassification or another MCP
  definition ends them, as for a pre-grant.
- **Scope**: a grant for some agents never matches an unknown agent. A deny for some agents does match an unknown
  agent (fail closed).
- **Effect classes** are the two existing axes (`effect_class(access, reach)`):

  | Effect class | Axes |
  |---|---|
  | `read_internal` | `read` x `internal` |
  | `write_internal` | `write` x `internal` |
  | `read_external` | `read` x `external` |
  | `write_external` | `write` x `external` |
  | `destructive` | `destructive`, any reach |

  An action `delete` always counts as destructive.

### Folders

A grant holds for a folder and every folder below it. A subfolder can be granted without its parent. A deny on a
parent blocks every subfolder, even one with its own grant.

Folders are compared by whole path components, never by string prefix: `/home/a/docs` does not cover
`/home/a/docs-private`. `folder_parts` normalizes a path first:

- Unicode NFC,
- `.` and `..` resolved lexically,
- repeated separators dropped.

The path is never trimmed: one with leading or trailing white space is refused.

A folder deny is stored canonical (`os.path.realpath`: links resolved, `..`, repeated and trailing separators gone),
like a folder grant (N-10), so a deny named through a link still holds for the canonical paths the folder tools ask
for.

Windows paths (`C:\...`, UNC, `\\?\`) get more normalization:
- both separators are accepted, and `\\?\C:\x` is `C:\x`;
- two leading separators of either kind always mean a Windows path, so `//host/share` is UNC (fail closed on POSIX
  too);
- trailing dots and spaces of a component are dropped, as Windows does;
- every component is lower-cased (`lower()`, not `casefold()`): `straße` and `STRASSE` stay two folders, so a grant
  for one never covers the other;
- `\\localhost\C$` is drive `C:`.

On Windows, a path without a drive or share (`/Windows`) is refused, because it depends on the current drive.

Denies and safety limits compare case-folded on top. Grants compare the normalized names exactly. Both choices fail
closed on case-insensitive file systems.

This is a comparison of names, not a guard on the file system. Callers resolve links before they ask
(`os.path.realpath`). The folder tools (N-10, [folders.md](folders.md)) evaluate a folder grant at every call and read
through the file-system guard `orkexa.fs.SafeRoot`, which handles links, junctions, hard links, swaps during a call
and network drives.

## In the tool gate

With the switch on, `ToolGate(access=AccessRules(store).current, offline=...)` reads the records at every call. It
passes them to `tool_policy.decide` through `CallContext.access`. A tool call is evaluated as resource `tool:<name>`,
action `execute`, with the tool's effect class. The agent is named `<workflow id>/<node id>`, and the conversation is
the root run.

In a conversation with an own agent (N-04, [conversations.md](conversations.md)) the conversation is the conversation
object: a `conversation` grant names its id and is revoked when it is deleted. Records scoped to a workflow or an
agent do not carry over to a conversation: the answer's workflow id is the conversation id and its agent is
`<conversation id>/agent`, whatever template answers. To block a tool in conversations as well, deny the tool with
scope `all`.

- A safety limit or a deny refuses the call first, with code `access.safety_limit` or `access.denied`.
  - The agent gets a short reason without the source.
  - The call's `ToolCallApproval.access_source` and the run's `tool_denied` event carry the source for the person.
- The same check runs again after a person approves a waiting call. A deny or limit set while it waited still
  refuses it, and the approval is audited `stale` (spec 7.3 (7)).
- A matching grant allows a call that S-2 would otherwise ask about, or refuse because nobody can answer.
  - The grant is recorded on the call (`access_source`, `grant:<id>`) and in a `tool_access_granted` event.
  - `access_source` is left out of stored records when unset (`Field(exclude_if=...)`, pydantic 2.12 or later), so
    records stay byte-identical while the switch is off. Its OpenAPI schema stays typed.
- A grant never lifts these rules: an orchestrator's narrowing (`tool.not_granted`), an `always` binding, the MCP
  rules (a changing MCP call is always asked) and the question before a destructive tool.
- A `once` grant does not count in the gate: one confirmed tool call is the S-2 approval itself.
- Without a grant or deny, S-2 decides as before.
- The warning before an unattended start (`tool_preflight`) evaluates the same rules for the named agent. It reports
  `tool.unattended_access_denied` and `tool.unattended_safety_limit`.
- An agent test never runs a tool on a grant: it keeps `tool.approval_not_in_test`, since a test agent has no
  identity and its calls have real effects.
- The run's own tools (`RUN_OWN_TOOLS`: memory, input files, knowledge proposals, skills) never pass the gate, so
  denies on them are refused.

## At the other call sites

With the switch on, the runtime also evaluates the records where a workflow, an MCP server, a connection or a cloud
model is used. Each check only adds refusals: a grant on these resources never lifts a question, an MCP rule or a
per-call approval. While the switch is off, none of them runs (a regression test compares `decide` with and without
the related resources over the whole S-2 matrix, and runs with stored records behave as before).

| Resource | Where | Action | Effect class | What refuses |
|---|---|---|---|---|
| `workflow` | top-level start (`RunManager.start`: manual, schedule, batch, API, resume) | `execute` | `read_internal` | deny, safety limit |
| `workflow` | sub-workflow node and orchestrator team start | `execute` | `read_internal` | deny, safety limit |
| `mcp_server` | every call of one of its tools, in the tool gate (`decide(related=...)`) | `execute` | the tool's, always external | deny, safety limit |
| `connection` | every call of a tool whose `tool_settings` name it, in the tool gate | `execute` | the tool's, always external | deny, safety limit |
| `cloud_provider` | every connection of an agent's turn, an orchestrator decision, an agent test, the live run chat and `Workspace.connect_model` (assistants, guide, skills, judges, model choice; every provider but `ollama`) | `submit` | `read_external` | deny, safety limit, missing grant with shared content |

- **Workflow start.** A top-level start is a person's or a trigger's, not an agent's: only records for all agents
  count there (`tool_policy.site_records`). A sub-workflow or team start names the calling node
  (`<workflow id>/<node id>`), and every record counts. The effect class is `read_internal`, so a start trips no
  effect-based limit; what the workflow does is checked at each of its calls (spec 7.2, "by its strongest effect").
  A missing grant never refuses a start.
- **MCP servers stay strict.** A deny or a safety limit on the server refuses the call. A grant on the server, like
  a grant on the tool, never replaces the question before a changing MCP call and never lets an MCP tool run
  unattended.
- **Local MCP servers.** Strict offline does not refuse the tools of a local (`stdio`) MCP server
  (`ToolGate(local_mcp=...)`, `AccessContext.local_servers`): it runs on this computer. Its denies still hold.
- **Connections** are the ones the agent's tool settings name. A deny for some agents also holds when the gate
  cannot name the agent (fail closed).
- **Cloud models.** The default rule of spec 7.2 allows a cloud model without shared content. Without N-08 only a
  deny or a safety limit (strict offline: `limit:offline`) refuses. With N-08's switch on, a conversation or run that
  holds content of a granted source marks it as shared content, and a cloud model then needs a grant
  ([content-provenance.md](content-provenance.md)). The check runs when a turn connects, so a new deny applies at the
  next turn; with N-08 a turn that holds granted content is checked before each request too. An agent test and the live run chat
  have no agent: a deny for some agents holds there too.
- `Workspace.connect_model(model, actor=..., attended=..., probe=...)`: `attended=False` for a caller nobody watches
  (a benchmark judge), so denies and limits for unattended use apply; `probe=True` for a check whether a model can
  be used at all (model choice), which is refused like a request but not audited. The connection test of the
  diagnostics (`Workspace.connection`) is not checked: it sends no content.
- If both a safety limit and a deny apply (on the tool or a related resource), the safety limit is reported.

**Recording a refusal.** A refused tool call records the source as before (`ToolCallApproval.access_source`, run
event `tool_denied`). A refused workflow start, sub-workflow or team start, or cloud model request:

- raises (or fails the step with) `access.denied`, `access.safety_limit` or `access.missing_grant` with the params
  `resource_type`, `resource_id` and `source`. Only a person sees `source`: when the caller (or the run's starter)
  is an API key, a published app or a trigger, the error leaves it out and only the audit keeps it. Run records and
  events (`ToolCallApproval.access_source`, the `source` of `tool_denied`, `tool_access_granted` and
  `access_refused`) keep it; the api leaves it out when it serves them to a caller that is not a person;
- writes the audit event `access_refused` (`tool_approval_audit`, outcome `denied` in the activity log and the
  platform mirror) with resource type, resource id (never a folder path), kind, source and agent;
- in a run, emits the run event `access_refused` with the same fields.

An orchestrator learns only that a team cannot start; the source goes to the person (event and audit). Agent tests
(`AgentTester.run(..., actor=...)`) audit through the gate's store.

**Reading the records.** `AccessRules.current()` returns only the records active now: grants are looked up by
`status` in the store (a grant stored before `status` existed no longer counts; grant it again), denies are read in
full and filtered, so an old deny still holds. The checks at sub-workflow and team starts run in the agents' thread
pool, not on the event loop.

## Who may do what and why (BER-1)

`tool_policy.explain(request, grants=..., denies=..., at=..., tool_kind=...)` is `evaluate` as the request's call
site applies it. It does no I/O. The enforcement uses the same two functions, so the explanation and the call site
cannot disagree (a property test checks every combination):

- `site_records`: a tool call ignores `once` grants, and a top-level start ignores records for some agents;
- `site_outcome`: what the evaluation means at the call site.

| `outcome` | `allowed` | Meaning |
|---|---|---|
| `refused` | `false` | A deny or a safety limit refuses it (and, for a cloud model with shared content or a folder, a missing grant) |
| `granted` | `true` | A grant allows it where grants count: a tool call S-2 would ask about (not an MCP tool), a cloud model, a folder |
| `allowed` | `true` | Nothing refuses it: a workflow start (a missing grant never refuses it), a cloud model by default |
| `per_call_rules` | `true` | The access model does not refuse it; the per-call rules of S-2 decide (tools, MCP servers, connections) |

The result (`AccessExplanation`) also names:

- the request (resource, action, effect class, agent, mode);
- `kind` and `code` (only when refused) and `source` (what `evaluate` found);
- the deciding `grant` or `deny`, and `grant_enforced`: whether that grant counts at this site. It does not count
  for MCP servers, connections, MCP tools and workflows: it is noted, not enforced;
- the deciding safety `limit` with its text (`limit_text`), or the `default_rule`;
- `also`: the other active records that address the request but did not decide it;
- `stale`: tool grants for this tool that no longer match it (another version, classification or MCP definition).

`explain` also takes `related` (the MCP server and connection a tool call uses) and `local_servers`: a deny or a
safety limit on a related resource refuses the tool row exactly as `access_refusal` refuses the call, and strict
offline refuses neither a local (`stdio`) MCP server nor its tools.

`tool_policy.who_may(grants, denies, at=..., resource=..., agent=..., mode=..., offline=..., protected_folders=...,
tools=..., local_servers=...)` explains every resource, action, agent and mode an active record names.

- A record for all agents is explained without an agent.
- A tool is explained as it is now. `tools` maps a tool name to a `ToolNow`: its facts, its kind and the resources a
  call uses (`related`: its MCP server, and a connection where the caller knows it). Without `tools`, a tool is
  explained as it was granted.
- `local_servers` names the local MCP servers, for MCP server rows and for the tools that use them.
- Folders are only explained when the caller names the protected data folder.

`effect_for(resource_type, action, tool=...)` gives the effect class the call sites use.

The api builds its query on `AccessRules(store).current()` and these functions, and passes the tools as they are
now. They are for people and the administration only; a model never gets an explanation (N-07).

## Not in this step

- The api query and write routes, and the studio approval cards and overview.
- Marking shared content is N-08, behind its own switch: [content-provenance.md](content-provenance.md).
- A grant on a workflow, MCP server or connection that lifts a question (spec 7.2 "Freigabe je Dienst/Ziel").
- The `DenialNotice` type (N-07).

See `docs/TODO.md` RT-086 and RT-088.
