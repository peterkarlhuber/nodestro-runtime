"""NAL-A4: e-mail and webhook drafts. Building is checked piece by piece; the tools are run in a real workflow; and the
tools are shown to have no way to send (plan A4 §10: "kein Agent kann versenden")."""

from __future__ import annotations

import ast
from email import message_from_bytes
from email.policy import default
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import draft_tools, mail
from nodestro_runtime.mail import (
    MAX_RECIPIENTS,
    Attachment,
    DraftError,
    MailDraft,
    address_problem,
    build_mail,
    clean_html,
    payload_bytes,
    schema_issue,
    signature,
    verify_signature,
)
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_spec import BUILTIN_SPECS
from nodestro_runtime.workspace import Workspace


def _code(call: Any) -> str:
    with pytest.raises(DraftError) as caught:
        call()
    return caught.value.code


# ---- addresses and headers ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["a@example.com", "first.last+tag@sub.example.co.uk", "x_y-z@example.org"],
)
def test_plain_addresses_pass(value: str) -> None:
    assert address_problem(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        " a@example.com",
        "a@example",
        "a b@example.com",
        "Name <a@example.com>",
        "a@example.com\r\nBcc: x@example.com",
        "a@example.com,b@example.com",
        ".a@example.com",
        "a..b@example.com",
        "ä@example.com",
        "a@[127.0.0.1]",
        "a@-example.com",
    ],
)
def test_other_forms_are_refused(value: str) -> None:
    assert address_problem(value) is not None


def test_headers_cannot_be_injected() -> None:
    base = {"to": ("a@example.com",), "body_text": "Hi"}
    assert _code(lambda: build_mail(MailDraft(subject="Hi\r\nBcc: x@example.com", **base))) == "mail.subject_invalid"
    assert _code(lambda: build_mail(MailDraft(subject="Hi\u2028there", **base))) == "mail.subject_invalid"
    assert _code(lambda: build_mail(MailDraft(subject="x" * 201, **base))) == "mail.subject_too_long"
    bad_to = MailDraft(to=("a@example.com\nBcc: x@example.com",), subject="Hi", body_text="Hi")
    assert _code(lambda: build_mail(bad_to)) == "mail.address_invalid"
    bad_from = MailDraft(subject="Hi", from_address="me@example.com\r\nX: y", **base)
    assert _code(lambda: build_mail(bad_from)) == "mail.address_invalid"


def test_limits() -> None:
    many = tuple(f"u{n}@example.com" for n in range(MAX_RECIPIENTS + 1))
    assert _code(lambda: build_mail(MailDraft(to=many, subject="x", body_text="x"))) == "mail.too_many_recipients"
    assert _code(lambda: build_mail(MailDraft(to=(), subject="x", body_text="x"))) == "mail.no_recipient"
    assert _code(lambda: build_mail(MailDraft(to=("a@example.com",), subject="x"))) == "mail.empty"
    big = Attachment("big.bin", "application/octet-stream", b"0" * (mail.MAX_MESSAGE_BYTES + 1))
    draft = MailDraft(to=("a@example.com",), subject="x", body_text="x", attachments=(big,))
    assert _code(lambda: build_mail(draft)) == "mail.too_large"
    huge_html = "<p>" + "x" * mail.MAX_HTML_BYTES + "</p>"
    assert _code(lambda: clean_html(huge_html)) == "mail.html_too_large"


def test_duplicate_recipients_are_kept_once() -> None:
    built = build_mail(
        MailDraft(to=("a@example.com", "A@example.com"), cc=("b@example.com",), subject="x", body_text="x")
    )
    assert built.preview["to"] == ["a@example.com"] and built.preview["cc"] == ["b@example.com"]


# ---- HTML ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("markup", "forbidden"),
    [
        ("<p onclick=alert(1)>x</p>", "onclick"),
        ("<script>alert(1)</script><p>x</p>", "alert"),
        ("<style>p{color:red}</style><p>x</p>", "color"),
        ('<img src="https://tracker.example/p.gif">', "tracker"),
        ('<a href="javascript:alert(1)">x</a>', "javascript"),
        ('<a href=" jav&#x09;ascript:alert(1)">x</a>', "script:"),
        ('<a href="data:text/html,<b>x</b>">x</a>', "data:"),
        ('<p style="background:url(https://tracker.example)">x</p>', "tracker"),
        ("<svg><script>alert(1)</script></svg>", "alert"),
        ('<iframe src="https://x.example"></iframe>', "iframe"),
        ('<form action="https://x.example"><input name="pw"></form>', "input"),
        ("<!--[if mso]><script>alert(1)</script><![endif]-->", "alert"),
        ('<base href="https://x.example/">', "base"),
        ('<meta http-equiv="refresh" content="0;url=https://x.example">', "refresh"),
    ],
)
def test_html_is_cleaned_to_the_allowlist(markup: str, forbidden: str) -> None:
    cleaned = clean_html(markup)
    assert forbidden not in cleaned.html.lower()


