# Isolation of extension tools (N-22)

Integrations reach the runtime through the entry point group `nodestro.integrations`
(`src/nodestro_runtime/extensions.py`). Until N-22 every installed entry point was imported into the host process:
its import-time code and its tools ran next to the unlocked vault, the process environment and the whole file system,
and a time limit did not stop them (security review 2026-11, phase 0R, findings S1 to S3). This page describes how
tools of installed integrations run now, what locks them and what the security evidence G-6 still has to show before
any class of them may be enabled by default.

## Rules

1. **No foreign code in the host process.** Only pinned entry points of Nodestro's own distribution
   (`trusted_integrations.TRUSTED_ENTRY_POINTS`: `nodestro-integrations`, `tesseract-ocr =
   nodestro_integrations.ocr:integration`) are imported into the host, for their OCR engine, and only after
   `trusted_integrations.verify` passed: the exact `module:attribute`; a `<name>-<version>.dist-info` folder that
   matches the metadata, in the installation directory of the files; every Python file of the pinned package listed in
   that `RECORD` with a SHA-256 hash, reached without a symbolic link and matching the hash. The import then runs from
   exactly those verified bytes (a finder first on `sys.meta_path`; no cached bytecode, no other copy on `sys.path`,
   no unlisted file). A distribution that only calls itself `nodestro-integrations` (any spelling) and fails a check
   is never imported and is reported as `invalid`. Every distribution is examined (no de-duplication by name, so an
   earlier copy on the path cannot hide another). An editable install of `nodestro-integrations` is not trusted.
   Every other entry point is read as data only (`extensions.find_packages`): its name, its `module:attribute`, the
   name and version of its distribution and the Python files the distribution's `RECORD` lists for that module (no
   symbolic link anywhere on the path). It is never imported, so import-time code never runs in the host. Tools of the
   trusted distribution stay locked as before (`isolation_unavailable`): they are imported into the host and have no
   isolated path.
2. **One execution path: the Docker sandbox.** Listing a package's tools and every call run in a new container of
   Orkexa's one sandbox shape (`orkexa.dynamic.docker.run_args`: no network, read-only root, no capabilities, no new
   privileges, user `nobody`, private IPC, memory without swap, CPU, process, open-file and file-size limits, `noexec`
   temporary file systems, no mount, no environment variable from the host, never pulled), in Nodestro's runtime image
   run by its id, and only after the daemon passed Orkexa's preflight and in-container self-test
   (`StreamedSandbox.ready_image`). The package's files, a small contract module, Orkexa's runner modules and one JSON
   request travel on stdin; one JSON response comes back on stdout (`extension_sandbox.py`).
