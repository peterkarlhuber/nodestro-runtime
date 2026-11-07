"""Office documents and scanned PDFs as text: DOCX, XLSX and optional local OCR.

Everything here runs locally with the standard library (``zipfile`` and
``xml.etree``). Scanned PDFs need an OCR engine (:class:`~nodestro_runtime.extensions.OcrEngine`);
the local tesseract engine is an optional integration (``nodestro-integrations``).
No external account and no new Python library is needed.

- **DOCX**: paragraphs (headings become Markdown headings, list paragraphs bullets)
  and tables (Markdown tables), in document order.
- **XLSX**: one section per sheet with a table of the used cells. A formula is never
  evaluated: the cell shows the value Excel saved with the file and, next to it, the
  formula as text (``=SUM(A1:A3)``), so readers can tell the two apart.
- **PDF text**: read by orkexa's :func:`~orkexa.retrieval.pdf_pages_isolated` in a separate, limited process that
  is stopped at the time budget (MUP-ONA A1); the limits come from the environment (:func:`pdf_limits`).
- **Scanned PDF**: a PDF without a text layer. When OCR is available, the recognised
  text is indexed; words recognised with low confidence are marked ``[?word]`` and
  counted, so agents and users know the text is uncertain.

The converted text is what Nodestro stores and indexes for DOCX and XLSX (as a
Markdown file named after the original); the size limits of the knowledge store apply.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import io
import math
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
from xml.etree import ElementTree
from xml.parsers import expat
import zipfile

from .problems import NodestroError

if TYPE_CHECKING:
    from orkexa.retrieval import PdfLimits

#: Formats converted to Markdown before they are stored.
CONVERTED = {".docx": "docx", ".xlsx": "xlsx"}
_MAX_UNPACKED = 64 * 1024 * 1024
_MAX_PART = 32 * 1024 * 1024
_MAX_ROWS = 5_000
_MAX_COLUMNS = 200
_MAX_CELL = 2_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocumentError(NodestroError):
    """A document cannot be read."""


@dataclass
class Converted:
    """The text of a document and what readers should know about it."""

    text: str
    source_format: str
    notes: dict[str, str] = field(default_factory=dict)


def _open(data: bytes, name: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocumentError("document.unreadable", f"{name} is not a valid Office file", name=name) from None
    total = sum(item.file_size for item in archive.infolist())
    if total > _MAX_UNPACKED or any(item.file_size > _MAX_PART for item in archive.infolist()):
        raise DocumentError("document.too_large", f"{name} unpacks to too much data", name=name)
    return archive


class _Declared(Exception):
    pass


def _declared(*_args: object) -> None:
    raise _Declared


def _declares_dtd(raw: bytes) -> bool:
    """Whether an XML part declares a document type or an entity anywhere (RT-044): the whole part, in any encoding
    the XML declares (UTF-16 too), as the parser sees it, not only its first bytes. A part the parser cannot read is
    left to the reader, which reports it as damaged."""
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _declared
    parser.EntityDeclHandler = _declared
    try:
        parser.Parse(raw, True)
    except _Declared:
        return True
    except expat.ExpatError:
        return False
    return False


def _xml(archive: zipfile.ZipFile, part: str, name: str) -> ElementTree.Element | None:
    try:
        raw = archive.read(part)
    except KeyError:
        return None
    if _declares_dtd(raw):
        # Office files never declare a document type or entities; refusing them rules out entity expansion.
        raise DocumentError("document.unreadable", f"{name} contains unsupported XML", name=name)
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise DocumentError("document.unreadable", f"{name}: {part} is damaged", name=name) from None


def _cell_text(value: str) -> str:
    value = " ".join(value.split())
    if len(value) > _MAX_CELL:
        value = value[:_MAX_CELL] + "…"
    return value.replace("|", "\\|")


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(_cell_text(cell) for cell in rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(_cell_text(cell) for cell in row) + " |" for row in rows[1:]]
    return "\n".join(lines)


# ---- DOCX ----------------------------------------------------------------------------


def _runs(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _paragraph(element: ElementTree.Element) -> str:
    text = _runs(element).strip()
    if not text:
        return ""
    style = element.find(f"{_W}pPr/{_W}pStyle")
    style_name = (style.get(f"{_W}val") or "") if style is not None else ""
    heading = re.match(r"(?i)^(heading|berschrift|titre|t[ií]tulo|titolo)\s*(\d)$", style_name.replace("Ü", ""))
    if style_name.casefold() == "title":
        return f"# {text}"
    if heading:
        return "#" * min(int(heading.group(2)) + 1, 6) + f" {text}"
    if element.find(f"{_W}pPr/{_W}numPr") is not None or "list" in style_name.casefold():
        return f"- {text}"
    return text


def _docx_table(element: ElementTree.Element) -> str:
    rows = []
    for row in element.findall(f"{_W}tr"):
        cells = []
        for cell in row.findall(f"{_W}tc"):
            cells.append(" ".join(_runs(paragraph).strip() for paragraph in cell.findall(f"{_W}p")).strip())
        rows.append(cells)
    return _table(rows)


def docx_text(data: bytes, name: str = "document.docx") -> Converted:
    archive = _open(data, name)
    root = _xml(archive, "word/document.xml", name)
    if root is None:
        raise DocumentError("document.unreadable", f"{name} has no document body", name=name)
    body = root.find(f"{_W}body")
    blocks: list[str] = []
    tables = 0
    for element in list(body) if body is not None else []:
        if element.tag == f"{_W}p":
            text = _paragraph(element)
            if text:
                blocks.append(text)
        elif element.tag == f"{_W}tbl":
            tables += 1
            blocks.append(_docx_table(element))
    text = "\n\n".join(block for block in blocks if block)
    return Converted(text=text, source_format="docx", notes={"tables": str(tables)})


# ---- XLSX ----------------------------------------------------------------------------


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    index = 0
    for letter in letters.group(0) if letters else "A":
        index = index * 26 + ord(letter) - 64
    return index - 1


def _shared_strings(archive: zipfile.ZipFile, name: str) -> list[str]:
    root = _xml(archive, "xl/sharedStrings.xml", name)
    if root is None:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{_S}t")) for item in root.findall(f"{_S}si")]


def _sheets(archive: zipfile.ZipFile, name: str) -> list[tuple[str, str]]:
    """``(sheet name, part)`` in workbook order."""
    workbook = _xml(archive, "xl/workbook.xml", name)
    relations = _xml(archive, "xl/_rels/workbook.xml.rels", name)
    if workbook is None:
        raise DocumentError("document.unreadable", f"{name} has no workbook", name=name)
    targets = {}
    for relation in relations.findall(f"{_PKG}Relationship") if relations is not None else []:
        target = relation.get("Target") or ""
        target = target.lstrip("/")
        targets[relation.get("Id")] = target if target.startswith("xl/") else f"xl/{target}"
    found = []
    for index, sheet in enumerate(workbook.iter(f"{_S}sheet"), start=1):
        part = targets.get(sheet.get(f"{_R}id")) or f"xl/worksheets/sheet{index}.xml"
        found.append((sheet.get("name") or f"Sheet{index}", part))
    return found


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    value = cell.find(f"{_S}v")
    raw = value.text if value is not None and value.text is not None else ""
    if kind == "s" and raw.isdigit():
        return shared[int(raw)] if int(raw) < len(shared) else ""
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_S}t"))
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


@dataclass
class Sheet:
    """The used cells of one worksheet: row number -> column index -> shown text."""

    name: str
    rows: dict[int, dict[int, str]]
    formulas: int = 0
    truncated: bool = False
    #: The workbook names the sheet, but its part is missing.
    missing: bool = False

    def grid(self) -> list[list[str]]:
        """The cells as rows of equal length, in row order (empty rows left out)."""
        if not self.rows:
            return []
        width = max(max(columns) for columns in self.rows.values()) + 1
        return [[self.rows[number].get(column, "") for column in range(width)] for number in sorted(self.rows)]


def xlsx_sheets(data: bytes, name: str = "workbook.xlsx", *, max_rows: int = _MAX_ROWS) -> list[Sheet]:
    """Every worksheet with its used cells; formulas are never evaluated (value plus formula text)."""
    archive = _open(data, name)
    shared = _shared_strings(archive, name)
    found: list[Sheet] = []
    for sheet_name, part in _sheets(archive, name):
        root = _xml(archive, part, name)
        sheet = Sheet(name=sheet_name, rows={})
        found.append(sheet)
        if root is None:
            sheet.missing = True
            continue
        for row_index, row in enumerate(root.iter(f"{_S}row")):
            if row_index >= max_rows:
                sheet.truncated = True
                break
            reference = row.get("r") or ""
            number = int(reference) if reference.isdigit() else row_index + 1
            for position, cell in enumerate(row.findall(f"{_S}c")):
                column = _column_index(cell.get("r") or "") if cell.get("r") else position
                if column >= _MAX_COLUMNS:
                    sheet.truncated = True
                    continue
                shown = _cell_value(cell, shared)
                formula = cell.find(f"{_S}f")
                expression = (formula.text or "").strip() if formula is not None else ""
                if expression:
                    # Never evaluated: the saved value and the formula as text.
                    sheet.formulas += 1
                    saved = shown if shown != "" else "?"
                    shown = f"{saved} (formula: ={expression})"
                if shown != "":
                    sheet.rows.setdefault(number, {})[column] = shown
    return found


def xlsx_text(data: bytes, name: str = "workbook.xlsx") -> Converted:
    sections: list[str] = []
    sheets = xlsx_sheets(data, name)
    for sheet in sheets:
        if sheet.missing:
            continue
        if not sheet.rows:
            sections.append(f"## {sheet.name}\n\n(empty)")
            continue
        width = max(max(columns) for columns in sheet.rows.values()) + 1
        header = ["Row"] + [_column_name(column) for column in range(width)]
        lines = [header] + [
            [str(number), *cells] for number, cells in zip(sorted(sheet.rows), sheet.grid(), strict=True)
        ]
        sections.append(f"## {sheet.name}\n\n{_table(lines)}")
    notes = {"sheets": str(len(sheets)), "formulas": str(sum(sheet.formulas for sheet in sheets))}
    if any(sheet.truncated for sheet in sheets):
        notes["truncated"] = "yes"
    intro = "Formulas are shown as text next to the value saved in the file; they are not calculated."
    return Converted(text=f"{intro}\n\n" + "\n\n".join(sections), source_format="xlsx", notes=notes)


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def convert(data: bytes, name: str) -> Converted | None:
    """The Markdown text of a DOCX or XLSX file; ``None`` for other formats."""
    kind = CONVERTED.get(Path(name).suffix.lower())
    if kind == "docx":
        return docx_text(data, name)
    if kind == "xlsx":
        return xlsx_text(data, name)
    return None


def rows_of_xlsx(data: bytes, name: str = "workbook.xlsx") -> list[dict[str, str]]:
    """The first sheet as records (first row = column names); values as saved, formulas not evaluated."""
    archive = _open(data, name)
    shared = _shared_strings(archive, name)
    sheets = _sheets(archive, name)
    if not sheets:
        return []
    root = _xml(archive, sheets[0][1], name)
    if root is None:
        return []
    grid: list[dict[int, str]] = []
    for row_index, row in enumerate(root.iter(f"{_S}row")):
        if row_index > _MAX_ROWS:
            break
        cells: dict[int, str] = {}
        for position, cell in enumerate(row.findall(f"{_S}c")):
            column = _column_index(cell.get("r") or "") if cell.get("r") else position
            cells[column] = _cell_value(cell, shared)
        grid.append(cells)
    if not grid:
        return []
    header = grid[0]
    columns = sorted(header)
    return [
        {header[column].strip() or _column_name(column): row.get(column, "") for column in columns}
        for row in grid[1:]
        if any(value.strip() for value in row.values())
    ]


# ---- scanned PDFs and OCR ------------------------------------------------------------


#: The PDF limits (MUP-ONA A1, decision O6): wall-clock seconds, pages, characters of text and the memory of the
#: extracting process in MiB (enforced on Linux only).
PDF_TIMEOUT_SECONDS = 30.0
PDF_MAX_PAGES = 2000
PDF_MAX_CHARS = 5_000_000
PDF_MAX_MEMORY_MB = 512
#: The environment variables that change them, by the field of :class:`orkexa.retrieval.PdfLimits` they set.
PDF_LIMITS_ENV = {
    "timeout": "NODESTRO_PDF_TIMEOUT_SECONDS",
    "max_pages": "NODESTRO_PDF_MAX_PAGES",
    "max_chars": "NODESTRO_PDF_MAX_CHARS",
    "max_memory_mb": "NODESTRO_PDF_MAX_MEMORY_MB",
}
#: The smallest memory limit orkexa accepts (MiB); less cannot start the interpreter.
_PDF_MIN_MEMORY_MB = 16


def pdf_limits(environ: Mapping[str, str] | None = None) -> PdfLimits:
    """The limits for reading one PDF: the defaults above, changed by the variables in :data:`PDF_LIMITS_ENV`.

    An empty variable keeps its default. A value that is not a number, not finite, not above zero (the time budget,
    pages, characters) or below 16 (the memory limit) raises :class:`DocumentError` ``document.limits_invalid`` with
    the variable's name, instead of reading PDFs without the limit the operator meant to set.
    """
    from orkexa.retrieval import PdfLimits

    environ = os.environ if environ is None else environ
    values: dict[str, float | int] = {
        "timeout": PDF_TIMEOUT_SECONDS,
        "max_pages": PDF_MAX_PAGES,
        "max_chars": PDF_MAX_CHARS,
        "max_memory_mb": PDF_MAX_MEMORY_MB,
    }
    for name, variable in PDF_LIMITS_ENV.items():
        raw = environ.get(variable, "").strip()
        if not raw:
            continue
        value: float | int
        try:
            value = float(raw) if name == "timeout" else int(raw)
        except ValueError:
            raise DocumentError(
                "document.limits_invalid", f"{variable} must be a number, not {raw[:40]!r}", variable=variable
            ) from None
        minimum = _PDF_MIN_MEMORY_MB if name == "max_memory_mb" else 0
        if not math.isfinite(value) or value < minimum or value <= 0:
            raise DocumentError(
                "document.limits_invalid",
                f"{variable} must be at least {minimum}" if minimum else f"{variable} must be greater than 0",
                variable=variable,
            )
        values[name] = value
    return PdfLimits(
        timeout=float(values["timeout"]),
        max_pages=int(values["max_pages"]),
        max_chars=int(values["max_chars"]),
        max_memory_mb=int(values["max_memory_mb"]),
    )


def pdf_pages(data: bytes, name: str, *, limits: PdfLimits | None = None) -> list[str]:
    """The text of each page of a PDF, read within the PDF limits (``limits``, else :func:`pdf_limits`).

    The text is extracted by orkexa's :func:`~orkexa.retrieval.pdf_pages_isolated` in a separate process with a
    minimal environment, which is stopped when the time budget runs out, so no PDF can block or exhaust the calling
    process. A PDF over a limit raises :class:`DocumentError` ``document.too_complex`` (parameters ``name`` and
    ``reason``: ``pages``, ``chars``, ``timeout``, ``memory``, ``limit`` or ``crash``); a PDF that cannot be read
    raises ``ValueError``.
    """
    from orkexa.retrieval import RetrievalError, pdf_pages_isolated

    chosen = pdf_limits() if limits is None else limits
    try:
        return pdf_pages_isolated(
            data,
            timeout=chosen.timeout,
            max_memory_mb=chosen.max_memory_mb,
            max_pages=chosen.max_pages,
            max_chars=chosen.max_chars,
        )
    except RetrievalError as exc:
        if exc.code != "document.too_complex":
            raise
        raise DocumentError(
            "document.too_complex", f"{name} is too complex to read: {exc}", name=name, reason=exc.reason or "crash"
        ) from None


def read_pdf(data: bytes, name: str) -> list[str] | None:
    """The text of each page of a PDF that is about to be stored, read within the PDF limits (:func:`pdf_pages`).

    Raises :class:`DocumentError`: ``document.too_complex`` over a limit, ``document.encrypted`` for a PDF protected
    by a password, ``document.unreadable`` for a damaged, truncated or empty file or one without pages. ``None``: the
    PDF opens and has pages, but its text cannot be extracted (it is stored without being searchable).
    """
    try:
        pages = pdf_pages(data, name)
    except DocumentError:
        raise
    except ValueError:
        # Which refusal: the same checks as before a PDF was read in its own process. They only open the file and
        # count its pages, work the limited process has already done without hitting a limit.
        check_pdf(data, name)
        return None
    if not pages:
        raise DocumentError("document.unreadable", f"{name} is not a readable PDF", name=name)
    return pages


def check_pdf(data: bytes, name: str) -> None:
    """Raise :class:`DocumentError` unless the PDF opens and its pages can be counted.

    Runs before a PDF is stored, so a damaged, truncated, empty or password-protected file
    is refused with a clear code instead of leaving a half-stored entry behind.
    """
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError

    try:
        reader = PdfReader(io.BytesIO(data))
        if not reader.pages:
            raise ValueError("no pages")
    except FileNotDecryptedError:
        raise DocumentError(
            "document.encrypted", f"{name} is protected by a password and cannot be read", name=name
        ) from None
    except Exception:  # pypdf raises many types for damaged files; all mean the same here
        raise DocumentError("document.unreadable", f"{name} is not a readable PDF", name=name) from None


@dataclass
class OcrResult:
    text: str
    pages: int
    uncertain: int
    words: int
    language: str


OcrRunner = Callable[[bytes, str], OcrResult]


def ocr_intro(result: OcrResult) -> str:
    pages = "1 page" if result.pages == 1 else f"{result.pages} pages"
    words = "1 word" if result.words == 1 else f"{result.words} words"
    return (
        f"Text recognised by OCR ({pages}, {result.uncertain} of {words} uncertain, "
        "marked [?word]). Check important passages against the original."
    )
