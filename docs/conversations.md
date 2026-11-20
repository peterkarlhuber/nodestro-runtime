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
workspace does, as for the Nodestro Assistant. A stored document of another tenant is never read. Both collections are
documents of the tenant's store: on PostgreSQL they are rows of `nodestro_documents` with `tenant_id`, so no schema
migration is needed and the tenant SQL check covers every statement (the module has no SQL of its own;
`tests/test_conversations_postgres.py`).

## Answers

Every answer is one run of the engine (`RunManager`), never a second agent loop:

- The run's workflow is a start step and the agent (node `agent`), with the conversation's id as workflow id and no
  inbox notifications. The trigger is `conversation` and `trigger_ref` is the conversation id.
- The agent is the template's version with the conversation's model, its task template replaced by `{{input}}`, and
  without cloud fallbacks.
- The run's input is the earlier messages (the newest 30 user messages and finished answers) followed by the new
  message. N-05 compacts long conversations.
- The run's usage is the answer's usage; the conversation's cost is the sum of its runs (failed and cancelled ones
  included). The runs are in the run list like every other run.
- With a conversation budget, each run gets the rest of it as its budget; an exhausted budget refuses the message
  (`conversation.budget_exhausted`).
- One answer at a time: a message while an answer runs is refused (`conversation.answer_running`). Nothing is stored
  when an answer cannot start.
- An answer message follows its run when it is read: its status, text, tool calls, sources and usage come from the
  run. So an answer that ran in a worker, or that was interrupted by a restart, is shown as its run ended.

Streaming uses the run's events (`RunEvent`); the API forwards them unchanged. Cancelling an answer cancels its run:
the engine sets the run's `CancelFlag` and closes the model connection (RT3-05).

Tool calls go through the run's per-call approval rules (S-2) and the access model v2 (N-06). A call that waits for a
person is decided with the existing approval routes; the answer lists the request in `approval_ids`. A grant with
duration `conversation` names the conversation id (`engine.conversation_of`), so it holds for every later answer of
the conversation; for other runs it still names the run.

## Models

A cloud model is never chosen automatically:

- Without an explicit model, a new conversation takes the agent's model or the workspace's default model, but only
  when it is a local model (`ollama`). Otherwise the person has to choose (`conversation.model_required`).
- Cloud fallbacks of the agent are dropped; local fallbacks stay.
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
| `conversation.too_many_files` | The conversation holds the most files (`limit`) |
| `conversation.run_missing` | The answer's run no longer exists (shown on the answer) |
