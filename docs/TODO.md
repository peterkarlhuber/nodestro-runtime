# TODO: nodestro-runtime

> Open work for this repository: release blockers, known bugs, security follow-ups, technical debt and planned improvements.
> GitHub issues are the source of truth for details; this file is the entry point and summary.
>
> **Last full review:** 2026-11-06 against master at 802fa8f (TODO abatement round)

## Summary

| Priority | Count |
|---|---|
| P0 Critical | 0 |
| P1 High | 1 |
| P2 Medium | 7 |
| P3 Low | 23 |
| P4 Optional | 14 |

**Release blockers:** None. No entry blocks the frozen desktop candidate v0.2.0-preview.1 or the next preview;
every entry below is not a release blocker. RT-001 (P1) gates only switching tool approvals on, which stay off
(`NODESTRO_TOOL_APPROVALS`).

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

Items that need an independent or external review (an internal audit does not replace it):

- RT-001: tool approvals and approval checkpoints; `NODESTRO_TOOL_APPROVALS` stays off until it is done.
- RT-030: the Axentra migration of the server profile (condition SR-01 is met; the external sign-off is outstanding).
- RT-017: an independent or Docker-backed re-check of some earlier audit fixes (listed under Tests).
- Owned elsewhere: the portable export (nodestro-api #85 with the hardening of #87, nodestro-studio #121) is tracked
  in nodestro-api; live tests of the Axentra provider adapters are tracked in axentra.

### RT-001 · Independent security review of tool approvals (S-2) and approval checkpoints (M6b)

- **Priority:** P1 High · **Status:** Needs external review · **Refs:** decision D10, S-2 pilots, M6b design
- **What:** Per-call tool approvals (`NODESTRO_TOOL_APPROVALS`) and resumable approval checkpoints
  (`NODESTRO_TOOL_APPROVAL_CHECKPOINTS`) are implemented and default off. They may only be switched on in an isolated
  pilot until an independent security review is done. The earlier pilot acceptances were made on frozen states and
  do not cover the later audit, MCP and orchestration changes.
- **Next step:** Run the independent review on a current master state (review brief exists), then re-accept the
  pilot state; activation outside a pilot is a separate owner decision.

### RT-003 · Optional gVisor runtime for tool containers

- **Priority:** P3 Low · **Status:** Open · **Refs:** nodestro-local-docker `docs/SECURITY-REVIEW.md` K7, K9
- **What:** The gateway checks the daemon's limit enforcement before create and build, and since the TODO abatement
  round reads every created container back and removes it when its effective limits, isolation, mounts, user or image
  differ from what was asked. An optional enforced gVisor (`runsc`) runtime for tool containers is named as the next
  hardening step. It is not a known bypass.
- **Next step:** Evaluate `runsc` support with a CI job that has it installed.

### RT-030 · Axentra migration follow-ups

- **Priority:** P2 Medium · **Status:** Needs external review · **Refs:** `docs/AXENTRA.md`, `axentra_platform.py`,
  nodestro-server SRV-019
- **What:** Every Axentra integration of the server profile is merged and off by default; none is active in
  production. Open:
  - an organizationally independent security review: the separate agent's review was accepted on 2026-11-02 (condition
    SR-01 met, its Lows fixed and re-reviewed, the round-3 gaps of the static tenant SQL check fixed in #89, a suspended
    tenant's tool generation jobs stopped (NF-1, nodestro-api #69), the remaining isolation risk without RLS stated in
    the doc); round 4 accepted too, its Lows R4-3 to R4-7 handled (no installation starts for a stopped tool job;
    the tenant SQL check's misses fixed or listed in the remaining isolation risk); an external sign-off is still
    outstanding;
  - a staging rehearsal on a production copy under the owner/runtime roles: `migrate --backup-dir`,
    `backfill-identity`, `reconcile-identity`, `secrets reencrypt`, `axentra reconcile`, `files migrate/verify`
    against a real S3 bucket (so far only moto's S3 server), then a restore of the backup (nodestro-server SRV-019);
  - production activation decisions per switch, after the review and the rehearsal;
  - API keys when publication comes to the server (boundary decided in the doc, "API keys (M6)");
  - limit enforcement in the budget admission with reservations (design in the doc, "Usage"; quotas are
    informational until then) and quota defaults per plan;
  - a Studio view of the inbox (`/api/notifications` exists) and process counters of the worker (it has no metrics
    listener; its side shows only in the database-derived metrics).
- **Rejected:** moving the usage dashboard to Axentra's meters (they lack the AI detail; Nodestro's records stay the
  source); e-mail notifications by default (inbox only; an e-mail channel would be its own explicit switch); shadow
  modes and database mirror triggers from the migration plan (the identity mirror writes both sides in one
  transaction, so there is nothing to compare or trigger); moving knowledge files (already in PostgreSQL with their
  search index); running jobs on Axentra (M11, variant A), Nodestro's migrations on Axentra's runner (M9) and
  row-level security on `nodestro_*` tables (M10): the reasons and measurements are in the doc.
- **Next step:** The external security sign-off, then the staging rehearsal. No switch is activated in production by
  this work.

### RT-032 · Source builds of Python packages only in a container: pin the resolver image

- **Priority:** P2 Medium · **Status:** Open · **Refs:** Docker-first plan R4, `container_images.UV_IMAGE`,
  `ContainerImages.compile_in_container`
- **What:** uv never builds a package on the host any more (`--no-build` for compile and sync). On the desktop, a
  plan a person asked for that hits `deps.build_required` is resolved again in a locked-down container (network only for that step, no
  bind mounts, proxy variables only and without credentials, no capabilities, `nobody`, read-only with a tmpfs,
  limits, owner and deadline labels, gated by preflight and self-test, input on stdin, lock on stdout; see RT-038). `UV_IMAGE` is pinned to the multi-arch index digest of `ghcr.io/astral-sh/uv:0.9.5-python3.13-bookworm-slim`
  (linux/amd64 and linux/arm64, checked 2026-11-02); an unpinned or placeholder image is refused. Without Docker such
  packages report `deps.build_required` (reason `sandbox.unavailable`). It cannot run through the sandbox gateway
  (which refuses containers with network and foreign images); outside the desktop source builds are off
  (`deps.source_build_disabled`, RT-038).
- **Next step:** The real-Docker test of the resolve container exists (`tests/test_resolve_real_engine.py`, CI
  `docker` job) and passes there, approved path included (master `53d7980`). The gateway offers a resolve container
  only together with a proven filtered build network (RT-040).

### RT-038 · Containers that run package code: hardened shape, residual network risk of the resolver

- **Priority:** P2 Medium · **Status:** Open · **Refs:** security review DS-1, DS-5, DS-7, DS-9 (2026-11-02),
  `ContainerImages.missing_modules`, `ContainerImages.compile_in_container`, `container_images.bounded_run`,
  `container_images.default_base`
- **What (done 2026-11-02):** The module check after an image build (`missing_modules`) runs package code (`.pth`
  files run at interpreter start, even with `-I`), so it now runs exactly like a tool call: Orkexa's `run_args`
  shape with the installation's owner and deadline labels, the image by its id, only after preflight and self-test
  (`sandbox.unavailable` otherwise), bounded output, killed and removed after 120 s or on cancel (`deps.image_failed`).
  The resolve container for source builds (`compile_in_container`) pulls uv's image by digest, checks the digest, runs
  it by id with `--pull never` after the same preflight and self-test, with the owner and deadline labels (the reaper
  removes orphans), private IPC, ulimits (nofile, fsize 1 GiB, core 0), no logging, bounded output and stop/removal on
  timeout or cancel; proxy URLs reach it without user name or password (`strip_credentials`). Every docker CLI call of
  `ContainerImages` reads at most 16 MiB of stdout and the last 256 KiB of stderr (`bounded_run`; more kills the CLI).
  A Python version without a pinned base image is refused (`deps.python_unsupported`) instead of building on the
  unpinned `python:<v>-slim` tag.
- **Source builds are desktop only (done 2026-11-02, owner decision):** Outside the desktop profile (a server
  tenant, Nodestro Local Docker) source builds are off by default: `PythonEnvironments.source_builds` and
  `ContainerImages.source_builds` are `False` unless the workspace runs the desktop profile, and a plan a person
  accepted reports `deps.source_build_disabled` (parameter `name`) before any docker call (no pull, no self-test, no
  container). Wheels-only plans and image builds (`--only-binary=:all:`, hashed downloads) are unchanged. There is no
  operator switch that turns source builds on again, and `NODESTRO_DOCKER_BUILD_NETWORK` does not either: a named
  network is not proof that it is filtered. Source builds come back outside the desktop only with a build network
  the runtime proves filtered (RT-040). The sandbox gateway never offered a resolve container (it refuses network,
  pulls and other images), so Local Docker could not build sources before either; the refusal now says why.
- **Proven shape (done 2026-11-02, review R-5):** The resolve and module-check containers are no longer written
  by hand. Both are built from Orkexa's `run_args` and checked against it right before they start
  (`container_images.check_shape`, `sandbox.shape_mismatch` otherwise, nothing started): the module check must be
  exactly the self-tested shape; the resolve container may differ only in `RESOLVE_ALLOWANCES` (`--network none`
  replaced by Docker's default or one named network, never `host` or another namespace mode; `noexec` of `/tmp`
  replaced by `exec`; `--env` for proxy variables without credentials). The resolve container thereby gained the
  noexec `/work` and the sealed `/run` and `/var/tmp` of the proven shape, and its sandbox runs Orkexa's self-test with exactly its
  own limits (`_RESOLVE_LIMITS`: memory, CPU, processes, open files, file size, tmpfs and `/dev/shm` sizes), so those
  values are proven in the resolver image too. What the self-test cannot prove is the allowed differences themselves:
  the network (by design) and the executable `/tmp`. An in-container probe of the resolve shape was not added: it
  would need its own image-specific checks and would prove only what the allowlist already states.
- **Residual risk (desktop):** The resolve container still has network (Docker's default bridge, or
  `NODESTRO_DOCKER_BUILD_NETWORK`) while untrusted build scripts run in it: they can reach the package index, the
  internet, hosts on the LAN, ports the host publishes on the bridge and, on cloud VMs, the instance metadata service.
  A pre-download on the host with `--network none` for the build is not feasible as is: the dependencies of a package
  that has only a source distribution are known only after its build script ran, so resolving and building cannot be
  separated in one step. A proxy that needs credentials no longer works for the resolver (credentials are not passed
  into code that can read them). `/tmp` in the resolve container is meant to be executable (compilers and build tools
  run there; see the real-engine findings below). The resolver is used only for plans a person
  explicitly accepted (library dialog), never for generated tools.
- **Real-engine test (added 2026-11-02; passes in the CI `docker` job, 13 tests on master `53d7980`):**
  `tests/test_resolve_real_engine.py` runs `compile_in_container` against a real daemon (`NODESTRO_DOCKER_TESTS=1`,
  a separate step of the CI `docker` job). On any daemon it proves: the self-test runs first, in the pinned image by
  its id and with exactly `_RESOLVE_LIMITS`; a resolve shape with an extra flag and a sandbox that proved other
  limits are refused with no resolve container created (checked with `docker events`): by the self-test on an unproven daemon, by the shape
  check (`sandbox.shape_mismatch`) on a proven one; a moved local tag of the
  resolver's name is never tested or run; off the desktop an accepted source build reports
  `deps.source_build_disabled` with no docker CLI call, container or pull; the desktop plan falls back to
  `deps.build_required` (reason `sandbox.unavailable`) on an unproven daemon; nothing of the module's owner, no
  resolve container and no volume is left. On a daemon that proves the resolve sandbox it also proves: a pure-Python
  wheel (`six==1.17.0`) resolves to its hashed lock in the real container; the running container's limits from
  `docker container inspect` and from inside (cgroup v2 memory, swap, pids and CPU; ulimits; uid/gid; capabilities;
  no-new-privileges; seccomp; read-only root; tmpfs options and sizes; sealed `/run` and `/var/tmp`; no mount, no
  volume, no host variable; Docker's default network) are the proven shape up to `RESOLVE_ALLOWANCES`; a timed-out
  and a cancelled resolve are stopped and their containers removed; proxy credentials appear nowhere (run command,
  inspect output, the container's environment, its output, the installation log), and only credential-free proxy
  variables reach it. Locally (cgroup v1) the approved-path tests skip with the self-test's refusal.
- **Real-engine findings (2026-11-02):**
  - Fixed in Orkexa #38 (pinned `0861c1b`): the resolve self-test refused every daemon, cgroup v2 included. The pinned
    `UV_IMAGE` sets no `LANG`, so Python's C-locale coercion (PEP 538) put `LC_CTYPE=C.UTF-8` into the self-test's
    `os.environ`, and `evaluate_self_test` refused it ("unexpected environment variables: LC_CTYPE"). Orkexa's
    self-test now reports `/proc/self/environ`, the environment the container was started with; its allowlist is
    unchanged. Before that pin, desktop source builds ended with `deps.build_required` (reason
    `sandbox.unavailable`), fail-closed. The environment images on `python:<v>-slim` set `LANG` and were not affected.
  - Fixed: `/tmp` of the resolve container was not executable. Docker mounts a `--tmpfs` `noexec` unless its options
    say `exec`, so only dropping `noexec` had no effect. The `exec_tmp` allowance now replaces `noexec` with `exec`
    (`_resolve_args` and `check_shape`), which is what R-5 accepted ("an executable `/tmp`"); `/work` stays
    `noexec`.
  - Fixed: the resolve container's proven shape is built from the sandbox's own limits (what its self-test ran), not
    from the constant, so a sandbox that proved other limits is refused (`sandbox.shape_mismatch`).
- **Next step:** RT-040 (a proven filtered build network, or iterative resolving without network) stays open and is
  not approved. A real-Docker test of `missing_modules` is still missing.

### RT-040 · A filtered build network, so servers can build Python packages from source again

- **Priority:** P3 Low · **Status:** Open · **Refs:** RT-038, [docs/build-network.md](build-network.md),
  `ContainerImages.compile_in_container`
- **What:** Source builds outside the desktop are refused (`deps.source_build_disabled`) because the resolve
  container's network is not filtered. They may come back only with a build network that reaches nothing but the
  package sources (or a package proxy) over IPv4 and IPv6, with no LAN, host or metadata access, and that the runtime
  proves on its daemon before each first use (a probe in the build shape, like the sandbox self-test). An operator
  switch is allowed only together with that proof; one that re-enables the unfiltered network is not.
- **Next step:** Choose between an internal Docker network with an allowlisting egress proxy and iterative resolving
  without network (both analyzed in docs/build-network.md), with an owner decision and a security review before any
  implementation.

### RT-033 · Docker-first sandbox status: follow-ups

- **Priority:** P2 Medium · **Status:** Open · **Refs:** Docker-first plan R2, `docker_status.py`,
  [docs/docker-sandbox.md](docker-sandbox.md)
- **What:** The Docker backend is `approved` only after Orkexa's preflight (TTL 5 s) and the in-container self-test in
  Nodestro's runtime image (once per daemon fingerprint) passed, checked in a background thread: the settings never
  wait for `docker info`; until the first check finished the state is `checking`. A container sandbox counts as
  isolating (`isolation_of`) only for a proven daemon; untrusted code is refused otherwise, and every call still
  checks itself (Orkexa). New schema fields `engine`, `rootless`, `cgroup_version`, `checks`, `tested_at` and the
  reasons `docker-*`. Open: the first self-test needs the runtime image, which is built on demand (network, uv), so the
  first start with Docker stays `checking` until that build finished (or reports `docker-image`); a call made while
  `checking` is refused instead of waiting; the background check was tested with stand-ins only (no daemon in the
  development container). The API endpoints (nodestro-api `sandbox_status.py`), the SDK model (`SandboxStatus`) and
  the Studio texts exist; remaining Studio work is nodestro-studio ST-033.
- **Next step:** A real-daemon test of the background check in the CI `docker` job; decide whether a tool call should
  wait (bounded) for a running first check.

### RT-036 · Docker-first for untrusted code and its libraries (DS-2, DS-3): follow-ups

- **Priority:** P2 Medium · **Status:** Open · **Refs:** security review DS-2, DS-3; product decision 2026-11-02
  (Docker-first); `sandbox_selection.py`, `tool_library.ToolLibrary._require`, `python_envs.PythonEnvironments.backend`
- **What:** Done 2026-11-02: the desktop's `auto` mode runs untrusted code only in Docker (refused with Docker's reason
  otherwise, never the built-in Linux sandbox automatically); the new `linux` mode is the only one that uses the
  built-in Linux sandbox, always with Nodestro's own interpreter; `subprocess` never runs untrusted code (reason
  `subprocess-mode`). Untrusted code's libraries are never a host venv (no `uv pip sync`, no host `verify_imports`):
  container images in `auto`/`docker`, refused in `subprocess`/`linux` (`sandbox.libraries_need_isolation`, param
  `mode`). The `linux` choice is in the SDK and the Studio settings, and the Studio has texts for the reason
  `subprocess-mode` and for the `auto` refusal with Docker's reason. Open: Studio wording for the status `unsupported`
  with `sandbox.isolation_required` / `sandbox.libraries_need_isolation` (not verified; nodestro-studio ST-033);
  installations that chose `subprocess` to use the
  built-in sandbox must choose `linux` (no automatic migration); host venvs that earlier releases built for untrusted
  scopes stay on disk unused until `cleanup`/removal; local MCP servers still need Docker mode (`containers()`), also
  in `auto` with Docker approved; a scope's trust for an unsaved tool comes from the code digest the caller passes
  (`Needed.digests`), so an API plan without code (reinstall) falls back to the stored tools (unknown counts as
  untrusted).
- **Next step:** Check the remaining Studio wording with ST-033; decide whether `auto` should also allow
  local MCP servers once Docker is approved; optionally run the built-in sandbox with an environment's packages by
  starting the interpreter with `-S` and adding site-packages after confinement.

### RT-034 · Container reaper: follow-ups

- **Priority:** P3 Low · **Status:** Open · **Refs:** Docker-first plan R2, `docker_status.DockerMonitor.start_reaper`,
  `docker_status.installation_owner`
- **What:** Tool containers carry `org.orkexa.sandbox=<owner>` and a deadline label. The owner of a desktop or
  Nodestro Local Docker is its own (`nodestro-<12 hex digits of SHA-256 of the resolved data directory and the
  installation id>`, done 2026-11-02): such an installation removes all of its own containers at the start
  (`reap(owner, remove_all=True)`), then the expired ones every 60 s, and never touches another installation's
  containers on the same daemon. Local MCP servers use `nodestro-mcp-<id>`, which the tool reaper never touches; they
  are reaped separately (RT-037). A server's API and worker processes share the owner `nodestro` and reap only expired
  containers. Open: through the sandbox gateway containers cannot be listed, so there the gateway's own reconcile does
  the work; containers an earlier release left with the owner `nodestro` on a desktop are no longer removed at the
  start (they have a deadline; a server on the same daemon removes them once expired, otherwise `docker rm`); a copy of
  a data directory (with its `python/installation-id`) at the same path on the same daemon, e.g. a second Nodestro
  Local Docker container on a copied volume, shares the owner.
- **Next step:** A real-daemon test of the reaper (two owners on one daemon) in the CI `docker` job.

### RT-037 · Local MCP containers and the sandbox gateway: residual risks (DS-4, DS-11)

- **Priority:** P3 Low · **Status:** Open · **Refs:** security review DS-4, DS-11; `mcp.stdio.prove`,
  `docker_status.DockerMonitor.reap_mcp_once`, `sandbox_gateway.Gateway.require_limits`
- **What:** Done 2026-11-02: every local MCP server container start needs Orkexa's preflight and the self-test of its
  image in the container's exact shape, and runs the image by the checked id; this installation's MCP containers are
  reaped at the start (all started before the reaper, safe because the process holds the data directory lock) and
  once expired (every 60 s). The gateway drops request environment variables, checks a tagged image's lock label and
  creates it by id, and confirms the daemon's limits and seccomp profile again after 5 s. Open: a server's processes
  share `nodestro-mcp` and reap only expired MCP containers (deadline 15 minutes: per-operation connections of at most 2 × 300 s); through the sandbox gateway containers
  cannot be listed, so its own reconcile (`max_lifetime`, 600 s by default) also ends MCP connections older than that;
  a daemon reconfigured between the preflight and a container start is noticed only at the next start; the gateway
  still inspects and removes `nodestro-env:*` tags without the lock-label check.
- **Next step:** Real-daemon test of the MCP proof and reaper in the CI `docker` job; decide whether the gateway
  should exempt MCP containers from `max_lifetime` or give them their own limit.

## Bugs and reliability

### RT-031 · Environment image caps and collection: follow-ups

- **Priority:** P3 Low · **Status:** Open · **Refs:** Docker-first plan R3, `container_images.py`, `image_limits.py`
- **What:** A built environment image above `NODESTRO_MAX_IMAGE_GB` (default 8) is removed and refused
  (`deps.image_too_large`), and every build collects the least recently used `nodestro-env` images (with the lock
  label) beyond 20 images or 20 GiB, never one an environment of the store names. Open: a server (shared daemon,
  `remove_images=False`) does not collect, so its operator still prunes; through the sandbox gateway images cannot be
  listed, so collection there sees only images in the data directory's usage record (`python/image-usage.json`);
  the size cap and collection were tested only with the fake docker CLI. (The Studio text for `deps.image_too_large`
  exists in all six languages.)
- **Next step:** A real-Docker test of the size cap and collection in the CI `docker` job.

### RT-006 · Waiting tool approvals hold worker threads and slots

- **Priority:** P3 Low · **Status:** Blocked (M6b checkpoint activation decision, see RT-001) · **Refs:** S-2 pilot
  finding F2, backend audit C-08, CHANGELOG "Known limit" (M6b)
- **What:** Without checkpoints, a call that waits for a person keeps its agent thread in the run pool and a waiting
  slot. A worker whose waiting slot is full still claims runs, so a second asking run is refused with
  `tool.approval_capacity` while another worker is idle. Agents with a quality check always wait in the thread.
  Fails closed.
- **Next step:** Resolved by activating approval checkpoints; otherwise consider delaying claims of a worker with no
  free waiting slot (needs a separate go). Operators can raise `max_waiting` meanwhile.

### RT-067 · Small local thinking models loop on tool calls

- **Priority:** P3 Low · **Status:** Open (needs a live local model) · **Refs:** settings/providers round 2026-11-06
  (Ollama 400 fix, O400-5)
- **What:** After the 400 fix, the production-downtime example on qwen3.5:2b (CPU) failed twice with
  `run.agent.MaxToolRoundsError` (single calculator calls, 8 rounds). The error now says what the calls show
  (`rounds`, `calls`, `repeated`, `tool`, `looping`, `local`) with advice, so a loop is told apart from a long task.
- **Next step:** Compare tool looping with and without reasoning sent back on a live local model; then decide on a
  higher default `max_tool_rounds` for local thinking models.

## Architecture and modularity

### RT-007 · `engine.py` / `RunManager` keeps growing

- **Priority:** P3 Low · **Status:** Open (deliberately deferred) · **Refs:** backend audit D-13
- **What:** `engine.py` has 4,800 lines on 2026-11-05 (3,381 at the first audit start, 3,865 on 2026-11-01);
  approvals, persistence, resume and orchestration live in one class, which raises the regression risk of every
  change.
- **Next step:** Extract one clearly bounded part (tool approvals, persistence or resume) with the next functional
  change to it; no big-bang refactor.

### RT-039 · Axentra contract step (M12): remove the legacy paths

- **Priority:** P3 Low · **Status:** Blocked (on production evidence per switch, RT-030) · **Refs:** `docs/AXENTRA.md`
  ("Legacy paths and their removal preconditions")
- **What:** The old implementations stay while they are the default, the rollback target or needed without Axentra:
  `IdentityStore` reads with `nodestro_memberships` and `nodestro_login_states`, Nodestro's OIDC client, the server's
  v1 tenant secrets and the server's volume copies of input files. The doc's table names each path and its removal
  precondition.
- **Next step:** Per path, once its precondition holds (production evidence, reconciliation with no difference,
  review passed): remove the code and drop the tables in a new migration, with a backup and a restore drill.

### RT-035 · Run budget decisions: remaining gaps

- **Priority:** P3 Low · **Status:** Open · **Refs:** `docs/BUDGET.md`, orkexa ORK-022
- **What:** Requests inside orkexa batches and `run_agent` sub-agents cannot suspend and fail with
  `run.budget_exceeded` as before. A request whose real cost exceeds its bound (a price change) is kept and the next
  request asks. Only simulated tests (no paid calls) cover the feature.
- **Next step:** Suspendable batches in orkexa (ORK-022), then let those requests ask too.

## Performance

### RT-008 · PostgreSQL store throughput and serialized writes

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit C-14 (partly fixed), #65
- **What:** With fresh statistics the pooled store is about 12 % below the pre-audit single-connection throughput,
  and writes of one process still run one after another. Not measured on server hardware under load. Local numbers
  (one 4-CPU machine, before and after Axentra) are in `docs/AXENTRA.md`, "Performance": with Axentra identity an
  API process is CPU-bound at about 40 % of the requests per second of the Axentra-off configuration.
- **Next step:** Load test on target hardware (run in nodestro-server, SRV-013); then decide on parallel writes or a pool-free read path
  for single-process use.

### RT-043 · Per-node overhead grows with workflow length

- **Priority:** P4 Optional · **Status:** Open · **Refs:** backend audit round 2 RT2-07
- **What:** Each node saves the whole run document; measured 24 ms per node at 10 nodes and 42 ms at 50 nodes
  (scripted model, shared machine). No memory leak over 600 runs.
- **Next step:** Only if long workflows matter: save node state incrementally.

## Features and enhancements

### RT-041 · Similarity hint for pasted code that resembles AI-generated code (R-4)

- **Priority:** P4 Optional · **Status:** Open · **Refs:** ADR-0006 (R-4 amendment), `tool_library.editor_trust`,
  `tool_provenance.code_digest`
- **What:** Code pasted into the tool editor counts as `user-authored` (decision R-4, default 2); only an exact copy
  of stored AI-generated code (normalized digest) is recognized. Slightly edited AI code is not detected.
- **Next step:** Decide whether a fuzzy match (for example token shingles against stored untrusted tools, drafts and
  job attempts) should show a *hint* in the editor. It must stay a hint: it never changes the trust class and is
  never a security boundary.

### RT-010 · Rotation of the server secret key

- **Priority:** P2 Medium · **Status:** In progress · **Refs:** `server_settings.py`, `axentra_platform.py`,
  `docs/AXENTRA.md`
- **What:** Changing `NODESTRO_SECRET_KEY` makes every stored tenant secret unreadable.
- **Done (2026-11-02, Axentra migration):** with `NODESTRO_AXENTRA=on` and `NODESTRO_SECRETS_FORMAT=axs1` tenant
  secrets use Axentra's keyring (several keys, rotation); `reencrypt_tenant_secrets` (operator command
  `nodestro-server secrets reencrypt`) moves v1 values and older keyring keys to the active key, resumable and
  without ever overwriting an unreadable value. Tested on PostgreSQL.
