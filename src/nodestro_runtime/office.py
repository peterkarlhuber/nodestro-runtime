"""Word (DOCX) and Excel (XLSX) result files, written locally with the standard library.

Both formats are ZIP packages of XML parts (Office Open XML). Nodestro writes them like the
other result formats (:mod:`nodestro_runtime.reports`): deterministically from the run's result, with
no AI request and no extra library. The same content always gives the same bytes (fixed
ZIP timestamps; the document date is the run's end).

- **DOCX** is written from the result as Markdown (the blocks of :func:`nodestro_runtime.reports.blocks`):
  headings (Word's heading styles, so the navigation pane works), paragraphs with bold,
  italic and code, bulleted and numbered lists, tables with a header row, code blocks and
  rules.
- **XLSX** turns the result into tables: a list of records is one sheet; an object gives a
  sheet per list of records plus a sheet *Summary* with the single values; Markdown tables in
  a text become sheets. Numbers, yes/no values and dates (``YYYY-MM-DD``) keep their type;
  every other value is text, so nothing is ever read as a formula. The header row is bold
  and frozen, with a filter and fitting column widths.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import html
import io
import json
import re
from typing import Any
import zipfile

_FIXED = (1980, 1, 1, 0, 0, 0)
_NUMBER = re.compile(r"^-?(0|[1-9]\d{0,14})(\.\d+)?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SHEET_UNSAFE = re.compile(r"[\[\]:*?/\\]")
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _x(text: str) -> str:
    """Text for an XML document: escaped, without characters XML does not allow."""
    return html.escape(_ILLEGAL_XML.sub("", text), quote=True)


def _package(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()


def _stamp(moment: datetime | None) -> str:
    return (moment or datetime(2000, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _core(title: str, moment: datetime | None) -> str:
    stamp = _stamp(moment)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_x(title)}</dc:title><dc:creator>Nodestro</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


_APP = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    "<Application>Nodestro</Application></Properties>"
)


# ---- DOCX ---------------------------------------------------------------------------------

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_INLINE = re.compile(
    r"(\*\*.+?\*\*|`[^`]+`|(?<![\w*])\*(?!\s)[^*]+?(?<!\s)\*(?![\w*])|(?<!\w)_(?!\s)[^_]+?(?<!\s)_(?!\w))"
)


def _runs(text: str, *, bold: bool = False, mono: bool = False) -> str:
    """Runs of a paragraph: **bold**, *italic*, `code`; links keep their text and address."""
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", text)
    runs = []
    for part in _INLINE.split(text):
        if not part:
            continue
        props = []
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, props = part[2:-2], ["<w:b/>"]
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, props = part[1:-1], ['<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>']
        elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_":
            part, props = part[1:-1], ["<w:i/>"]
        if bold:
            props.append("<w:b/>")
        if mono:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        rpr = f"<w:rPr>{''.join(dict.fromkeys(props))}</w:rPr>" if props else ""
        lines = part.split("\n")
        content = "<w:br/>".join(f'<w:t xml:space="preserve">{_x(line)}</w:t>' for line in lines)
        runs.append(f"<w:r>{rpr}{content}</w:r>")
    return "".join(runs)


def _paragraph(text: str, style: str | None = None, *, extra: str = "", bold: bool = False, mono: bool = False) -> str:
    props = (f'<w:pStyle w:val="{style}"/>' if style else "") + extra
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{_runs(text, bold=bold, mono=mono)}</w:p>"


def _docx_table(rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    border = '<w:{side} w:val="single" w:sz="4" w:space="0" w:color="A0A4B8"/>'
    borders = "".join(border.format(side=side) for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    grid = "".join(f'<w:gridCol w:w="{9000 // width}"/>' for _ in range(width))
    lines = [
        f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{borders}</w:tblBorders>'
        f'<w:tblLook w:val="04A0" w:firstRow="1"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
    ]
    for index, row in enumerate(rows):
        cells = row + [""] * (width - len(row))
        header = index == 0
        trpr = "<w:trPr><w:tblHeader/></w:trPr>" if header else ""
        shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEF0F6"/>' if header else ""
        body = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{9000 // width}" w:type="dxa"/>{shade}</w:tcPr>'
            f"{_paragraph(cell, 'TableText', bold=header)}</w:tc>"
            for cell in cells
        )
        lines.append(f"<w:tr>{trpr}{body}</w:tr>")
    lines.append("</w:tbl>")
    return "".join(lines)


_DOCX_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {_W_NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="12" w:space="4" w:color="D4E157"/></w:pBdr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/><w:color w:val="121428"/></w:rPr></w:style>
{"".join(f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="{280 - level * 30}" w:after="100"/><w:outlineLvl w:val="{level - 1}"/></w:pPr><w:rPr><w:b/><w:sz w:val="{36 - level * 4}"/><w:color w:val="121428"/></w:rPr></w:style>' for level in range(1, 7))}
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="EEF0F6"/><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0"/></w:pPr><w:rPr><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:rPr><w:i/><w:color w:val="5A5F78"/></w:rPr></w:style>
</w:styles>"""


