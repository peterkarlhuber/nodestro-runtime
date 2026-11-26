# Egress protection by content provenance (N-08)

Status: **in development, off by default.** The switch is `NODESTRO_CONTENT_PROVENANCE=1`. It only takes effect
together with the access model v2 (`NODESTRO_ACCESS_GRANTS=1` and `NODESTRO_TOOL_APPROVALS=1`), because the grants it
asks for are that model's records ([access-grants.md](access-grants.md)). While it is off, nothing is recorded and
every decision is exactly as before. Activating it is an owner decision, like activating the access model.

## What it does

A conversation or run that holds **content of a granted source** must not hand that content to a recipient outside
the computer without a person's permission. The granted sources are:

| Source | When it enters the context | Recorded as |
|---|---|---|
| A granted folder (N-10) | a call of `list_folder`, `read_file` or `search_files` | `folder` |
| Knowledge | knowledge given in full (mode `context`) at the start of a step, or a call of the knowledge store's tool (`files`) or search tool (`search`) | `knowledge` |
| Persistent memory | a call of the memory tool of an agent with a memory space | `memory` |
| An attached file | files of the run delivered into the step's context at its start (also the overview of a conversation's earlier files), or a call of the input files tool | `attachment` |

Run memory (`memory.mode` `run`) is not a granted source: it holds only what the run itself wrote. The agent test's
writable copy of a persistent memory space (`agent_builder.SpaceCopy`) is: it holds the space's content.

