# Web search and page reading (NAL-A2)

Nodestro's built-in tools `search_web` and `fetch_web_page` (`src/nodestro_runtime/web_tools.py`) give an agent the
web without a provider-specific agent. The network parts live in Orkexa (`orkexa.net.guard`, `orkexa.net.http`,
`orkexa.web`; Orkexa ADR 0017).

## Tools

| Tool | Needs | Spec | Binding parameters |
|---|---|---|---|
| `search_web(query, kind, max_results, region, language, time_range, page)` | a `search` connection | read + external, never offline, 20 s, 10 calls per run, cost unit `search_request` | `max_results`, `region`, `language`, `time_range` (defaults), `allow_hosts`, `deny_hosts` (filter hits), `max_calls` |
| `fetch_web_page(url, max_chars)` | nothing | read + external, never offline, 30 s, 20 calls per run | `allow_hosts`, `deny_hosts`, `max_chars` (default 12 000), `max_calls` |

Both return their result marked as external content (`[External web content <nonce> ...]` up to the line
`[End of external web content <nonce>]`, a new random nonce per call) and record what they saw in the run's source
ledger (`Run.sources`, ids `S1`, `S2` ...; see `source_ledger.py`). A structured result field with
`source="ledger"` must cite those ids or addresses (`output.unknown_source` otherwise).

## Providers (connection `search`, `provider`)

| Provider | Key | Price booked | Modes | Notes |
|---|---|---|---|---|
| `test` | yes (any) | 0 | all | Deterministic, no network: the only provider of development and automated tests |
| `ddgs` | no | 0 | desktop, Local Docker | Free metasearch without guarantees; may be blocked; never a default, never on a server; needs the extra `web-ddgs` |
| `brave` | yes | 0.005 USD per request (documented), or the connection's price | all | Results must not be stored (terms): the ledger keeps addresses only, per run |
| `tavily` | yes | the connection's `price_per_request_usd` (required) | all | Billed in plan credits |
| `mojeek` | yes | the connection's `price_per_request_usd` (required) | all | Operated in Europe |
| `searxng` | optional bearer token | 0 | all | A self-hosted instance at `endpoint`; private addresses allowed for that host only, never on a server |

No provider is chosen automatically (owner decision pending, E-3). A paid provider without a known price is not
called (`search.price_unknown`). The connection check never sends a real search (RT-076).

## Network guard

`fetch_web_page` and the HTTP adapters use `orkexa.net.http.guarded_client`: URL rules (scheme, standard ports, no
user info, no numeric host forms, metadata names), every resolved address checked and the connection made to the
checked address (DNS rebinding), redirects followed by hand with each hop checked, no environment proxies, no
compressed answers, a byte limit counted while reading and one deadline for the whole exchange. Errors reach the model
as codes (`web.address_refused, loopback`, `fetch.unsupported_type` ...), never with the service's own text.

## Modes, budget and checks

- Strict offline mode: the workflow check refuses both tools (`check.offline_tool`).
- Server profile: not offered (`check.tool_mode_unsupported`; every call refused with `tool.mode_unsupported`) unless
  the operator sets `NODESTRO_WEB_TOOLS_SERVER=1`, which waits for the independent review D1 (RT-078).
- Budget: a paid search checks the run budget first and books `units x price` as a token-free usage record
  (`web_search_requests`).
- Pages read in one step are kept in memory for that step only (`RunServices.cache`); nothing is stored across runs.
- Call limits: per step (the spec's `max_calls_per_run`, lowered by the binding's `max_calls`) and per run across
  all steps and loops (`RUN_CALL_FACTOR` = 5 times the spec's limit).
- Host lists (`allow_hosts`, `deny_hosts`): `example.com` matches that name only, `*.example.com` every name below
  it; in a deny list it also matches example.com itself. Search filters and the fetch guard use the same rule.
- A host that builds agents sets `AgentBuilder.mode`; without it the builder assumes `server`.
- HTML is read in a separate, time- and memory-limited process within the rest of the request's deadline (Orkexa).

Not included: image and video search, crawling, JavaScript rendering, sign-in on web pages, OpenAI and Gemini native
web search (RT-065).
