"""Office XML parts that declare a document type or entities are refused, wherever the declaration is and however
the part is encoded (RT-044, backend audit round 2 RT2-09)."""

from __future__ import annotations

import io
import zipfile

import pytest

from nodestro_runtime.documents import DocumentError, docx_text, xlsx_text

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY = f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{{text}}</w:t></w:r></w:p></w:body></w:document>'


def _docx(document: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_a_plain_document_is_read() -> None:
    assert docx_text(_docx(BODY.format(text="Hello").encode())).text == "Hello"


def test_text_that_looks_like_a_declaration_is_kept() -> None:
    escaped = BODY.format(text="&lt;!DOCTYPE x&gt; &lt;!ENTITY y&gt;").encode()
    assert docx_text(_docx(escaped)).text == "<!DOCTYPE x> <!ENTITY y>"


@pytest.mark.parametrize(
    "document",
    [
        # At the start, as before.
        b'<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "b">]>' + BODY.format(text="&a;").encode(),
        # Behind a long comment: past the bytes the old check read.
        b'<?xml version="1.0"?><!--'
        + b"x" * 8000
        + b'--><!DOCTYPE w:document [<!ENTITY a "b">]>'
        + BODY.format(text="&a;").encode(),
        # A document type without entities.
        b'<?xml version="1.0"?><!DOCTYPE w:document SYSTEM "x.dtd">' + BODY.format(text="x").encode(),
        # UTF-16: the bytes of the declaration are not ASCII.
        (
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE w:document [<!ENTITY a "b">]>' + BODY.format(text="&a;")
        ).encode("utf-16"),
    ],
    ids=["start", "after-8kb", "doctype-only", "utf-16"],
)
def test_declarations_are_refused_anywhere(document: bytes) -> None:
    with pytest.raises(DocumentError) as caught:
        docx_text(_docx(document))
    assert caught.value.code == "document.unreadable"
    assert "unsupported XML" in str(caught.value)


def test_a_damaged_part_is_reported_as_damaged() -> None:
    with pytest.raises(DocumentError) as caught:
        docx_text(_docx(b"<w:document><unclosed>"))
    assert "damaged" in str(caught.value)


def test_workbook_parts_are_checked_too() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            b'<?xml version="1.0"?><!--' + b"y" * 5000 + b'--><!DOCTYPE workbook [<!ENTITY a "b">]><workbook/>',
        )
    with pytest.raises(DocumentError, match="unsupported XML"):
        xlsx_text(buffer.getvalue())
