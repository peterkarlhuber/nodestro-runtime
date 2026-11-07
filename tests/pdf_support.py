"""PDF files for the PDF limit tests (MUP-ONA A1), built byte for byte without a PDF library.

The same constructions as orkexa's ``tests/fixtures/pdf/make_pdf_fixtures.py`` (the fixtures of its T-PDF tests),
which the installed orkexa package does not ship: a text PDF, a PDF with 100,000 pages and one whose content streams
make text extraction run for many seconds.
"""

from __future__ import annotations

from collections.abc import Sequence
import zlib

_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"


def _pdf(objects: Sequence[bytes]) -> bytes:
    """A PDF whose object N (1-based) is ``objects[N - 1]``; object 1 must be the catalog."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _stream(data: bytes, *, compress: bool = False) -> bytes:
    if compress:
        data = zlib.compress(data, 9)
        return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
    return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _text(line: str) -> bytes:
    escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
    return b"BT /F1 12 Tf 72 720 Td (" + escaped + b") Tj ET"


def _page(contents: bytes) -> bytes:
    return (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents "
        + contents
        + b" >>"
    )


def _pages_with_streams(streams: Sequence[bytes]) -> bytes:
    """One page per stream; every page has its own content stream object."""
    count = len(streams)
    kids = b" ".join(b"%d 0 R" % (4 + 2 * index) for index in range(count))
    objects = [_CATALOG, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count, _FONT]
    for index, stream in enumerate(streams):
        objects += [_page(b"%d 0 R" % (5 + 2 * index)), stream]
    return _pdf(objects)


def text_pdf(lines: Sequence[str]) -> bytes:
    """One page per line of text (an empty line gives a page without text)."""
    return _pages_with_streams([_stream(_text(line) if line else b"") for line in lines])


def pages_100k(count: int = 100_000) -> bytes:
    """``count`` pages sharing one content stream: far over the page limit (about 12 MB)."""
    kids = b" ".join(b"%d 0 R" % (5 + index) for index in range(count))
    objects = [
        _CATALOG,
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count,
        _FONT,
        _stream(_text("One of very many pages.")),
    ]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>"] * count
    return _pdf(objects)


def slow_content_stream(pages: int = 5, operators: int = 150_000) -> bytes:
    """Pages whose content streams hold many text operators, so that text extraction runs for many seconds."""
    line = b"1 0 0 1 72 720 Tm (x) Tj\n"
    return _pages_with_streams(
        [_stream(b"BT /F1 12 Tf\n%d 0 Td\n" % page + line * operators + b"ET", compress=True) for page in range(pages)]
    )
