"""The supported file formats and how Nodestro reads them: one place for every file feature.

Workflow inputs (:mod:`nodestro_runtime.input_files`), knowledge (:mod:`nodestro_runtime.knowledge`),
batches and the published APIs all recognise, check and read files here, so a format is
added once and works everywhere:

- :data:`FORMATS` lists the formats with their group (text, data, document, image,
  archive), file extensions and media type. Fields of an input form name the formats or
  groups they accept (:func:`accepted`).
- :func:`detect` decides the format from the name *and* the content (magic bytes, valid
  UTF-8, valid JSON, an Office package that is really a Word file ...). A file whose
  content does not fit its extension is refused.
- :func:`extract` reads a file locally and deterministically, without any AI request:
  text and Markdown as they are, CSV and XLSX as tables (every sheet), JSON with a short
  description of its shape, PDF page by page (optionally local OCR for scans), DOCX with
  headings and tables (:mod:`nodestro_runtime.documents`), images only their size, and ZIP
  archives as a list of entries (:func:`inspect_archive`).
- ZIP archives are never unpacked to disk. Every entry is checked (paths, links,
  encryption, size, compression ratio) and read one at a time on demand
  (:func:`read_member`); nested archives are listed but not opened, and nothing in an
  archive is ever executed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import csv
from dataclasses import dataclass
import io
import json
from pathlib import PurePosixPath
import re
import stat
from typing import Any, Literal
import zipfile

from pydantic import BaseModel, Field

from . import documents
from .problems import NodestroError

Group = Literal["text", "data", "document", "image", "archive"]
GROUPS: tuple[str, ...] = ("text", "data", "document", "image", "archive")


@dataclass(frozen=True)
class FileFormat:
    """One supported format."""

    key: str
    group: Group
    extensions: tuple[str, ...]
    media_type: str
    #: Whether knowledge can search the file's text.
    indexable: bool = True


#: Source code and configuration files: read as text (inside ZIP archives and as uploads).
CODE_EXTENSIONS: tuple[str, ...] = (
    ".py", ".pyi", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts", ".scala",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".m", ".r", ".jl",
    ".lua", ".pl", ".dart", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".html", ".htm",
    ".css", ".scss", ".less", ".vue", ".svelte", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env.example", ".properties", ".gradle", ".tex", ".rst", ".adoc", ".graphql", ".proto",
    ".tf", ".dockerfile", ".gitignore", ".editorconfig", ".lock", ".log",
)  # fmt: skip
#: Extension-less files that are text by convention.
_TEXT_NAMES = {"dockerfile", "makefile", "license", "readme", "procfile", "gemfile", "rakefile", "jenkinsfile"}

FORMATS: dict[str, FileFormat] = {
    item.key: item
    for item in (
        FileFormat("txt", "text", (".txt",), "text/plain"),
        FileFormat("md", "text", (".md", ".markdown"), "text/markdown"),
        FileFormat("code", "text", CODE_EXTENSIONS, "text/plain"),
        FileFormat("csv", "data", (".csv", ".tsv"), "text/csv"),
        FileFormat("json", "data", (".json",), "application/json"),
        FileFormat("xlsx", "data", (".xlsx",), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        FileFormat("pdf", "document", (".pdf",), "application/pdf"),
        FileFormat(
            "docx", "document", (".docx",), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        FileFormat("png", "image", (".png",), "image/png", indexable=False),
        FileFormat("jpg", "image", (".jpg", ".jpeg"), "image/jpeg", indexable=False),
        FileFormat("webp", "image", (".webp",), "image/webp", indexable=False),
        FileFormat("zip", "archive", (".zip",), "application/zip", indexable=False),
    )
}
_BY_EXTENSION = {extension: item for item in FORMATS.values() for extension in item.extensions}
#: Archives inside an archive: listed, never opened (the processing depth is one archive).
_NESTED_ARCHIVES = (".zip", ".jar", ".war", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar")
#: Programs and scripts: they are text to read at most, never something to run.
EXECUTABLE_EXTENSIONS = (".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".app", ".com", ".scr", ".apk")

# ---- limits ------------------------------------------------------------------------------

#: The largest single upload (MB); a field can set a lower limit.
MAX_UPLOAD_MB = 50
#: The default size limit of a file field (MB).
DEFAULT_FIELD_MB = 20
#: Entries of one archive.
MAX_ARCHIVE_ENTRIES = 5000
#: Uncompressed size of all entries of one archive.
MAX_ARCHIVE_UNPACKED = 512 * 1024 * 1024
#: Uncompressed size of one entry that is read.
MAX_MEMBER_BYTES = 25 * 1024 * 1024
#: Compression ratio above which a large entry counts as a "zip bomb".
MAX_RATIO = 100
#: Characters of readable text kept per file (the rest is available in parts via the files tool).
MAX_TEXT_CHARS = 2_000_000
#: Rows kept per table.
MAX_TABLE_ROWS = 20_000


class FileFormatError(NodestroError):
    """A file cannot be accepted or read."""


# ---- formats -----------------------------------------------------------------------------


def suffix_of(name: str) -> str:
    """The lower-case extension with its dot (``.pdf``); empty without one."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(".env.example"):
        return ".env.example"
    return "." + base.rsplit(".", 1)[-1] if "." in base else ""


