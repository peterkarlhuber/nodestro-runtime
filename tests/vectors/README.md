# Shared security test vectors

`security-vectors.json` is the canonical corpus of security test vectors that Nodestro and Axentra both run
(MUP-ONA MP-A8, finding VB-7). Egress, ID token and redaction checks exist twice (Nodestro: `mcp/netguard.py`,
`sanitize.py`, nodestro-api `identity.py`; Axentra: `events/http_client.py`, `identity/oidc.py`,
`core/redaction.py`). The corpus makes both answer the same questions; every place where they answer differently is
either fixed or listed with its reason in `known_differences`. No code or test dependency between the platforms
arises: each consumer keeps its own checksummed copy.

## Consumers

| Repository | File | Test | What it runs |
|---|---|---|---|
| nodestro-runtime (canonical) | `tests/vectors/security-vectors.json` | `tests/test_security_vectors.py` (T-VEC-1) | `egress` against `netguard`, `redaction` against `sanitize.redact_secrets` |
| axentra (copy) | `tests/vectors/security-vectors.json`, `tests/vectors/SOURCE.md` | `tests/test_security_vectors.py` (T-VEC-2) | `egress` against the webhook check, `jwt` against `OidcClient.verify`, `redaction` against `redact` |
| nodestro-api (pinned copy) | `tests/vectors/security-vectors.json`, `tests/vectors/SOURCE.md` | `tests/test_security_vectors_jwt.py` (T-VEC-3) | `jwt` against `OidcClient.verify` |
| nodestro-runtime | (Axentra's copy, checked out) | `tests/test_security_vectors_sync.py` (T-VEC-4) | Axentra's copy and its `SOURCE.md` equal the canonical file |

The runtime has no ID token check of its own, so it does not run the `jwt` vectors; nodestro-api does. Axentra's and
nodestro-api's tests first check that their copy has the SHA-256 and version that their `SOURCE.md` records. Only the
runtime compares Axentra's copy with the canonical file (Axentra has no reference to Nodestro): the CI job
`security-vectors-sync` checks out axentra's `master` anonymously (public repository, read-only token, no secret)
and runs `tests/test_security_vectors_sync.py` with `NODESTRO_AXENTRA_SRC` and `NODESTRO_REQUIRE_VECTOR_SYNC=1`; a
missing checkout fails the job. In the main test matrix the comparison is skipped with the reason listed in
`tests/ci/skipped-test.txt`. Locally:

```bash
NODESTRO_AXENTRA_SRC=../axentra pytest tests/test_security_vectors_sync.py
```

## Format

- `version` (integer): raised with every change of the file.
- `egress[]`: `{id, address | url, resolves_to?, expect: "block" | "allow", note}`. An `address` is an IP literal
  judged by the address check alone (`netguard.address_problem`, Axentra `_blocked`). A `url` is a target as it is
  configured (an MCP server address, a webhook URL in production): the static URL checks, then the address check of
  every DNS answer. `resolves_to` lists the answers of a fake resolver (no network); a host without it that is no IP
  literal resolves the way the C library reads numeric hosts (`inet_aton`: `2130706433`, `0x7f.1`, `0177.0.0.1`,
  `127.1`).
- `jwt[]`: `{id, header, claims, key_ref, tamper?, expect: "accept" | "reject"}`. One ID token for `jwt_context`
  (issuer, client id, nonce, leeway, maximum age). Claim values `@issuer`, `@client_id` and `@nonce` stand for the
  context values, `@now`, `@now+N` and `@now-N` for the current Unix time plus or minus N seconds. `key_ref` names a
  key the test generates (`jwt_context.key_refs`; never a real key): `rsa-a`, `ec-p256` and `ed25519` are published
  in the provider's key set under their names as `kid`, `rsa-unpublished` is not, `hmac-shared` and
  `hmac-rsa-a-public` sign with HMAC (the latter keyed with rsa-a's public key in PEM form: algorithm confusion),
  `none` leaves the signature empty. `tamper: "signature"` changes one byte of a valid signature.
- `redaction[]`: `{id, input, must_not_contain[]}`. `input` is a string or a JSON object handed to the platform's
  credential redaction; none of the `must_not_contain` strings may appear in the result.
- `known_differences[]`: `{category, id, platform, actual, reason}`. One platform (`nodestro` or `axentra`) judges
  vector `id` differently: `actual` is its verdict (`block`/`allow`, `accept`/`reject`, or `not_redacted`), and
  `reason` says why that is acceptable. The platform's test asserts `actual`, so a difference that disappears fails
  until its entry is removed. A difference that is a security bug is fixed instead (a separate commit with a
  CHANGELOG entry in the affected repository), never listed.

## Changing the corpus

1. Change `security-vectors.json` here and raise `version`. Run `tests/test_security_vectors.py`; a new deviation
   of Nodestro is fixed or gets a `known_differences` entry with its reason.
2. In axentra, in the same period: copy the file unchanged to `tests/vectors/security-vectors.json`, update
   `tests/vectors/SOURCE.md` (origin commit, `Version`, `SHA-256` from `sha256sum`), run its
   `tests/test_security_vectors.py` and fix or list Axentra's deviations (a new `known_differences` entry goes into
   the canonical file first). Merge the axentra pull request **before** the runtime one: the runtime's
   `security-vectors-sync` job compares with axentra's `master` and stays red until the copy there is current.
3. In nodestro-api: update its pinned copy and `tests/vectors/SOURCE.md` the same way when the `jwt` vectors or
   `jwt_context` changed (its test reads only its own copy, so it never breaks on a runtime change).
4. Never edit a copy by hand; the copies are byte-identical to a version of the canonical file.
