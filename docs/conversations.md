# Conversations with own agents (N-04)

Status: **in development, off by default.** The runtime module `nodestro_runtime.conversations` has no effect until
a conversation is created. nodestro-api serves it only while `NODESTRO_CONVERSATIONS=1` (otherwise its routes answer
`404 conversation.disabled`).

A conversation talks to one agent template in a fixed version, with one model. Both can be switched between answers.
The Nodestro Assistant may move onto this object later (ECO-4); it is not part of N-04.

## Objects

`Conversation` (collection `conversations`, key = id):

| Field | Meaning |
|---|---|
| `id`, `tenant_id`, `owner` | The tenant whose store holds it, and the server user who started it (`None` on the desktop) |
| `title`, `auto_title` | The title; made from the first message until it is set |
| `agent` | `TemplateRef`: the agent template and its version (`template_id`, `version`) |
| `model` | The model of the next answer (`provider:model`) |
| `offline` | Only local models answer; a cloud model is refused (`conversation.offline_cloud_model`) |
| `provenance` | Placeholder for N-08 (always `unchecked`) |
| `budget_usd` | The budget over all answers (`None`: each answer has the default budget) |
| `files` | The conversation's files: input files of origin `conversation` |

`ConversationMessage` (collection `conversation_messages`, key = `<conversation id>:<seq>`):

| Field | Meaning |
|---|---|
| `role`, `content`, `seq` | `user` or `assistant`, the text, the order |
| `attachments` | User messages: the files sent with it and how the model received them (`mode`, as the run's `ProvidedFile`) |
| `tool_calls` | Answers: `id`, `name`, `arguments`, `result`, `is_error`, `started_at`, `finished_at`, `duration_ms`, `approval` |
| `sources` | Answers: the web sources the agent's tools saw (the run's source ledger) |
| `status`, `error` | `complete` for a user message; an answer has the status of its run |
| `agent`, `model` | Answers: the agent version and model that answered |
| `usage`, `cost_usd` | Answers: the usage and cost of the answer's run |
| `run_id`, `approval_ids` | Answers: the run, and the tool approval requests it waits for |

Only the owner sees a conversation on a server (another owner's id is not found); on the desktop everyone of the
workspace does, as for the Nodestro Assistant. A conversation stored without an owner (made on a desktop, then restored
into a server) is seen by nobody on a server, administrators included: it fails closed and stays orphaned until it
is restored on a desktop (L-7) or the server's operator assigns it an owner who is a member of the tenant
(`nodestro-server assistant backfill-owners --tenant --user --conversation`, audited; nodestro-api
`docs/conversations.md`, W3-S1). Reading it never assigns an owner. A stored document of another tenant is never read.

Who asks without an owner identity on a multi-user installation passes `NO_OWNER` (W3-S1): a principal without a
role in the tenant, such as a service, a machine or anything else that is not a member. It sees no conversation, no
message, no file and no answer run (nor a run below one), and it starts no conversation (`access.person_required`),
so no conversation is ever shared by everyone without an owner identity. Before, such a principal was passed as `None`
and saw every conversation of the tenant. Only the desktop's single person (and Nodestro Local Docker's) passes
`None`; the API decides which owner a request has (nodestro-api `owner_of`, `docs/access.md` there). An administrator
is a member like any other: the role grants no read of another member's conversation. Both collections are
documents of the tenant's store: on PostgreSQL they are rows of `nodestro_documents` with `tenant_id`, so no schema
migration is needed and the tenant SQL check covers every statement (the module has no SQL of its own;
`tests/test_conversations_postgres.py`).

## Answers

Every answer is one run of the engine (`RunManager`), never a second agent loop:

- The run's workflow is a start step and the agent (node `agent`), with the conversation's id as workflow id and no
  inbox notifications. The trigger is `conversation` and `trigger_ref` is the conversation id.
- The agent is the template's version with the conversation's model, its task template replaced by `{{input}}`, and
  without cloud fallbacks. Persistent memory becomes run memory (W3-S1 review): a memory space is shared by every
  member (`/api/memory`), so an answer never writes the conversation into one; run memory belongs to the answer's
  run, which only the conversation's owner sees, and goes with it. A memory of one's own per conversation is later
  work (RT-089).
- The run's input is the earlier messages (the newest 30 user messages and finished answers) followed by the new
  message. N-05 compacts long conversations.
- The run's usage is the answer's usage; the conversation's cost is the sum of its runs (failed and cancelled ones
  included). The runs are in the run list like every other run.
- With a conversation budget, each run gets the rest of it as its budget; an exhausted budget refuses the message
  (`conversation.budget_exhausted`).
- One answer at a time, across processes (M-2): the answer is reserved first, in one store transaction under the
  conversation's lock (`store.lock`), together with the checks (another answer running, the budget, the files). The
  two message keys are written only when free (`put_raw_if_absent`). The run starts after that; if it cannot start,
  the reservation is removed and nothing stays stored. A message while an answer runs is refused
  (`conversation.answer_running`). A reservation whose run never started (the process ended in between) counts as
  failed after `RESERVATION_TIMEOUT` (10 minutes, `conversation.run_missing`). A reserved answer has `run_id` `""`;
  the send that writes its run and a reader that fails it both replace it only while `run_id` is still `""` (a
  compare-and-set), so exactly one wins. A send that loses (its launch took longer than the timeout) cancels its run
  and answers `conversation.answer_running`, so two answers never run (N-4).