def format_of_name(name: str) -> FileFormat | None:
    """The format a file name suggests (``None``: not supported)."""
    suffix = suffix_of(name)
    if suffix in _BY_EXTENSION:
        return _BY_EXTENSION[suffix]
    base = name.rsplit("/", 1)[-1].lower()
    if not suffix and base.split(".")[0] in _TEXT_NAMES:
        return FORMATS["code"]
    return None


def accept_known(item: str) -> bool:
    """Whether an ``accept`` entry names a format, a group or a supported extension."""
    value = item.strip().lower().lstrip(".")
    return value in GROUPS or value in FORMATS or value == "jpeg" or f".{value}" in _BY_EXTENSION


def accepted(accept: Iterable[str]) -> set[str]:
    """The format keys of a field's ``accept`` list (format keys and/or groups); empty: all."""
    keys: set[str] = set()
    for item in accept:
        value = item.strip().lower().lstrip(".")
        if value in GROUPS:
            keys |= {key for key, spec in FORMATS.items() if spec.group == value}
        elif value in FORMATS:
            keys.add(value)
        elif value == "jpeg":
            keys.add("jpg")
        elif f".{value}" in _BY_EXTENSION:
            keys.add(_BY_EXTENSION[f".{value}"].key)
    return keys or set(FORMATS)


def accept_extensions(accept: Iterable[str]) -> list[str]:
    """The file extensions of an ``accept`` list (for the browser's file chooser)."""
    keys = accepted(accept)
    return sorted({extension for key in keys for extension in FORMATS[key].extensions})


def formats_overview() -> list[dict[str, Any]]:
    """Every format for the interface: key, group, extensions, whether knowledge can search it."""
    return [
        {"key": item.key, "group": item.group, "extensions": list(item.extensions), "indexable": item.indexable}
        for item in FORMATS.values()
    ]


# ---- detection ---------------------------------------------------------------------------


def _decode(data: bytes) -> tuple[str, str | None]:
    """Text of UTF-8 data (with or without BOM); Windows-1252 as a fallback, reported as a note."""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:8192]:
        raise UnicodeDecodeError("utf-8", data[:1], 0, 1, "binary data")
    return data.decode("cp1252", errors="replace"), "input.decoded_cp1252"


