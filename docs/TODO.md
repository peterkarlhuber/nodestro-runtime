# TODO: nodestro-runtime

> Open work for this repository: release blockers, known bugs, security follow-ups, technical debt and planned improvements.
> GitHub issues are the source of truth for details; this file is the entry point and summary.
>
> **Last full review:** 2026-11-01 against `master` at `a42b672`.

## Summary

| Priority | Count |
|---|---|
| P0 Critical | 0 |
| P1 High | 1 |
| P2 Medium | 2 |
| P3 Low | 19 |
| P4 Optional | 6 |

**Release blockers:** None for this repository.

The engine, both stores, the built-in Linux sandbox (#17/#23), Amazon Bedrock (#66), live run chat (#67/#68) and the
orchestration work up to M6b are merged; the backend audit fixes are in (#64/#65). What remains is mostly the
independent review that gates tool approvals, a few bounded bugs and gaps from the audit and the UX initiative,
platform coverage, and documentation that lags behind the code. No production deployment is approved.

## Priorities and statuses

- **P0 Critical**: data loss, security exposure, or broken core function; blocks any release.
- **P1 High**: blocks a planned release or a promised capability, or a significant risk.
- **P2 Medium**: real defect or gap with a workaround; schedule soon.
- **P3 Low**: minor defect, polish, or small debt.
- **P4 Optional**: improvement idea; not a defect.

Statuses: `Open`, `In progress` (PR linked), `Blocked` (names the blocker), `Needs decision` (owner decision pending),
`Needs external review`, `Accepted risk` (deliberately not fixed; reason stated).

## Release blockers and critical items

None known.

## Security

### RT-001 · Independent security review of tool approvals (S-2) and approval checkpoints (M6b)

- **Priority:** P1 High · **Status:** Needs external review · **Refs:** decision D10, S-2 pilots, M6b design
- **What:** Per-call tool approvals (`NODESTRO_TOOL_APPROVALS`) and resumable approval checkpoints
  (`NODESTRO_TOOL_APPROVAL_CHECKPOINTS`) are implemented and default off. They may only be switched on in an isolated
  pilot until an independent security review is done. The earlier pilot acceptances were made on frozen states and
  do not cover the later audit, MCP and orchestration changes.
- **Next step:** Run the independent review on a current master state (review brief exists), then re-accept the
  pilot state; activation outside a pilot is a separate owner decision.

### RT-002 · Prompt-injection hardening for retrieved content and tool results

- **Priority:** P3 Low · **Status:** Open · **Refs:** internal backend audit 2026-11-01, B-04
- **What:** Agent prompts do not yet mark knowledge content and tool results as untrusted data. This is a hardening
  step, not a known bypass; the tool gate and sandbox policy do not rely on it.
- **Next step:** Add a standard data-not-instructions framing in `agent_builder` and test it with the scripted client.

### RT-003 · Additional sandbox gateway hardening

- **Priority:** P3 Low · **Status:** Open · **Refs:** nodestro-local-docker `docs/SECURITY-REVIEW.md` K7, K9
- **What:** The gateway checks the daemon's limit enforcement before create and build, but does not re-check the
  effective limits of each created container. An optional enforced gVisor (`runsc`) runtime for tool containers is
  named as the next hardening step. Neither is a known bypass.
- **Next step:** Inspect each created container's limits and remove it on mismatch (`sandbox_gateway.py`); evaluate
  `runsc` support with a CI job that has it installed.

## Bugs and reliability

### RT-004 · Internal agents set a fixed reasoning effort

- **Priority:** P2 Medium · **Status:** Open · **Refs:** #69
- **What:** Orchestrator decisions (`effort="medium"`) and run chat answers (`effort="low"`) set an effort without
  checking the model. Orkexa refuses that on OpenAI-compatible endpoints and non-reasoning models, so an orchestrator
  or assistant model on `compatible`, `ollama` or `vllm` fails on every call.
- **Next step:** Use `providers.effort(model, ...)` in `engine._decide_sync` and `run_chat.answer_question`; add tests.

### RT-005 · Approval decision returns the run still as `paused` (X-01)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #70, backend audit X-01
- **What:** `RunManager.decide` returns the run before the scheduled resume, so the response says `paused`. The
  studio works around it.
- **Next step:** Return the new state (or a reliable field) and test it; API and SDK follow.

### RT-006 · Waiting tool approvals hold worker threads and slots

- **Priority:** P3 Low · **Status:** Blocked (M6b checkpoint activation decision, see RT-001) · **Refs:** S-2 pilot
  finding F2, backend audit C-08, CHANGELOG "Known limit" (M6b)
- **What:** Without checkpoints, a call that waits for a person keeps its agent thread in the run pool and a waiting
  slot. A worker whose waiting slot is full still claims runs, so a second asking run is refused with
  `tool.approval_capacity` while another worker is idle. Agents with a quality check always wait in the thread.
  Fails closed.
- **Next step:** Resolved by activating approval checkpoints; otherwise consider delaying claims of a worker with no
  free waiting slot (needs a separate go). Operators can raise `max_waiting` meanwhile.

## Architecture and modularity

### RT-007 · `engine.py` / `RunManager` keeps growing

- **Priority:** P3 Low · **Status:** Open (deliberately deferred) · **Refs:** backend audit D-13
- **What:** `engine.py` has 3,865 lines (3,381 at the audit start); approvals, persistence, resume and orchestration
  live in one class, which raises the regression risk of every change.
- **Next step:** Extract one clearly bounded part (tool approvals, persistence or resume) with the next functional
  change to it; no big-bang refactor.

## Performance

### RT-008 · PostgreSQL store throughput and serialized writes

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit C-14 (partly fixed), #65
- **What:** With fresh statistics the pooled store is about 12 % below the pre-audit single-connection throughput,
  and writes of one process still run one after another. Not measured on server hardware under load.
- **Next step:** Load test on target hardware (see RT-020); then decide on parallel writes or a pool-free read path
  for single-process use.

### RT-009 · Usage entries are filtered and sorted in Python

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit C-05 (partly fixed)
- **What:** `UsageLog.entries(since=...)` loads the whole collection and filters in Python; `/api/usage` takes about
  0.8 s (SQLite) / 1.4 s (PostgreSQL) at 5,000 runs and grows linearly.
- **Next step:** A store-level time filter (or SQL aggregation) for usage entries and run costs; the API uses it.

## Features and enhancements

### RT-010 · Rotation of the server secret key

- **Priority:** P2 Medium · **Status:** Open · **Refs:** `server_settings.py` module docstring, README
- **What:** Changing `NODESTRO_SECRET_KEY` makes every stored tenant secret unreadable; there is no way to re-encrypt
  with a new key, for example after a suspected key exposure.
- **Next step:** Let `SecretBox` accept an old key for decryption during a transition and add a re-encrypt
  function; the operator command lives in nodestro-api.
- **Depends on:** nodestro-api (operator CLI)

### RT-011 · Machine-readable catalog of error codes (X-03)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #71, backend audit X-03
- **What:** No list of the codes the runtime raises; new codes reach the UI without translations.
- **Next step:** A code registry plus a test that fails on unregistered codes; export it for the API.

### RT-012 · Run-level cause of a failed run (AD-02)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #72, UX audit AD-02
- **What:** A failed run only says `run.nodes_failed` with node names; the cause lives on the node.
- **Next step:** Add the first failing node's code and params to `Run`; API, SDK and studio follow.

### RT-013 · Gaps in unattended tool-call warnings

- **Priority:** P4 Optional · **Status:** Open · **Refs:** S-2 R1 limits (runtime #53)
- **What:** The preflight cannot see which teams an orchestrator will pick at run time, published APIs have no
  approval phase of their own (always warned), and batch pre-grants need a started batch id.
- **Next step:** Revisit after RT-001; warn per pool member for orchestrators if useful.

### RT-014 · Configurable retention (C-12)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #73, backend audit C-12
- **What:** Runs, usage entries, workflow versions and local backup files (`before-restore-*`, `pre-migration-*`)
  grow without limit.
- **Next step:** Retention setting per collection (default unchanged) with deletion in runtime, called by the API.

### RT-015 · Region in Bedrock usage entries

- **Priority:** P4 Optional · **Status:** Open · **Refs:** Bedrock final report section 7
- **What:** `UsageEntry` has no region, so usage views cannot show Bedrock costs by region.
- **Next step:** Add an optional region field when a Bedrock request is recorded.

### RT-016 · Deferred scope decisions

- **Priority:** P4 Optional · **Status:** Needs decision · **Refs:** AO M6b, D10, MCP closing decision
- **What:** Deliberately not implemented: orchestrators creating teams or agents from components (AO level 3),
  approval deadlines longer than 15 minutes (D10d), and MCP beyond the read-only server pilot (desktop MCP, write
  tools, unattended calls, stdio servers that are not Python packages).
- **Next step:** Owner decision per topic; none is planned now.

## Tests and quality assurance

### RT-017 · Fixes that still need an independent or Docker-backed check

- **Priority:** P3 Low · **Status:** Needs external review · **Refs:** backend audit X-02, A-07, B-01, B-03, C-11
- **What:** X-02 (orchestrator `started_at` kept on resume, team outcomes referencing the child run) is implemented
  but not independently re-checked; A-07 (shared environment images) was tested only with a stub; container paths
  of B-01/B-03 and the built-in sandbox cancel path in the full product were not run locally; the default reaper
  wait of C-11 was not waited out.
- **Next step:** Re-verify on a machine with Docker and add tests where the CI `docker` job can run them.

## Platform coverage

### RT-018 · Built-in Linux sandbox beyond the validated systems

- **Priority:** P3 Low · **Status:** Open · **Refs:** #17, #23, desktop systems workflow
- **What:** The built-in sandbox (Landlock + seccomp) is validated on Ubuntu 24.04 and Debian 13, x86_64, including
  the packaged desktop in VMs. Other distributions, kernels with older Landlock ABIs, and aarch64 are untested; the
  sandbox then reports itself unavailable and untrusted tools need Docker.
- **Next step:** Add further systems to the probe matrix when they are to be supported.

### RT-019 · No isolating sandbox without Docker on macOS and Windows

- **Priority:** P3 Low · **Status:** Open · **Refs:** `sandbox_policy.py`
- **What:** The built-in sandbox is Linux-only. On macOS and Windows untrusted (AI-generated) tools need Docker; the
  runtime itself is not tested on these systems at all.
- **Next step:** Test the runtime on macOS and Windows; decide whether a native isolation is wanted there.

### RT-020 · Real providers and PostgreSQL at scale

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit sections 3 and 16, Bedrock and LRC-1 reports
- **What:** Tests use scripted models only: no real provider calls (including a live AWS Bedrock smoke test and live
  run chat with real models), no load against PostgreSQL on server hardware, no multi-process test of every race fix.
- **Next step:** Run the prepared Bedrock smoke test and a PostgreSQL load test once credentials and hardware are
  approved.

## Documentation

### RT-021 · README lags behind the code

- **Priority:** P3 Low · **Status:** Open · **Refs:** #74
- **What:** The sandbox policy text still names only Docker, the PostgreSQL text says "owns one connection", and the
  contents table and feature sections miss orchestration, live run chat, Bedrock, jobs, tool approvals and MCP modules.
- **Next step:** Update those README sections (keep the quickstart block that `tests/test_readme.py` runs).

## Technical debt

### RT-022 · Python 3.10 support after its end of life

- **Priority:** P3 Low · **Status:** Needs decision · **Refs:** `pyproject.toml`, CI matrix
- **What:** Python 3.10 reached end of life in October 2026, but the package and CI still target it, and MCP already
  needs 3.11 (`mcp.unavailable` on 3.10).
- **Next step:** Decide on 3.11 as the minimum together with the dependent repositories, then update the
  classifiers, ruff/mypy targets and CI matrix.

### RT-023 · Reproducible Python dependencies

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-05, `.github/dependabot.yml`
- **What:** Dependencies are ranges without a lock or constraints file, so image rebuilds resolve differently over
  time; Dependabot covers only GitHub Actions because the git-URL dependencies are private.
- **Next step:** A hashed constraints file for CI and images, or a scheduled `pip-audit` plus upgrade check.

### RT-024 · German text in prompts and default names

- **Priority:** P4 Optional · **Status:** Needs decision · **Refs:** runtime quality round PR #3
- **What:** `engine.py` keeps German prompt fragments ("# Eingaben", "Bitte beginne.") and default node names
  ("Ausgabe", "Entscheidung", "Freigabe"); `schemas.py` "Neuer Workflow". They are kept on purpose for prompt
  stability, but conflict with the English-only rule.
- **Next step:** Decide; a change needs coordinated updates of API tests and the OpenAPI snapshot.

## Later optimizations

None known.

## Accepted risks

### RT-025 · Reserved tool name checked against the function name (MCP L-1)

- **Priority:** P3 Low · **Status:** Accepted risk · **Refs:** #62
- **What:** A tool can be stored under a reserved name; it is never run (lookups and calls refuse it). Accepted by the
  owner as a low residual risk when the MCP project closed.
- **Next step:** Check the final stored name too, when the backlog is picked up.

### RT-026 · PostgreSQL token search test misses encoded forms (MCP L-2)

- **Priority:** P3 Low · **Status:** Accepted risk · **Refs:** #63
- **What:** Test coverage gap only (bytea columns, encoded token forms, silent skip); no secret was found. Accepted as
  low residual risk.
- **Next step:** Extend `tests/test_mcp_secret_search.py` as described in the issue.

### RT-027 · Limits of local process execution

- **Priority:** P4 Optional · **Status:** Accepted risk · **Refs:** README "Tools" and "Security", Bedrock report
- **What:** The process sandbox is not a security boundary: trusted and user-authored tools in it can read the user's
  files (including local cloud credentials). A source build that a person explicitly asks for in a library plan runs
  with the process's rights. Both are documented; untrusted code never runs there.
- **Next step:** Re-evaluate if the process sandbox is ever allowed for untrusted code.

### RT-028 · Unpriced requests in standard price mode

- **Priority:** P4 Optional · **Status:** Accepted risk · **Refs:** CHANGELOG (AO M2, sub-workflow budgets)
- **What:** In standard price mode a request without a price is still sent; its cost stays unknown (never zero) and is
  not counted against a budget, so local models stay usable. Strict price mode refuses it.
- **Next step:** None planned.

## Cross-repository dependencies

| This repo item | Other repo | Other item | Relation |
|---|---|---|---|
| — | orkexa | ORK-001 (orkexa #22), B-03: cancel `SubprocessSandbox`/`DockerSandbox` calls | Orkexa owns the change; then runtime passes the cancel signal through `sandbox_cancel` |
| — | orkexa | ORK-002 (orkexa #24): `ToolRegistry.refresh()` | Afterwards the refreshing `ToolLibrary.registry` workaround from #57 can be dropped |
| — | orkexa | A-05: redirects when `orkexa.Agent` is used directly | Orkexa owns it; runtime's server path already refuses redirects |
| RT-005 | nodestro-api, nodestro-sdk | decision response model | Follow the new run state |
| RT-010 | nodestro-api | operator command for key rotation | Runtime provides `SecretBox` support |
| RT-011 | nodestro-api, nodestro-studio | X-03 combined code list, translations | Runtime provides its catalog |
| RT-012 | nodestro-api, nodestro-studio | AD-02 run cause in lists and banners | Runtime provides the field |
| RT-014 | nodestro-api | leader cleanup task | API calls runtime retention |
| RT-001 | nodestro-studio, nodestro-server | S-2 pilot and review | Review covers the whole approval flow |

## Maintaining this file

- Add an entry when work is identified but not done in the current PR. Use the next free ID; IDs are never reused.
- Link a GitHub issue for anything concrete enough to be picked up; keep details in the issue, not here.
- When an item is finished, delete its entry in the PR that completes it and mention the ID in the PR description.
  Git history is the record; do not keep ticked checklists.
- Accepted risks stay listed with their reason until the risk is removed or re-evaluated.
- Re-review the whole file before each release and update the "Last full review" line.
- Security issues that are not yet fixed are described only in general terms here. Report details privately
  (see SECURITY.md or GitHub private vulnerability reporting), never in this file or in public issues.
