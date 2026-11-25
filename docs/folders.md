# Granted folders (N-10)

A person grants a folder on their computer for reading. Agents then read it with three tools: `list_folder`,
`read_file` and `search_files`. The tools never write, and an agent sees only the folder's name, never its absolute
path.

Everything here is off by default. `NODESTRO_FOLDER_GRANTS=1` switches it on, and activating it is an owner decision
after the independent review. With the switch off, the tools are not offered, the tool library does not register
them, and every write is refused (`folder.disabled`). The tools run only on the desktop and in Nodestro Local Docker
(`ToolSpec.modes`), never in the server profile, because a server has no person's folders.

Modules:

- `nodestro_runtime.folders`: the records and the reader.
- `nodestro_runtime.folder_grants`: the writer, used by the api only.
- `nodestro_runtime.folder_tools`: the tools.

## The grant

A `FolderGrant` (collection `folder_grants`) is an access grant of the access model v2 (N-06, `Grant`, resource
`folder`, action `read`), plus what only a folder needs:

| Field | Meaning |
| --- | --- |
| `name` | The name agents use. 1 to 80 printable characters, no slash, unique among the active grants (case-folded) |
| `path` | The canonical folder: links resolved, and on Windows the final path |
| `identity` | `(device, inode)` or `(volume, file index)` when it was granted. If another folder replaces it, reads are refused (`fs.root_changed`) |
| `access` | Always `read` |
| `include`, `exclude` | Patterns, see below |
| `default_excludes` | `true` (default): the common secret files are left out, see "Patterns". Only an administrator turns it off for one grant |
| `limits` | `FolderLimits`, see below |
| `index` | The person asked for a search index. It is stored only; indexing granted folders is a later step |
| `grant_id` | The access grant that holds duration (`until_revoked`, or `until` with `expires_at`), scope (`all`, `agents`, `conversation`), mode (`supervised`, `unattended`, `both`) and revocation |

There is no second approval logic. Expiry, revocation, scope, mode and the audit (`access_granted`,
`access_grant_revoked`) all belong to the access grant. A folder grant is never deleted; it ends with its access
grant. The access grant and the folder grant are stored in one transaction, and the name is checked inside it, so
two grants of the same name sent at once cannot both be stored.

`FolderGrants(store, data_dir).grant(name=..., path=..., by=Person(...))` is the only writer. Only a `Person`
writes, and no module of the runtime imports the writer (`tests/test_access_grants_gate.py`). The api calls it for
a signed-in administrator. Desktop-to-server imports skip `folder_grants`, like the access grants
(`postgres_import`).

### What can never be granted

A folder is refused with `folder.not_grantable`. The param `reason` says why:

| `reason` | When |
| --- | --- |
| `protected_root` | `orkexa.fs.SafeRoot` refuses it as a root. That covers a drive or file-system root, the home folder or a folder above it, any user's home folder, a system folder, a blocked credential folder (`.ssh`, `.gnupg`, cloud credentials), and Nodestro's data folder or a folder inside it. Links count as their target |
| `network_path` | A UNC or network path |
| `link` | The path itself is a link the guard refuses |
| `not_found`, `not_a_directory` | There is no such folder |
| `invalid_path` | Not an absolute path, or not a path at all |
| `temp_root` | A shared temporary folder itself: `/tmp`, `/var/tmp`, `/private/tmp`, `/private/var/tmp`, `/private/var/folders` and the temporary folder of the process (`tempfile.gettempdir()`). Every program writes there. A folder inside one can be granted |
| `safety_limit` | A global safety limit of the access model applies (`limit:protected_folder`, `limit:unresolvable_path`, the system folders of `tool_policy.SYSTEM_FOLDERS`), checked for both the given and the canonical spelling |

A folder above the data folder can be granted, but the data folder stays closed inside it (`fs.blocked`, by name and
by identity, also after it is moved), and the access model refuses it as a safety limit. Even if a grant record were
written by hand, every call evaluates the safety limits again.

## The tools