def _image_matches(key: str, data: bytes) -> bool:
    if key == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if key == "jpg":
        return data.startswith(b"\xff\xd8\xff")
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _office_part(data: bytes) -> str | None:
    """``docx``/``xlsx`` for a Word or Excel package, ``zip`` for another ZIP archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "zip"


def detect(name: str, data: bytes) -> FileFormat:
    """The format of a file, checked against its content; raises :class:`FileFormatError`."""
    spec = format_of_name(name)
    if spec is None:
        raise FileFormatError(
            "input.unsupported_format", f"{name}: this file type is not supported", name=name, suffix=suffix_of(name)
        )
    if not data:
        raise FileFormatError("input.empty", f"{name} is empty", name=name)
    mismatch = FileFormatError(
        "input.content_mismatch", f"{name}: the content is not a {spec.key.upper()} file", name=name, format=spec.key
    )
    if spec.group == "image":
        if not _image_matches(spec.key, data):
            raise mismatch
    elif spec.key == "pdf":
        if b"%PDF-" not in data[:1024]:
            raise mismatch
    elif spec.key in ("docx", "xlsx", "zip"):
        if not data.startswith(b"PK"):
            raise mismatch
        found = _office_part(data)
        if found is None:
            raise FileFormatError("input.damaged", f"{name} is damaged and cannot be opened", name=name)
        if spec.key in ("docx", "xlsx") and found != spec.key:
            raise mismatch
    else:
        if data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff")):
            raise mismatch
        try:
            text, _note = _decode(data)
        except UnicodeDecodeError:
            raise FileFormatError("input.not_text", f"{name} is not a text file", name=name) from None
        if spec.key == "json":
            try:
                json.loads(text)
            except ValueError as exc:
                raise FileFormatError(
                    "input.json_invalid", f"{name} is not valid JSON: {exc}", name=name, detail=str(exc)[:200]
                ) from None
    return spec


# ---- extraction --------------------------------------------------------------------------


class Table(BaseModel):
    """A table of a CSV file or an Excel sheet: column names and rows (texts as shown)."""

    name: str
    columns: list[str]
    rows: list[list[str]] = Field(default_factory=list)
    #: Data rows in the file (``rows`` may hold fewer when the table was cut).
    total_rows: int = 0
    truncated: bool = False
    formulas: int = 0


class ArchiveEntry(BaseModel):
    """One entry of a ZIP archive."""

    path: str
    size: int = 0
    compressed: int = 0
    is_dir: bool = False
    format: str | None = None
    group: str | None = None
    #: ``ok`` readable; ``unsupported`` kept in the list, not read; ``nested`` an archive inside the
    #: archive (not opened); ``skipped`` refused for safety (see ``reason``).
    status: Literal["ok", "unsupported", "nested", "skipped"] = "ok"
    #: ``unsafe_path``, ``link``, ``encrypted``, ``too_large``, ``ratio``, ``system_file`` or ``executable``.
    reason: str | None = None


class Extracted(BaseModel):
    """What Nodestro read from a file, locally and without AI."""

    format: str
    group: str
    #: The readable content as Markdown (tables as Markdown tables); empty for images and archives.
    text: str = ""
    #: Length of the whole text (``text`` may be cut at :data:`MAX_TEXT_CHARS`).
    chars: int = 0
    #: PDF: the text of each page.
    pages: list[str] = Field(default_factory=list)
    #: CSV and XLSX: the tables.
    tables: list[Table] = Field(default_factory=list)
    #: ZIP: the entries.
    entries: list[ArchiveEntry] = Field(default_factory=list)
    #: Numbers worth showing: pages, sheets, rows, columns, width, height, entries, files ...
    info: dict[str, Any] = Field(default_factory=dict)
    #: Codes of things the user and the agents should know (``input.pdf_scanned`` ...).
    notes: list[str] = Field(default_factory=list)
    #: A short description of the structure (shown to agents instead of a large content).
    outline: str = ""


def _cut(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


def read_csv(data: bytes) -> tuple[list[list[str]], str | None]:
    """The rows of a CSV file (comma, semicolon or tab; UTF-8 or Windows-1252) and an encoding note."""
    content, note = _decode(data)
    try:
        dialect: Any = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(content), dialect=dialect)]
    return [row for row in rows if any(cell for cell in row)], note


def _table_from_grid(name: str, grid: list[list[str]], *, truncated: bool = False, formulas: int = 0) -> Table:
    if not grid:
        return Table(name=name, columns=[], total_rows=0, truncated=truncated, formulas=formulas)
    width = max(len(row) for row in grid)
    header = [cell.strip() or documents._column_name(index) for index, cell in enumerate(grid[0] + [""] * width)][
        :width
    ]
    body = [row + [""] * (width - len(row)) for row in grid[1:]]
    return Table(
        name=name,
        columns=header,
        rows=body[:MAX_TABLE_ROWS],
        total_rows=len(body),
        truncated=truncated or len(body) > MAX_TABLE_ROWS,
        formulas=formulas,
    )


def table_markdown(table: Table, *, start: int = 0, limit: int | None = None) -> str:
    """Rows ``start`` to ``start + limit`` of a table as a Markdown table with its column names."""
    rows = table.rows[start : (start + limit) if limit is not None else None]
    return documents._table([table.columns, *rows]) if table.columns else ""


def _shape(value: Any, depth: int = 0) -> str:
    """A short description of a JSON value's structure."""
    indent = "  " * depth
    if isinstance(value, dict):
        if depth > 2:
            return f"{indent}object with {len(value)} keys"
        lines = [f"{indent}object with {len(value)} keys:"]
        for key, item in list(value.items())[:30]:
            kind = _shape(item, depth + 1).strip().split("\n")[0]
            lines.append(f"{indent}  - {key}: {kind}")
        if len(value) > 30:
            lines.append(f"{indent}  - … {len(value) - 30} more keys")
        return "\n".join(lines)
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value[:50]):
            keys = list(dict.fromkeys(key for item in value[:50] for key in item))
            return f"{indent}list of {len(value)} records with the fields {', '.join(map(str, keys[:30]))}"
        return f"{indent}list of {len(value)} values"
    if isinstance(value, str):
        return f"{indent}text"
    if isinstance(value, bool):
        return f"{indent}boolean"
    if isinstance(value, (int, float)):
        return f"{indent}number"
    return f"{indent}null"


