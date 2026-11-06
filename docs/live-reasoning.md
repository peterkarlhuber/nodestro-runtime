# Live reasoning (thinking and reasoning summaries)

What a model transmits of its reasoning while it works is shown live in the studio: in workflow runs, the agent
test, the assistant, the Workflow Architect and the live run chat. This page describes the chain from the provider
to the interface and what each provider can deliver.

## Principles

- **Only what the provider sends.** Reasoning text is never generated, completed or paraphrased by Nodestro or
  Orkexa. A provider that keeps the reasoning to itself produces at most a *status* ("the model is thinking"), and
  the interface says that the text is not available, and why.
- **Enabling is not displaying.** Whether and how much a model reasons is the agent's effort (and Anthropic's
  `thinking` / Gemini's thinking budget). Whether reasoning text is *shown* is a preference of the person in the
  studio (globally and per conversation). Whether a provider is *asked* to send text where it sends it only on
  request is the workspace setting `reasoning_summaries` (OpenAI, Gemini) or the agent's `thinking_display`
  (Anthropic).
- **Thinking and summaries are told apart.** `thinking` is the model's reasoning text as generated (open-weight
  models); `summary` is a summary the provider writes of reasoning it does not expose (OpenAI, Gemini, Claude 4 and
  later).

## The chain