- Every change of a conversation (rename, switch, files) is made on the stored document under the same lock, so it
  never writes back an older one (M-1). No state per conversation is kept in memory.
- An answer message follows its run when it is read: its status, text, tool calls, sources and usage come from the
  run. So an answer that ran in a worker, or that was interrupted by a restart, is shown as its run ended.

Streaming uses the run's events (`RunEvent`); the API forwards them unchanged. Cancelling an answer cancels its run:
the engine sets the run's `CancelFlag` and closes the model connection (RT3-05).

Tool calls go through the run's per-call approval rules (S-2) and the access model v2 (N-06). A call that waits for a
person is decided with the existing approval routes; the answer lists the request in `approval_ids`. A grant with
duration `conversation` names the conversation id (`engine.conversation_of`), so it holds for every later answer of
the conversation; for other runs it still names the run. Deleting a conversation revokes its active grants for
"this conversation" (`AccessRules.revoke_conversation`, called by the API's delete route; L-2).

Records scoped to a workflow or an agent do not reach a conversation (L-1): an answer's workflow id is the
conversation id, and its agent is named `<conversation id>/agent`, not after the agent template or a workflow that
uses it. A deny for workflow `wf_x` or for agent `wf_x/node` therefore does not hold in a conversation with the same
template. Use a deny for the tool (or another resource) with scope `all` to block it in conversations too. Agent ids
of their own (spec 7.1) will close this gap.

## The answers' runs belong to the conversation

An answer's run holds the conversation (its input), the answer (its output) and the files sent with it. So it belongs
to the conversation (H-1):

- `Conversations.run_visible(run_id, owner)`: only who sees the conversation sees its runs (also the runs below one;
  `NO_OWNER` sees none).
  `summary_visible(run, owner)` does the same for a run at hand (a run list's summary): a top-level run that is not an
  answer needs no store read, an answer's run one (N-2).
  The API checks it on every route with a run id (runs, export, files, cancel, resume, approvals, tool approvals,
  budget, inputs, run chat, events) and in the lists of runs and pending approvals; another user gets
  `404 run.not_found`. A run whose conversation is gone is seen by nobody. So is a run below another one whose top
  cannot be reached (a parent is missing, or the parents form a loop): whose it is cannot be told, so it fails
  closed (review Low 1).
- Deleting a conversation deletes its answers' runs with their result files, tool checkpoints, live chat and the
  input files only they used, after cancelling a running one (it waits up to 10 seconds for it to stop; a run still
  going then stays, hidden). The usage ledger keeps the cost.
- The run's workflow is named `Conversation`, never the conversation's title, because the run list and the usage
  report show the name to every member.
- Aggregates still count the answers' runs (N-3, by design): the usage report (by workflow, keyed by the conversation
  id, named `Conversation`) and the skill usage statistics. They show numbers and the conversation id, never the
  messages, the answers or the files.

## Models

A cloud model is never chosen automatically:

- Without an explicit model, a new conversation takes the agent's model or the workspace's default model, but only
  when it is a local model (`ollama`). Otherwise the person has to choose (`conversation.model_required`).
- Cloud fallbacks of the agent are dropped; local fallbacks stay.
- The agent's quality check runs only with a local judge model; with a cloud judge (the default judge is a cloud
  model) it is switched off for the answer (H-2).
- Before the run starts, every model it will use (`preflight.agent_models`: the agent's and its judge's) is checked:
  an offline conversation uses local models only (`conversation.offline_cloud_model`), and the only cloud model an
  answer may use is the conversation's own, chosen by the person (`conversation.cloud_model_not_chosen`).
- Switching the agent keeps the conversation's model.

## Files

Files are input files of origin `conversation`. They stay until they are removed from the conversation or the
conversation is deleted (the input file clean-up keeps them). A message sends some of them; they go to the answer's run
as run files, so the run's file delivery decides how the model gets them. An image goes as an image only to a model
with image input; otherwise it is marked `unavailable` and the model only learns that it exists.

## Export

`Conversations.export_markdown` writes the title, agent, model and cost, then every message with its files, tool calls
(arguments, result, duration), sources, run and cost.

## Codes

| Code | When |
|---|---|
| `conversation.agent_not_found` | The agent template or version does not exist (`template_id`) |
| `conversation.model_required` | No model given, and neither the agent's nor the default model is local |
| `conversation.offline_cloud_model` | A cloud model for an offline conversation (`model`) |
| `conversation.empty_message` | Neither text nor files |
| `conversation.answer_running` | Another answer of the conversation is still running |
| `conversation.budget_exhausted` | The conversation's budget is used up (`budget_usd`) |
| `conversation.cloud_model_not_chosen` | The answer would use a cloud model nobody chose for it (`model`) |
| `conversation.too_many_files` | The conversation holds the most files (`limit`) |
| `conversation.run_missing` | The answer's run no longer exists (shown on the answer) |
