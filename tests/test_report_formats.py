"""The result file formats: one table (reports.REPORT_FORMATS), real Word and Excel files, stable stored ids."""

from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from typing import Any, get_args
import zipfile

from pydantic import ValidationError
import pytest

from nodestro_runtime import reports
from nodestro_runtime.schemas import OutputNodeConfig, ReportFormat, ResultFileSpec, Run, Workflow
from nodestro_runtime.storage import SQLiteStore

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

RESULT = [
    {"position": "Rent", "amount": 1200.5, "paid": True, "due": "2026-01-31"},
    {"position": "Power", "amount": 80, "paid": False, "due": "2026-02-28"},
]


def make_run(output: Any = RESULT, name: str = "Monthly costs") -> Run:
    finished = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    return Run(
        workflow_id="wf_1",
        workflow_name=name,
        workflow=Workflow(name=name),
        status="succeeded",
        output=output,
        created_at=finished,
        finished_at=finished,
    )


def test_the_table_names_every_format_once_in_the_stored_order() -> None:
    # The ids are stored in workflows and runs: they must never be renamed (md … xlsx, as in ReportFormat).
    assert list(reports.REPORT_FORMATS) == list(get_args(ReportFormat))
    assert list(get_args(ReportFormat)) == ["md", "json", "csv", "html", "pdf", "docx", "xlsx"]
    for key, info in reports.REPORT_FORMATS.items():
        assert info.id == key and info.extension == key
        assert reports.MEDIA_TYPES[key] == info.media_type
    assert sorted(reports.INLINE_FORMATS) == ["html", "md", "pdf"]
    assert reports.REPORT_FORMATS["docx"].media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert (
        reports.REPORT_FORMATS["xlsx"].media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_stored_result_file_configurations_keep_loading() -> None:
    # A result node as earlier versions stored it (no name, no sanitize) and with every format.
    stored = {"files": [{"format": key} for key in get_args(ReportFormat)] + [{"format": "docx", "name": "Report"}]}
    config = OutputNodeConfig.model_validate(json.loads(json.dumps(stored)))
    assert [spec.format for spec in config.files] == [*get_args(ReportFormat), "docx"]
    assert config.files[-1].name == "Report" and config.files[-1].sanitize is False
    assert json.loads(config.model_dump_json())["files"][5] == {"format": "docx", "name": "", "sanitize": False}


@pytest.mark.parametrize("value", ["doc", "xls", "DOCX", "word", "excel", ".xlsx", ""])
def test_unknown_formats_are_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        ResultFileSpec(format=value)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        reports.FileRequest(format=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "Monthly costs"),
        ("Costs Q1", "Costs Q1"),
        ("Costs Q1.docx", "Costs Q1"),
        ("../../etc/passwd", "passwd"),
        ("a:b*c?", "a_b_c"),
    ],
)
@pytest.mark.parametrize("key", ["docx", "xlsx"])
def test_office_files_get_safe_names_with_their_extension(tmp_path: Any, name: str, expected: str, key: str) -> None:
    files = reports.ResultFiles(SQLiteStore(tmp_path / "data.db"))
    run = make_run()
    name = name.replace(".docx", f".{key}")  # a name typed with the extension keeps one extension
    entry = files.create(run, reports.FileRequest(format=key, name=name))  # type: ignore[arg-type]
    assert entry.name == f"{expected}.{key}"
    assert entry.media_type == reports.REPORT_FORMATS[key].media_type  # type: ignore[index]
    again = files.create(run, reports.FileRequest(format=key, name=name))  # type: ignore[arg-type]
    assert again.name == f"{expected} (2).{key}"


def test_word_files_open_with_their_content(tmp_path: Any) -> None:
    files = reports.ResultFiles(SQLiteStore(tmp_path / "data.db"))
    run = make_run({"summary": "Costs are **stable**.", "items": RESULT})
    entry = files.create(run, reports.FileRequest(format="docx", name="Costs"))
    _, data = files.content(run.id, entry.id)
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    assert {"[Content_Types].xml", "word/document.xml", "_rels/.rels"} <= set(archive.namelist())
    docx = pytest.importorskip("docx")
    document = docx.Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Costs" in text and "Costs are stable." in text
    assert any(run.bold for paragraph in document.paragraphs for run in paragraph.runs if run.text == "stable")
    cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    assert "Rent" in cells and "Power" in cells


def test_excel_files_open_with_typed_cells(tmp_path: Any) -> None:
    files = reports.ResultFiles(SQLiteStore(tmp_path / "data.db"))
    run = make_run()
    entry = files.create(run, reports.FileRequest(format="xlsx", name="Costs"))
    _, data = files.content(run.id, entry.id)
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    assert {"[Content_Types].xml", "xl/workbook.xml"} <= set(archive.namelist())
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    sheet = workbook.worksheets[0]
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    header = rows[0]
    assert header == ["position", "amount", "paid", "due"]
    assert rows[1][:3] == ["Rent", 1200.5, True] and rows[2][:3] == ["Power", 80, False]
    due = rows[1][3]
    assert isinstance(due, datetime) and due.date().isoformat() == "2026-01-31"


def test_an_office_file_that_cannot_be_written_says_why(tmp_path: Any) -> None:
    files = reports.ResultFiles(SQLiteStore(tmp_path / "data.db"))
    entry = files.create(make_run(output=None), reports.FileRequest(format="xlsx"), strict=False)
    assert entry.error_code == "report.no_result" and entry.size == 0 and entry.name == "Monthly costs.xlsx"
    with pytest.raises(reports.ReportError):
        files.content(entry.run_id, entry.id)
