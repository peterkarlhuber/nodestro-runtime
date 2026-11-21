# Offline per agent and per conversation (N-09)

The strict offline mode (`Settings.strict_offline`, D12) holds for the whole installation. `AgentConfig.offline` and
`Conversation.offline` apply the same rules to one agent, or to every answer of one conversation. Another agent of
the same workspace keeps its cloud models and internet tools. Both flags are off by default; with both off, a run
behaves exactly as before, and an agent without the flag is stored without the field (byte-identical records).

Module: `nodestro_runtime.offline`.

## What an offline agent or conversation cannot use

| What | Rule | Code |
|---|---|---|
| Cloud models | Every provider except the local model server (`ollama`) counts as a cloud model, also an OpenAI-compatible or vLLM endpoint on `localhost` (as in the strict offline mode). This includes the agent's model, its quality judge, retries and an orchestrator's decisions | `offline.cloud_model` (`model`, `scope`) |
| A local model server elsewhere | The server must be on this computer (`offline.is_local_endpoint`, below) | `offline.remote_endpoint` (`scope`) |
| Server tools | They run at the provider and never pass the tool hook | `offline.server_tools` (`names`, `scope`) |
| Tools with external reach | See the per-tool state below | `offline.tool_unavailable` (`name`, `reason`) |
| Shared memory | A persistent memory space (`memory.mode = persistent`) is read by every agent of the space, cloud agents too, and a knowledge proposal becomes knowledge any agent searches. An offline agent gets run memory only and no knowledge proposals, as an answer in a conversation | `offline.shared_memory` (`fields`: `memory`, `knowledge_proposals`; `scope`) |

`scope` is `agent` or `conversation`: which setting refused the call.

