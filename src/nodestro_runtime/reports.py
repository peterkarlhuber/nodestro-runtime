"""Result files: the result of a run as Markdown, JSON, CSV, HTML, PDF, Word (DOCX) or Excel (XLSX).

A result node can list files it writes when a run ends (``OutputNodeConfig.files``);
users can also create files for any finished run later. Files belong to their run:
they are stored with it (collections ``result_files`` and ``result_file_data``), are
part of backups, can be downloaded again at any time, and are deleted with the run.

Word and Excel files are written by :mod:`nodestro_runtime.office` (DOCX from the same Markdown
blocks, XLSX from the result's tables with typed cells and one sheet per table).
Everything is generated locally without libraries: Markdown is converted to HTML by a
small converter (headings, paragraphs, lists, tables, code, emphasis), the PDF writer
lays out the same blocks on A4 pages with the PDF base fonts (Helvetica, Courier) in
the Windows-1252 character set; characters outside it are replaced by ``?``.

A file can be sanitized like a trace export (:mod:`nodestro_runtime.sanitize`): credentials and,
optionally, personal data are replaced by placeholders before the file is written.
A file that cannot be written is stored as a file with an error code, so the run shows
clearly what failed and why.
"""

from __future__ import annotations

import base64
import builtins
import csv
from datetime import datetime
import html
import io
import json
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, Field

from .conditions import parse_result
from .office import to_docx, to_xlsx
from .problems import NodestroError
from .sanitize import Sanitizer
from .schemas import ReportFormat, ResultFileSpec, Run, new_id, now
from .storage import DocumentStore

MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_BYTES = 10 * 1024 * 1024
FILES = "result_files"
DATA = "result_file_data"
_UNSAFE = re.compile(r"[^\w\-. ]+", re.UNICODE)


class ReportError(NodestroError):
    """A result file cannot be written."""


class ResultFile(BaseModel):
    id: str = Field(default_factory=lambda: new_id("file"))
    run_id: str
    name: str
    format: ReportFormat
    media_type: str
    size: int = 0
    created_at: datetime = Field(default_factory=now)
    #: ``output``: written by a result node at the end of the run; ``manual``: created later.
    source: Literal["output", "manual"] = "manual"
    node_id: str | None = None
    sanitized: bool = False
    #: Set when the file could not be written; the file then has no content.
    error_code: str | None = None
    error: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)


class FileRequest(BaseModel):
    format: ReportFormat = "md"
    name: str = ""
    #: The result node whose result is written; the run's result when empty.
    node_id: str | None = None
    sanitize: bool = False
    personal_data: bool = True


# ---- names ---------------------------------------------------------------------------


def file_name(name: str, fallback: str, extension: str) -> str:
    """A safe, readable file name: no paths, no control characters, the right extension."""
    base = (name or "").strip() or fallback or "result"
    base = base.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(f".{extension}"):
        base = base[: -len(extension) - 1]
    base = _UNSAFE.sub("_", base).strip(" ._") or "result"
    return f"{base[:80]}.{extension}"