- **Next step:** Servers that keep the v1 format still cannot rotate `NODESTRO_SECRET_KEY`; decide whether the
  keyring becomes the default for new servers (then v1 is read-only), and run the procedure on a staging copy.

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

### RT-054 · Checked key figures: agent tests and the inputs of a tool call

- **Priority:** P4 Optional · **Status:** Open · **Refs:** `value_checks`, `OutputField.source`, CHANGELOG (checked
  key figures)
- **What:** A key figure with `source="tool"` is checked in workflow runs only. The agent test in the inspector shows
  the value without the check, because a step that copies a figure computed by an earlier step (a reality check, a
  plausibility check) has no earlier steps there. The check proves that a value is a tool result, and for
  `table_totals` that no input row was left out; it does not prove that the agent passed the right column or reference
  total, and it does not look at figures in free text. A small local model sometimes reports a correct figure it did
  not compute: in the live household-budget runs with `qwen3.5:4b` (2026-11-04) one of two runs failed with
  `run.value_unverified` after its correction, the other computed the surplus with the calculator and passed. The step
  then fails safely (no decision on the figure), but the run has to be repeated. Since audit round 3 (RT3-01) numbers
  echoed from a tool's input and changed tables no longer count, and the correction pass only computes (RT3-03). The
  check still proves provenance only: a calculation written out from made-up numbers, `table_totals` over a table the
  step's input does not contain, or another tool whose result contains the number (input files, knowledge search)
  still back a value; calls of every node of the run count, also parallel branches.
