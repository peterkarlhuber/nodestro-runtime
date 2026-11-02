# Run budget decisions (BUDGET-1)

A run's budget is a hard limit on what its model requests may cost. Before BUDGET-1 a run whose next request did not
fit failed with `run.budget_exceeded`. Now an **attended** run pauses before the request is sent and asks for a
decision: the person who started the run (or an admin) raises the limit **of this run only**, or ends the run.

## Behaviour

- Every model request of a run (agent turns, quality judges, orchestrator decisions, sub-workflows and team runs) is
  admitted by orkexa's `BudgetTracker` with its most expensive possible cost (input estimate plus `max_tokens` at the
  model's price). Attended runs use `on_oversized="reject"`: a request that cannot fit is refused **before** it is
  sent, never sent alone and paid for afterwards.
- A refused request pauses the run: the run is `paused`, the waiting node is `awaiting_budget`,
  `run.budget.pending_request_id` names the open request (collection `budget_requests`, key `<run id>:<request id>`).
  The agent's turn is saved as a request-stage checkpoint (`agent_checkpoints`, `kind="budget"`); no thread, worker
  slot or job is held while the run waits.
- One run has at most one open request. Parallel agents that hit the limit join it (`requests[]`), every other model
  request of the run is held until it is decided.
- A raise is a compare-and-set on the request: one decision wins, a repeated one is refused with
  `budget.not_pending`, the same decision sent again with the same `idempotency_key` by the same person answers the
  stored result. The run continues from its checkpoint; nothing that was sent or run is repeated. If the raise is
  still too small, the run asks again, as often as needed.
- Ending the run cancels it with `run.budget_ended`. An unanswered request expires after the tenant's
  `budget_decision_hours` (default 24, at most 720); the run then fails with `run.budget_exceeded` (`expired`).
- A request that already ran keeps its cost even when it cost more than its bound allowed (a price change): the
  overshoot is recorded, and the **next** request asks for a decision.

## What is raised

`block.scope` names the limit that refused the request:

| Scope | Raised limit | Unchanged |
|---|---|---|
| `run` | the top-level run's `run.budget.limit_usd` | the workflow, the default budget, other runs |
| `subworkflow` | the child run's limit (its own workflow budget) | the parent run's limit (checked next) |
| `team` | the team run's limit below an orchestrator | the parent run's limit (checked next) |

A raise never changes a stored workflow, a release, the default budget or another run (also runs of the same
workflow). Every raise is recorded in `run.budget.raises` of the run whose limit it changed.

## Who may decide, and how far

- The runtime stores and validates decisions; who may decide is the API's check: the `run` permission **and**
  (the person who started the run, `run.started_by`, or an admin). Read-only users cannot decide.
- A new limit must be higher than the current one, have at most two decimal places and stay at most the lower of the
  tenant's `max_run_budget_usd` and the operator's `NODESTRO_MAX_RUN_BUDGET_USD` (`budget.over_policy`).
- Unattended runs (`schedule`, `batch`, `benchmark`, `api`) never pause for a budget: they fail as before. There is
  no automatic raise.
- A Live Run Chat message is never a budget decision; an instruction sent while the run waits is used by the next
  orchestrator decision (and can make that request larger, which may ask again).
- A raise never approves a tool call: tool and MCP approvals still ask on their own.

## Prices

The suggestion (`suggested_limit_usd`) admits exactly the refused request (its maximum cost rounded up to the cent);
it is a maximum for that request, never a forecast of the run's total. Unknown prices are no budget question: with
strict pricing the request fails with the pricing error as before (`unknown_cost` marks partial knowledge). A raise
does not make an unknown price known.

## Audit

Every request, raise, end, refusal, expiry and cancellation is an event in `budget_audit` (the newest 5,000 per
tenant): run, request, actor, limits before and after, result code, scope and continuation. With a platform events
sink (the server's Axentra mirror, off by default) each event is mirrored as `nodestro.run_budget.<kind>` in the same
transaction.

## Operations

- `RunManager.sweep_budget_waits()` expires overdue requests, closes requests of runs that are gone, removes their
  checkpoints and continues paused runs whose request was decided while nothing executed them. Workers call it next
  to the tool-approval sweep; the desktop calls it on start.
- Settings: `max_run_budget_usd` (tenant cap, empty: none), `budget_decision_hours`, and the read-only
  `operator_max_run_budget_usd` from `NODESTRO_MAX_RUN_BUDGET_USD`.
- Lost checkpoints fail the run with `run.budget_checkpoint_lost` instead of repeating paid work.

## Limits

- orkexa batches and `run_agent` sub-agents cannot suspend (orkexa ORK-022); inside such a call a refused request
  fails with `run.budget_exceeded` as before.
- Live Run Chat answers and the Studio's agent test have budgets of their own and never ask for a budget decision.
