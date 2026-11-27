# Exchange formats (N-14)

Nodestro moves configuration and knowledge between workspaces as versioned files: `nodestro_runtime.exchange`
(with `nodestro_runtime.mcp.exchange` for `nodestro-mcp/1`, next to the MCP client) writes and reads them,
nodestro-api serves them (`docs/exchange.md` there). The formats are product-neutral: they hold
no product name and work on the runtime's own services (`McpService`, `ConnectionService`, `KnowledgeBase`).

| Format | File | Content |
| --- | --- | --- |
| `nodestro-mcp/1` | JSON | MCP services: name, transport, address or stdio command (packages, module, arguments, memory), kind of sign-in (`none`, `bearer`, `oauth`), loopback consent, time and output limits |
| `nodestro-connection/1` | JSON | Service connections: type, provider, name, description, settings |
| `nodestro-collection/1` | ZIP | One knowledge collection: documents, retrieval settings, embedding identity |
| `nodestro-workflow/1` | JSON | A workflow export (`format` is optional; the workflow import checks it) |
| `nodestro-agent/1` | JSON | An agent template (nodestro-api, unchanged) |
| `nodestro-backup` v1 | ZIP | A complete backup (nodestro-api, unchanged) |

Every file names its format and version in one field, `"format": "<name>/<version>"`, next to `created_at` and
`producer` (`nodestro-runtime <version>`). A version changes only when an older reader would misread a file; a
reader refuses a newer version instead of guessing (`exchange.version_newer` with `version` and `supported`).

## What never travels

- **Secrets.** The MCP and connection formats have no field for a token, a sign-in or a connection secret, and no id,
  so a secret of this workspace can never be attached to an imported entry (the binding rules of `nodestro-backup`
  `_keepable` are not even needed: an import always creates new entries). An export is checked before it is written
  (`ensure_no_secrets`): a non-empty field with a secret-like name (`token`, `secret`, `password`, `api_key`, ...), an
  address with credentials or a key in it (`has_credentials`, `url_secret_problem`), a value shaped like a well-known
  key (`sk-...`, `ghp_...`, `AKIA...`, a private key block, `Bearer ...`), or **any secret the workspace holds for one
  of the exported entries** (the opened token or connection secret, every longer text of stored OAuth credentials,
  the sealed value) refuses the export with `exchange.secret_detected`; `places` names the fields, never the values.
- **State of this workspace.** Ids, `enabled`, tool approvals and discovered tools, OAuth state, check results, the
  reachable hosts of a connection (derived again from type, provider and settings), audit events, provenance of
  knowledge files and vectors.

## The shared import check

Every import runs the same steps, in this order, before anything is stored:

1. **Size** before parsing: a JSON file at most 1 MB (`MAX_JSON_BYTES`); a collection file at most 256 MiB
   (`MAX_COLLECTION_BYTES`) with its manifest at most 2 MiB.
