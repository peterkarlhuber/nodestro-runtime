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
- `nodestro_runtime.folder_index`: the search index of a folder grant with `index` (N-12, see "The search index").

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

## The search index (N-12)

A grant with `index` set keeps a search index of the folder (`nodestro_runtime.folder_index`). It lives in the
knowledge base's search index (`KnowledgeBase`), one index source per file (`folder:<folder grant id>/<path>`), and
never in its file store: a folder's files are no knowledge files, no knowledge list shows them, and no knowledge
search returns them (an agent's knowledge view holds only the files assigned to it, a search without files only the
knowledge files). Agents search it with `search_files`: a folder with a ready index is searched there instead of file
by file (the best passages of its text files and PDFs, ranked like the knowledge search, `index: true`, `page` for a
PDF). So every search is a call of the folder tools: the access model decides for the caller at that moment, the
access log names the grant, and the run is marked as holding granted content (N-08, source `folder`).

- **First index and changes.** `FolderIndex.sync` walks the folder through `orkexa.fs.SafeRoot` with the grant's
  patterns, limits, identity and the default excludes, and reads text files (`.txt`, `.md`, `.markdown`, `.csv`,
  `.json`, `.yaml`, `.yml`, `.html`, `.xml`) and PDFs (in the isolated extraction with the grant's page, time and memory
  budgets); other types are left out. At most `max_search_files` files and 10,000 queued folders, at most 400 chunks
  of one file (`MAX_FILE_CHUNKS`, which also bounds how long the index's write lock is held), and at most 50,000
  chunks and 512 MiB of files of one folder (`MAX_FOLDER_CHUNKS`, `MAX_FOLDER_BYTES`); what is over a budget is left
  out and the state says `truncated`.
  A file whose size, modification time or change time differs from the manifest (collection `folder_index_files`) is
  read again; its chunks are replaced only when its SHA-256 changed. Files that are gone, excluded, denied, too large
  or no longer readable lose their chunks. The times are read with `os.stat` without following links, only as a hint
  of a change; the content is read through the path guard.
- **Rights stay on the grant.** Before each file's chunks are stored, the access grant is checked again inside the
  knowledge base's write lock. A revocation (`DELETE /api/folders/{id}` or `DELETE /api/access/grants/{id}`) drops
  the folder's chunks with their vectors and its manifest at once (`Workspace.folder_access_changed`,
  `FolderIndex.drop_ended`); a sync running at that moment stores nothing more. An expiry is found by the indexer's
  sweep (every 30 seconds), also between the files of a long sync of another folder. Until a chunk is gone, the
  folder tools refuse the folder anyway, and every hit of an index search is checked again against the access model
  (the caller's scope and denies) and the path guard after the search, so a search over a revoked folder, also one
  already running, returns nothing. A deny on a subfolder holds at the next
  search and leaves the index at the next sync. Denies that name agents keep a path out of the index for everyone (the
  index serves the grant as a whole; who may search it is checked per call).
- **Reconciliation.** `FolderIndexer` (a background thread on the desktop and in Nodestro Local Docker) runs
  `FolderIndex.reconcile` at the start and every 5 minutes: ended grants and chunks without a grant are dropped,
  every active grant with `index` is synced. A new grant is indexed at once (the API wakes the indexer).
- **Egress (N-08, RT-094; review H-1).** Folder text is granted content, and its rule fails closed whatever the
  other switches: it is embedded only when `KnowledgeBase.folder_embedding_allowed` (the workspace:
  `RunManager.folder_embedding_allowed`) allows the address the embedding model really sends to. A server on this
  computer (`localhost`, loopback; in Local Docker also `ollama`) always may; a server elsewhere only while the
  provenance switch (`NODESTRO_CONTENT_PROVENANCE`, with the access model) is on and a person's grant on
  `cloud_provider` `ollama` for all agents allows shared content (a refusal is audited). Otherwise, also with
  approvals, the access model or provenance off, the text is indexed without vectors (full text finds it), an index
  search does not embed its query (it runs in full text), and a rebuild of the semantic index for such a model removes
  every folder index before it starts and stores no folder chunk while it runs (the indexer rebuilds them without
  vectors at its next run). The rebuild is judged by its model's address alone, also when no folder is indexed yet
  (review H-1b: a first index during the rebuild was embedded), and as a last guard its model refuses every batch
  while the index holds a folder chunk (the rebuild then fails, nothing is sent). When the query may be embedded, an
  index search in vector or hybrid mode also asks the run's check first (`RunServices.embedding_check`), like a
  knowledge search; without a semantic index it searches in full text.
- **Copies.** The copy of the database made before a schema migration (`<data dir>/backups/pre-migration-*.db`)
  holds no folder index (`storage.strip_copy`, review L-b); backups, the safety backup before a restore and the data
  export leave it out in the API (its `docs/backup.md`). The index is built again from the folder while the grant
  holds. Fail closed without blocking (Welle 5): a legacy `vec0` vector table that cannot be cleaned leaves the copy
  with its storage tables and its schema entry (no broken table stays); when a folder's chunks cannot be removed one
  by one, the copy loses its whole search index instead (`storage.drop_search_index`; the knowledge files stay) and
  the migration goes on; copies an older release made before a migration lose their folder index at the next start,
  a copy that cannot be stripped is deleted, and leftovers of an interrupted copy are removed
  (`workspace.strip_migration_copies`).
- **A failed removal before a rebuild.** When removing the folder indexes before a rebuild of the semantic index fails,
  the rebuild does not start (`knowledge.reindex_failed`; it would embed what is left) and nothing stays marked: the
  folder indexes keep working and the next rebuild tries again (Welle 5).
- **Switch.** The index exists only while `NODESTRO_FOLDER_GRANTS=1` and never in the server profile. With the switch
  off, the workspace removes every folder index at its start.

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
- `tests/test_folder_index.py` (N-12): first index, change by time and hash, a touch without a change, deletion, a
  deny, revocation (also during a search and during a sync), expiry, the reconciliation at the start, the switch
  turned off, that knowledge searches never return a folder's chunks, the file and folder budgets, and the sweep
  during a long sync. `tests/test_content_provenance.py`: an index search marks the run; folder text and a marked
  run's query reach an embedding server elsewhere only with a grant, in every combination of the approvals, access
  and provenance switches (review H-1), a server on this computer embeds it in every combination, and a rebuild for a
  server elsewhere drops the folder indexes first, a folder indexed for the first time while such a rebuild runs is
  not embedded in any combination (review H-1b), and the rebuild's model stops at a folder chunk from any route.
  `tests/test_folder_index.py` also covers the copy before a migration, the legacy vector table, a copy that cannot
  be stripped and older copies; `tests/test_content_provenance.py` a failed removal before a rebuild.

## Not in this step

- Indexing DOCX and XLSX files of a granted folder (the knowledge upload converts them; the index reads text and PDF
  files only), and showing a folder index's state (`folder_index_state`) on the folders page.
- An opt-in for files with several hard links.
- Writing to a granted folder.
- On macOS, the access model's system folders include `/private/var`, so a folder below the temporary folder
  (`/private/var/folders`) cannot be granted, although `orkexa.fs` would allow it.

See `docs/TODO.md` RT-093.