def _records_table(name: str, value: Any) -> Table | None:
    """A JSON list of records as a table (for targeted reading of large JSON data)."""
    records = value if isinstance(value, list) else None
    if records is None and isinstance(value, dict):
        lists = [item for item in value.values() if isinstance(item, list)]
        records = lists[0] if len(lists) == 1 else None
    if not records or not all(isinstance(item, dict) for item in records):
        return None
    columns = list(dict.fromkeys(str(key) for item in records for key in item))
    rows = [
        [
            item.get(column) if isinstance(item.get(column), str) else json.dumps(item.get(column), ensure_ascii=False)
            for column in columns
        ]
        for item in records[:MAX_TABLE_ROWS]
    ]
    rows = [["" if cell in (None, "null") else str(cell) for cell in row] for row in rows]
    return Table(
        name=name, columns=columns, rows=rows, total_rows=len(records), truncated=len(records) > MAX_TABLE_ROWS
    )


def _image_size(key: str, data: bytes) -> tuple[int, int] | None:
    try:
        if key == "png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if key == "webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8 ":
                return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if key == "jpg":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                length = int.from_bytes(data[index + 2 : index + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(data[index + 7 : index + 9], "big"), int.from_bytes(
                        data[index + 5 : index + 7], "big"
                    )
                index += 2 + length
    except (IndexError, ValueError):
        return None
    return None


def extract(
    name: str,
    data: bytes,
    *,
    spec: FileFormat | None = None,
    ocr: Callable[[bytes, str], documents.OcrResult] | None = None,
) -> Extracted:
    """Read a file (see the module description); raises :class:`FileFormatError` when it is damaged.

    ``ocr`` recognises the text of a scanned PDF (local tesseract); without it a scan has no text.
    """
    spec = spec or detect(name, data)
    result = Extracted(format=spec.key, group=spec.group)
    try:
        _extract_into(result, spec, name, data, ocr)
    except FileFormatError:
        raise
    except documents.DocumentError as error:
        raise FileFormatError(
            "input.damaged" if error.code == "document.unreadable" else error.code,
            str(error),
            name=name,
            **{key: value for key, value in error.params.items() if key != "name"},
        ) from None
    except Exception as error:  # a damaged file must never stop the caller
        raise FileFormatError("input.damaged", f"{name} cannot be read: {error}", name=name) from None
    full = result.text
    result.chars = result.chars or len(full)
    result.text, cut = _cut(full)
    if cut:
        result.notes.append("input.text_cut")
    return result


def _extract_into(
    result: Extracted,
    spec: FileFormat,
    name: str,
    data: bytes,
    ocr: Callable[[bytes, str], documents.OcrResult] | None,
) -> None:
    key = spec.key
    if key in ("txt", "md", "code"):
        text, note = _decode(data)
        result.text = text
        result.info["lines"] = text.count("\n") + 1
        if note:
            result.notes.append(note)
        result.outline = f"{spec.key} text, {result.info['lines']} lines, {len(text)} characters"
    elif key == "csv":
        grid, note = read_csv(data)
        if note:
            result.notes.append(note)
        table = _table_from_grid(PurePosixPath(name).stem or "table", grid)
        result.tables = [table]
        result.info.update(rows=table.total_rows, columns=len(table.columns))
        result.text = table_markdown(table)
        result.outline = (
            f"table with {table.total_rows} rows and {len(table.columns)} columns: {', '.join(table.columns[:40])}"
        )
    elif key == "json":
        text, note = _decode(data)
        value = json.loads(text)
        if note:
            result.notes.append(note)
        result.text = json.dumps(value, ensure_ascii=False, indent=2)
        result.outline = _shape(value)
        records = _records_table(PurePosixPath(name).stem or "records", value)
        if records is not None:
            result.tables = [records]
            result.info.update(rows=records.total_rows, columns=len(records.columns))
    elif key == "xlsx":
        sheets = documents.xlsx_sheets(data, name, max_rows=MAX_TABLE_ROWS + 1)
        tables = [
            _table_from_grid(sheet.name, sheet.grid(), truncated=sheet.truncated, formulas=sheet.formulas)
            for sheet in sheets
            if not sheet.missing
        ]
        result.tables = tables
        result.info.update(
            sheets=len(tables), rows=sum(table.total_rows for table in tables), formulas=sum(t.formulas for t in tables)
        )
        if result.info["formulas"]:
            result.notes.append("input.formulas_not_evaluated")
        if any(table.truncated for table in tables):
            result.notes.append("input.table_cut")
        result.text = "\n\n".join(f"## {table.name}\n\n{table_markdown(table) or '(empty)'}" for table in tables)
        result.outline = "\n".join(
            f"sheet {table.name!r}: {table.total_rows} rows, columns {', '.join(table.columns[:30])}"
            for table in tables
        )
    elif key == "docx":
        converted = documents.docx_text(data, name)
        result.text = converted.text
        result.info["tables"] = int(converted.notes.get("tables", "0"))
        headings = [line.lstrip("#").strip() for line in converted.text.splitlines() if line.startswith("#")]
        result.info["headings"] = len(headings)
        result.outline = "document" + (f" with the headings: {'; '.join(headings[:40])}" if headings else "")
    elif key == "pdf":
        pages = documents.pdf_pages(data)
        result.info["pages"] = len(pages)
        if not any(page.strip() for page in pages):
            result.notes.append("input.pdf_scanned")
            if ocr is not None:
                try:
                    recognised = ocr(data, name)
                except documents.DocumentError:
                    result.notes.append("input.ocr_failed")
                else:
                    result.notes.append("input.ocr_used")
                    result.info.update(ocr_uncertain=recognised.uncertain, ocr_words=recognised.words)
                    pages = [part.split("\n\n", 1)[-1] for part in recognised.text.split("## Page ")[1:]] or [
                        recognised.text
                    ]
                    result.pages = pages
                    result.text = documents.ocr_intro(recognised) + "\n\n" + _pages_text(pages)
            result.outline = f"PDF with {result.info['pages']} pages (scanned, no text layer)"
        else:
            result.pages = pages
            result.text = _pages_text(pages)
            result.outline = f"PDF with {len(pages)} pages"
    elif spec.group == "image":
        size = _image_size(key, data)
        if size is not None:
            result.info.update(width=size[0], height=size[1])
        result.outline = f"image ({key.upper()}" + (f", {size[0]}x{size[1]} px)" if size else ")")
    elif key == "zip":
        entries = inspect_archive(data, name)
        result.entries = entries
        files = [entry for entry in entries if not entry.is_dir]
        result.info.update(
            entries=len(files),
            readable=sum(entry.status == "ok" for entry in files),
            skipped=sum(entry.status == "skipped" for entry in files),
            unpacked=sum(entry.size for entry in files),
        )
        if result.info["skipped"]:
            result.notes.append("input.archive_entries_skipped")
        if any(entry.status == "nested" for entry in files):
            result.notes.append("input.archive_nested")
        result.outline = archive_tree(name, entries)


def _pages_text(pages: list[str]) -> str:
    return "\n\n".join(f"## Page {number}\n\n{page.strip()}" for number, page in enumerate(pages, start=1))


# ---- ZIP archives ------------------------------------------------------------------------

_SYSTEM_FILES = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")


def safe_path(raw: str) -> str | None:
    """The normalised relative path of an archive entry, or ``None`` when it is unsafe."""
    path = raw.replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path) or "\x00" in path:
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts) or not parts:
        return None
    if any(ord(char) < 32 for char in path):
        return None
    return "/".join(parts) + ("/" if path.endswith("/") else "")