def _unique(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    stem, _, extension = name.rpartition(".")
    number = 2
    while f"{stem} ({number}).{extension}" in taken:
        number += 1
    return f"{stem} ({number}).{extension}"


# ---- content -------------------------------------------------------------------------


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _records(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return value
    return None


def _md_cell(value: Any) -> str:
    return " ".join(_scalar(value).split()).replace("|", "\\|")


def records_table(records: list[dict[str, Any]]) -> str:
    columns = list(dict.fromkeys(key for record in records for key in record))
    lines = ["| " + " | ".join(_md_cell(column) for column in columns) + " |", "|" + " --- |" * len(columns)]
    lines += ["| " + " | ".join(_md_cell(record.get(column)) for column in columns) + " |" for record in records]
    return "\n".join(lines)


def to_markdown(value: Any, *, level: int = 2) -> str:
    """A result as Markdown: texts as they are, records as tables, objects as sections."""
    value = parse_result(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    records = _records(value)
    if records is not None:
        return records_table(records)
    if isinstance(value, list):
        return "\n".join(f"- {_scalar(parse_result(item)) if not isinstance(item, str) else item}" for item in value)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            heading = "#" * min(level, 6)
            parts.append(f"{heading} {key}\n\n{to_markdown(item, level=level + 1)}")
        return "\n\n".join(parts)
    return _scalar(value)


def document_markdown(title: str, run: Run, value: Any) -> str:
    stamp = (run.finished_at or run.created_at).strftime("%Y-%m-%d %H:%M UTC")
    return f"# {title}\n\n_{run.workflow_name} · {stamp}_\n\n{to_markdown(value)}\n"


def _markdown_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            cells = [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]
            if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    return tables


def to_rows(value: Any) -> list[list[str]]:
    """The rows of a CSV file; raises ``report.no_table`` when the result has no table."""
    value = parse_result(value)
    records = _records(value)
    if records is None and isinstance(value, dict):
        # An object with exactly one list of records (``{"items": [...]}``) is that table.
        tables = [item for item in value.values() if _records(parse_result(item)) is not None]
        if len(tables) == 1:
            records = _records(parse_result(tables[0]))
        elif all(not isinstance(item, (dict, list)) for item in value.values()):
            return [["field", "value"], *[[str(key), _scalar(item)] for key, item in value.items()]]
    if records is not None:
        columns = list(dict.fromkeys(key for record in records for key in record))
        return [columns, *[[_scalar(record.get(column)) for column in columns] for record in records]]
    if isinstance(value, str):
        tables = _markdown_tables(value)
        if tables:
            return max(tables, key=len)
    raise ReportError(
        "report.no_table",
        "The result contains no table: CSV needs a list of records, an object of single values or a Markdown table",
    )


def csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        # A leading =, +, - or @ would be a formula in spreadsheet programs; the quote keeps it text.
        writer.writerow(
            ["'" + cell if cell[:1] in ("=", "+", "-", "@") and not _is_number(cell) else cell for cell in row]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _is_number(text: str) -> bool:
    return re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", text.strip()) is not None


# ---- Markdown blocks (shared by HTML and PDF) -----------------------------------------

Block = tuple[str, Any]


def blocks(markdown: str) -> list[Block]:
    """Markdown as blocks: ``(heading, (level, text))``, ``(paragraph, text)``,
    ``(list, [items])``, ``(table, rows)``, ``(code, text)``, ``(rule, None)``."""
    found: list[Block] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    index = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            found.append(("paragraph", " ".join(part.strip() for part in paragraph)))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            found.append(("code", "\n".join(code)))
        elif not stripped:
            flush()
        elif re.match(r"^#{1,6}\s", stripped):
            flush()
            marks = len(stripped) - len(stripped.lstrip("#"))
            found.append(("heading", (marks, stripped[marks:].strip())))
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush()
            found.append(("rule", None))
        elif stripped.startswith("|") and stripped.endswith("|"):
            flush()
            table: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table.append(lines[index])
                index += 1
            rows = _markdown_tables("\n".join(table))
            if rows:
                found.append(("table", rows[0]))
            continue
        elif re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            flush()
            ordered = bool(re.match(r"^\d", stripped))
            items: list[str] = []
            while index < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[index]):
                items.append(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[index]).strip())
                index += 1
            found.append(("olist" if ordered else "list", items))
            continue
        else:
            paragraph.append(line)
        index += 1
    flush()
    return found


def _inline_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", escaped)
    # Links keep their text and show the address; nothing becomes clickable script.
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", escaped)


def to_html(markdown: str, title: str) -> str:
    body: list[str] = []
    for kind, value in blocks(markdown):
        if kind == "heading":
            level, text = value
            body.append(f"<h{level}>{_inline_html(text)}</h{level}>")
        elif kind == "paragraph":
            body.append(f"<p>{_inline_html(value)}</p>")
        elif kind in ("list", "olist"):
            tag = "ol" if kind == "olist" else "ul"
            body.append(f"<{tag}>" + "".join(f"<li>{_inline_html(item)}</li>" for item in value) + f"</{tag}>")
        elif kind == "table":
            head, *rows = value
            cells = "".join(f"<th>{_inline_html(cell)}</th>" for cell in head)
            lines = [f"<table><thead><tr>{cells}</tr></thead><tbody>"]
            for row in rows:
                lines.append("<tr>" + "".join(f"<td>{_inline_html(cell)}</td>" for cell in row) + "</tr>")
            body.append("".join(lines) + "</tbody></table>")
        elif kind == "code":
            body.append(f"<pre><code>{html.escape(value)}</code></pre>")
        elif kind == "rule":
            body.append("<hr>")
    style = (
        "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:860px;margin:2rem auto;"
        "padding:0 1rem;color:#1b1d2a;line-height:1.55}h1,h2,h3{color:#121428;line-height:1.25}"
        "h1{border-bottom:3px solid #d4e157;padding-bottom:.3rem}table{border-collapse:collapse;margin:1rem 0;"
        "width:100%}th,td{border:1px solid #c9ccd8;padding:.35rem .55rem;text-align:left;vertical-align:top}"
        "th{background:#eef0f6}code{background:#eef0f6;padding:0 .2rem;border-radius:3px}"
        "pre{background:#eef0f6;padding:.8rem;overflow:auto;border-radius:6px}"
        "@media print{body{margin:0;max-width:none}}"
    )
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{style}</style></head>\n<body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )


# ---- PDF -------------------------------------------------------------------------------

# Advance widths of Helvetica (1/1000 em) for ASCII 32..126, from the Adobe font metrics.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]  # fmt: skip
_PAGE_WIDTH, _PAGE_HEIGHT, _MARGIN = 595.0, 842.0, 56.0


def _width(text: str, size: float, font: str) -> float:
    if font == "F3":
        return len(text) * 600 * size / 1000
    total = 0
    for char in text:
        base = unicodedata.normalize("NFKD", char)[:1] or char
        code = ord(base)
        total += _HELVETICA[code - 32] if 32 <= code <= 126 else 556
    factor = 1.06 if font == "F2" else 1.0
    return total * size * factor / 1000


def _encode(text: str) -> bytes:
    data = text.encode("cp1252", errors="replace")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _plain(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", text)


def _wrap(text: str, width: float, size: float, font: str) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        current = ""
        for word in raw.split(" "):
            candidate = f"{current} {word}" if current else word
            if _width(candidate, size, font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # A word longer than the line is cut.
            while _width(word, size, font) > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and _width(word[:cut], size, font) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


class _Pages:
    def __init__(self) -> None:
        self.pages: list[list[bytes]] = [[]]
        self.y = _PAGE_HEIGHT - _MARGIN

    def need(self, height: float) -> None:
        if self.y - height < _MARGIN:
            self.pages.append([])
            self.y = _PAGE_HEIGHT - _MARGIN

    def text(self, x: float, line: str, size: float, font: str) -> None:
        self.need(size * 1.35)
        self.y -= size * 1.35
        self.pages[-1].append(
            b"BT /%s %.1f Tf %.2f %.2f Td (%s) Tj ET" % (font.encode(), size, x, self.y, _encode(line))
        )

    def rule(self) -> None:
        self.need(10)
        self.y -= 6
        self.pages[-1].append(
            b"0.75 0.77 0.82 RG 0.6 w %.2f %.2f m %.2f %.2f l S" % (_MARGIN, self.y, _PAGE_WIDTH - _MARGIN, self.y)
        )
        self.y -= 4

    def gap(self, height: float) -> None:
        self.y -= height


def to_pdf(markdown: str, title: str) -> bytes:
    width = _PAGE_WIDTH - 2 * _MARGIN
    pages = _Pages()
    for kind, value in blocks(markdown):
        if kind == "heading":
            level, text = value
            size = {1: 18.0, 2: 14.0, 3: 12.0}.get(level, 11.0)
            pages.gap(size * 0.5)
            pages.need(size * 3)
            for line in _wrap(_plain(text), width, size, "F2"):
                pages.text(_MARGIN, line, size, "F2")
            if level == 1:
                pages.rule()
        elif kind == "paragraph":
            for line in _wrap(_plain(value), width, 10.5, "F1"):
                pages.text(_MARGIN, line, 10.5, "F1")
            pages.gap(5)
        elif kind in ("list", "olist"):
            for number, item in enumerate(value, start=1):
                marker = f"{number}." if kind == "olist" else "\u2022"
                lines = _wrap(_plain(item), width - 16, 10.5, "F1")
                for position, line in enumerate(lines):
                    pages.text(_MARGIN + 16, line, 10.5, "F1")
                    if position == 0:
                        pages.pages[-1].append(
                            b"BT /F1 10.5 Tf %.2f %.2f Td (%s) Tj ET" % (_MARGIN + 2, pages.y, _encode(marker))
                        )
            pages.gap(5)
        elif kind == "table":
            columns = max(len(row) for row in value)
            column_width = width / max(columns, 1)
            for row_number, row in enumerate(value):
                font = "F2" if row_number == 0 else "F1"
                wrapped = [_wrap(_plain(cell), column_width - 6, 9, font) for cell in row]
                height = max(len(cell) for cell in wrapped) if wrapped else 1
                pages.need(height * 9 * 1.35 + 4)
                top = pages.y
                for column, cell in enumerate(wrapped):
                    pages.y = top
                    for line in cell:
                        pages.text(_MARGIN + column * column_width, line, 9, font)
                pages.y = top - height * 9 * 1.35
                pages.rule()
            pages.gap(4)
        elif kind == "code":
            for line in _wrap(value, width, 9, "F3"):
                pages.text(_MARGIN, line, 9, "F3")
            pages.gap(5)
        elif kind == "rule":
            pages.rule()
    return _pdf_file(pages.pages, title)


def _pdf_file(pages: list[list[bytes]], title: str) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    tree = add(b"")
    fonts = [
        add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name)
        for name in (b"Helvetica", b"Helvetica-Bold", b"Courier")
    ]
    resources = b"<< /Font << /F1 %d 0 R /F2 %d 0 R /F3 %d 0 R >> >>" % tuple(fonts)
    kids = []
    total = len(pages)
    for number, content in enumerate(pages, start=1):
        footer = b"BT /F1 8 Tf %.2f 30 Td (%s) Tj ET" % (_MARGIN, _encode(f"{title} - {number}/{total}"))
        stream = b"\n".join([*content, footer])
        content_id = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R >>"
                % (tree, int(_PAGE_WIDTH), int(_PAGE_HEIGHT), resources, content_id)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    info = add(b"<< /Title (%s) /Producer (Nodestro) >>" % _encode(title))
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    start = output.tell()
    output.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        output.write(b"%010d 00000 n \n" % offset)
    output.write(
        b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog, info, start)
    )
    return output.getvalue()


# ---- rendering -------------------------------------------------------------------------


def render(value: Any, fmt: ReportFormat, *, title: str, run: Run, sanitizer: Sanitizer | None = None) -> bytes:
    """The bytes of one result file; raises :class:`ReportError`."""
    if value is None:
        raise ReportError("report.no_result", "The run has no result to write")
    value = parse_result(value)
    if sanitizer is not None:
        value = sanitizer.value(value)
    if fmt == "json":
        document = {
            "workflow": run.workflow_name,
            "run_id": run.id,
            "status": run.status,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "result": value,
        }
        if run.values:
            document["input_values"] = sanitizer.value(run.values) if sanitizer else run.values
        data = json.dumps(document, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    elif fmt == "csv":
        data = csv_bytes(to_rows(value))
    elif fmt == "xlsx":
        data = to_xlsx(value, title, run.finished_at or run.created_at)
    else:
        markdown = document_markdown(title, run, value)
        if fmt == "md":
            data = markdown.encode("utf-8")
        elif fmt == "html":
            data = to_html(markdown, title).encode("utf-8")
        elif fmt == "docx":
            data = to_docx(markdown, title, run.finished_at or run.created_at)
        else:
            data = to_pdf(markdown, title)
    if len(data) > MAX_BYTES:
        raise ReportError("report.too_large", "The file would be larger than 10 MB", limit_mb=10)
    return data


class ResultFiles:
    """The result files of runs."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def list(self, run_id: str) -> list[ResultFile]:
        return sorted(self.store.list_prefix(FILES, f"{run_id}:", ResultFile), key=lambda entry: entry.created_at)

    def get(self, run_id: str, file_id: str) -> ResultFile:
        found = self.store.get(FILES, f"{run_id}:{file_id}", ResultFile)
        if found is None:
            raise KeyError(file_id)
        return found

    def content(self, run_id: str, file_id: str) -> tuple[ResultFile, bytes]:
        entry = self.get(run_id, file_id)
        if entry.error_code:
            raise ReportError(entry.error_code, entry.error or "The file could not be written", **entry.error_params)
        raw = self.store.get_raw(DATA, f"{run_id}:{file_id}")
        if raw is None:
            raise KeyError(file_id)
        return entry, base64.b64decode(raw)

    def delete_run(self, run_id: str) -> None:
        self.store.delete_prefix(FILES, f"{run_id}:")
        self.store.delete_prefix(DATA, f"{run_id}:")

    def delete(self, run_id: str, file_id: str) -> None:
        if not self.store.delete(FILES, f"{run_id}:{file_id}"):
            raise KeyError(file_id)
        self.store.delete(DATA, f"{run_id}:{file_id}")

    def _value(self, run: Run, node_id: str | None) -> tuple[Any, str]:
        if node_id:
            node = next((item for item in run.workflow.nodes if item.id == node_id), None)
            state = run.nodes.get(node_id)
            if node is None or state is None:
                raise KeyError(node_id)
            if state.status != "succeeded":
                raise ReportError("report.node_not_finished", "This step has no result", node=node.label or node_id)
            return state.output, node.label
        return run.output, ""

    def create(
        self, run: Run, request: FileRequest, *, source: Literal["output", "manual"] = "manual", strict: bool = True
    ) -> ResultFile:
        """Write one file; with ``strict=False`` a failure is stored as a file with an error."""
        if run.imported_at is not None and source == "output":
            raise ReportError("report.imported", "Imported runs do not write files")
        taken = {entry.name for entry in self.list(run.id)}
        name = _unique(file_name(request.name, run.workflow_name, request.format), taken)
        entry = ResultFile(
            run_id=run.id,
            name=name,
            format=request.format,
            media_type=MEDIA_TYPES[request.format],
            source=source,
            node_id=request.node_id,
            sanitized=request.sanitize,
        )
        try:
            value, label = self._value(run, request.node_id)
            sanitizer = Sanitizer(personal_data=request.personal_data) if request.sanitize else None
            title = name.rsplit(".", 1)[0] if request.name.strip() else (label or run.workflow_name)
            data = render(value, request.format, title=title, run=run, sanitizer=sanitizer)
        except ReportError as error:
            if strict:
                raise
            entry.error_code, entry.error, entry.error_params = error.code, str(error), dict(error.params)
            self.store.put(FILES, f"{run.id}:{entry.id}", entry)
            return entry
        entry.size = len(data)
        self.store.put_raw(DATA, f"{run.id}:{entry.id}", base64.b64encode(data).decode("ascii"))
        self.store.put(FILES, f"{run.id}:{entry.id}", entry)
        return entry

    def write_outputs(self, run: Run) -> builtins.list[ResultFile]:
        """The files the result nodes of a finished run are configured to write."""
        written = []
        for node in run.workflow.nodes:
            if node.type != "output" or node.output is None or not node.output.files:
                continue
            state = run.nodes.get(node.id)
            if state is None or state.status != "succeeded":
                continue
            for spec in node.output.files:
                written.append(self.create(run, _request(spec, node.id), source="output", strict=False))
        return written


def _request(spec: ResultFileSpec, node_id: str) -> FileRequest:
    return FileRequest(format=spec.format, name=spec.name, node_id=node_id, sanitize=spec.sanitize)
