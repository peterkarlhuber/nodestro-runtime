"""The draft tools of NAL-A4: an agent prepares an e-mail or a webhook payload; a person sends it.

``draft_email`` writes an ``.eml`` file and ``draft_webhook`` a ``.json`` file into the run's result files, each with
a short preview (recipients, subject, attachment names and sizes; never the body). Neither tool has a network path:
their spec is ``write`` + ``internal``, ``network="none"``, usable offline, and this module imports nothing that
sends (``tests/test_draft_tools.py`` checks the imports). Real delivery is NAL-A5, locked until the security
acceptance (RT-001) and the owner's release.

Attachments come only from the run itself: the files it was started with and the result files it wrote so far,
named by their file name. The tool never opens a path or an address.
"""

from __future__ import annotations

import json
from typing import Any

from orkexa import Tool, ToolResultError

from .mail import (
    MAX_ATTACHMENTS,
    MAX_MESSAGE_BYTES,
    MAX_PAYLOAD_BYTES,
    Attachment,
    DraftError,
    MailDraft,
    build_mail,
    payload_bytes,
)
from .problems import NodestroError

#: The most draft files one run writes per tool (the spec's per-run limit says the same).
MAX_DRAFTS_PER_RUN = 10


def _services() -> Any:
    from .tool_spec import current_tool_context

    context = current_tool_context()
    services = context.services if context is not None else None
    if services is None or services.save_file is None:
        raise ToolResultError("Error: drafts cannot be stored here (draft.unavailable)")
    return context


def _draft_error(exc: DraftError) -> ToolResultError:
    return ToolResultError(f"Error: {exc} ({exc.code})")


def _attachments(context: Any, names: list[str]) -> tuple[Attachment, ...]:
    """The named run files, read one by one: the count, duplicates and every size are checked before a file is read,
    and the total after each one (review M1: a long list must not read large files again and again)."""
    wanted = list(dict.fromkeys(name.strip() for name in names))
    if len(wanted) > MAX_ATTACHMENTS:
        raise ToolResultError(f"Error: at most {MAX_ATTACHMENTS} attachments (mail.too_many_attachments)")
    found: list[Attachment] = []
    reader = context.services.run_file
    total = 0
    for name in wanted:
        try:
            item = reader(name, MAX_MESSAGE_BYTES - total) if reader is not None else None
        except DraftError as exc:
            raise _draft_error(exc) from None
        if item is None:
            raise ToolResultError(
                f"Error: {name!r} is not a file of this run (mail.attachment_unknown); attach only files the run "
                "was given or wrote"
            )
        total += len(item.data)
        found.append(item)
    return tuple(found)


def _save(context: Any, name: str, fmt: str, data: bytes, preview: dict[str, Any]) -> str:
    try:
        return str(context.services.save_file(name, fmt, data, preview))
    except NodestroError as exc:
        raise ToolResultError(f"Error: the draft cannot be stored ({exc.code})") from None


@Tool
def draft_email(
    to: list[str],
    subject: str,
    body_text: str = "",
    body_html: str = "",
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
    file_name: str = "",
) -> str:
    """Prepare an e-mail as a draft file (.eml) for a person to check and send. Nothing is sent.

    Args:
        to: Recipient addresses (plain addresses such as name@example.com).
        subject: One line, at most 200 characters.
        body_text: The message as plain text.
        body_html: Optional HTML version; scripts, styles, images and forms are removed.
        cc: Optional copy recipients. At most 50 recipients in total.
        attachments: Optional file names of this run's files (its input files or result files) to attach.
        file_name: Optional name of the draft file.

    Returns:
        The draft file's name and id, and what it contains (recipients, subject, attachments).
    """
    context = _services()
    params = context.params
    sender = params.get("from_address") or None
    try:
        built = build_mail(
            MailDraft(
                to=tuple(to or ()),
                cc=tuple(cc or ()),
                subject=subject,
                body_text=body_text or "",
                body_html=body_html or None,
                from_address=sender,
                attachments=_attachments(context, list(attachments or [])),
            )
        )
    except DraftError as exc:
        raise _draft_error(exc) from None
    saved = _save(context, file_name or subject or "email", "eml", built.data, built.preview)
    attached = ", ".join(item["name"] for item in built.preview["attachments"]) or "none"
    return (
        f"Draft saved as {saved}. Not sent: a person opens and sends it.\n"
        f"To: {', '.join(built.preview['to'])}; cc: {', '.join(built.preview['cc']) or 'none'}; "
        f"subject: {built.preview['subject']}; attachments: {attached}."
    )


@Tool
def draft_webhook(payload_json: str, file_name: str = "") -> str:
    """Prepare a webhook payload as a JSON draft file for a person to check and send. Nothing is sent.

    Args:
        payload_json: The JSON object (or array) to send later, as JSON text. If the agent's settings name a schema,
            the payload must match it.
        file_name: Optional name of the draft file.

    Returns:
        The draft file's name and id.
    """
    context = _services()
    schema = context.params.get("payload_schema")
    if len(payload_json) > MAX_PAYLOAD_BYTES * 2:
        raise ToolResultError("Error: the payload is larger than 256 KB (webhook.payload_too_large)")
    try:
        payload = json.loads(payload_json)
    except (ValueError, RecursionError):
        raise ToolResultError("Error: payload_json is not valid JSON (webhook.payload_invalid)") from None
    try:
        data = payload_bytes(payload, schema if isinstance(schema, dict) else None)
    except DraftError as exc:
        raise _draft_error(exc) from None
    preview = {"kind": "webhook", "size": len(data), "schema": isinstance(schema, dict)}
    saved = _save(context, file_name or "webhook", "json", data, preview)
    return f"Draft saved as {saved}. Not sent: a person checks and sends it."


#: The draft tools, as the tool library offers them.
DRAFT_TOOLS: tuple[Tool, ...] = (draft_email, draft_webhook)

__all__ = ["DRAFT_TOOLS", "MAX_DRAFTS_PER_RUN", "draft_email", "draft_webhook"]
