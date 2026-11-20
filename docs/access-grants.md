# Access grants (access model v2, N-06)

Status: **groundwork, off by default.** The switch is `NODESTRO_ACCESS_GRANTS=1`, and it only takes effect together
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
| who, when | `granted_by`, `created_at`, `revoked_at`, `revoked_by` | `denied_by`, same |

A `once` grant has `status` `active` until `AccessRules.use` marks it `used` (one compare-and-set).

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
| `access.expiry` | `until` without `expires_at`, an expiry with another duration, or an expiry in the past |
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
   - `protected_folder`: a folder in `SYSTEM_FOLDERS` or in the caller's `protected_folders` (Nodestro's own data
     folder).
   - `unresolvable_path`: a folder that is not absolute.
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
- **Unattended** requests need a grant with mode `unattended`. Beyond `read_internal`, that grant must also be dated
  (`until`) or `once` (spec 7.2, D10).
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
- repeated separators dropped,
- Windows drive letters in lower case, both separators accepted.

Denies and safety limits compare case-insensitively. Grants compare exactly. Both choices fail closed on
case-insensitive file systems.

This is a comparison of names, not a guard on the file system. Callers resolve links before they ask
(`os.path.realpath`). Hard links, junctions, rename races and network drives are open points of the W0-6 spike for
the file-system guard (N-10).

## In the tool gate

With the switch on, `ToolGate(access=AccessRules(store).current, offline=...)` reads the records at every call. It
passes them to `tool_policy.decide` through `CallContext.access`. A tool call is evaluated as resource `tool:<name>`,
action `execute`, with the tool's effect class. The agent is named `<workflow id>/<node id>`, and the conversation is
the root run.

- A safety limit or a deny refuses the call first, with code `access.safety_limit` or `access.denied`. The agent gets
  a short reason without the source. The run's `tool_denied` event carries `source` for the person.
- A matching grant allows a call that S-2 would otherwise ask about, or refuse because nobody can answer
  (`GateDecision.access_source`).
- A grant never lifts these rules: an orchestrator's narrowing (`tool.not_granted`), an `always` binding, the MCP
  rules (a changing MCP call is always asked) and the question before a destructive tool.
- A `once` grant does not count in the gate: one confirmed tool call is the S-2 approval itself.
- Without a grant or deny, S-2 decides as before.

## Not in this step

- The api query "who may do what and why" (BER-1).
- The studio approval cards with scope and duration.
- Folder, workflow, MCP server, connection and cloud provider checks at their call sites. These need the file-system
  guard (N-10), the content provenance (N-08) and the run start check.
- The `DenialNotice` type (N-07).

See `docs/TODO.md` RT-086.