What counts as this computer (`offline.is_local_endpoint`, shared with the strict offline mode's pre-check): the host
name `localhost` or a loopback IP address (`127.0.0.0/8`, `::1`, also IPv4-mapped). The host is parsed as an IP
address, so a DNS name that merely starts with `127.` (`127.0.0.1.nip.io`, `127.attacker.example`) is not local. The
host name `ollama` (the Ollama container on Nodestro Local Docker's internal network) is local only in the
`local-container` profile; in the other profiles it is any host of that name and is refused.

## Per-tool state

`offline.tool_state(library, name)` says whether a tool of the agent can be used offline, and why not (`reason`):

| Tool | Available offline when | `reason` otherwise |
|---|---|---|
| Built-in tool with a spec | `ToolSpec.offline` is true (`search_web` and `fetch_web_page` are not) | `network` |
| Integration tool without a spec | never (its network use is unknown) | `network` |
| MCP tool | its server is a local `stdio` server (it runs in Nodestro's sandbox container without network) | `remote_mcp` (elsewhere) or `http_mcp` (an HTTP server on this computer, see below) |
| Own or generated tool | its capability's `reach` is `internal` (stated, or derived from a sandbox without network); an unstated capability counts as external. At call time a derived `internal` is judged again by the sandbox the call really runs in (the workflow's Python environment can be a host subprocess with network) | `external_reach` |
| A tool that cannot be found | never (fails closed) | `unknown` |

The run's own tools (input files, run memory, knowledge search, skills) are not library tools of the agent and stay
available: what they write stays in the run. Memory and knowledge proposals are the exception above: a persistent
memory space and a knowledge proposal outlive the run and other agents read them, so an offline agent uses neither
(`offline.shared_memory`). The knowledge search embeds its query with the local model server, which an offline step
only reaches when it is local.

The agent's validation (`AgentBuilder.validate`, so the pre-check and the agent editor) reports, while `offline` is on:

- `offline.cloud_model` as an error for a cloud model (`field` `model`) or a cloud judge (`field` `quality`);
- `offline.server_tools` as an error;
- `offline.shared_memory` as an error for a persistent memory space (`field` `memory`) or knowledge proposals
  (`field` `knowledge_proposals`);
- `offline.tool_unavailable` as a **warning** for each tool that is not available offline, with its reason. The agent
  keeps the tool; a call of it is refused at run time and the model reads that the tool is not available offline.

The pre-check also reports `offline.remote_endpoint` for an offline agent whose local model server is not local.

## Enforcement at call time

The pre-check is not the only guard. The engine checks again at every call, so a run started without the pre-check, a
tool or model reached in another way, a retry or a resumed run is refused too:

- **Models**: every connection of a step goes through `RunManager._checked_connect(context, node_id, config)`. For an
  offline step, a cloud provider is refused before its connection is resolved, and the local connection is checked
  for its address. This covers the agent's turns, its quality judge and an orchestrator's decisions. Anthropic's
  fallbacks only apply to an Anthropic model, which is refused first.
- **Tools**: the engine's `before_tool` hook refuses a call of a library tool that is not available offline before
  the per-call approval gate (S-2) is asked, and also when the gate is off. The call record has
  `approval.status = denied` and `approval.code = offline.tool_unavailable`, and the run's warnings list it.
- **Server tools**: an offline step with server tools does not start (`offline.server_tools`).
- **Shared memory**: an offline step with a persistent memory space or knowledge proposals does not start
  (`offline.shared_memory`), so nothing of it is written where other agents read. This also holds for an agent below
  an offline conversation (a team or sub-workflow agent); the answer's own agent already has run memory, and an
  offline answer proposes no knowledge (`answer_agent`).

Which steps are offline (`RunManager.offline_scope`):

- the agent's own `offline` flag;
- the conversation of the top-level run (trigger `conversation`). It is read from the store at every call
  (`Conversations.is_offline`), so switching a conversation offline applies at the next call of a running answer, and
  it holds for every run below the answer (sub-workflows, team runs of an orchestrator). An unknown conversation
  counts as offline.

The answer's agent of an offline conversation is stored offline in the run's workflow (`answer_agent(..., offline=True)`),
and an offline agent template answers with local models only, also in a conversation that is not offline
(`conversation.offline_cloud_model` when it is created or switched to a cloud model).

## Other paths

Content of a finished run goes to a model outside the run in a few places. Each one asks
`RunManager.content_offline_scope(run)`: the run answers an offline conversation (`conversation`), or an offline agent
ran in it, above it or in any run below it (`agent`). A continued run (`resumed_from`) and a node result taken over
from an earlier run (`reused_from`) hold that run's content, so the earlier run counts the same way. A parent, child
or earlier run that cannot be read counts as offline.
Then only a local model on this computer gets the content: `Workspace.connect_model(model, offline=scope)` refuses a
cloud model before its connection is resolved (`offline.cloud_model`) and a local model server elsewhere
(`offline.remote_endpoint`).

- **Run chat** (`run_chat.answer_question`): a question about such a run is answered by local models only, because
  the snapshot holds the step's content.
- **The assistant's run attachment** and **a test series' AI judge** (nodestro-api) use the same rule.
- **A conversation's history** (`Conversations.send`): every answer gets the earlier messages as its input
  (`answer_input`). If an earlier answer is offline content (`Conversations.offline_history`), the conversation stays
  on local models even after its offline switch is turned off: a cloud model is refused with `offline.cloud_model`
  before anything is stored or sent, and a local answer is offline again. An answer whose run cannot be read counts
  as offline.
- **Agent test** (`AgentTester.run`): the same model, server tool and tool rules as a run.
- **Strict offline mode**: unchanged. It still refuses cloud models in `Workspace.connection` and checks tools in the
  pre-check and in the approval gate.

## Limits

- A local `stdio` MCP server counts as local, as in the strict offline mode: it runs in Nodestro's sandbox container
  without network.
- An MCP server over HTTP is not available offline, also on `localhost` (`http_mcp`): the address says where the
  server runs, not whether it reaches the internet itself, so it fails closed. There is no switch to allow one; run
  the server as a local `stdio` server instead. The strict offline mode's pre-check counts only `stdio` servers as
  local too (`check.offline_mcp`), as its approval gate already did.
- On a server whose local model server is the operator's endpoint on another host, offline agents cannot run (the
  same rule as the strict offline mode).
- An agent's output goes wherever its workflow sends it. A later agent that is not offline may pass it to a cloud
  model; mark every agent that handles the content offline, or the conversation.