def _numbering(ordered_lists: int) -> str:
    bullet = (
        '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
        '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    )
    decimal = (
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
        '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    )
    nums = ['<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
    # One numbering per ordered list, so every list starts at 1.
    for index in range(ordered_lists):
        nums.append(
            f'<w:num w:numId="{index + 2}"><w:abstractNumId w:val="1"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        )
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering {_W_NS}>{bullet}{decimal}{"".join(nums)}</w:numbering>'


def to_docx(markdown: str, title: str, moment: datetime | None = None) -> bytes:
    """A Word document of Markdown text (see the module description)."""
    from .reports import blocks

    body: list[str] = []
    ordered = 0
    first_heading = True
    for kind, value in blocks(markdown):
        if kind == "heading":
            level, text = value
            # "# Title" opens the document; "##" is Word's heading 1 and so on.
            style = ("Title" if first_heading else "Heading1") if level == 1 else f"Heading{min(level - 1, 6)}"
            first_heading = False
            body.append(_paragraph(text, style))
        elif kind == "paragraph":
            if re.fullmatch(r"_[^_]+_", value.strip()):
                body.append(_paragraph(value.strip()[1:-1], "Subtitle"))
            else:
                body.append(_paragraph(value))
        elif kind in ("list", "olist"):
            if kind == "olist":
                ordered += 1
                number = ordered + 1
            else:
                number = 1
            numbering = f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{number}"/></w:numPr>'
            body.extend(_paragraph(item, "ListParagraph", extra=numbering) for item in value)
        elif kind == "table":
            body.append(_docx_table(value))
            body.append(_paragraph(""))
        elif kind == "code":
            body.extend(_paragraph(line, "Code", mono=True) for line in (value.split("\n") or [""]))
            body.append(_paragraph(""))
        elif kind == "rule":
            body.append(
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="A0A4B8"/>'
                "</w:pBdr></w:pPr></w:p>"
            )
    section = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {_W_NS}><w:body>'
        + "".join(body)
        + section
        + "</w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
        "</Relationships>"
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )
    return _package(
        {
            "[Content_Types].xml": types,
            "_rels/.rels": root_rels,
            "word/document.xml": document,
            "word/styles.xml": _DOCX_STYLES,
            "word/numbering.xml": _numbering(ordered),
            "word/_rels/document.xml.rels": rels,
            "docProps/core.xml": _core(title, moment),
            "docProps/app.xml": _APP,
        }
    )


# ---- XLSX ---------------------------------------------------------------------------------

Sheet = tuple[str, list[str], list[list[Any]]]


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _is_records(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


def _records_sheet(name: str, records: list[dict[str, Any]]) -> Sheet:
    columns = list(dict.fromkeys(str(key) for record in records for key in record))
    return name, columns, [[_scalar(record.get(column)) for column in columns] for record in records]


def _flatten(prefix: str, value: Any, into: list[list[Any]]) -> None:
    if isinstance(value, dict) and value and len(into) < 5000:
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, into)
    else:
        into.append([prefix, _scalar(value)])


def xlsx_sheets(value: Any) -> list[Sheet]:
    """The sheets of a result (see the module description)."""
    from .conditions import parse_result
    from .reports import _markdown_tables

    value = parse_result(value)
    if _is_records(value):
        return [_records_sheet("Result", value)]
    if isinstance(value, dict):
        sheets: list[Sheet] = []
        summary: list[list[Any]] = []
        for key, item in value.items():
            item = parse_result(item)
            if _is_records(item):
                sheets.append(_records_sheet(str(key), item))
            elif isinstance(item, list) and item and all(not isinstance(entry, (dict, list)) for entry in item):
                sheets.append((str(key), [str(key)], [[_scalar(entry)] for entry in item]))
            elif isinstance(item, str) and _markdown_tables(item):
                for number, table in enumerate(_markdown_tables(item), start=1):
                    sheets.append((f"{key} {number}" if number > 1 else str(key), table[0], table[1:]))
            else:
                _flatten(str(key), item, summary)
        if summary:
            sheets.insert(0, ("Summary", ["field", "value"], summary))
        return sheets or [("Result", ["value"], [])]
    if isinstance(value, list):
        return [("Result", ["value"], [[_scalar(item)] for item in value])]
    text = str(value)
    tables = _markdown_tables(text)
    if tables:
        return [(f"Table {number}", table[0], table[1:]) for number, table in enumerate(tables, start=1)]
    return [("Result", ["text"], [[line] for line in text.splitlines() if line.strip()])]


def _sheet_name(name: str, taken: set[str]) -> str:
    base = _SHEET_UNSAFE.sub(" ", name).strip().strip("'")[:31] or "Sheet"
    candidate, number = base, 2
    while candidate.casefold() in taken:
        suffix = f" ({number})"
        candidate = base[: 31 - len(suffix)] + suffix
        number += 1
    taken.add(candidate.casefold())
    return candidate


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _typed(value: Any) -> Any:
    """Texts that are plainly numbers or dates become numbers or dates; everything else stays as it is."""
    if isinstance(value, str):
        stripped = value.strip()
        if _NUMBER.match(stripped) and not (len(stripped) > 1 and stripped.startswith("0") and stripped[1] != "."):
            return float(stripped) if "." in stripped else int(stripped)
        match = _DATE.match(stripped)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return value
    return value


def _cell(reference: str, value: Any, style: int = 0) -> tuple[str, int]:
    """The XML of one cell and the width its content needs."""
    value = _typed(value)
    attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return "", 0
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{attr}><v>{1 if value else 0}</v></c>', 5
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"{attr}><v>{value!r}</v></c>', len(str(value))
    if isinstance(value, date):
        serial = (value - date(1899, 12, 30)).days
        return f'<c r="{reference}" s="{style or 2}"><v>{serial}</v></c>', 10
    text = str(value)[:32767]
    # Inline strings are always text: a leading "=" is never a formula.
    return f'<c r="{reference}" t="inlineStr"{attr}><is><t xml:space="preserve">{_x(text)}</t></is></c>', min(
        max(len(line) for line in text.split("\n")), 80
    )


def _worksheet(columns: list[str], rows: list[list[Any]]) -> str:
    width = max([len(columns), *(len(row) for row in rows)]) if (columns or rows) else 1
    widths = [8] * width
    lines = []
    header_cells = []
    for index, column in enumerate(columns):
        xml, size = _cell(f"{_column(index)}1", column, 1)
        header_cells.append(xml)
        widths[index] = max(widths[index], size + 2)
    lines.append(f'<row r="1">{"".join(header_cells)}</row>')
    for number, row in enumerate(rows, start=2):
        cells = []
        for index, value in enumerate(row):
            xml, size = _cell(f"{_column(index)}{number}", value)
            cells.append(xml)
            widths[index] = max(widths[index], min(size + 2, 60))
        lines.append(f'<row r="{number}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{index + 1}" max="{index + 1}" width="{size}" customWidth="1"/>'
        for index, size in enumerate(widths)
    )
    last = f"{_column(max(width - 1, 0))}{len(rows) + 1}"
    frozen = (
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
    )
    autofilter = f'<autoFilter ref="A1:{last}"/>' if columns else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"{frozen}<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData>{autofilter}</worksheet>"
    )


_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF0F6"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def to_xlsx(value: Any, title: str, moment: datetime | None = None) -> bytes:
    """An Excel workbook of a result (see the module description)."""
    taken: set[str] = set()
    sheets = [(_sheet_name(name, taken), columns, rows) for name, columns, rows in xlsx_sheets(value)]
    parts: dict[str, str] = {}
    entries = []
    relations = []
    overrides = []
    for index, (name, columns, rows) in enumerate(sheets, start=1):
        parts[f"xl/worksheets/sheet{index}.xml"] = _worksheet([str(column) for column in columns], rows)
        entries.append(f'<sheet name="{_x(name)}" sheetId="{index}" r:id="rId{index}"/>')
        relations.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    styles_id = len(sheets) + 1
    relations.append(
        f'<Relationship Id="rId{styles_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    defined = "".join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{index}" hidden="1">'
        f"'{_x(name)}'!$A$1:${_column(max(len(columns) - 1, 0))}${len(rows) + 1}</definedName>"
        for index, (name, columns, rows) in enumerate(sheets)
        if columns
    )
    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(entries)}</sheets>"
        + (f"<definedNames>{defined}</definedNames>" if defined else "")
        + "</workbook>"
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(relations)
        + "</Relationships>"
    )
    parts["xl/styles.xml"] = _XLSX_STYLES
    parts["docProps/core.xml"] = _core(title, moment)
    parts["docProps/app.xml"] = _APP
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(overrides)
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )
    order = ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"]
    ordered = {name: parts[name] for name in order}
    ordered.update({name: content for name, content in parts.items() if name not in ordered})
    return _package(ordered)
