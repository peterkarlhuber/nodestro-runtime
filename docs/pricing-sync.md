# Price sync: sources and their integrity

`nodestro_runtime.pricing_sync` fetches reference prices (`PricingSyncService` with `default_sources()`). A server
switches the sources with the API's settings `NODESTRO_PRICE_SYNC`, `NODESTRO_PRICE_LITELLM` and
`NODESTRO_PRICE_SNAPSHOT_FILE` (nodestro-api README, "Model prices"); the desktop uses the same defaults.

| Source | Network | Integrity |
|---|---|---|
| `OrkexaBuiltinSource` | none | ships with the pinned Orkexa (verified prices) |
| `SnapshotFileSource` | none | a reviewed snapshot file the operator supplies |
| `LiteLLMSource` | one HTTPS download | fixed commit and SHA-256 (below) |

## LiteLLM's cost map (MUP-ONA A7, VB-1)

- **Pinned source.** The map is read from LiteLLM's repository at a fixed commit (`LITELLM_COMMIT`, the URL is
  `LITELLM_URL`), not from `main`. Content at a commit does not change.
- **Checksum.** The download must have the SHA-256 shipped with the release (`LITELLM_SHA256`). Any other file is not
  imported: the source reports an error, a warning is logged ("... not imported"), and the last valid prices stay
  active. Every price is still validated (`validate_price` through `PricingService.submit`); unverified LiteLLM prices
  never replace a verified one.
- **No redirects.** Downloads are HTTPS only, from `raw.githubusercontent.com`, and never follow a redirect (a 3xx
  answer is an error), with a timeout, a size limit and bounded retries.
- **Why a pin and not only a configurable checksum.** A checksum alone on the moving `main` file would fail with
  every upstream change; a pin alone could not detect a changed file. The pair is fixed in the code, so there is no
  setting to get wrong and nothing to configure. In code, `LiteLLMSource(url=..., sha256=...)` takes another pair;
  another URL without its own checksum is refused.
- **Newer prices.** A runtime release moves the pin (both values, reviewed like a dependency pin). An operator who
  needs a newer price before that supplies a reviewed snapshot file (`NODESTRO_PRICE_SNAPSHOT_FILE`) or turns the
  LiteLLM source off (`NODESTRO_PRICE_LITELLM=off`).

Moving the pin:

```bash
commit=$(git ls-remote https://github.com/BerriAI/litellm refs/heads/main | cut -f1)
curl -sSf "https://raw.githubusercontent.com/BerriAI/litellm/$commit/model_prices_and_context_window.json" | sha256sum
```

Then set `LITELLM_COMMIT` and `LITELLM_SHA256` in `pricing_sync.py` and review the price changes the next sync shows.
