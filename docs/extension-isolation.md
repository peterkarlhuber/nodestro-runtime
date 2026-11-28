# Isolation of extension tools (N-22)

Integrations reach the runtime through the entry point group `nodestro.integrations`
(`src/nodestro_runtime/extensions.py`). Until N-22 every installed entry point was imported into the host process:
its import-time code and its tools ran next to the unlocked vault, the process environment and the whole file system,
and a time limit did not stop them (security review 2026-11, phase 0R, findings S1 to S3). This page describes how
tools of installed integrations run now, what locks them and what the security evidence G-6 still has to show before
any class of them may be enabled by default.

## Architecture decision (owner, 2026-11-27, binding)

1. Foreign extension packages are never installed into the host Python environment, by pip or any other way.
2. Foreign extensions are never loaded through the host import path or run in the host process.
3. The trusted OCR integration (`nodestro-integrations`) stays usable, provided its origin and integrity are verified
   and no user-writable path can bypass the check (rule 1 below and
   [Trust boundary of the host import](#trust-boundary-of-the-host-import-r1)).
4. The separate extension store `<data>/extensions` is a binding work package before any G-6 release (RT-098).
5. The store is strictly separate from the host Python environment; its packages run only in the Docker sandbox.
6. Until the store is built and has passed its security review, foreign extension tools stay locked by default.

What the runtime does about it: a foreign package that `find_packages()` finds in the host environment (any entry
point other than the pinned trusted one) is refused as `invalid` with the detail "installed in the host environment"
(`extensions.HOST_INSTALLED`), whatever `NODESTRO_EXTENSION_TOOLS` says. Only its metadata (entry point name,
`module:attribute`, distribution name and version) is read to report it; it is never offered, imported or read, and
nothing of it is sent to the sandbox (`test_extension_isolation.py::test_a_foreign_package_in_the_host_environment_is_refused`).
Foreign packages come only from the data-only extension store (RT-098, below), which feeds the sandbox.

## The data-only extension store (RT-098)

`src/nodestro_runtime/extension_store.py`, `tests/test_extension_store.py`. Only on this computer (desktop, Nodestro
Local Docker: `Workspace.extension_store`); a server has none; with explicit integrations (`Workspace(integrations=[...])`) it can be changed but its packages are not listed.

```text
<data>/extensions/            0700, never on sys.path (the data directory is refused there, environment_problems)
  index.json                  installed packages and revoked hashes; a package counts only while it is listed here
  <sha256>/                   one wheel, unpacked, files 0444 and folders 0555; the name is the wheel's SHA-256
  .staging/<random>/          an intake in progress (never valid)
  .trash/<random>/            a removal in progress (never valid)
```

| Owner requirement (Welle 7) | How the store meets it | Check (`tests/test_extension_store.py`) |
|---|---|---|
| 1. never installed into the host environment, never imported in the host | the store writes files only below `<data>/extensions`; nothing there is on `sys.path`, `find_spec` does not find the module, `discover`/`find_packages` do not list it; the files only travel to the container on stdin | `::test_a_checked_wheel_is_kept_read_only_by_its_hash_and_never_imported`, `nothing_imported` in every test; Docker: `test_extension_isolation.py::Real` takes its package from a real store |
| 2. install, update, removal only through the controlled interfaces | `ExtensionStore.install` (with `replaces=` for an update), `remove`, `revoke`; the HTTP API's admin routes call them (nodestro-api API-081, branch `claude/w7-ext-store`, in review: permission `admin`, a signed-in person only, the actor is the authenticated principal, never a form field). A folder named like a package hash that the index does not list (copied in by hand, or left by a crash) is never used and is removed when the store opens; nothing else in the folder is ever deleted. The store folder is used only while it is a plain folder (not a symbolic link) of this user that no group or other user can write; otherwise nothing of it is listed, run, changed or cleaned. Removal and revocation take only a SHA-256, also when the index lists another key | `::test_an_interrupted_intake_never_leaves_a_valid_package`, `::test_a_retry_after_a_crash_between_rename_and_commit_works_without_reopening`, `::test_an_unsafe_store_folder_is_never_read_changed_or_cleaned` (4 cases), `::test_cleaning_deletes_only_what_the_store_made`, `::test_a_hash_from_the_index_never_names_a_path_outside_the_store`; api: `test_extensions_api.py` |
| 3. size, origin, hash, manifest, dependencies, file paths; unsafe archives, links, traversal, tampering refused | the administrator states the expected SHA-256 and the origin; the wheel is at most 8 MiB, at most 1,000 members, 1 MiB per file and 8 MiB unpacked (each member's real size is read, not trusted); no absolute, `..`, backslash, drive or duplicate path (also by case and Unicode normalization), no name that is both a file and a folder, no name Windows cannot keep (device names such as `aux.py`, a trailing space or dot, `:` streams, `<>"|?*`), no symbolic link, device, FIFO or other special member, no encrypted member, only stored or deflated members; exactly one `<name>-<version>.dist-info` whose `RECORD` lists exactly the members, each with its SHA-256 and size; `Root-Is-Purelib: true` and `py3-none-any` only; `METADATA` matching the folder; no `.data` part, no `.pth`, no `sitecustomize`/`usercustomize`, no native code; only `nodestro.integrations` entry points that name a module of the wheel (no `console_scripts`); no dependency except those the sandbox provides (`PROVIDED`; optional extras are ignored); no distribution named like a trusted or built-in one (`RESERVED_DISTRIBUTIONS`: `nodestro-integrations`, `nodestro-runtime`, `orkexa` and others) and no top-level module of the runtime, its libraries or the standard library (`RESERVED_MODULES`). An operating system error while writing is a refusal (`storage`), audited like the others. After intake every read checks the folder against `RECORD` (whose own hash the index keeps): a changed, added or removed file, a symbolic link or a hard link makes the package `invalid` | `::test_an_unsafe_or_inconsistent_wheel_is_refused_before_anything_is_written` (47 cases), `::test_a_failed_write_is_an_audited_refusal`, `::test_the_expected_hash_and_the_origin_are_required`, `::test_optional_and_provided_dependencies_are_accepted`, `::test_a_package_changed_after_intake_is_never_handed_to_the_sandbox` (6 cases), `::test_a_file_changed_together_with_its_record_is_never_handed_to_the_sandbox` |
| 4. atomic intake | written to `.staging/<random>/`, each file `fsync`ed and made read-only, the folder renamed to `<sha256>/`, then the index replaced in one `os.replace` (the commit point). A crash before the commit leaves only folders that are never valid and are removed at the next open | `::test_an_interrupted_intake_never_leaves_a_valid_package` (crash while writing, crash before the commit) |
| 5. never on `sys.path` or the trusted integrations' search path | as 1; the existing checks keep refusing the data directory on the import path and every foreign package in the host environment (`HOST_INSTALLED`) | `::test_a_host_installed_package_stays_refused_next_to_the_store`, `test_trusted_integrations.py::test_the_data_directory_on_the_path_is_a_problem` |
| 6. execution only in the Docker sandbox; locked without it | store packages go through the same `Extensions.load_packages` and `ExtensionSandbox` as before: off without the switch, `not_secured` without a proven daemon and seccomp probe | `::test_the_workspace_offers_store_packages_only_through_the_sandbox`; Docker: G-6 items 5 to 13 and 16 run against a store package |
| 7. N-06 and N-13 mechanisms | a store tool is an extension tool like before: capability at least `UNKNOWN` (`floor_capability`), the tool gate of the access model and the approvals apply unchanged; intake (also a refused one), update, removal and revocation are activity events (source `extension`, resource `extension_package` = the SHA-256, the actor) | `::test_intake_removal_and_revocation_are_in_the_activity_log` |
| 8. removal, revocation, update never make an invalid package runnable again | before every container of a call the package is checked against the index (still listed with the same `RECORD` hash, not revoked); the sandbox gets the bytes verified when the package was listed, never a fresh read; a revoked hash is never installed again; an update installs the new hash and removes the old one in the same commit | `::test_a_removed_revoked_or_replaced_package_never_runs_again` (3 cases), `::test_the_check_before_a_call_compares_the_record_the_package_was_verified_with`, `::test_a_revoked_hash_is_never_installed_again`, `::test_one_distribution_at_a_time_and_updates_only_of_the_same` |
| 9. product neutral | no product name; desktop and Nodestro Local Docker use the same store | — |

Limits: a change takes effect for the listing when the workspace opens; a removal or revocation takes effect for calls
at once (the check). A call that passed its check before a revocation was committed finishes (as any running action
does). One version per distribution is installed at a time. Whoever can write the data directory can also write the
index; the store protects against foreign packages and their files, not against a compromised data directory.

**Acceptance criteria of the extension store** (all needed before any class is unlocked):

- data only: each package unpacked read-only into `<data>/extensions/<sha256>/` (the wheel's SHA-256), never on
  `sys.path` (the data directory is already refused there, `environment_problems`): built;
- wheel checks before unpacking: zip safety (no absolute or `..` paths, no links, no device files), every file's
  `RECORD` hash, limits on file count, file size and total size; no `.pth` file, script or `.data` part is ever run:
  built (`.pth`, `.data` and scripts are refused, not only never run);
- upload and removal by an administrator only, both audited: the runtime audits; the admin-only routes are
  nodestro-api API-081;
- execution only in the Docker sandbox, with the existing bounds (rules 2 to 7 below): built;
- discovery from the store only (`find_packages` no longer offers anything from the host environment; it only
  reports and refuses it): built;
- an independent security review of the store before any class is unlocked: open (G-6).

**Residual risk.** The checks run inside the process they check, so they cannot catch a foreign package that is already
in the host environment (its code can run before them and change them, review NEW-4). The boundary is therefore
enforced by the environment, not by these checks: the host Python environment holds only the shipped lock, and the
products ship without pip. Installing anything else into it is outside the trust boundary.

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
   The distribution must also be installed in the directory the running `nodestro_runtime` package was imported from
   (`trusted_integrations.installation_home`), and the import path must be closed (`environment_problems`, see
   [Trust boundary of the host import](#trust-boundary-of-the-host-import-r1)); otherwise it is `invalid` with the reason.
   Every other entry point is refused as installed in the host environment (`extensions.find_packages`, owner
   decision above): only its name, its `module:attribute` and the name and version of its distribution are read, to
   report it. It is never imported, so import-time code never runs in the host, and its files are never read. Tools of the
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
7. **Off by default.** `NODESTRO_EXTENSION_TOOLS=1` is a development switch. A package found in the host environment
   is refused with or without it (`invalid`, "installed in the host environment"). For packages from the store
   (RT-098): without the switch (the default in every profile) they are listed as locked with the reason `off` and
   no container is started. With it, the
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
- The Docker sandbox shares the kernel with the host (or Docker Desktop's VM). Extension containers run under
  Nodestro's own seccomp profile (`extension_seccomp.json`: Moby's default profile `moby/profiles` `seccomp/v0.1.0`
  without its capability rules and without `ptrace`, `process_vm_readv`/`writev`, `memfd_create`, `execveat`,
  `personality`, `kcmp`, `pidfd_getfd`, NUMA and file handle calls; `extension_seccomp.EXTRA_BLOCKED`). The docker
  CLI sends the file's content, so it works with a remote daemon, Docker Desktop's VM and through Nodestro Local
  Docker's sandbox gateway, which accepts exactly this profile and no other. A probe container of the same shape
  without package code proves once per daemon and image that the profile is applied (`ptrace`, `memfd_create`,
  `execveat`, ... refused), or the tools stay `not_secured`. Podman with this option is not tested.
- User namespaces: whether the daemon remaps container users (`userns-remap`, rootless engines) is detected and
  reported (`orkexa` `DockerHost.userns_remap`, in the self-test facts). It is not required: the container user is
  `nobody` without capabilities and with `no-new-privileges` either way, and requiring it would lock every default
  Docker installation. Decision for G-6: recommended in the operating guide, not enforced.
- Dependencies: the runtime image extension tools run in is built from a hashed lock (`pip install --no-index
  --require-hashes`, nothing fetched at run time, run by its image id). A package's own dependencies are not
  installed; a package that needs one fails its listing (`invalid`). Design for later: the package's requirements as an
  approved, hashed environment image like a local MCP server's (`container_images`), never installed in the host.
- Tool classes: designed in `extension_classes.py`, nothing released. A package's statement cannot lower its
  capability, so a class follows from the container's grants instead (no grant: `read` with reach `internal`; a
  folder grant: `read`/`write`, `internal`; a network grant: `external`). A class counts with that capability and is
  offered without the development switch only once it is listed in `extension_classes.RELEASES` with the G-6 evidence,
  the internal and the independent review; `RELEASES` is empty. Every grant is refused (`require_supported`): there is
  no network or folder grant path for extension tools yet (default no access). Planned shape: folders only through
  `orkexa.fs` on the host and a `FolderGrant` (never a mount), network only to granted hosts through the egress
  guard.
- Packages are told apart by distribution, entry point name and `module:attribute`; two that cannot be told apart
  are both `invalid`. Listing runs one container per package, one after the other (up to 60 s each) while the
  workspace opens with the switch on. Descriptions lose control and bidi characters but stay untrusted text that a
  user interface must escape.
- Residual risk R1 (provisionally accepted by the owner for internal development, see below): a complete,
  self-consistent forged copy of `nodestro-integrations` is imported only when it replaces the genuine files in the
  runtime's own installation directory without any installer recording it (a foreign package installed there that
  lists them is refused). A copy anywhere else on `sys.path` is refused. Writing files there by hand needs the same
  write access as replacing `nodestro_runtime` itself; release-pinned file hashes are not used (owner decision).
  This holds only while no foreign package is installed into the host environment at all (see "The boundary"
  below); the data-only extension store (RT-098) is a binding G-6 prerequisite.
- Residual risk R2: a module `nodestro_integrations` that host code imported normally before `discover()` is accepted
  when its real path is the verified one (its bytes were not loaded through the verified finder). Only host code can
  cause that import; no extension can.
- Residual risk: whoever can write into the backend's Python environment can already run code there at every start
  (for example through a `.pth` file) or change a trusted file between its check and its import. Installing a package
  is a trust decision of whoever controls that environment; the rules above protect against a package's tools, its
  import-time code and a package that claims to be Nodestro's own, not against a compromised environment.

## Trust boundary of the host import (R1)

**The boundary.** The host Python environment must contain only the shipped lock; the products have no pip.
Installing any foreign package there (an extension package included) is outside the trust boundary, and the checks
below cannot detect it reliably: a foreign package's code can run before them and change them (review NEW-4, for
example a new module such as `winreg.py` that a standard module imports when it exists). The fix decided by the
owner is the data-only extension store (foreign packages kept as files outside every import path and only ever handed
to the sandbox, never installed into the host environment); it is built (RT-098, [above](#the-data-only-extension-store-rt-098))
and must pass its independent review before any tool class is unlocked under G-6. Discovering extension packages from
the host environment (`extensions.find_packages`) only reports and refuses them (architecture decision above).

Owner decision on R1: the residual risk is accepted provisionally for internal development only if the Python
installation can be changed exclusively by trusted people and processes; user-writable search paths, working
directories and foreign package installs must not replace the trusted integration unnoticed. Owner decision of
2026-11-27 (binding, see [Architecture decision](#architecture-decision-owner-2026-11-27-binding)): harden now; the
data-only extension store follows as its own work package, before any G-6 unlock. The boundary is the one above: the host Python environment holds only the shipped lock, and foreign
packages are never installed into it. The "unnoticed replacement" clause is met by that boundary, not by the
in-process checks below. This is no G-6 release.
The runtime enforces the part it can check and refuses the host import otherwise (`invalid`, with the reason, in
`Extensions.packages_status()`; OCR is then unavailable, nothing else changes):

| Path into the host process | How it is closed | Check (`tests/test_trusted_integrations.py`) |
|---|---|---|
| a forged copy ahead on `sys.path` (`PYTHONPATH`, working directory, script directory, a `.pth` entry) | only the copy in the runtime's own directory is accepted | `::test_a_forged_self_consistent_copy_ahead_on_the_path_is_refused`, `::test_a_verified_copy_away_from_the_running_runtime_is_refused` |
| the user's site-packages (`~/.local`, `pip install --user`, its `.pth` files and `usercustomize`) | refused while it is on `sys.path` (off with `-I`, `-s` and in a virtual environment without system site packages) | `::test_the_user_site_packages_on_the_path_is_a_problem` |
| an entry, or the installation, in the data directory (where approved Python environments, tool images and uploads live) | refused | `::test_the_data_directory_on_the_path_is_a_problem`, `::test_the_workspace_hands_its_data_directory_to_the_checks` |
| an entry or a parent directory writable by every user (sticky parents like `/tmp` excepted), owned by another user than root and this one, or writable by a group other than root's (gid 0) and this process's own, also when root owns it (`root:staff 0775` is refused, review M1; macOS's `admin` group is not exempted: its members need a password to become root but can write such a folder directly); the working directory for an empty or relative entry | refused | `::test_folders_writable_by_every_user_are_a_problem`, `::test_folders_of_another_user_or_a_foreign_group_are_a_problem`, `::test_a_folder_of_root_writable_by_another_group_is_a_problem` |
| a symbolic link on the way to an entry, also several in a row, in a folder another user can write or owned by another user (they could repoint it) | the entry is resolved link by link (`os.readlink`); every folder that holds one of the links, and its parents, is checked like an entry's parents, and so is the link's owner; more than 40 links are refused (reviews L1, N4) | `::test_the_folder_holding_a_symbolic_link_on_the_path_is_checked`, `::test_every_link_on_the_way_to_an_entry_is_checked` |
| a foreign package that replaces or adds to the trusted files, also from its wheel's `.data` folder or from another site directory with `..` (split `lib` and `lib64` layouts; pip and uv list every such file in that distribution's `RECORD`, relative to its folder) | the `RECORD` of every `.dist-info` in every site directory and every folder of `sys.path` is read; a path that names a file in the trusted package or its metadata folder is refused. Paths are compared by file identity (device and inode), so another case on a case-insensitive file system, a symbolic link or `..` count. A `.dist-info` without a readable `RECORD`, a second metadata folder of the trusted name next to the runtime and a legacy installation (`.egg-info`, `.egg`, `.egg-link`) are refused too, and so is a distribution that is not a folder of files (a zip archive) (reviews H1, N2, N3, N5) | `::test_a_foreign_distribution_that_replaced_the_trusted_files_is_refused`, `::test_a_foreign_wheel_installed_by_pip_over_the_trusted_files_is_refused` (real pip), `::test_records_of_every_folder_of_the_path_are_read`, `::test_another_name_of_the_trusted_folder_counts`, `::test_a_distribution_in_a_zip_archive_is_refused_cleanly`, `::test_every_installation_check_names_its_problem`, `::test_the_genuine_installation_next_to_other_distributions_is_imported` |
| a `.pth` file in a site directory (the interpreter reads each at start, decodes it with or without a byte order mark, splits it with `str.splitlines`, so also at `\r`, form feed, U+0085 or U+2028, runs every line that starts with `import` and puts every other one on `sys.path`) | refused unless its exact bytes are on the allowlist `KNOWN_PTH` (SHA-256), which holds only setuptools' `distutils-precedence.pth`. Content is not judged: a `.pth` that only names a folder is refused too (review N1) | `::test_a_pth_file_is_refused_however_its_code_is_hidden` (five variants the interpreter runs, checked with `site.addsitedir`), `::test_every_installation_check_names_its_problem[pth-code]`, `[pth-folder]`, `::test_pth_files_of_the_site_directories_are_checked_too` |
| a foreign package that replaces any other module the host imports (the runtime's own, a dependency such as orkexa or pydantic), from its wheel's `.data` folder | refused when one file is named by the `RECORD`s of two distributions (compared by file identity), and when any `RECORD` but the owner's names a file in the running runtime's package or metadata folder or in the trusted integration's (review NEW-1) | `::test_a_foreign_wheel_that_replaces_a_dependency_refuses_the_trusted_import`, `::test_a_foreign_wheel_that_replaces_the_runtime_s_checks_is_named` (real pip), control `::test_genuine_wheels_installed_by_pip_have_no_problem` |
| a `sitecustomize` or `usercustomize` module (imported by `site` at every start), for example a wheel that ships only a top-level `sitecustomize.py` | refused when one is loaded (`sys.modules`) or a search of `sys.path` finds one, unless its exact bytes are on `KNOWN_STARTUP_MODULES` (empty: the products have none); a package or a module without source is refused (review NEW-2) | `::test_a_startup_module_on_the_path_is_refused` (real pip), `::test_a_loaded_or_unreadable_startup_module_is_refused` |
| a sealed installation (the process cannot write it) with an entry the process can write (its data volume as working directory, a writable `PYTHONPATH` entry) | refused | `::test_a_writable_entry_of_a_sealed_installation_is_a_problem` |
| permissions that cannot be read (Windows: ACLs) | refused: not proven | `::test_without_readable_permissions_the_host_import_is_refused` |
| `sitecustomize`, `PYTHONHOME`, `PYTHONSTARTUP` | `sitecustomize` and `PYTHONHOME` act through `sys.path` entries (checked above); `PYTHONSTARTUP` runs only in an interactive interpreter | as above |

The checks run when the workspace opens, after the interpreter started: code that a writable location already ran at
start (a `.pth` file, `sitecustomize`) is not undone, but the trusted import is refused and the reason is shown.
Because every `.pth` file outside the allowlist refuses the trusted import, these environments refuse it too (OCR is
then unavailable, with the reason shown): a virtual environment made by uv or virtualenv (`_virtualenv.pth`),
coverage.py 7.13 or later and pytest-cov (`a1_coverage.pth`, `pytest-cov.pth`), editable installs
(`__editable__.*.pth`, hatchling's `_editable_impl_<name>.pth`), setuptools namespace packages (`*-nspkg.pth`) and distribution
Pythons that add one (Debian's `apt-dist-packages.pth`). The products need none: the images make their environment
with `python -m venv` and the desktop installs into its own interpreter, and the full studio lock (95 packages)
installed that way has no `.pth` file and no legacy installation (checked 2026-11-27; the trusted import is then
accepted). A studio development environment with an editable studio refuses OCR; the studio's components jobs run
the integrations suite with the studio installed as a wheel for that step.
No layout needed a lock of its own: purelib and platlib (`lib` and `lib64`) are both on `sys.path` and both are read.

**What these checks cannot do.** They run inside the process whose environment they check. A foreign package
installed into that environment has already won before they run: a replaced `nodestro_runtime/trusted_integrations.py`
(or any module imported before it, also a new module a standard module imports only when it exists, review NEW-4) does the checking itself and says what its author wants, and a `.pth` file or a
`sitecustomize` ran its code at interpreter start. The checks turn installer-recorded and start-up changes into a
visible refusal while the checking code is still the genuine one; they are not a boundary against a foreign package
in the host environment. That boundary is R1's condition: only trusted people and processes change the installation,
and installing a foreign extension package into it is such an untrusted change. A Python whose standard library
ships a `sitecustomize` (some distribution and sandbox builds) refuses the trusted import too.
Approved Python environments of workflows and tools are separate virtual environments in `<data>/python/envs` (or
container images), created without system site packages; local MCP servers and extension tools run only in
containers; nothing in Nodestro installs into its own environment (the products ship without pip).

Consequences: on Windows the trusted host import is locked, so OCR (`tesseract-ocr` of `nodestro-integrations`) is
unavailable there until the installation's ACLs are checked (no Windows product ships yet). A Python whose user
site-packages directory exists and is on `sys.path` (a non-virtual-environment interpreter, for example a CI runner
after a `pip install --user`) refuses the trusted import too; virtual environments, `-I` and `-s` are not affected.

Per product (as of this change):

| Product | Proven | Assumed |
|---|---|---|
| Desktop (Linux x86_64 archive) | the shell starts `backend/python/bin/python3 -I -m nodestro_studio` (`app/src/backend.rs`, `backend_command`): no `PYTHONPATH`, `PYTHONHOME`, user site-packages, working or script directory; the bundle has no pip; the runtime refuses the host import on an open path | the unpacked archive belongs to the person's account, so every process of that account can change it, as it can change the desktop executable and `nodestro_runtime`; host-run code of the person's own (trusted tools without Docker) runs as that account. The account is the boundary. `NODESTRO_BACKEND` replaces the backend for development. macOS and Windows are not built (Windows is refused, see above) |
| Nodestro Local Docker (image target `local`) | `/opt/nodestro` belongs to root, the process runs as uid 10001 (`image/Dockerfile` of nodestro-server), the entry point is the `nodestro-local` console script (script directory `/opt/nodestro/venv/bin`, not the working directory `/var/lib/nodestro`), the virtual environment has no system site packages (user site-packages off), no pip; a writable entry of this sealed installation refuses the host import | whoever builds or runs the image (compose files, `PYTHONPATH` in an override) is trusted; NOT tested end to end inside the image yet (the runtime check runs at every start and would lock OCR, visibly) |
| Server profile (image target `server`) | as Local Docker (`nodestro-server` console script, uid 10001) | as Local Docker |
| Studio development (`pip install -r platform/internal.txt`) | an editable runtime or integration is refused; the same path checks apply | the developer's virtual environment belongs to the developer and holds whatever they install; not a product |

## G-6: evidence before a tool class is enabled

A class is a combination of `access` × `reach` (for example read + internal). Enabling one by default needs every
item below passed for that class on the real engines (Linux CI Docker, Docker Desktop on macOS and Windows), an
internal review and the independent review (gate G-6). Items marked with a test name are covered now; the others are
open.

Mandatory prerequisite for every class (owner decision 2026-11-27): the data-only extension store, built to the
acceptance criteria in [Architecture decision](#architecture-decision-owner-2026-11-27-binding) and passed by its
independent security review, so that no foreign package is installed into the host environment. State (Welle 7): the
store is built in the runtime (items 25 to 29); its admin routes, client methods and user interface are in review in
nodestro-api, nodestro-sdk and nodestro-studio (branch `claude/w7-ext-store`); the first independent review of the
runtime part passed with findings F1 to F6, all fixed with tests, and its re-review is open, so foreign extension tools stay locked
by default and no class is unlocked. Residual risk carried into G-6: in-process checks cannot catch a foreign package
already in the host environment; the boundary is the environment (only the shipped lock, no pip in the products).

| # | Check | Expected | Covered by |
|---|---|---|---|
| 0 | a foreign package installed in the host environment, switch on or off | refused (`invalid`, "installed in the host environment"): never offered, imported, read or sent to the sandbox | `test_extension_isolation.py::test_a_foreign_package_in_the_host_environment_is_refused` |
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
| 16 | run cancelled during a call; host process killed during a call | container removed; a left-over container removed by the reaper (all of the installation's at start, expired ones by deadline) | Docker: `::test_a_cancelled_run_removes_the_container`, `::test_a_container_left_by_a_crashed_host_is_reaped` |
| 17 | network grant for a class with `reach=external` | only the granted host, through the egress guard | NOT implemented. Default no access proven (item 9); every grant refused: `::test_no_tool_class_is_released_and_no_grant_exists`. Classes that depend on it and stay locked while it is missing: `read`/`external` and `write`/`external` (network clients: tools that call web services or APIs), `extension_classes.PREREQUISITES`; a release of them is refused (`test_extension_store.py::test_classes_whose_prerequisite_is_missing_cannot_be_released`) |
| 18 | folder grant for a class that reads or writes files | only through `orkexa.fs` and a grant | NOT implemented. Default no access proven (items 10, 11); every grant refused (as 17). Classes that depend on it and stay locked while it is missing: `write`/`internal` (file writers: converters and generators that save into a granted folder) and `write`/`external`; a file reader (`read`/`internal` with a folder grant) needs it too, because `require_supported` refuses every folder grant until it exists. Only `read`/`internal` without any grant (isolated computation on the call's arguments) does not depend on it |
| 19 | Docker Desktop (macOS, Windows) | items 5 to 12 pass there | NOT tested, evidence missing: no CI runner offers Docker Desktop (licensing, nested virtualization), and no real machine was available in Welle 7. Orkexa's experimental colima macOS job covers only Orkexa's own sandbox and is no Docker Desktop acceptance. Needs a manual run of the `docker` job's tests on real machines before a G-6 release that includes these platforms |
| 20 | own seccomp profile, user namespace remapping | profile shipped, applied and proven by a probe; remapping reported, not required | `::test_without_the_seccomp_profile_applied_the_tools_stay_locked`, `::test_every_extension_container_runs_under_the_seccomp_profile`, `::test_the_extension_seccomp_profile_is_stricter_than_the_default`, Docker: `::test_the_extension_seccomp_profile_is_applied_by_the_real_engine`, gateway: `test_sandbox_gateway.py::test_only_the_extension_seccomp_profile_is_accepted_as_a_seccomp_option`; Orkexa `test_docker_host.py::test_user_namespace_remapping_is_reported` |
| 21 | dependencies of a package | built as a hashed image like tool environments, never in the host | NOT implemented. The runtime image is hashed and offline at run time; the store refuses every package with a dependency the sandbox does not provide (`PROVIDED`: `nodestro-runtime`, `orkexa`, `pydantic`, `typing-extensions`), `test_extension_store.py::test_an_unsafe_or_inconsistent_wheel_is_refused_before_anything_is_written[a dependency]`. Depends on it, in every class: any package with third-party libraries (parsers, SDK clients, data libraries); it cannot even be installed until the dependency image exists |
| 22 | per-class enabling and the trust exception (WZ-8) | designed, reviewed | designed and locked (`extension_classes.py`, `RELEASES` empty): `::test_a_class_counts_with_its_capability_only_once_released`; review open; WZ-8 not implemented on purpose |
| 23 | a forged, self-consistent copy of the trusted integration ahead on the path (R1), an open import path | refused (only the copy next to the runtime; closed import path) | `test_trusted_integrations.py::test_a_forged_self_consistent_copy_ahead_on_the_path_is_refused` and the path checks above; replacing the copy next to the runtime is the accepted residual |
| 24 | trust checks on Windows and macOS paths (short names, case, `/private`) | same verdicts | Platforms workflow: `test_trusted_integrations.py` |
| 25 | intake into the data-only store: size, hash, origin, zip safety, links, devices, traversal, `RECORD`, manifest, dependencies, `.pth`/`.data`/scripts/native code | refused before anything is written; audited | `test_extension_store.py::test_an_unsafe_or_inconsistent_wheel_is_refused_before_anything_is_written`, `::test_the_expected_hash_and_the_origin_are_required`, `::test_intake_removal_and_revocation_are_in_the_activity_log` |
| 26 | an intake interrupted at any point | no partial package is ever valid; left-overs removed at the next open | `test_extension_store.py::test_an_interrupted_intake_never_leaves_a_valid_package` |
| 27 | a stored package changed after intake (file, `RECORD`, added or removed file, symbolic or hard link) | `invalid`, nothing sent to a container | `test_extension_store.py::test_a_package_changed_after_intake_is_never_handed_to_the_sandbox` |
| 28 | removal, revocation, update while tools are listed | no later call starts a container; a revoked hash never comes back | `test_extension_store.py::test_a_removed_revoked_or_replaced_package_never_runs_again`, `::test_a_revoked_hash_is_never_installed_again` |
| 29 | host modules and the store from inside the container | the host's modules (workspace, vault, store) cannot be imported; no host folder, data directory or store on the container's import path | Docker: `::test_host_modules_are_not_reachable`, `::test_host_files_and_secrets_are_not_visible` (also the runtime's package folder and the store) |
| 30 | Podman (rootful and rootless) with the extension seccomp profile | items 5 to 12 and 20 pass there | NOT tested, evidence missing: no Podman engine in CI and none was available in Welle 7; Podman is not offered by the products today. Required before a G-6 release that names Podman |
| 31 | the store's independent security review | passed | open: a review of the runtime part in Welle 7 passed with findings (F1 to F6, fixed with tests), its re-review is open; the G-6 review itself is prepared, not run (needs the owner's separate consent) |

The checks with "Docker:" run in the CI job `docker` of this repository (`NODESTRO_REQUIRE_DOCKER=1`: a daemon that
does not pass Orkexa's preflight fails them instead of skipping them).

State of the catalog (Welle 7). Proven on Linux CI Docker: 0 to 16, 20, 22 (designed and locked), 23 to 29. Not
implemented, and every class that depends on them stays locked: 17 (network clients), 18 (file readers and writers),
21 (packages with their own dependencies). Evidence missing on real platforms: 19 (Docker Desktop on macOS and
Windows), 30 (Podman); no simulated or substitute run counts as their acceptance. Open reviews: the internal review of
the store and the independent G-6 review (31). The only class without a missing item is `read`/`internal` without
any grant; it too stays locked (`RELEASES` empty) until 19 or 30 is proven for the platforms its release names, and
both reviews passed.