Files a person uploads into an assistant chat (the API's assistant, not a conversation's run) are not a granted
source: the person hands them to the model they chose for that chat themselves, so sending them is the person's own
act, not an agent reading a source someone granted for agents. A run or knowledge attached to such a chat is checked
(the API passes the run's mark, memory files and knowledge text as `shared_content`).

Once such content is there, the run is **marked** (`Run.granted_content`), and so is the conversation it answers in
(`Conversation.provenance` = `granted_content`). The mark is monotonic: it is never cleared. From then on:

- **Every tool call that reaches outside** (`reach` `external`: the web tools, every MCP tool, own tools that reach
  outside, tools that use a connection to another service) needs an N-06 grant: a grant on the tool itself, or on the
  service it uses (its MCP server or its connection). Without one:
  - a person watches (an attended run, a conversation): the call is **asked** with the existing approval card, which
    shows the call's arguments before anything is sent. For a web search that is the query. Approving lets this one
    call run (like a `once` grant); the card can also save a grant (N-06, administrators);
  - nobody watches (a schedule, a batch, an API run): the call is refused with `access.missing_grant`. The agent reads
    a fixed text without the source.
- **Every cloud model request** needs a grant on the cloud provider (`cloud_provider`, action `submit`). The default
  of spec 7.2 ("a cloud model without shared content") no longer applies. This holds for every model of a step: the
  agent's, its fallbacks (always the agent's provider) and its quality judge, whose requests check the judge's own
  provider. The local model server (`ollama`) is no cloud provider while its address is on this computer
  (`offline.is_local_endpoint`, as in N-09's offline mode); a local model server elsewhere is a recipient outside
  the computer and needs a grant on `cloud_provider` `ollama` (`RunManager.leaves_computer`). Without a grant the step
  fails with `access.missing_grant` before the request is sent.
- **Every server tool that reaches outside** (`web_search`, `web_fetch`) needs a grant on the tool (`tool:<key>`).
  Server tools run at the provider and never pass the tool hook, so a request that offers one is refused as a whole.

A grant never lifts a deny or a safety limit, and both come first: strict offline refuses every one of these paths
(`access.safety_limit`), with or without a grant. A grant on a service only satisfies this rule: it never lifts a
per-call question of S-2, the question before a changing MCP call, or the MCP rules for unattended runs.

## Where it is checked

All checks are the runtime's; the studio only shows what happens.

| Path | Where | What refuses |
|---|---|---|
| Tool call | the tool gate, `tool_policy.decide` (`provenance_refusal`, `egress_granted`), at every call | missing grant: ask or `access.missing_grant` |
| Cloud model, at the connection | `RunManager._access_connect` (every connection of an agent's turn, orchestrator decisions, judges), `Workspace.connect_model(..., shared_content=)`, `RunManager.check_cloud_access(..., shared_content=)` | `access.missing_grant` |
| Cloud model, per request | the `on_request` hook of an agent's turn and an orchestrator's decision (`RunManager._check_egress`): granted content can enter a turn after it connected | `access.missing_grant` |
| Quality judge, per request | the judge's own `on_request` (`execute_agent(..., judge_hooks=)`), in a run and in an agent test: the judge connects when the step starts, maybe before the mark, and may use another provider than the agent | `access.missing_grant` |
| Server tools | the same `on_request` check | `access.missing_grant` |
| Run chat about a run | `run_chat` passes `RunManager.content_granted` of the run and its top-level run | `access.missing_grant` |
| A conversation's summary (N-05) | `Conversations._summarize` passes the conversation's mark to `connect_model` | `access.missing_grant` (the history is trimmed instead) |

Every refusal is recorded for the person:

- a refused tool call: `ToolCallApproval` (status `denied`, code `access.missing_grant`), the run event `tool_denied`
  and the activity event `tool.called` with outcome `denied`;
- a refused model request or server tool: the audit event `access_refused` (activity `tool_approval.access_refused`,
  outcome `denied`) and the run event `access_refused`, as N-06 does.

The mark itself is the run event `content_provenance` (`source`, `tool`, `first`).

## How the mark travels

- **Within an execution**: a step marks its run and the top-level run. Each run is stored at its own first mark, a
  sub-workflow's too, so a reader of the run sees the mark before the run ends. Sub-workflows and team runs share the
  top-level run's execution, so a mark anywhere holds for every later call of the whole execution, above and below.
- **A sub-workflow is a recipient like any other**: it runs on this computer, but what it sends outside or to a cloud
  model is checked with the mark it inherits. Nodestro has no transport of its own that starts a workflow on another
  installation; a workflow elsewhere is reached through a tool (an MCP server, or a tool whose connection reaches
  another service), and that tool counts as external.
- **Conversations**: the engine marks the conversation (`Conversations.mark_granted`, one transaction under its lock);
  every answer of a marked conversation starts marked (`RunManager.conversation_granted`, read at every check). The
  history repeats earlier answers, so the mark stays when the next answer runs on another model.
- **A continued run** (`resumed_from`) starts with the earlier run's mark.
- **Finished runs**: `RunManager.content_granted(run)` says whether a run, its top-level run or its conversation is
  marked (fails closed when a parent cannot be read). Callers that send a run's content to a model pass it as
  `shared_content` (run chat in the runtime; the assistant's run attachment and a test series' judge in the API).

## How tools declare their source

The source is a declaration of the tool (orkexa `Tool(content_source=...)`), which the agent reports on every call
(`ToolCall.content_source`). Nodestro's folder tools declare `folder`, the input files tool `attachment`; orkexa's
knowledge store and index tools declare `files` and `index` (recorded as `knowledge`), and the memory tool its
backend's source (`memory`; run memory sets none). A call that the gate refused marks nothing. A tool that ran and
failed marks the run anyway (its error can name what it found).

## Known limits

- Knowledge embeddings (vector retrieval) go to the workspace's local model server whatever its address, as before:
  indexing does not run in a step, and a search query is not checked against the mark. Keep the embedding server on
  this computer when knowledge is granted content.
- A tool without a declaration is not seen. MCP tools never declare a source: their results are external data, not
  granted content.
- Content that entered a conversation or run while the switch was off is not recorded: such a conversation is
  `unchecked`.
- The mark is per conversation and execution, not per piece of content: after one folder read, every outbound call of
  the conversation needs a grant, also one that carries nothing of the folder.
- An agent test (`AgentTester`) holds its own mark for the one test: its cloud model needs a grant after a granted
  source's tool ran, and an outbound tool is refused as before (a test never asks and never acts on a tool grant).
- `tool_policy.explain` and `who_may` explain the records, not a run's mark: a tool row of a marked run can read
  `per_call_rules` where the gate asks.

See `docs/TODO.md` RT-094.