def test_html_keeps_structure_and_makes_a_text_alternative() -> None:
    cleaned = clean_html(
        '<h1>Report</h1><p>See <a href="https://example.com/a?b=1&c=2" target="_blank">this</a>.</p>'
        "<ul><li>one<li>two</ul><table><tr><th>A<th>B<tr><td>1<td>2</table>"
    )
    assert '<a href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer">this</a>' in cleaned.html
    assert "target" not in cleaned.html
    assert "<li>one</li><li>two</li>" in cleaned.html
    assert "<tr><td>1</td><td>2</td></tr>" in cleaned.html
    assert cleaned.text.splitlines()[0] == "Report"
    assert "this (https://example.com/a?b=1&c=2)" in cleaned.text and "- one\n- two" in cleaned.text


def test_deep_nesting_is_bounded() -> None:
    cleaned = clean_html("<div>" * 500 + "x" + "</div>" * 500)
    assert cleaned.html.count("<div>") == mail.MAX_HTML_DEPTH and cleaned.text == "x"


# ---- the .eml file ------------------------------------------------------------------------------------------------


def test_a_draft_is_a_valid_message_marked_unsent() -> None:
    built = build_mail(
        MailDraft(
            to=("a@example.com",),
            cc=("b@example.com",),
            subject="Grüße aus Wien",
            body_text="Hallo\nWelt",
            body_html="<p>Hallo <b>Welt</b></p>",
            from_address="me@example.com",
            attachments=(Attachment("../../etc/report.csv", "text/csv; charset=utf-8", b"a;b\n1;2\n"),),
        )
    )
    parsed = message_from_bytes(built.data, policy=default)
    assert parsed["Subject"] == "Grüße aus Wien" and parsed["X-Unsent"] == "1"
    assert parsed["To"] == "a@example.com" and parsed["Cc"] == "b@example.com" and parsed["From"] == "me@example.com"
    assert parsed.get_body(("plain",)).get_content() == "Hallo\r\nWelt\r\n"  # SMTP line ends
    assert "<b>Welt</b>" in parsed.get_body(("html",)).get_content()
    [attachment] = list(parsed.iter_attachments())
    assert attachment.get_filename() == "report.csv" and attachment.get_content_type() == "text/csv"
    assert built.preview["attachments"] == [{"name": "report.csv", "media_type": "text/csv", "size": 8}]
    assert "Hallo" not in json.dumps(built.preview)  # the preview never carries the body


# ---- webhook payloads ---------------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "required": ["order", "items"],
    "additionalProperties": False,
    "properties": {
        "order": {"type": "string", "maxLength": 10},
        "items": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}},
        "status": {"enum": ["new", "paid"]},
    },
}


def test_payloads_follow_the_schema() -> None:
    data = payload_bytes({"order": "A-1", "items": [2, 1], "status": "new"}, SCHEMA)
    assert data == b'{"items":[2,1],"order":"A-1","status":"new"}'
    with pytest.raises(DraftError) as caught:
        payload_bytes({"order": "A-1234567890", "items": [0], "extra": True, "status": "x"}, SCHEMA)
    assert caught.value.code == "webhook.schema_mismatch"
    assert caught.value.params["problems"] == [
        "/order: maxLength",
        "/items/0: minimum",
        "/extra: additionalProperties",
        "/status: enum",
    ]
    assert _code(lambda: payload_bytes("text")) == "webhook.payload_invalid"
    assert _code(lambda: payload_bytes({"x": float("nan")})) == "webhook.payload_invalid"
    assert _code(lambda: payload_bytes({"x": "y" * mail.MAX_PAYLOAD_BYTES})) == "webhook.payload_too_large"


def test_unsupported_schema_keywords_are_refused() -> None:
    assert schema_issue({"type": "object", "properties": {"a": {"pattern": "^(a+)+$"}}}) == "/properties/a/pattern"
    assert schema_issue({"$ref": "https://example.com/schema.json"}) == "/$ref"
    assert schema_issue(SCHEMA) is None
    assert _code(lambda: payload_bytes({}, {"oneOf": []})) == "webhook.schema_invalid"


def test_signature_matches_reference_values() -> None:
    # Reference: printf '1700000000.{"a":1}' | openssl dgst -sha256 -hmac key
    header = signature("key", 1_700_000_000, b'{"a":1}')
    assert header == "t=1700000000,v1=a438e398bfafc57e4396bb7fc2304422f0f768e965d073ca313cb52e22e6ad03"
    assert verify_signature("key", header, b'{"a":1}', now_ts=1_700_000_100)
    assert not verify_signature("key", header, b'{"a":2}', now_ts=1_700_000_100)
    assert not verify_signature("other", header, b'{"a":1}', now_ts=1_700_000_100)
    assert not verify_signature("key", header, b'{"a":1}', now_ts=1_700_001_000)


# ---- no way to send -----------------------------------------------------------------------------------------------

SENDING_MODULES = {"smtplib", "socket", "ssl", "http", "urllib", "httpx", "httpcore", "requests", "aiohttp"}