3. **Locked without a proven sandbox.** Without Docker, with a daemon that fails the preflight or the self-test, or
   when the image cannot be built, the tools stay locked with the reason `not_secured` ("locked, not sufficiently
   secured"). There is no fallback: the process sandbox is for trusted code only and is never used for them.
4. **The time limit ends the container.** A call runs under its time limit: a tool without a spec gets
   `tool_spec.HOST_TOOL_TIMEOUT_SECONDS` (300 s); a stated limit can only lower it. At the limit the docker client is
   killed and the container is killed and removed (`orkexa.dynamic.docker.stop_container`); the runtime then checks
   that no container of that name exists any more and removes it again otherwise. The run's wait for the tool ends a
   little later (`tool_spec.STOP_GRACE_SECONDS`), so the result is the container's timeout, not an abandoned thread.
   Cancelling a run removes the container the same way (`orkexa.dynamic.cancel_scope`).
5. **No self-declared capability lowers anything.** A tool of an installed integration counts at least as
   `UNKNOWN` (write, external). What the package states for its tools can only raise it (`destructive`), never lower
   it (`extension_sandbox.floor_capability`); the same holds for the time limit. Names, descriptions and schemas it
   reports are checked and bounded like those of an MCP server.
6. **No network, no host files, no secrets.** The container has only the loopback interface; there is no network
   grant for extension tools yet. It sees no host path, no vault, no connection secret and no provider key: the only
   input is the call's JSON arguments.
7. **Off by default.** `NODESTRO_EXTENSION_TOOLS=1` is a development switch: without it (the default in every
   profile) installed packages are listed as locked with the reason `off` and no container is started. With it, the
   runtime lists the packages' tools in the sandbox when the workspace opens and offers them on the desktop and in
   Nodestro Local Docker (never in the server profile: there no listing container starts). Turning it on is not a release: no tool class is enabled
   until G-6 (below) is passed, and nothing in this change enables one.

`Extensions.packages_status()` (also in `Extensions.summary()`) reports each package with its state (`locked` or
`sandboxed`), the reason (`off`, `not_secured`, `invalid`) and the text "locked, not sufficiently secured" for a
locked package.

## Limits of this version

- A package can use only its own pure-Python files, the standard library, `pydantic` and `typing-extensions` (the
  runtime image) and the contract module: other dependencies are not installed and fail the listing (`invalid`).
- The contract module in the container (`_extension_contract.py`) offers `Integration`, `ToolSpec` and
  `ToolCapability` as plain values; a spec is read only for a stated capability (to raise it) and a lower time limit.
  OCR engines of foreign packages are not used.
- Each call starts a new container (about a second of overhead); state does not survive between calls.
- The Docker sandbox shares the kernel with the host (or Docker Desktop's VM) and uses the daemon's default seccomp
  profile (see [docker-sandbox.md](docker-sandbox.md)).
- Packages are told apart by distribution, entry point name and `module:attribute`; two that cannot be told apart
  are both `invalid`. Listing runs one container per package, one after the other (up to 60 s each) while the
  workspace opens with the switch on. Descriptions lose control and bidi characters but stay untrusted text that a
  user interface must escape.
- Residual risk R1 (open, for G-6): a complete, self-consistent forged copy of `nodestro-integrations` (its own
  `.dist-info` folder and a `RECORD` whose hashes match its own files) placed ahead on `sys.path` still passes the
  checks and runs in the host. Placing it needs write access to the environment or control of `PYTHONPATH`: the same
  position as a `.pth` file or a modified runtime. Closing it needs file hashes of each trusted integration release
  pinned in the runtime, which ties runtime releases to integration releases; that is an open owner decision.
- Residual risk R2: a module `nodestro_integrations` that host code imported normally before `discover()` is accepted
  when its real path is the verified one (its bytes were not loaded through the verified finder). Only host code can
  cause that import; no extension can.
- Residual risk: whoever can write into the backend's Python environment can already run code there at every start
  (for example through a `.pth` file) or change a trusted file between its check and its import. Installing a package
  is a trust decision of whoever controls that environment; the rules above protect against a package's tools, its
  import-time code and a package that claims to be Nodestro's own, not against a compromised environment.

## G-6: evidence before a tool class is enabled

A class is a combination of `access` × `reach` (for example read + internal). Enabling one by default needs every
item below passed for that class on the real engines (Linux CI Docker, Docker Desktop on macOS and Windows), an
internal review and the independent review (gate G-6). Items marked with a test name are covered now; the others are
open.

| # | Check | Expected | Covered by |
|---|---|---|---|
| 1 | switch missing or off | no tool offered, no container, no import of the package | `test_extension_isolation.py::test_switch_off_*` |
| 2 | import-time code of a package, also one that claims to be `nodestro-integrations` | never runs in the host (switch off and on) | `::test_import_time_code_never_runs_in_the_host`, `::test_a_distribution_that_claims_the_trusted_name_is_never_imported`, `::test_every_trust_check_refuses_the_import`, `::test_a_copy_earlier_on_the_path_neither_hides_nor_replaces_the_verified_one`, Docker: `::test_import_time_code_runs_only_in_the_container` |
| 3 | no Docker, failed preflight or self-test | locked `not_secured`, no host fallback | `::test_without_a_proven_sandbox_the_tools_stay_locked`, `::test_a_missing_docker_cli_locks_the_tools_with_the_real_sandbox` |
| 4 | self-declared capability `read`/`internal` | ignored, counts as unknown; approval applies | `::test_a_stated_capability_can_only_raise` |
| 5 | endless loop | container removed after the limit (`docker ps -a` empty), host thread free | Docker: `::test_an_endless_loop_is_killed_and_the_container_removed` |
| 6 | time limit without spec | 300 s in the container, a stated limit only lowers | `::test_time_limits_are_capped` |
| 7 | fork bomb, process limit | refused in the container, host unaffected | Docker: `::test_forbidden_resources_are_refused` |
| 8 | 4 GB allocation | refused or killed in the container (memory limit) | Docker: `::test_memory_beyond_the_limit_is_refused_in_the_container` |
| 9 | TCP, UDP/DNS, IPv6, 169.254.169.254 without grant | fails | Docker: `::test_no_network_without_a_grant` |
| 10 | host files outside the mount (data directory, home, `/etc` of the host, the API key file) | not visible | Docker: `::test_host_files_and_secrets_are_not_visible` |
| 11 | secrets: environment, `/proc/1/environ`, vault key, mounted credentials (`~/.aws`, `~/.docker`) | not visible | Docker: `::test_host_files_and_secrets_are_not_visible` |
| 12 | Docker socket, host `/proc`, devices, `setuid`, capabilities, `NoNewPrivs` | not reachable / refused | Docker: `::test_forbidden_resources_are_refused` |
| 13 | huge, broken or non-JSON output, line over the limit | clean error, host memory bounded | `::test_broken_responses_fail_cleanly`, Docker: `::test_huge_output_fails_cleanly` |
| 14 | product profiles without the switch | no extension tool callable | `::test_no_extension_tool_in_a_product_profile_without_the_switch` |
| 15 | image tag moved after the check | only the checked image id runs | Orkexa / `StreamedSandbox` (calls run the image id) |
| 16 | run cancelled during a call | container removed | open: test through `sandbox_cancel` with a real container |
| 17 | network grant for a class with `reach=external` | only the granted host, through the egress guard | open: no grant path exists yet |
| 18 | folder grant for a class that reads files | only through `orkexa.fs` and a grant | open: no grant path exists yet |
| 19 | Docker Desktop (macOS, Windows) | items 5 to 12 pass there | open |
| 20 | own seccomp profile, user namespace remapping | decided and, if chosen, enforced by the preflight | open |
| 21 | dependencies of a package | built as a hashed image like tool environments, never in the host | open |
| 22 | per-class enabling and the trust exception (WZ-8) | designed, reviewed | open: not implemented on purpose |
| 23 | a forged, self-consistent copy of the trusted integration ahead on the path (R1) | refused (release-pinned file hashes) | open: owner decision |
| 24 | trust checks on Windows and macOS paths (short names, case, `/private`) | same verdicts | Platforms workflow: `test_trusted_integrations.py` |

The checks with "Docker:" run in the CI job `docker` of this repository (`NODESTRO_REQUIRE_DOCKER=1`: a daemon that
does not pass Orkexa's preflight fails them instead of skipping them).