| Tool | What it does |
| --- | --- |
| `list_folder(folder="", path="")` | Without a folder, it lists the granted folders' names. With one, it lists the entries of that folder or a subfolder: `name`, `path`, `kind` (`file`, `dir`, `link`, `other`) and `size`; `name` and `path` have bidirectional controls and zero-width characters escaped (`\uXXXX`), so an entry with such a name is shown but cannot be opened. Links are shown but never followed. Files and subfolders that a deny blocks are left out. Without a folder, the call is logged too (`folder.listed` without a grant id) |
| `read_file(folder, path, offset=0)` | Reads the text of a text file or a PDF. It returns `source` (`<folder>/<path>`), `kind`, `size`, `pages` for a PDF, and the text from `offset`. At most `max_chars` characters come per call, and `next_offset` continues |
| `search_files(folder, query, path="")` | Finds files whose name or text contains `query` (1 to 200 characters, case-insensitive). It returns the matches with `name` and `path` (escaped like a listing's: bidirectional controls and zero-width characters as `\uXXXX`), `line` and a snippet, plus `files_searched`, `skipped` (subfolders that could not be listed, with their code; only when there are some) and `truncated`. A snippet is file content and comes between the same random data markers as `read_file`'s text. PDFs are found by name only |

All three tools have `access=read`, `reach=internal` and `source=folder` (`tool_policy.BUILTIN_CAPABILITIES`), so
they stay available in the strict offline mode and to offline agents (N-09). They use category `files`.

| Tool | Time limit | Calls per run |
| --- | --- | --- |
| `list_folder` | 30 s | 100 |
| `read_file` | 90 s | 100 |
| `search_files` | 60 s | 30 |

The text of a file comes back between two marker lines with a random value. The first line says that the content
may contain instructions and is data, not instructions, like web content. The tool descriptions ask the agent to
name the files it used by their source. The run also names them: every file read is the run event
`folder_file_read` (folder name, grant id, relative path, `file_kind`, size).

A refusal is a tool result error `Error: <reason> (<code>)`. The code names the rule:

- `fs.*`: the guard;
- `access.*`: the access model;
- `folder.unknown`, `folder.invalid_query`, `folder.unavailable`: the tools.

A file that is too large says so and asks the agent to request a smaller file or a higher limit from the person
(`fs.too_large`).

## Every call is checked

`FolderReader` (`RunServices.folders`, built per step by the engine) works the same way at every call:

1. It finds the grant by name: the active one, else the newest one that ended. A call on an ended grant is refused
   as `access.missing_grant`, not as an unknown folder.
2. It evaluates the access model (`tool_policy.evaluate`) for the folder and then for the exact subfolder or file.
   The evaluation uses the folder grant's own access grant and every deny:
   - the agent `<workflow id>/<node id>`;
   - mode `unattended` for schedules, batches, benchmarks and API calls;
   - the conversation of the top-level run;
   - the data folder as the protected folder.

   So a revocation, an expiry or a deny on a subfolder applies at the next call, and another folder's grant never
   opens this one. A deny compares names as the path guard does (`orkexa.fs.fold`: case folding, dotless i as i,
   zero-width and other default-ignorable characters dropped), so it holds for every spelling a case-insensitive
   file system may open as the denied folder.
3. It opens the folder through `orkexa.fs.SafeRoot` with the grant's patterns, limits and identity. That means:
   - no links and no junctions, also not as a parent folder;
   - no `..` and no absolute paths;
   - no files with several hard links (`fs.hard_link`; there is no opt-in);
   - no special files;
   - every folder is opened relative to its parent, so a swap during the call is refused (`fs.root_changed`,
     `fs.link`) instead of followed;
   - while a path is opened, the access model is evaluated again for the names the file system stored for each
     component (`SafeRoot(on_open=...)`), not only for the spelling asked for: on NTFS or exFAT `pr\u0131vate`
     (dotless i) opens `private`, and a deny on `private` refuses it (`access.denied`).
4. A read checks the grant again after the file was read, so a grant revoked during a long PDF extraction returns
   nothing. A listing checks every entry, files too, so a deny on one exact file leaves it out. A search reads the
   grants and denies again for every folder it takes from its queue: it checks that folder (a deny added while it
   was queued holds), then every file and subfolder in it before it reads or queues them. Before it returns, it
   checks the grant and every match once more. A subfolder that cannot be listed (`fs.too_large`, replaced or
   removed meanwhile) is skipped and named in `skipped`; a changed granted folder (`fs.root_changed`) still ends the
   search.
5. It writes one event to the activity log (N-13). The event has source `file`, action `folder.listed`,
   `folder.read` or `folder.searched`, resource type `folder_grant` with the grant's id, the agent, the run, the
   outcome (`success`, `denied`, `failure`) and the refusal code. It never names a path or a file.
   `FolderGrants.accesses(id)` returns this access log. A new grant is the event `folder.granted` (the person as
   actor); its code is `default_excludes_off` when an administrator turned the default excludes off.

## Patterns

`include` and `exclude` hold up to 100 patterns each, in the syntax of `orkexa.fs`:

- `*` matches within one name, `**` matches any number of folders, `?` matches one character.
- A pattern is anchored at the granted folder unless it starts with `**/`. So `drafts/**` covers the folder
  `drafts` at the top, and `**/*.tmp` covers every `.tmp` file.
- Names are compared after Unicode NFC normalization and case folding, on every platform.
- An excluded folder covers everything below it, so it cannot be listed, read or searched, and a pattern cannot open
  it again.
- With `include` set, only matching files are read; folders stay visible so their files can be reached.
- An invalid pattern is refused when granting (`folder.invalid_pattern`).

Every grant also excludes the common secret files (`folders.DEFAULT_EXCLUDES`, compared without case): `**/.env*`,
`**/*.pem`, `**/*.key`, `**/id_rsa*`, `**/id_ed25519*`, `**/.npmrc`, `**/.pypirc`, `**/.netrc`, `**/*.kdbx`,
`**/*.p12`, `**/*.pfx`, `**/*.ppk` and `**/credentials*`. They
cannot be listed, read or searched (`fs.excluded`; some are already refused by the guard's credential blocklist,
`fs.blocked`). An administrator can turn this off for one grant with `default_excludes=false`; the guard's
blocklist still holds then.

## Limits

`FolderLimits` is set per grant within fixed bounds:

| Limit | Default | Bounds |
| --- | --- | --- |
| `max_file_bytes` (largest file read or searched) | 10 MiB | 1 B to 50 MiB |
| `max_entries` (entries one listing reads) | 2,000 | up to 10,000 |
| `max_search_files` (files one search reads) | 2,000 | up to 20,000 |
| `max_results` (matches one search returns) | 50 | up to 500 |
| `max_chars` (characters one `read_file` call returns) | 50,000 | 1,000 to 500,000 |
| `pdf_pages` | 200 | up to 500 |
| `pdf_seconds` | 20 | up to 60 |
| `pdf_memory_mb` | 256 | 64 to 1,024 |

A PDF is extracted in an isolated process with these budgets (`orkexa.fs.TextBudget`). A PDF bomb ends with
`fs.too_complex` within its time budget. A search stops after 30 seconds (`SEARCH_SECONDS`), checked before every
folder and every entry, also in a tree without files. It queues at most 10,000 folders (`MAX_PENDING`); deeper ones
are then left out. Either way it returns what it found as `truncated`. Binary files are not read (`fs.unsupported_type`).

## Tests

- `tests/test_folders.py`: the grant rules, the tools, the refusals, the switch, the capability, the PDF budget and
  the source naming in a run.
- The folder grant cases of `orkexa.testing.conformance` (N-63) run through the real tools against a real workspace:
  `tests/folder_support.py` adapts the tools to `FolderAccess`. They cover the attack catalog of
  `orkexa.testing.pathguard` (links out, link swaps during the call, `..`, absolute paths, junctions, hard links,
  Unicode normalization, case, invalid UTF-8), system folders and the data folder, include and exclude patterns, the
  size limit, revocation, expiry, a replaced folder, Unicode names, a person's denies on a file and a subfolder
  in any spelling, the grant's scope and mode, and the access log.
- CI runs `tests/test_folders.py` on Linux, macOS and Windows (`.github/workflows/platforms.yml`).

## Not in this step

- Indexing a granted folder (`index` is stored only).
- An opt-in for files with several hard links.
- Writing to a granted folder.
- On macOS, the access model's system folders include `/private/var`, so a folder below the temporary folder
  (`/private/var/folders`) cannot be granted, although `orkexa.fs` would allow it.

See `docs/TODO.md` RT-093.