- **Next step:** Count only computing tools and the calls of the step and its ancestors. Show an unbacked key figure in the agent test as a warning (not a failure); consider letting a field
  name the tool and argument it must come from when a workflow needs more than provenance; measure the failure rate of
  small local models over more runs before deciding on a second correction.

### RT-016 · Deferred scope decisions

- **Priority:** P4 Optional · **Status:** Needs decision · **Refs:** AO M6b, D10, MCP closing decision
- **What:** Deliberately not implemented: orchestrators creating teams or agents from components (AO level 3),
  approval deadlines longer than 15 minutes (D10d), and MCP beyond the read-only server pilot (desktop MCP, write
  tools, unattended calls, stdio servers that are not Python packages).
- **Next step:** Owner decision per topic; none is planned now.

### RT-062 · Capabilities of non-catalog local models are known only after the download

- **Priority:** P3 Low · **Status:** Open · **Refs:** settings/providers round 2026-11-06 (missing-model stream)
- **What:** For a local model that is neither in the catalog nor installed, a missing capability (tools, vision, embeddings) is found only after the download.
- **Next step:** Read the capabilities from the registry manifest before the download and report `capability_missing` up front.

### RT-064 · Overall pull ETA for local model downloads

- **Priority:** P4 Optional · **Status:** Open · **Refs:** settings/providers round 2026-11-06 (local setup stream)
- **What:** Progress and ETA are per file until Ollama verifies; the overall remaining time appears only once all files are known.
- **Next step:** Compute an overall ETA as soon as Ollama reports the total size.

