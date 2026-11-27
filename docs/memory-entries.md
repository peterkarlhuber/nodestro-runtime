# Memory entries (N-24)

Every file of a memory space is an **entry** the space's owner sees, edits and deletes. There is one memory
architecture: the spaces are Orkexa memory namespaces (`SQLiteMemory` on the desktop, `PostgresMemory` on a server),
and the entries come from Orkexa's `MemoryBackend.list_entries`.

## What an entry holds

`MemorySpaces.open(name).entries()` returns `MemoryFile` items:

| Field | Meaning |
|---|---|
| `path` | the file below `/memories` |
| `size` | its size in bytes |
| `updated_at` | when it was last written (UTC) |
| `origin` | who wrote it last: `tool` (an agent's memory tool), `user` (a person, by an explicit action), `import` (the memory seed of an imported project), `null` (unknown: written before origins were kept) |
| `scope` | whose space it is: today always `user` (one person's space); `project` and `agent` are reserved |

A rename keeps the origin; an edit by the person makes it `user`. `MemorySpaceInfo` names the space's `scope` too.

## Remembering by hand

A person remembers something by an explicit action: `BrowsableMemory.write(path, text)` (the HTTP API's
`PUT /api/memory/{space}/file`) writes with the origin `user`. No model tool writes with this origin; an agent's
memory tool writes with `tool`.

## Who may see and change entries

The owner rules of RT-089c and the consents of RT-089d apply unchanged: on a server only the space's owner lists,
reads, edits and deletes its entries; another member (an administrator included) and another tenant get
`memory.space_not_found`, as for a space that does not exist. The desktop's single person uses every space.

## Storage and migration

- SQLite: the column `origin` of `memory_files`; `SQLiteMemory` adds it to an older table when it opens it. Older
  files keep their content and time and list the origin `null`.
- PostgreSQL: migration `0011_memory_origin.sql` adds the nullable column (no rewrite of the table); Orkexa's schema
  version 2. The start check (`require_knowledge_schema`) reports a database without it.
- The desktop import to PostgreSQL (`postgres_import`) carries the origin; backups and "export everything" hold it
  (nodestro-api).