def _open_archive(data: bytes, name: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise FileFormatError("input.archive_unreadable", f"{name} is not a readable ZIP archive", name=name) from None


def inspect_archive(data: bytes, name: str = "archive.zip") -> list[ArchiveEntry]:
    """Every entry of a ZIP archive with its status; raises when the archive as a whole is unsafe."""
    with _open_archive(data, name) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise FileFormatError(
                "input.archive_too_many_entries",
                f"{name} has more than {MAX_ARCHIVE_ENTRIES} entries",
                name=name,
                count=len(infos),
                limit=MAX_ARCHIVE_ENTRIES,
            )
        total = sum(info.file_size for info in infos)
        if total > MAX_ARCHIVE_UNPACKED:
            raise FileFormatError(
                "input.archive_too_large",
                f"{name} unpacks to more than {MAX_ARCHIVE_UNPACKED // (1024 * 1024)} MB",
                name=name,
                limit_mb=MAX_ARCHIVE_UNPACKED // (1024 * 1024),
            )
        compressed = sum(info.compress_size for info in infos)
        if total > 50 * 1024 * 1024 and compressed and total / compressed > MAX_RATIO:
            raise FileFormatError(
                "input.archive_ratio", f"{name} is compressed suspiciously strongly", name=name, ratio=MAX_RATIO
            )
        entries: list[ArchiveEntry] = []
        seen: set[str] = set()
        for info in infos:
            path = safe_path(info.filename)
            entry = ArchiveEntry(
                path=path.rstrip("/") if path else info.filename[:300],
                size=info.file_size,
                compressed=info.compress_size,
                is_dir=info.is_dir(),
            )
            mode = (info.external_attr >> 16) & 0o170000
            if path is None:
                entry.status, entry.reason = "skipped", "unsafe_path"
            elif mode == stat.S_IFLNK:
                entry.status, entry.reason = "skipped", "link"
            elif mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                entry.status, entry.reason = "skipped", "special"
            elif info.flag_bits & 0x1:
                entry.status, entry.reason = "skipped", "encrypted"
            elif any(part in path for part in _SYSTEM_FILES):
                continue
            elif entry.is_dir:
                pass
            elif entry.path.lower() in seen:
                entry.status, entry.reason = "skipped", "duplicate"
            elif info.file_size > MAX_MEMBER_BYTES:
                entry.status, entry.reason = "skipped", "too_large"
            elif (
                info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > MAX_RATIO
            ):
                entry.status, entry.reason = "skipped", "ratio"
            else:
                lowered = entry.path.lower()
                spec = format_of_name(entry.path)
                if lowered.endswith(_NESTED_ARCHIVES):
                    entry.status, entry.group, entry.format = (
                        "nested",
                        "archive",
                        "zip" if lowered.endswith(".zip") else None,
                    )
                elif lowered.endswith(EXECUTABLE_EXTENSIONS):
                    entry.status, entry.reason = "unsupported", "executable"
                elif spec is None:
                    entry.status = "unsupported"
                else:
                    entry.format, entry.group = spec.key, spec.group
            if not entry.is_dir:
                seen.add(entry.path.lower())
            entries.append(entry)
    return entries


def read_member(data: bytes, path: str, name: str = "archive.zip") -> bytes:
    """The bytes of one readable archive entry; the declared size is enforced while reading."""
    wanted = safe_path(path)
    if wanted is None:
        raise FileFormatError("input.archive_path_unsafe", f"{path} is not a safe path", path=path[:200])
    with _open_archive(data, name) as archive:
        entries = {entry.path: entry for entry in inspect_archive(data, name)}
        entry = entries.get(wanted.rstrip("/"))
        if entry is None or entry.is_dir:
            raise FileFormatError("input.archive_member_missing", f"{name} has no file {path}", path=path[:200])
        if entry.status == "skipped":
            raise FileFormatError(
                "input.archive_member_skipped", f"{path} was skipped ({entry.reason})", path=path, reason=entry.reason
            )
        info = next(item for item in archive.infolist() if safe_path(item.filename) == wanted)
        limit = min(info.file_size, MAX_MEMBER_BYTES)
        with archive.open(info) as handle:
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise FileFormatError("input.archive_ratio", f"{path} is larger than declared", path=path)
        return content


def archive_tree(name: str, entries: list[ArchiveEntry], *, limit: int = 400) -> str:
    """The directory tree of an archive as text, like ``tree`` (``├──`` lines), with sizes and notes."""
    tree: dict[str, Any] = {}
    for entry in entries:
        node = tree
        parts = entry.path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        if entry.is_dir:
            node.setdefault(parts[-1] + "/", {})
        else:
            node[parts[-1]] = entry
    lines = [name]

    def walk(node: dict[str, Any], prefix: str) -> None:
        items = sorted(node.items(), key=lambda pair: (not pair[0].endswith("/"), pair[0].lower()))
        for index, (key, value) in enumerate(items):
            if len(lines) > limit:
                return
            last = index == len(items) - 1
            branch = "└── " if last else "├── "
            if isinstance(value, dict):
                lines.append(f"{prefix}{branch}{key}")
                walk(value, prefix + ("    " if last else "│   "))
            else:
                note = ""
                if value.status == "skipped":
                    note = f"  [skipped: {value.reason}]"
                elif value.status == "nested":
                    note = "  [archive, not opened]"
                elif value.status == "unsupported":
                    note = "  [not readable]"
                lines.append(f"{prefix}{branch}{key} ({format_size(value.size)}){note}")

    walk(tree, "")
    files = [entry for entry in entries if not entry.is_dir]
    if len(lines) > limit:
        lines = [*lines[:limit], f"… ({len(files)} files in total)"]
    return "\n".join(lines)


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def extract_member(data: bytes, path: str, name: str = "archive.zip") -> tuple[ArchiveEntry, Extracted, bytes]:
    """Read one entry of an archive like a file of its own (nested archives are not opened)."""
    content = read_member(data, path, name)
    entry = next(item for item in inspect_archive(data, name) if item.path == safe_path(path).rstrip("/"))  # type: ignore[union-attr]
    if entry.status == "nested":
        raise FileFormatError("input.archive_nested", f"{path} is an archive inside the archive", path=path)
    if entry.status != "ok" or entry.format is None:
        raise FileFormatError("input.archive_member_unsupported", f"{path} cannot be read", path=path)
    spec = detect(entry.path, content)
    return entry, extract(entry.path, content, spec=spec), content