2. **Syntax**: valid JSON (also nesting deeper than the parser's limit is `exchange.invalid_json`), an object.
3. **Format and version** (`format_version`): `exchange.format_invalid` (not an exchange file),
   `exchange.format_mismatch` (another format), `exchange.version_newer`.
4. **Content** against strict models: unknown fields anywhere are refused (`exchange.content_invalid`, with
   `problems` as field names and reasons, never values), every value within the limits of the stored models, at most
   100 entries per JSON file (`MAX_ITEMS`).
5. **Archive** (collections only, `open_collection`): at most 501 entries (checked in the ZIP end record before the
   archive is indexed, `exchange.too_many_entries`); only `manifest.json` and `documents/<nnnn>` (`exchange.unsafe_path`
   for absolute paths, `..`, backslashes, drive letters, links and duplicates; `exchange.unexpected_file` for any other
   name); no encrypted entries or other compression methods (`exchange.zip_invalid`); an entry over 1 MiB may expand
   at most 100 times (`exchange.zip_bomb`); at most 512 MiB unpacked and 50 MiB per document (`exchange.too_large`);
   the files must be exactly the manifest's (`exchange.incomplete`), each with its declared size and SHA-256
   (`exchange.checksum`; a read never goes past the declared size).
6. **Rules of the target**: each entry is checked by the rules of adding it by hand. An MCP service by
   `McpService.check_new` (address rules, `mcp.url_secret`, loopback only for this computer, `mcp.too_many_servers`,
   and a stdio server only with the Docker sandbox: `sandbox.isolation_required`); a connection by
   `ConnectionService.check_new` (type and mode, provider, settings, address rules, a unique name, also within the
   file); a document by the upload's own rules when it is stored (conversion, PDF limits).

The result is an `ImportReport`: one `ImportItem` per entry with `status` `ready`, `refused` (with the `code` and
`params` adding it by hand would answer), `created` (with the new `id`) or `exists` (a document whose content the
workspace has already), `needs_secret`, and for the whole file `secrets_to_reenter`, `left_out` and, for a
collection, `collection`, `collection_exists` and `embedding`.

**Preview and apply.** `dry_run=True` runs every step and stores nothing. Applying runs the preview again; if any entry
is refused, nothing is stored (`exchange.items_refused` with `refused` and the `first` code). Otherwise all entries are
stored, and a failure in between removes the ones already stored: all or nothing.

## What an import never switches on

- An MCP service is stored **switched off** (`enabled: false`), without a token and with no tool approved: an
  administrator enters the token or signs in (`secrets_to_reenter`: `mcp:<id>` for `bearer` and `oauth`), discovers
  the tools, approves them and switches the server on. A stdio server needs the Docker sandbox and the approval of its
  hash-pinned packages, as when it is added by hand.
- A connection is stored without a secret (`connection:<id>` when its type and provider need one) and unchecked.
- A collection never changes the workspace's settings (retrieval mode, embedding model, OCR switch). The
  `settings` in its manifest are information only.
- Switches stay as they are: an import adds entries, it never changes `NODESTRO_*` settings, unattended MCP access,
  tool approvals or access grants.

## `nodestro-collection/1`

```text
manifest.json        {"format": "nodestro-collection/1", "created_at", "producer", "name",
                      "settings": {"retrieval_mode", "embedding_model"},
                      "embedding": {"provider", "model", "dimensions", "digest", ...} | null,
                      "documents": [{"path": "documents/0001", "name", "media_type", "description",
                                     "size", "sha256", "source_format": "docx" | "xlsx" | null, "notes"}]}
documents/0001       the stored bytes of the document (for a converted DOCX or XLSX: its Markdown text)
```

- **Documents** are the stored bytes, so a round trip gives the same files (name, type, description, size, SHA-256,
  conversion notes). A converted DOCX or XLSX keeps its name and is stored as its Markdown again
  (`KnowledgeBase.add(..., converted_from=...)`).
- **Embedding identity.** The manifest names the identity of the source index's vectors (`EmbeddingIdentity`), never
  the vectors. An import indexes every document again in this workspace: in full text, and with this workspace's own
  embedding model when its semantic index is on. `embedding.matches` says whether both identities are the same model
  build; when they differ, nothing else is needed (the documents are embedded here anyway), and the knowledge index
  status shows whether the workspace's own index needs a reindex.
- **Target.** The documents go to a new collection: the manifest's name or the one given; a collection that has files
  already is refused (`exchange.collection_exists`). A document whose content the workspace has already stays as it
  is (`exists`): the file store keeps one entry per content, so storing it again would rename or move that file.

## Limits

| Limit | Value |
| --- | --- |
| JSON exchange file | 1 MB, 100 entries |
| Collection file | 256 MiB, 500 documents, 512 MiB unpacked, 50 MiB per document, 2 MiB manifest |
| Expansion of one ZIP entry over 1 MiB | 100 times |

## Error codes

`exchange.format_invalid`, `exchange.format_mismatch`, `exchange.version_newer`, `exchange.too_large`,
`exchange.invalid_json`, `exchange.content_invalid`, `exchange.items_refused`, `exchange.secret_detected`,
`exchange.zip_invalid`, `exchange.too_many_entries`, `exchange.unsafe_path`, `exchange.unexpected_file`,
`exchange.zip_bomb`, `exchange.incomplete`, `exchange.checksum`, `exchange.collection_not_found`,
`exchange.collection_exists`, `exchange.collection_name_invalid`; and per entry the codes of the target's own rules
(`mcp.*`, `connection.*`, `sandbox.isolation_required`).
