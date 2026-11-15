"""E-mail and webhook drafts (NAL-A4): building a message, never sending one.

Everything here is pure: no network, no files. The draft tools (:mod:`nodestro_runtime.draft_tools`) use it to turn an
agent's request into an ``.eml`` file or a JSON payload stored with the run; a person opens or sends the result
themselves. Sending (the connection check and a person's test send) lives in :mod:`nodestro_runtime.mail_transport`,
which no tool imports.

The rules follow Peter's Levuvo Mail (reviewed at ``f532039``; ported, not copied, owner decision 2026-11-13):

- the MIME message is built exactly once and the same bytes are stored and shown (``send-service.mjs:4-25``);
- attachments are bytes handed in by the caller (the run's own files), never a path or URL the builder would open
  (``disableFileAccess``/``disableUrlAccess``, ``:21-22``);
- a subject is one line, recipients are capped (``send-utils.mjs:119-150``);
- outgoing HTML passes a strict tag allowlist with a 2 MB cap, and a text alternative is generated
  (``html-mail.mjs:1-40``). Nodestro allows no ``style`` attribute and no images at all, so a draft cannot load
  anything when it is opened.

Webhook payloads are JSON checked against an optional schema of a small, documented subset of JSON Schema
(:func:`schema_problems`), and signed for a test send like Axentra's webhooks (own implementation):
``Nodestro-Signature: t=<unix time>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import hashlib
import hmac
import html
from html.parser import HTMLParser
import json
import math
import re
from typing import Any

from .problems import NodestroError
from .schemas import now

#: The most recipients of one draft (``to`` and ``cc`` together).
MAX_RECIPIENTS = 50
MAX_SUBJECT = 200
MAX_TEXT_CHARS = 1_000_000
#: The most bytes of the HTML part before it is cleaned (Levuvo Mail: 2 MB).
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS = 10
#: The most bytes of the whole message, attachments included.
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
#: The most bytes of a webhook payload as JSON.
MAX_PAYLOAD_BYTES = 256 * 1024
#: The most nesting of HTML elements kept; deeper elements are dropped (their text stays).
MAX_HTML_DEPTH = 64

_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ADDRESS = re.compile(rf"^(?=.{{3,254}}$)(?={_LOCAL}@)(?P<local>[^@]{{1,64}})@(?P<domain>{_LABEL}(?:\.{_LABEL})+)$")
_LOCAL_PART = re.compile(rf"^{_LOCAL}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]")
_NAME_UNSAFE = re.compile(r"[^\w\-. ()]+", re.UNICODE)


class DraftError(NodestroError):
    """A draft cannot be built (``mail.*`` or ``webhook.*``); the message names the field, never its content."""


# ---- addresses and headers ----------------------------------------------------------------------------------------


def address_problem(value: str) -> str | None:
    """Why ``value`` is not a plain e-mail address (``user@example.com``), or ``None``. Display names, comments,
    quoted local parts, IP literals and non-ASCII addresses are refused: a draft is not the place to guess."""
    if not isinstance(value, str) or value != value.strip() or not value:
        return "form"
    if _CONTROL.search(value):
        return "control"
    match = _ADDRESS.match(value)
    if match is None or not _LOCAL_PART.match(match["local"]):
        return "form"
    return None


def check_addresses(values: Sequence[str], field_name: str) -> list[str]:
    """``values`` without duplicates (case-insensitive), each a plain address; raises :class:`DraftError`."""
    seen: dict[str, str] = {}
    for index, value in enumerate(values):
        if address_problem(value) is not None:
            raise DraftError(
                "mail.address_invalid",
                f"Entry {index + 1} of {field_name} is not an e-mail address",
                field=field_name,
                index=index,
            )
        seen.setdefault(value.lower(), value)
    return list(seen.values())


def check_subject(value: str) -> str:
    """A one-line subject of at most :data:`MAX_SUBJECT` characters (no line breaks: header injection)."""
    if _CONTROL.search(value):
        raise DraftError("mail.subject_invalid", "The subject must be one line without control characters")
    value = value.strip()
    if len(value) > MAX_SUBJECT:
        raise DraftError(
            "mail.subject_too_long", f"The subject is longer than {MAX_SUBJECT} characters", limit=MAX_SUBJECT
        )
    return value


def attachment_name(name: str) -> str:
    """A file name safe for a ``Content-Disposition`` header: no path, no control or quoting characters."""
    base = (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = _NAME_UNSAFE.sub("_", _CONTROL.sub("", base)).strip(" ._")
    return (base or "attachment")[:120]


# ---- HTML ---------------------------------------------------------------------------------------------------------

_ALLOWED_TAGS = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol",
        "p", "pre", "s", "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
        "u", "ul"
    }
)  # fmt: skip
_VOID = frozenset({"br", "hr"})
#: Elements dropped together with everything inside them.
_DROP_WITH_CONTENT = frozenset(
    {
        "script", "style", "head", "title", "template", "iframe", "frame", "frameset", "object", "embed", "applet",
        "noscript", "noembed", "svg", "math", "textarea", "select", "option", "button", "form", "audio", "video",
        "canvas", "xmp", "plaintext", "noframes"
    }
)  # fmt: skip
_BLOCKS = frozenset(
    {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "tr", "ul", "ol", "hr"}
)
#: The open elements a start tag closes first, innermost first (HTML's implied end tags, simplified).
_IMPLIED_END: dict[str, tuple[str, ...]] = {
    "li": ("li",),
    "p": ("p",),
    "td": ("td", "th"),
    "th": ("td", "th"),
    "tr": ("td", "th", "tr"),
}
_LINK_SCHEMES = ("http://", "https://", "mailto:")
_DIGITS = re.compile(r"^[0-9]{1,3}$")


def _attribute(tag: str, name: str, value: str | None) -> str | None:
    """The cleaned value of an allowed attribute, or ``None`` to drop it."""
    if value is None:
        return None
    if tag == "a" and name == "href":
        compact = _CONTROL.sub("", value).strip()
        compact = re.sub(r"\s", "", compact)
        return compact if compact.lower().startswith(_LINK_SCHEMES) and len(compact) <= 2000 else None
    if tag == "a" and name == "title":
        return _CONTROL.sub(" ", value)[:300]
    if tag in ("td", "th") and name in ("colspan", "rowspan"):
        return value if _DIGITS.match(value) else None
    if tag == "ol" and name == "start":
        return value if _DIGITS.match(value) else None
    return None


class _Cleaner(HTMLParser):
    """Rewrites HTML into the allowlist and collects the text alternative in the same pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.text: list[str] = []
        self.open: list[str] = []
        self.dropping: list[str] = []
        self.links: list[str] = []
        self.too_deep = 0

    # text alternative ----------------------------------------------------------------------------------------------

    def _break(self, count: int = 1) -> None:
        if not self.text:
            return
        tail = "".join(self.text[-4:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < count:
            self.text.append("\n" * (count - have))

    # parser callbacks ------------------------------------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.dropping:
            if tag in _DROP_WITH_CONTENT:
                self.dropping.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            self.dropping.append(tag)
            return
        if tag not in _ALLOWED_TAGS:
            return
        for closes in _IMPLIED_END.get(tag, ()):  # <li>one<li>two, <td>a<td>b<tr>
            if self.open and self.open[-1] == closes:
                self.handle_endtag(closes)
        if tag in _BLOCKS:
            self._break(2 if tag != "tr" else 1)
        if tag == "br":
            self.text.append("\n")
        elif tag == "li":
            self._break(1)
            self.text.append("- ")
        elif tag in ("td", "th") and self.text and not self.text[-1].endswith("\n"):
            self.text.append(" | ")
        kept: list[tuple[str, str]] = []
        href = None
        for name, value in attrs:
            clean = _attribute(tag, name.lower(), value)
            if clean is not None and name.lower() not in {item[0] for item in kept}:
                kept.append((name.lower(), clean))
                if name.lower() == "href":
                    href = clean
        if tag in _VOID:
            self.out.append(f"<{tag}>")
            return
        if len(self.open) >= MAX_HTML_DEPTH:
            self.too_deep += 1
            return
        rendered = "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in kept)
        if tag == "a" and href is not None:
            rendered += ' rel="noopener noreferrer"'
        self.out.append(f"<{tag}{rendered}>")
        self.open.append(tag)
        if tag == "a":
            self.links.append(href or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.dropping:
            return
        if tag in _VOID:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.dropping:
            if tag == self.dropping[-1]:
                self.dropping.pop()
            return
        if tag not in _ALLOWED_TAGS or tag in _VOID:
            return
        if self.too_deep:  # the end of an element dropped for its depth
            self.too_deep -= 1
            return
        if tag not in self.open:
            return
        while self.open:
            current = self.open.pop()
            self.out.append(f"</{current}>")
            if current == "a":
                href = self.links.pop() if self.links else ""
                if href and not href.lower().startswith("mailto:"):
                    self.text.append(f" ({href})")
            if current == tag:
                break
        if tag in _BLOCKS or tag == "li":
            self._break(2 if tag in _BLOCKS and tag not in ("tr",) else 1)

    def handle_data(self, data: str) -> None:
        if self.dropping:
            return
        self.out.append(html.escape(data, quote=False))
        inside_pre = "pre" in self.open
        self.text.append(data if inside_pre else re.sub(r"\s+", " ", data))

    def close(self) -> None:
        super().close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")


@dataclass(frozen=True)
class CleanHtml:
    """Cleaned HTML (a body fragment) and its generated text alternative."""

    html: str
    text: str


def clean_html(markup: str) -> CleanHtml:
    """``markup`` reduced to the allowlist: no scripts, styles, event handlers, images, forms or frames; links only
    ``http``, ``https`` and ``mailto``. Raises :class:`DraftError` above :data:`MAX_HTML_BYTES`."""
    if len(markup.encode("utf-8")) > MAX_HTML_BYTES:
        raise DraftError("mail.html_too_large", "The HTML part is larger than 2 MB", limit=MAX_HTML_BYTES)
    cleaner = _Cleaner()
    cleaner.feed(markup)
    cleaner.close()
    text = re.sub(r"[ \t]+\n", "\n", "".join(cleaner.text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return CleanHtml(html="".join(cleaner.out).strip(), text=text)


def html_document(fragment: str, title: str) -> str:
    """A complete HTML document around a cleaned fragment (UTF-8, no external resources)."""
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f"<title>{html.escape(title, quote=False)}</title></head>\n<body>\n{fragment}\n</body></html>\n"
    )


# ---- e-mail drafts ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    """A file of the run, already read: the builder never opens a path or an address itself."""

    name: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class MailDraft:
    to: tuple[str, ...]
    subject: str
    body_text: str = ""
    body_html: str | None = None
    cc: tuple[str, ...] = ()
    from_address: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class BuiltMail:
    """The message as stored (``data``, built once) and what the run shows about it (no body)."""

    data: bytes
    preview: dict[str, Any] = field(default_factory=dict)


def _media_type(value: str) -> tuple[str, str]:
    """The attachment's type; containers (``multipart/*``, ``message/*``) become plain bytes, so an attachment can
    never be shown as a nested message that skipped the HTML cleaning (review L5)."""
    main, _, sub = (value or "").split(";", 1)[0].strip().lower().partition("/")
    if not re.match(r"^[a-z0-9][a-z0-9.+-]{0,62}$", main) or not re.match(r"^[a-z0-9][a-z0-9.+-]{0,126}$", sub):
        return "application", "octet-stream"
    if main in ("multipart", "message"):
        return "application", "octet-stream"
    return main, sub


def build_mail(draft: MailDraft, *, at: datetime | None = None, unsent: bool = True) -> BuiltMail:
    """The ``.eml`` bytes of ``draft`` (RFC 5322, UTF-8 where needed) and its preview. Validates everything first;
    raises :class:`DraftError`. A draft is marked as unsent (``X-Unsent: 1``, ``unsent=False`` for a person's test
    send), so mail programs that know the header open it as a new message to send."""
    to = check_addresses(draft.to, "to")
    cc = check_addresses(draft.cc, "cc")
    if not to:
        raise DraftError("mail.no_recipient", "A draft needs at least one recipient in 'to'")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise DraftError("mail.too_many_recipients", f"At most {MAX_RECIPIENTS} recipients", limit=MAX_RECIPIENTS)
    sender = None
    if draft.from_address:
        if address_problem(draft.from_address) is not None:
            raise DraftError("mail.address_invalid", "The sender is not an e-mail address", field="from", index=0)
        sender = draft.from_address
    subject = check_subject(draft.subject)
    if len(draft.body_text) > MAX_TEXT_CHARS:
        raise DraftError("mail.text_too_long", "The text is too long", limit=MAX_TEXT_CHARS)
    cleaned = clean_html(draft.body_html) if draft.body_html and draft.body_html.strip() else None
    text = draft.body_text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() and cleaned is not None:
        text = cleaned.text
    if not text.strip() and cleaned is None:
        raise DraftError("mail.empty", "A draft needs a text or an HTML body")
    if len(draft.attachments) > MAX_ATTACHMENTS:
        raise DraftError("mail.too_many_attachments", f"At most {MAX_ATTACHMENTS} attachments", limit=MAX_ATTACHMENTS)
    if sum(len(item.data) for item in draft.attachments) > MAX_MESSAGE_BYTES:
        raise DraftError("mail.too_large", "The draft is larger than 10 MB", limit=MAX_MESSAGE_BYTES)

    message = EmailMessage(policy=policy.SMTP)
    if sender:
        message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = format_datetime(at or now())
    domain = (sender or "").rpartition("@")[2] or "nodestro.invalid"
    message["Message-ID"] = make_msgid(domain=domain)
    if unsent:
        message["X-Unsent"] = "1"
    message.set_content(text if text.endswith("\n") else text + "\n", subtype="plain", charset="utf-8")
    if cleaned is not None:
        message.add_alternative(html_document(cleaned.html, subject), subtype="html", charset="utf-8")
    names: list[dict[str, Any]] = []
    for item in draft.attachments:
        main, sub = _media_type(item.media_type)
        name = attachment_name(item.name)
        message.add_attachment(item.data, maintype=main, subtype=sub, filename=name)
        names.append({"name": name, "media_type": f"{main}/{sub}", "size": len(item.data)})
    data = message.as_bytes()
    if len(data) > MAX_MESSAGE_BYTES:
        raise DraftError("mail.too_large", "The draft is larger than 10 MB", limit=MAX_MESSAGE_BYTES)
    preview = {
        "kind": "email",
        "from": sender,
        "to": to,
        "cc": cc,
        "subject": subject,
        "html": cleaned is not None,
        "attachments": names,
        "size": len(data),
    }
    return BuiltMail(data=data, preview=preview)


# ---- webhook payloads ---------------------------------------------------------------------------------------------

#: The JSON Schema keywords :func:`schema_problems` understands; any other keyword is refused when the schema is set,
#: so a schema never looks stricter than it is.
SCHEMA_KEYWORDS = frozenset(
    {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const", "minLength", "maxLength",
        "minimum", "maximum", "minItems", "maxItems", "title", "description", "$schema"
    }
)  # fmt: skip
_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
MAX_SCHEMA_DEPTH = 16
MAX_PROBLEMS = 20


def schema_issue(schema: Any, *, path: str = "", depth: int = 0) -> str | None:
    """Why ``schema`` is not a schema of the supported subset (the path of the first problem), or ``None``."""
    if not isinstance(schema, Mapping):
        return path or "/"
    if depth > MAX_SCHEMA_DEPTH:
        return path or "/"
    for key, value in schema.items():
        here = f"{path}/{key}"
        if key not in SCHEMA_KEYWORDS:
            return here
        if key == "type":
            names = value if isinstance(value, list) else [value]
            if not names or any(name not in _TYPES for name in names):
                return here
        elif key == "properties":
            if not isinstance(value, Mapping):
                return here
            for name, inner in value.items():
                found = schema_issue(inner, path=f"{here}/{name}", depth=depth + 1)
                if found:
                    return found
        elif key in ("items",):
            found = schema_issue(value, path=here, depth=depth + 1)
            if found:
                return found
        elif key == "additionalProperties":
            if not isinstance(value, bool):
                found = schema_issue(value, path=here, depth=depth + 1)
                if found:
                    return found
        elif key == "required":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return here
        elif key == "enum":
            if not isinstance(value, list) or not value:
                return here
        elif key in ("minLength", "maxLength", "minItems", "maxItems"):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return here
        elif key in ("minimum", "maximum") and (not isinstance(value, int | float) or isinstance(value, bool)):
            return here
    return None


def _same(left: Any, right: Any) -> bool:
    """JSON equality: ``true`` is not ``1`` and ``1.0`` equals ``1`` only as numbers of the same JSON kind."""
    kinds = {_type_of(left), _type_of(right)}
    if kinds <= {"integer", "number"}:
        return bool(left == right)
    return _type_of(left) == _type_of(right) and json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _matches_type(value: Any, names: Iterable[str]) -> bool:
    kind = _type_of(value)
    return any(kind == name or (name == "number" and kind == "integer") for name in names)


def schema_problems(value: Any, schema: Mapping[str, Any], *, path: str = "") -> list[str]:
    """Where ``value`` breaks ``schema`` (JSON pointers, at most :data:`MAX_PROBLEMS`; never the values)."""
    problems: list[str] = []

    def visit(item: Any, rule: Mapping[str, Any], here: str) -> None:
        if len(problems) >= MAX_PROBLEMS:
            return
        where = here or "/"
        if "type" in rule:
            names = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
            if not _matches_type(item, names):
                problems.append(f"{where}: type")
                return
        if "const" in rule and not _same(item, rule["const"]):
            problems.append(f"{where}: const")
        if "enum" in rule and not any(_same(item, option) for option in rule["enum"]):
            problems.append(f"{where}: enum")
        if isinstance(item, str):
            if "minLength" in rule and len(item) < rule["minLength"]:
                problems.append(f"{where}: minLength")
            if "maxLength" in rule and len(item) > rule["maxLength"]:
                problems.append(f"{where}: maxLength")
        if isinstance(item, int | float) and not isinstance(item, bool):
            if "minimum" in rule and item < rule["minimum"]:
                problems.append(f"{where}: minimum")
            if "maximum" in rule and item > rule["maximum"]:
                problems.append(f"{where}: maximum")
        if isinstance(item, list):
            if "minItems" in rule and len(item) < rule["minItems"]:
                problems.append(f"{where}: minItems")
            if "maxItems" in rule and len(item) > rule["maxItems"]:
                problems.append(f"{where}: maxItems")
            if isinstance(rule.get("items"), Mapping):
                for index, inner in enumerate(item):
                    visit(inner, rule["items"], f"{here}/{index}")
        if isinstance(item, dict):
            properties = rule.get("properties") or {}
            for name in rule.get("required") or []:
                if name not in item:
                    problems.append(f"{here}/{_pointer(name)}: required")
            extra = rule.get("additionalProperties", True)
            for name, inner in item.items():
                inner_path = f"{here}/{_pointer(name)}"
                if name in properties:
                    visit(inner, properties[name], inner_path)
                elif extra is False:
                    problems.append(f"{inner_path}: additionalProperties")
                elif isinstance(extra, Mapping):
                    visit(inner, extra, inner_path)

    visit(value, schema, path)
    return problems[:MAX_PROBLEMS]


def _pointer(name: str) -> str:
    return str(name).replace("~", "~0").replace("/", "~1")[:100]


#: The deepest nesting of a payload (objects and arrays).
MAX_PAYLOAD_DEPTH = 64


def _finite(value: Any) -> bool:
    """Whether ``value`` holds only finite numbers and string keys, at most :data:`MAX_PAYLOAD_DEPTH` deep (checked
    without recursion, so a deeply nested payload is an error, not a crash)."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, list | dict):
            if depth >= MAX_PAYLOAD_DEPTH:
                return False
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    return False
                pending.extend((inner, depth + 1) for inner in item.values())
            else:
                pending.extend((inner, depth + 1) for inner in item)
    return True


def payload_bytes(payload: Any, schema: Mapping[str, Any] | None = None) -> bytes:
    """``payload`` as compact UTF-8 JSON (sorted keys: the bytes that would be signed), checked against ``schema``.
    Raises :class:`DraftError` (``webhook.payload_invalid``, ``webhook.payload_too_large``, ``webhook.schema_mismatch``,
    ``webhook.schema_invalid``)."""
    if not isinstance(payload, dict | list) or not _finite(payload):
        raise DraftError(
            "webhook.payload_invalid",
            f"The payload must be a JSON object or array of finite values, at most {MAX_PAYLOAD_DEPTH} levels deep",
        )
    if schema is not None:
        issue = schema_issue(schema)
        if issue is not None:
            raise DraftError("webhook.schema_invalid", "The payload schema uses an unsupported form", at=issue)
        problems = schema_problems(payload, schema)
        if problems:
            raise DraftError(
                "webhook.schema_mismatch",
                "The payload does not match the schema: " + "; ".join(problems),
                problems=problems,
            )
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise DraftError("webhook.payload_too_large", "The payload is larger than 256 KB", limit=MAX_PAYLOAD_BYTES)
    return data


SIGNATURE_HEADER = "Nodestro-Signature"


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """The value of :data:`SIGNATURE_HEADER`: ``t=<timestamp>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>``."""
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, header: str, body: bytes, *, now_ts: int, tolerance: int = 300) -> bool:
    """Whether ``header`` signs ``body`` with ``secret`` within ``tolerance`` seconds (for receivers and tests)."""
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(now_ts - timestamp) > tolerance:
        return False
    expected = signature(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_HTML_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "MAX_RECIPIENTS",
    "MAX_SUBJECT",
    "SCHEMA_KEYWORDS",
    "SIGNATURE_HEADER",
    "Attachment",
    "BuiltMail",
    "CleanHtml",
    "DraftError",
    "MailDraft",
    "address_problem",
    "attachment_name",
    "build_mail",
    "check_addresses",
    "check_subject",
    "clean_html",
    "html_document",
    "payload_bytes",
    "schema_issue",
    "schema_problems",
    "signature",
    "verify_signature",
]
