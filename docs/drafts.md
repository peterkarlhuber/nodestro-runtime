# E-mail and webhook drafts (NAL-A4)

Agents can **prepare** an e-mail or a webhook payload. They cannot send anything: a person opens the draft and sends
it, or checks the connection and sends a test from the studio. Real delivery from workflows is NAL-A5 and stays
locked until the security acceptance (RT-001) and the owner's release.

## Tools

| Tool | Writes | Spec | Binding parameters |
|---|---|---|---|
| `draft_email(to, subject, body_text, body_html, cc, attachments, file_name)` | an `.eml` result file (`message/rfc822`) | write + internal, no network, offline, 20 s, 10 per run | `from_address` (the sender written into the draft) |
| `draft_webhook(payload_json, file_name)` | a `.json` result file | write + internal, no network, offline, 10 s, 10 per run | `payload_schema` (a JSON Schema the payload must match) |

Both store their file with the **top-level run** (`ResultFile.source = "tool"`, `node_id` = the step, `preview` =
recipients, subject, attachment names and sizes, HTML yes/no, size; never the body). Outside a run, in an imported
run or without result files they answer `draft.unavailable`.

## E-mail rules (`mail.py`, ported from Levuvo Mail at `f532039`)

- Addresses are plain `local@domain` (ASCII, no display names, no IP literals); duplicates are kept once; at most 50
  recipients (`to` + `cc`).
- The subject is one line of at most 200 characters; a line break or other control character is refused
  (`mail.subject_invalid`), so no header can be injected.
- HTML (at most 2 MB) is reduced to an allowlist of text elements (paragraphs, headings, lists, tables, emphasis,
  links). Removed: scripts, styles and `style` attributes, event handlers, images, forms, frames, SVG, MathML,
  comments. Links keep only `http`, `https` and `mailto`. A text alternative is generated when `body_text` is empty.
- Attachments are named files of the same run (its input files and the result files written so far); the tool never
  opens a path or an address. At most 10 attachments, 10 MB for the whole message.
- The message is built once (`email.message.EmailMessage`, SMTP line ends) and stored as it is. It carries
  `X-Unsent: 1`, so mail programs that know the header open it as a new message to send.

## Webhook payloads

`payload_json` must be a JSON object or array of finite values, at most 256 KB. The stored bytes are compact JSON with
sorted keys (what a later send would sign). `payload_schema` supports a documented subset of JSON Schema: `type`,
`properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `minLength`, `maxLength`, `minimum`,
`maximum`, `minItems`, `maxItems` (`title`, `description` and `$schema` are ignored). Any other keyword is refused
(`webhook.schema_invalid`), so a schema never looks stricter than it is. Mismatches are reported as JSON pointers
(`/items/0: minimum`), never with values.

## Connections: check and test send (`mail_transport.py`)

Not reachable from tools. `ConnectionService.check` and `ConnectionService.test_send` use it.

- **SMTP:** every resolved address is checked by the shared egress rules and the connection goes to that address;
  TLS 1.2 or newer with certificate and host name verification; `security = "tls"` (implicit) or `"starttls"`
  (required, except for a server on this computer outside the server profile); 20 s per step. The check logs in and
  quits. A test send mails a fixed English text to `settings.test_address` (`connection.test_address_missing`
  without one). OAuth 2.0 is not supported yet (`connection.auth_unsupported`).
- **Webhook:** Orkexa's guarded client (no environment proxies, every address checked, no redirects:
  `connection.redirect_refused`); `https` only, `http` only for loopback outside the server profile, and then only
  that host. The check sends `HEAD`; a test send posts `{"test": true, "event": "nodestro.connection_test", "id",
  "sent_at"}` with the connection's method, signed `Nodestro-Signature: t=<unix time>,v1=<hex HMAC-SHA256(secret,
  "<t>.<body>")>` (`auth = "hmac-sha256"`) or with a bearer token; any 2xx answer succeeds
  (`connection.http_status` otherwise).
- **Limit and audit:** at most 5 test sends per connection and hour; each attempt writes `test_sent` events
  (`outcome` `started`, then `sent` or `failed` with the code) with the acting person. Results and events never
  carry the service's own text, an address or the secret.

## Tests

`tests/test_draft_tools.py` (addresses, header injection, limits, HTML allowlist, the `.eml` file, payload schema,
signature reference value, the static proof that the draft tools cannot send, two workflows) and
`tests/test_mail_transport.py` (local SMTP server with STARTTLS, implicit TLS and login; certificate check; address
rules; local webhook receiver with signature, bearer token, redirect and error answers; limit and audit). Real mail
servers are never contacted by the tests.