def _imports(module: Any) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def test_the_draft_tools_cannot_send() -> None:
    for name in ("draft_email", "draft_webhook"):
        spec = BUILTIN_SPECS[name]
        assert spec.network == "none" and spec.offline and spec.connection_type is None
        assert (spec.capability.access, spec.capability.reach) == ("write", "internal")
    for module in (draft_tools, mail):
        names = _imports(module)
        assert not {item.split(".")[0] for item in names} & SENDING_MODULES, (module.__name__, names)
        assert not any("mail_transport" in item or "connections" in item for item in names), names


# ---- in a workflow ------------------------------------------------------------------------------------------------


def _workspace(tmp_path: Path, script: dict[str, Any]) -> Workspace:
    client = ScriptedClient(script)
    return Workspace(tmp_path / "data", client_factory=lambda _p: client, integrations=[])


def _flow(prompt: str, tools: list[str], settings: dict[str, Any]) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "Drafts",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "agent": {"name": "A", "system_prompt": prompt, "tools": tools, "tool_settings": settings},
                },
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


async def test_a_workflow_writes_drafts_as_run_files(tmp_path: Path) -> None:
    """Acceptance (plan A4 §10): a workflow writes a valid .eml with an attachment from the run; nothing is sent."""
    calls = [
        tool_use(
            "draft_email",
            {
                "to": ["client@example.com"],
                "subject": "Your offer",
                "body_text": "Please find the offer attached.",
                "attachments": ["offer.csv"],
            },
            call_id="t1",
        ),
        tool_use(
            "draft_email", {"to": ["x@example.com"], "subject": "x", "attachments": ["/etc/passwd"]}, call_id="t2"
        ),
        tool_use("draft_webhook", {"payload_json": json.dumps({"order": "A-1", "items": [1]})}, call_id="t3"),
        tool_use("draft_webhook", {"payload_json": json.dumps({"order": 7})}, call_id="t4"),
    ]
    workspace = _workspace(tmp_path, {"DRAFT": [*calls, text("Done.")]})
    try:
        upload = workspace.inputs.add(b"item;price\nA;10\n", name="offer.csv", origin="test")
        settings = {
            "draft_email": {"params": {"from_address": "sales@example.com"}},
            "draft_webhook": {"params": {"payload_schema": SCHEMA}},
        }
        flow = workspace.save_workflow(_flow("DRAFT", ["draft_email", "draft_webhook"], settings))
        run = await workspace.wait((await workspace.run(flow, RunIn(input="go", file_ids=[upload.id]))).id, timeout=10)
        assert run.status == "succeeded", run.error
        outputs = [str(call.output) for call in run.nodes["a"].tool_calls]
        assert outputs[0].startswith("Draft saved as Your offer.eml (") and "Not sent" in outputs[0]
        assert "mail.attachment_unknown" in outputs[1]
        assert outputs[2].startswith("Draft saved as webhook.json (")
        assert "webhook.schema_mismatch" in outputs[3]

        files = workspace.files.list(run.id)
        assert [(entry.name, entry.format, entry.source) for entry in files] == [
            ("Your offer.eml", "eml", "tool"),
            ("webhook.json", "json", "tool"),
        ]
        eml = files[0]
        assert eml.media_type == "message/rfc822" and eml.node_id == "a"
        assert eml.preview["to"] == ["client@example.com"] and eml.preview["attachments"][0]["name"] == "offer.csv"
        _, data = workspace.files.content(run.id, eml.id)
        parsed = message_from_bytes(data, policy=default)
        assert parsed["From"] == "sales@example.com" and parsed["X-Unsent"] == "1"
        [attachment] = list(parsed.iter_attachments())
        assert attachment.get_content() == "item;price\nA;10\n"
        _, payload = workspace.files.content(run.id, files[1].id)
        assert json.loads(payload) == {"order": "A-1", "items": [1]}
    finally:
        workspace.close()


async def test_drafts_written_in_a_step_can_be_attached_later(tmp_path: Path) -> None:
    calls = [
        tool_use("draft_webhook", {"payload_json": '{"a": 1}', "file_name": "payload"}, call_id="t1"),
        tool_use(
            "draft_email",
            {
                "to": ["a@example.com"],
                "subject": "Payload",
                "body_html": "<p>See file</p>",
                "attachments": ["payload.json"],
            },
            call_id="t2",
        ),
    ]
    workspace = _workspace(tmp_path, {"CHAIN": [*calls, text("Done.")]})
    try:
        flow = workspace.save_workflow(_flow("CHAIN", ["draft_email", "draft_webhook"], {}))
        run = await workspace.wait((await workspace.run(flow, RunIn(input="go"))).id, timeout=10)
        assert run.status == "succeeded", run.error
        names = [entry.name for entry in workspace.files.list(run.id)]
        assert names == ["payload.json", "Payload.eml"]
        eml = workspace.files.list(run.id)[1]
        assert eml.preview["html"] is True and eml.preview["from"] is None
    finally:
        workspace.close()
