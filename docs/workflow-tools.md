# Workflows as tools (N-20)

An agent can call a saved Nodestro workflow in one fixed version as a tool: in a conversation, in a workflow step or
in a published release. The code is `nodestro_runtime.workflow_tools`; the execution is the engine's
(`Runs._call_workflow_tool`). **Off by default**: only while `NODESTRO_WORKFLOW_TOOLS=1`; with the switch off an
agent that has workflow tools does not start (`workflow_tool.disabled`).

## The binding

`AgentConfig.workflow_tools` holds `WorkflowToolConfig` entries: `workflow_id`, `version` (required), `name` (the
tool name the model sees), an optional `description` and the `effect` computed when the workflow was added
(`describe`, API `GET /api/workflows/{workflow_id}/versions/{number}/tool`). An agent without workflow tools is
stored exactly as before (the field is left out while empty).

- **Fixed version.** The binding names its version; a newer saved version is only reported
  (`workflow_tools.newer_versions`, `AgentConversationDetail.workflow_tool_updates`) and never used by itself.
- **Releases, exports, consents, pre-checks.** `graph.workflow_refs` names workflow tools like sub-workflows and
  teams, so publishing freezes the called workflow version into the release bundle (`subworkflows`), a release run
  resolves it from the bundle only, a memory consent covers it (RT-089d: its id, version and digest are consent
  targets), and the recursion and unattended checks follow it.
- **Arguments.** The workflow's start form (`arguments_schema`; text, number, date, boolean and select fields) and an
  optional free text `input`; file fields are never offered to a model. A call is checked like a manual start
  (`forms.prepare`); an argument the workflow does not take is refused.

## The effect

`workflow_tools.aggregate` computes what the workflow may do as a whole: the highest `access` (`read` < `write` <
`destructive`) and `reach` (`internal` < `external`) of everything it contains, at every depth:

- the models of its agents (fallbacks and a quality judge included) and of its orchestrators: a model that is not
  served by the local model server (Ollama) on this computer counts as `read` + `external`, also a configurable
  endpoint and an Ollama server whose saved or external address is not loopback (`Workspace.local_model_server_here`;
  the managed and the container's server are local), so an offline step never reaches a cloud model through a
  workflow;
- the tools of its agents (library capability, `tool_policy`), MCP tools (the administrator's classification) and
  the provider's server tools;
- result files and persistent memory spaces (`write` + `internal`);
- sub-workflows, orchestrator teams and nested workflow tools, in their pinned versions.

Anything unknown (a tool that is not there, a workflow that cannot be read, teams chosen at run time, nesting beyond
the engine's limit) counts as `tool_policy.UNKNOWN` (`write` + `external`) and is listed in `effect.unknown`. The
stored effect is computed again at every call and the higher one counts (`effective`): a stored effect never lowers
what the workflow may do now.

## A call

1. **Before the start.** The per-call gate (S-2) asks with the effect as the tool's capability (kind `workflow`,
   the fixed version, a hash of workflow, version and effect). A workflow tool that is not read-only is asked before
   every start, whatever the binding says (like a changing MCP tool); an unattended run cannot start one (a pre-grant
   names a tool and version only, never a workflow's effect, so none applies). Without
   the gate (`NODESTRO_TOOL_APPROVALS` off) such a call is refused (`workflow_tool.approval_required`, fail closed);
   a read-only workflow runs. An offline step (N-09) refuses a workflow that reaches outside. Access grants and
   denies on the workflow (N-06) are checked before the start; with granted content in the conversation (N-08) the
   gate's provenance rules apply to the workflow's effect, and every call inside the workflow is checked again.
2. **The run.** A child run of the agent's run below its step (trigger `subworkflow`, `parent_node_id` = the step),
   executed with `_execute_child` exactly like a sub-workflow step: its own workflow budget inside the agent run's
   budget (so its cost is the answer's and the conversation's), the agent run's limits, its approval steps and tool
   approvals. The step's record names it (`ToolCallRecord.workflow`, found by the call's id, also for parallel calls;
   `NodeRun.child_run_ids`). A workflow tool's name never equals another tool's the step has or could have (library,
   built-in, server and MCP tools, `memory`, `files`; `workflow_tool.name_taken`).
3. **Waiting for a person.** When the workflow waits (an approval step, a tool approval, a budget decision, at any
   depth) the call waits in the agent's thread and goes on once something was decided, also by another process
   (it reads the stored runs, tool approval requests and budget requests). A decision about such a run needs no
   paused top-level run (`Runs.decide`), but a running one: once the agent's run ended, the decision is refused
   (`workflow_tool.not_waiting`). After `WORKFLOW_TOOL_WAIT_SECONDS` (24 hours) without a decision the workflow's run
   is cancelled and the call fails.
   **Threads:** an agent step with workflow tools runs on a thread pool of its own per nesting depth
   (`NODESTRO_RUN_THREADS` threads each), never on the pool of ordinary steps. A waiting call therefore never holds a
   thread that the workflow's own steps (or any other run's ordinary steps) need; at most that many steps with
   workflow tools of one depth run at once, later ones wait for a thread.
4. **The result.** The workflow's output and the names and formats of its result files, as JSON the model reads as
   data. A failed or stopped workflow is an error result for the agent.
5. **Cancel and errors.** Cancelling the agent's run (a conversation's answer) cancels the workflow's run; a
   workflow's run left waiting when the call ends is cancelled. A process that ends during a call leaves the agent's
   run interrupted, and its waiting workflow runs are cancelled with it (the restart's recovery, the job reaper);
   continuing it asks before the step runs again, because the call is in the step's tool journal.

## In a conversation

An answer's tool call of a workflow tool has `workflow` (`WorkflowCallView`): the workflow, its version, its run and
status, the approval steps and tool approvals its run tree waits for (cards: decided with
`POST /api/runs/{run_id}/approvals/{node_id}` and the tool approval routes; the run routes see these runs only for
the conversation's owner, H-1), and its result files. The answer's `result_files` are its attachments (download with
`GET /api/runs/{run_id}/files/{file_id}`). A long workflow runs while the answer is open, like every answer in the
background; the answer is the result message.

## Not covered

- Access grants and denies (N-06) are checked for the workflow itself before the start (for the calling agent) and
  for every call inside it as that workflow's agents; a deny that names only the calling agent does not follow into
  the workflow's own agents.
- Sub-workflows inside the called workflow that name no version are pinned when their step starts, not once per
  call, so a version saved meanwhile can run although the effect was computed with the earlier one (the effect is
  computed again at the next call). Pinning the whole tree once per call is open (RT-096).

- File fields of the start form (the conversation's files are not passed to the workflow).
