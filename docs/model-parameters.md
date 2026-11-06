# Model parameters, output limits and provider capabilities

How Nodestro decides which output limit a request sends, which agent options a model offers, and what each provider
really does with them. Code: `nodestro_runtime/output_limits.py`, `nodestro_runtime/model_profile.py`,
`AgentBuilder.profile/limits/output_limit` (`agent_builder.py`), `Connection.model_limits/effort` (`providers.py`);
Orkexa: `ModelLimits` (`orkexa/core/capabilities.py`) and `Provider.limits(model)`.

## Token kinds

| Kind | Meaning | Where it is set |
|---|---|---|
| Context window | Input + output of one request | Documented per model (Orkexa), declared for compatible endpoints, read from the local Ollama server (`OLLAMA_CONTEXT_LENGTH`; a server setting, not a per-request value) |
| Input tokens | The prompt, history, tool results | Not limited by a setting; the budget pre-check estimates them |
| Output tokens (`max_tokens`) | The answer **including** reasoning | Agents: `AgentConfig.max_tokens`; Nodestro's own steps: `output_limits` (below) |
| Reasoning | Thinking inside the output | `effort` (graded), `thinking` (Anthropic switch), `thinking_budget` (Gemini 2.5) |

The budget pre-check (`orkexa.core.cost_bound.request_cost_bound`) reserves exactly the `max_tokens` a request sends,
so a higher limit never bypasses a budget.

## Output limits of Nodestro's own operations

Three levels, the first that applies wins, then the model cap:

1. Workspace setting `output_limits[<operation>]` (Settings > Model output limits; `PUT /api/settings`).
2. Built-in default (`output_limits.OPERATIONS`).
3. Cap: the model's documented/declared `max_output_tokens`, else half the context window when only that is known.
   A lowered value is reported (`EffectiveLimit.reduced`; architect log `architect.limit_reduced`).

| Operation | Default | Former hard-coded value |
|---|---|---|
| `architect_analysis` | 8,000 | 8,000 |
| `architect_plan` | 32,000 | 16,000 |
| `architect_agent` | 16,000 | library default |
| `architect_repair` | 8,000 | 8,000 |
| `assistant_chat` | 16,000 | 16,000 |
| `agent_design` | 16,000 | library default |
| `memory_proposal` | 8,000 | 8,000 |
| `api_guide` | 8,000 | 8,000 |
| `run_chat` | 4,000 | 1,500 |
| `orchestrator` | 8,000 | library default |
| `quality_judge` | 8,000 | library default |
| `tool_generation` | 16,000 | library default |

Settings range: 256 to 1,000,000 (`settings.output_limit_invalid`); `null`/`0` removes an override.

**Truncated answers (Workflow Architect).** A response with `stop_reason` `max_tokens`/`length` raises Orkexa's
`IncompleteResponseError`; the half answer is never parsed. With `architect_retry_truncated` (default on) the step is
asked once more per doubling, up to `architect_retry_max_tokens` (default 64,000) and the model maximum
(`output_limits.retry_limit`). A retry is sent only when its whole cost bound fits into the job's remaining budget
(never as an "oversized" request); otherwise the job fails with `architect.truncated_over_budget`. Without a retry the
job fails with `architect.truncated` (params `operation`, `max_tokens`, `retry`, `reason`).

**Agents.** `AgentConfig.max_tokens` (1 to 1,000,000) is validated against the model's maximum
(`config.max_tokens_above_model`, error) and context window (`config.max_tokens_above_context`, warning). Drafts of
the agent designer stay at most 32,000 and are lowered to the chosen model's maximum (`draft.max_tokens_reduced`).

## Parameter names per provider (what is really sent)

| Provider | Output limit | Effort | Thinking |
|---|---|---|---|
| Anthropic | `max_tokens` | `output_config.effort` | `thinking.type` (`adaptive`, `between_tools`, `disabled`) |
| OpenAI (Responses) | `max_output_tokens` | `reasoning.effort` | – |
| Google Gemini | `config.max_output_tokens` | `thinking_config.thinking_level` (Gemini 3/4) | `thinking_config.thinking_budget` (Gemini 2.5) |
| Amazon Bedrock (Converse) | `inferenceConfig.maxTokens` | `outputConfig.effort` (Claude) | – |
| OpenAI-compatible / vLLM | `max_tokens` or `max_completion_tokens` (`max_tokens_param`) | `reasoning_effort` (only when declared) | – |
| Ollama (OpenAI-compatible) | `max_tokens` → `num_predict` | `reasoning_effort` for thinking models | – |

Orkexa's tests assert each name with recording doubles (`orkexa/tests/test_model_limits.py`).

## Capability matrix: real, simulated, unavailable

"Real" = the provider does it and Nodestro sends it; "simulated" = Nodestro provides it itself; "unavailable" = shown
as not available with a reason code (`model_profile.OptionSupport.reason`), never faked.

| Feature | Anthropic | OpenAI | Gemini | Bedrock (Claude) | Compatible / vLLM | Ollama |
|---|---|---|---|---|---|---|
| Effort | real (documented levels) | real (gpt-6: all five, classic: none) | real (3/4: low–high) | real | declared only | real for thinking models (no grades) |
| Thinking switch | real (modes per model) | unavailable (`provider`) | unavailable (`provider`) | unavailable (`provider`) | unavailable | unavailable |
| Thinking budget | unavailable (`provider`) | unavailable | real on 2.5 (`model` elsewhere) | unavailable | unavailable | unavailable |
| Prompt caching option | real | provider automatic (`automatic`) | provider automatic (`automatic`) | real (cache points after system prompt, tools and conversation; models with `caching`) | unavailable | unavailable |
| Fallback models | real | unavailable | unavailable | unavailable | unavailable | unavailable |
| Provider-native tools (web search, fetch, code execution) | real (priced) | unavailable (`not_integrated`: fees not priced) | unavailable (`not_integrated`) | unavailable (`provider`) | unavailable | unavailable |
| Tool calling | real | real | real | real | per capability | per model (server reports) |
| Nodestro tools / MCP tools | simulated (run by Nodestro under approvals and sandbox rules) | same | same | same | same | same |
| Image / document input | per capability (`capabilities.image_input`, `document_input`) | same | same | same | declared | per model |

The four tool kinds stay separate: provider-native tools (run at the provider), the model's tool calling, Nodestro
tools and MCP tools (Nodestro tools whose calls go to an MCP server). Only the first depends on the provider beyond
tool calling, and none bypasses tool approvals, sandbox rules or budgets.

## API for the studio

`GET /api/models/profile?model=<provider:model>` → `ModelProfile`: `capabilities` (supported / unsupported /
unknown), `limits` (context window, max output, efforts, thinking modes, thinking budget, source), `max_tokens_limit`,
`options` (thinking, thinking_display, thinking_budget, cache_control, cache_ttl, fallbacks: `available`, `reason`,
`values`) and `native_tools`. Free and local: no provider request is made. `GET /api/models/capabilities` stays for
older clients.

## Effort levels a model does not take

An agent's effort is mapped to a level the model documents (nearest lower, else the lowest; none for a model without
levels such as Claude Haiku 4.5) and the validation warns (`config.effort_adjusted`, `config.effort_dropped`), so
agents saved before the levels were documented keep running. A model without the reasoning capability still refuses
an effort, as before.