### RT-065 · Native OpenAI and Gemini tools are not integrated

- **Priority:** P4 Optional · **Status:** Open · **Refs:** settings/providers round 2026-11-06 (params stream)
- **What:** Orkexa passes native web search and code execution through, but Nodestro shows them as `not_integrated` because their fees are not priced.
- **Next step:** Price the fees in the pricing engine, then enable them through `model_profile` like Anthropic's server tools.

## Tests and quality assurance

### RT-056 · The test double answers in German in every language

- **Priority:** P4 Optional · **Status:** Open · **Refs:** UX round 3 handover B-3
- **What:** The answering test double (`testing.AnswerAll`) finishes orchestrations since the TODO abatement round, but
  its text answers are German placeholders ("Ergebnis des Agenten.", "Beispiel") in every language. Tests of other
  repositories assert this exact text, and a run has no language field the double could follow.
- **Next step:** Decide where the run's language comes from (a copied example now stores its language in
  `Workflow.origin`, but other workflows have none); then answer in it and update the asserting tests together.

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
  the packaged desktop in VMs. Other distributions and kernels with older Landlock ABIs are untested; the sandbox
  then reports itself unavailable and untrusted tools need Docker. aarch64 was audited on 2026-11-02
  ([sandbox-arm64.md](sandbox-arm64.md)): the seccomp filter and the probes use x86-64 system call numbers, so the
  sandbox is deliberately unavailable there; host check, confinement check and the filter's `arch` check each fail
  closed, tested by `tests/test_sandbox_arch.py` and the CI job `builtin-sandbox-arm64` on `ubuntu-24.04-arm`. The
  probes refuse to run on non-x86-64 machines. The filter is a deny list: calls newer than the list (`quotactl_fd`,
  `statmount`, `listmount`) are allowed on x86-64 (finding A6, low).
