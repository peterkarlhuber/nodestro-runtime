# Activity log (N-13)

One place that answers "who did what, with which agent, to what, and how did it end" for a workspace: every file,
tool, workflow and agent action is one event in `nodestro_runtime.activity`. The log holds identifiers, names, kinds
and outcomes only; it never holds contents or secrets.

The existing records stay the source of truth for their own purpose and are unchanged: the approval audit trail
(`tool_approval_audit`), the notes of started tool calls (`tool_call_journal`), the MCP event log (`mcp_events`), the
connection events (`connection_events`) and the runs. The activity log is written at the same call sites, next to
them.

## The event

`activity.ActivityEvent` (extra fields are refused):

| Field | Meaning |
| --- | --- |
| `id` | `act_<hex>` |
| `at` | when (UTC) |
| `source` | `tool_call`, `tool_approval`, `mcp`, `connection`, `workflow`, `agent` or `file` |
| `action` | `<area>.<verb>`, see below |
| `actor` | who: a principal id (`local` on a desktop, a user id on a server), `api:<key id>`, or `system` |
| `agent` | the agent's configured name, for tool calls and agent steps |
| `resource_type` | `tool`, `run`, `workflow`, `node`, `input_file`, `knowledge_file`, `mcp_server`, `mcp_tool`, `service_connection`, `tool_pre_grant` |
| `resource_id` | the resource's Nodestro id or name (never a file path or file name) |
| `outcome` | `success`, `failure` or `denied` (Axentra's audit outcomes) |
| `code` | a problem code, a run or step status, a decision (`approved`, `rejected`) or a file format; never free text |
| `run_id`, `node_id` | the run and the step it belongs to |
| `conversation_id` | the conversation it belongs to (set by callers outside runs, such as the assistant) |
| `source_event` | the id of the record it was taken from (approval audit, MCP or connection event) |

Identifiers and names must be printable single-line text of at most 200 characters, codes must be short codes
(`[A-Za-z0-9][A-Za-z0-9_.:-]*`, at most 100 characters); anything else is dropped rather than stored.

## Sources and actions

| Source | Action | Written by | Once per |
| --- | --- | --- | --- |
| `tool_call` | `tool.called` | `engine` (`after_tool`, next to the tool journal note) | tool call of a run, also a refused one (`denied`, with its code) or a failed one (`failure`) |
| `agent` | `agent.finished` | `engine`, when an agent step ends | execution of an agent step (a step that pauses for a person is logged when it ends) |
| `workflow` | `workflow.started`, `workflow.finished` | `engine` | run (also a sub-workflow's run); `finished` when the run ends, not when it pauses |
| `file` | `file.uploaded`, `file.deleted` | `input_files.InputFiles.add` / `delete`, `knowledge.KnowledgeBase.add` / `delete` | upload or deletion; the cleanup deletes as `system` |
| `tool_approval` | `tool_approval.<kind>` | `tool_approvals.record_audit` | approval audit event |
| `mcp` | `mcp.<kind>` | `mcp.service.McpService.event` | MCP event |
| `connection` | `connection.<kind>` | `connections.ConnectionService.event` | connection event |

The actor of run events is whoever started the top-level run (`Run.started_by`), else its API key (`api:<id>`), else
`system` (schedules, batches). Tool calls of the agent test in the inspector are not run actions and are not logged.
A run that never executes (cancelled while queued, imported) has no events.

Writing an event never fails the action it describes: a store error or an event that cannot be built is logged and
skipped.

## What is never stored

Tool arguments (also not their values or hash), prompts, model answers, tool outputs, file contents, file names and
paths, addresses, tokens, secrets and the free-text `details` of the source records. The tests put one canary value
into a prompt, tool arguments, a tool's output, file contents and names, a connection secret, an MCP token and an
audit event's details, and check that neither the stored events nor their exports contain it
(`tests/test_activity.py`).

### Decision: no file paths or file names in resource identifiers

A path or a file name is not content, but it often names a person, a customer or a project, and the activity log is
exported and kept for its own retention period, independent of the file. Resource identifiers are therefore Nodestro
ids (`in_<hex>` for input files, the knowledge file's id); the id leads to the file's name for as long as the file
exists. Tools that take a path (the sandbox's file tools) pass it as an argument, and arguments are never stored. An
input file's event carries its detected format as `code`.

## Query and export

```python
from nodestro_runtime import activity

page = activity.query(store, since=..., sources=["tool_call"], actor="usr_1", limit=200)
more = activity.query(store, before=page[-1].key, limit=200)  # keyset paging, newest first
for line in activity.export(activity.query(store, run_id=run.id, limit=None), "csv"):  # or "jsonl"
    ...
```

`query` filters by time (`since` inclusive, `until` exclusive), source, action, actor, agent, resource, outcome, run
and conversation. `limit=None` returns every matching event, which is what an export uses. `export` yields lines:
JSON Lines (one object per event, the fields of `activity.FIELDS` in that order) or CSV (a header, then one row per
event). A CSV cell that a spreadsheet would read as a formula (starting with `=`, `+`, `-` or `@`) gets a leading `'`.

The HTTP endpoints come with nodestro-api; the runtime offers only these functions.

## Storage and retention

One document per event in the tenant's store, collection `activity_log`, key `<time>_<event id>` (the pattern of the
other audit records). `activity.prune(store, days=activity.retention_days())` deletes events older than
`NODESTRO_ACTIVITY_RETENTION_DAYS` days (default 90; `0` keeps them without limit; an invalid value logs a warning and
uses the default). The desktop prunes when the workspace opens; on a server the API's housekeeping calls `prune`
(open, see TODO RT-084). A later data-retention policy sets the number of days; nothing else needs to change.

## Mirroring into Axentra

The existing path is `platform_events` (a server with `NODESTRO_AXENTRA_AUDIT` switched on): the record and its
mirror commit in one transaction. Approval audit, MCP and connection events are already mirrored by their own
records, so their activity events are not mirrored again. Every other activity event (tool calls, agent steps, runs,
files) is mirrored through the same sink as action `nodestro.<action>` with its ids, outcome and code in `details`.
`nodestro-server axentra reconcile` writes the activity events whose mirror is missing, like the other audit
records. The backlog gauge (`axentra_platform.reconciliation_backlog`) does not count activity events yet.