1. **Provider stream → Orkexa.** Each adapter translates its stream into two neutral events (Orkexa
   `orkexa.ReasoningStatus` and `orkexa.ReasoningDelta`, see Orkexa's *Streaming, hooks and errors* guide):
   `ReasoningStatus(state="started"|"finished", kind, redacted)` and `ReasoningDelta(text, kind)`. They arrive in the
   provider's order between text and tool call events.
2. **Orkexa → runtime.** :mod:`nodestro_runtime.reasoning` turns them into calls (`reasoning_hook`) and accumulates
   them (`ReasoningTrace`: `text`, `kind`, `phases`, `redacted`, `active`). The engine always installs the hook (for
   every provider; the former Anthropic-only `thinking_display` gate is gone).
3. **Runtime → records and events.**
   - Workflow runs: `NodeRun.thinking`, `thinking_kind`, `thinking_phases`, `thinking_redacted`; run events
     `thinking` (`delta`, `reasoning_kind`) and `thinking_status` (`state`, `reasoning_kind`, `redacted`).
   - Agent test: `AgentTestResult.reasoning` (a `ReasoningTrace`; the test answers at once, so it is shown with the
     result, not live).
   - Live run chat: `RunChat.live_answers(run_id)` (in-process, while an answer is written) and
     `RunMessage.reasoning` on the stored answer.
4. **API → studio (Server-Sent Events).**
   - Runs: `GET /api/runs/{id}/events` forwards the run events unchanged.
   - Assistant: `ReplyEvent` types `thinking` (text in `text`, `data.reasoning_kind`) and `thinking_status`
     (`data.state`, `data.reasoning_kind`, `data.redacted`); `ChatMessage.reasoning` holds the trace. The change and
     the event are published in one step, so a reconnecting subscriber never gets text twice.
   - Workflow Architect: job events `thinking` (`message` = text, `data.step`, `data.index`, `data.reasoning_kind`)
     and `thinking_status`; they are streamed but not kept in `ArchitectJob.events`. `ArchitectJob.reasoning` keeps one
     entry per request that reasoned (`step`: `analysis`, `plan`, `agent_revision`, `repair`; at most 60,000
     characters each).
   - Run chat: `GET /api/runs/{id}/chat/events` sends `answering` (`answers`: the live answers of this process, polled
     every 0.25 s while one is written); the view's `answering` field holds them for a new subscriber. An answer
     written by another API process shows as a pending question until it is stored.
   - `GET /api/models/capabilities?model=...` has `reasoning` (`ReasoningStreamInfo`: `text`, `status`,
     `request_option`, `requested`, `reason`) for every provider, so the interface can explain an empty stream.
5. **Studio.** One component (`components/ReasoningView.tsx`) shows the states everywhere: *thinking* (status only),
   *thinking text*, *reasoning summary*, *not transmitted* (redacted, or the provider sends no text) and *not exposed*
   (the model does not reason or the provider never sends reasoning), clearly apart from the answer. It is used by
   the assistant (`MessageView`), the run chat (`TeamLeaderChat`, live answers from `answering`), workflow runs and
   the agent test (`RunDetails`, from the `thinking*` fields of a step), and the architect (`ArchitectReasoning`).
   `lib/reasoningTrace.ts` applies the live events the way the server keeps them.
6. **Display preference.** `lib/thinkingDisplay.ts`: `live` (the text opens while it arrives), `collapsed` (default;
   the status shows, the text is one click away) or `status` (never the text). Kept in the browser
   (`localStorage`), globally (settings, *Reasoning display*) and per conversation (`conversation:<id>`,
   `run-chat:<run id>`); it never reaches the server and changes no workflow. The workspace setting
   `reasoning_summaries` (administrators) is a separate switch: it decides what OpenAI and Gemini are asked to send.

## What each provider transmits

| Provider / model | Text while streaming | Phase without text | Asking for the text |
| --- | --- | --- | --- |
| Anthropic (Claude 4 and later) | summary (summarized thinking) | yes, also `redacted_thinking` | agent option `thinking_display` (`summarized`; `omitted` sends blocks without text; the default depends on the model) |
| OpenAI (Responses API, reasoning models) | summary; the reasoning itself is never exposed | yes (reasoning items) | workspace setting `reasoning_summaries` → `reasoning.summary: "auto"` (OpenAI may require a verified organization) |
| OpenAI classic models (GPT-4o, GPT-4.1) | none: they do not reason | no | — |
| Gemini 2.5, 3, 4 | summary (thought summaries) | no (only `thoughts_token_count` afterwards) | workspace setting `reasoning_summaries` → `include_thoughts` |
| Gemini 2.0 and older | none | no | — |
| Amazon Bedrock, Claude | summary | yes, also `redactedContent` | thinking through the agent's request options |
| Amazon Bedrock, other reasoning models | thinking, when the model sends `reasoningContent` | yes | — |
| Ollama (thinking models) | thinking (`reasoning` deltas) | the first text marks it | none needed (the CPU saving mode may answer without thinking) |
| vLLM / OpenAI-compatible | thinking where the server separates it (`reasoning_content`, `reasoning`), else unknown | the first text marks it | server configuration (a reasoning parser) |

Summaries are display information: Orkexa does not send `reasoning.summary` or `include_thoughts` to a model that
does not reason, so the workspace setting cannot break requests to such models. Asking for summaries changes no
answer and no budget rule; reasoning tokens are billed by the provider whether or not text is transmitted.

## Tests

- Orkexa: `tests/test_reasoning_stream.py` (recorded stream shapes per provider through the real SDKs, interleaved
  with text and tool calls; redacted thinking; signatures; declarations).
- Runtime: `tests/test_reasoning.py` (run events in order, redacted phases, the OpenAI chain with the workspace
  setting, the agent test, live chat answers) and `tests/test_run_chat.py::test_an_answer_streams_its_reasoning_live_and_keeps_it`.
- API: `tests/test_reasoning_api.py` (assistant SSE order and snapshot consistency, architect steps, capabilities)
  and `tests/test_run_chat_api.py::test_an_answer_streams_its_reasoning_and_text_live`.
- Studio: `components/ReasoningView.test.tsx` (the five states, the display modes, the event reducers, the
  preference) and the browser suites `e2e/reasoning.mjs` (assistant, settings, themes, French, phone width),
  `e2e/runChat.mjs` and `e2e/architect.mjs`, whose servers stream Claude-shaped thinking slowly
  (`nodestro_runtime.testing.thinking` with `STREAM_DELAY`).