- **Next step:** Add further systems to the probe matrix when they are to be supported. aarch64 support, if needed
  again (macOS VM guest, paused), follows the checklist in the decision of sandbox-arm64.md; review the deny list
  against current kernels (A6).

### RT-049 · Managed Ollama untested on macOS and Windows

- **Priority:** P2 Medium · **Status:** Open · **Refs:** #99, orkexa ORK-030
- **What:** `local_service` (managed Ollama, model folder moves, imports) was tested on Linux x86-64 with a real
  Ollama 0.40.14 and a fake binary. macOS, Windows, GPUs and the large models (`qwen3.6:35b-a3b`, `qwen3.8:27b`) were
  not run.
- **Next step:** Run the local-model tests and a real workflow on macOS (Apple silicon) and Windows, with a GPU. The
  CPU saving mode (RT-050, done in #107) was measured only on Linux without a GPU; on a GPU it must stay off
  (`accelerated`), and the GPU detection of a remote server (`/api/ps` `size_vram`) needs a real GPU server.

### RT-052 · Ministral models need an explicit system prompt

- **Priority:** P3 Low · **Status:** Open · **Refs:** #103, EU evaluation 2026-11-03
- **What:** `ministral-3:3b` and `ministral-3:8b` routed 2/6 and 4/6 cases right without a system prompt and 6/6 with
  one, and they ask for confirmation before tools such as `create_refund` in multi-step chains. `ministral-3:14b`
  and `devstral-small-2:24b` are in the catalog but were not run on this hardware (14 GB and 15 GB of memory).
- **Next step:** Run the 14b and 24b models on a larger machine; consider a catalog hint that these models need
  agent instructions.

### RT-053 · The mDenseOn build depends on a third-party template header

- **Priority:** P3 Low · **Status:** Open · **Refs:** #104, orkexa #43 / ORK-035
- **What:** `mdenseon:307m` is built offline from the pinned LightOn weights and a 6 MB GGUF header taken from a
  community repository (`mykor/mmBERT-base-GGUF`), checked by SHA-256 and against reference vectors. The build needs
  `gguf` and `numpy` (orkexa `[convert]`), which the desktop and server images now carry. Reindex timing for a large
  knowledge base with mDenseOn was not measured.
- **Next step:** Replace the community header with one generated from llama.cpp's converter in CI; measure indexing
  throughput against `qwen3-embedding:0.6b`.

### RT-060 · Only NVIDIA and Apple GPUs are detected

- **Priority:** P3 Low · **Status:** Open (needs AMD/Intel hardware) · **Refs:** product check 2026-11-05,
  `local_models.detect_hardware`, `local_service.LocalModelService._accelerated`
- **What:** `detect_hardware` knows `nvidia` (nvidia-smi) and Apple silicon only; on this computer the hardware
  decides over Ollama's `size_vram` (which reports GPU memory even on a VM without a GPU). An AMD (ROCm) or Intel/AMD
  (Vulkan) GPU that Ollama uses therefore counts as "no GPU": the CPU saving mode switches thinking off at effort
  `medium` and the advice says the computer is slow. Workaround: the setting `local_reasoning: full`.
- **Next step:** Detect ROCm and Vulkan devices (or trust Ollama's `size_vram` once a model is fully loaded and the
  device is real) and test it on such hardware; do not guess without a device to test on.

### RT-019 · No isolating sandbox without Docker on macOS and Windows

- **Priority:** P3 Low · **Status:** Open · **Refs:** `sandbox_policy.py`, `sandbox_selection.py`, Orkexa ADR 0012
- **What:** The built-in sandbox is Linux-only. On macOS and Windows untrusted (AI-generated) tools need Docker, which
  is the default sandbox for such code (Docker-first). Since 2026-11-02 the runtime imports and starts on macOS
  (arm64, x86-64) and Windows (x64, ARM64), tested by the `Platforms` workflow. The `auto` mode (default) uses Docker
  once it passed its preflight and in-container self-test (RT-033) and reports why code is blocked otherwise.
  Orkexa's macOS (Seatbelt) sandbox was removed (not releasable): Nodestro reports `macos-seatbelt` as `unavailable`
  with the reason `removed` and never probes or uses it. Windows has no native sandbox (ADR-0009 in the studio repo).
- **Next step:** A Windows backend (AppContainer + Job Object, or MXC) as a separate decision; no macOS-native backend
  is planned.

### RT-020 · Real providers and PostgreSQL at scale

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit sections 3 and 16, Bedrock and LRC-1 reports
- **What:** Tests use scripted models only: no real provider calls (including a live AWS Bedrock smoke test and live
  run chat with real models), no load against PostgreSQL on server hardware, no multi-process test of every race fix.
- **Next step:** Run the prepared Bedrock smoke test once credentials are approved; the PostgreSQL load test is
  tracked in nodestro-server SRV-013.

### RT-066 · No live Ollama reasoning check in CI

- **Priority:** P4 Optional · **Status:** Open · **Refs:** settings/providers round 2026-11-06 (thinking stream, THINK-4)
- **What:** Ollama's reasoning stream was checked against a recorded fixture only, not against a live model server.
- **Next step:** Check it live with a small reasoning model, then add an optional CI job (as for the local model tests).

## Documentation

None known.

## Technical debt

### RT-023 · Reproducible Python dependencies

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-05, `.github/dependabot.yml`
- **What:** Dependencies are ranges without a lock or constraints file, so image rebuilds resolve differently over
  time; Dependabot covers only GitHub Actions (its comment says why: component pins move with the pin cascade, CI's
  blocking `pip-audit` covers the third-party ranges; corrected in the dependency round of 2026-11-05).
- **Next step:** A hashed constraints file for CI and images, or a scheduled `pip-audit` plus upgrade check. Decide
  together with nodestro-studio ST-028 and nodestro-server SRV-014.

### RT-024 · German text in prompts and default names

- **Priority:** P4 Optional · **Status:** Needs decision · **Refs:** runtime quality round PR #3
- **What:** `engine.py` keeps German prompt fragments ("# Eingaben", "Bitte beginne.") and default node names
  ("Ausgabe", "Entscheidung", "Freigabe"); `schemas.py` "Neuer Workflow". They are kept on purpose for prompt
  stability, but conflict with the English-only rule.
- **Next step:** Decide; a change needs coordinated updates of API tests and the OpenAPI snapshot.

### RT-048 · pgvector installed after migration 0010

- **Priority:** P3 Low · **Status:** Open · **Refs:** #99, `migrations/postgres/0010_knowledge_vectors.sql`
- **What:** Migration 0010 installs pgvector only when it is available at that moment. Installed later, the runtime
  keeps ranking vectors itself until an operator runs `CREATE EXTENSION vector` in the schema.
- **Next step:** A `nodestro-server` check that reports it, and the command in the operator documentation.

## Later optimizations

### RT-047 · Exact vector search only

- **Priority:** P4 Optional · **Status:** Open · **Refs:** #99, orkexa ORK-032
- **What:** Vector and hybrid knowledge search rank exactly (SQLite `vec_distance_cosine`, PostgreSQL pgvector or the
  runtime). That is right for desktop-sized knowledge bases; without pgvector the runtime reads every candidate vector,
  so each query transfers every vector of the namespace from PostgreSQL.
- **Next step:** Only for large tenants: an HNSW index per space (pgvector) once a model's dimensions are fixed.

## Accepted risks

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
| (pointer) | nodestro-studio | ST-052 (Dependabot policy: weekly groups, cooldown, manual merge; auto-merge prerequisites) | Full entry in studio |
| — | axentra | axentra #42 (`cfa80dc`): IP and request id on login/setup audit events | Runtime pins axentra `0a1d1cc`. #42 adds only optional parameters and does not change what Nodestro calls; runtime `test_axentra_*` and the api server-profile tests pass against `cfa80dc`. Move the pin in the next functional cascade (no cascade of its own) |
| — | orkexa | ORK-003 (A-05, ORK3-10): redirects and loopback rules when `orkexa.Agent` is used directly | Compatible endpoints refuse redirects in the pinned orkexa `1b61fda`; the loopback rules stay with orkexa. Runtime's server path already refuses redirects |
| RT-010 | nodestro-api | API-005 (operator command for key rotation) | Runtime provides `SecretBox` support |
| RT-014 | nodestro-api | API-009 (leader cleanup task) | API calls runtime retention |
| RT-015 | nodestro-api | API-021 (region in usage views) | Runtime records the region first |
| RT-001 | nodestro-api, nodestro-studio, nodestro-server | S-2 approval routes, approval UI, SRV-004 | One review covers the whole approval flow; only this entry tracks it |
| RT-003 | nodestro-local-docker | LD-001 (gateway review), LD-012 (container escape) | Gateway code and hardening live here |
| RT-008, RT-020 | nodestro-server | SRV-013 (PostgreSQL load test) | Load test runs there; results feed back here |
| RT-049 | orkexa, nodestro-desktop | ORK-030, DSK-022 (managed Ollama on macOS/Windows) | Orkexa owns the managed server; runtime reruns its local-model tests there |
| RT-023 | nodestro-studio, nodestro-server | ST-028, SRV-014 (D-05) | One shared locking decision |
| — | nodestro-integrations | INT-001: cap on parallel OCR documents | Decide whether the cap lives in the integration or where the runtime calls OCR |

## CI access to private repositories

Keep the repository secret `NODESTRO_READ`; do not delete it. CI currently installs the sibling repositories anonymously
because they are public, and does not need the secret. When the repositories become private, CI authenticates again
with this read-only secret, passed only in the environment of the install steps (never in a global Git configuration,
a log or an image layer).

## Maintaining this file

- Add an entry when work is identified but not done in the current PR. Use the next free ID; IDs are never reused.
- Link a GitHub issue for anything concrete enough to be picked up; keep details in the issue, not here.
- When an item is finished, delete its entry in the PR that completes it and mention the ID in the PR description.
  Git history is the record; do not keep ticked checklists.
- Accepted risks stay listed with their reason until the risk is removed or re-evaluated.
- Re-review the whole file before each release and update the "Last full review" line.
- Security issues that are not yet fixed are described only in general terms here. Report details privately
  (GitHub private vulnerability reporting), never in this file or in public issues.
