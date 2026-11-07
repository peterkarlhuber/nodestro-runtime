"""PDFs are read within limits in a separate process, on every path that reads one (MUP-ONA A1, PS-1).

Knowledge uploads (``KnowledgeBase.add``) and input files (``file_formats.extract``) read a PDF's text through
``documents.pdf_pages``, that is orkexa's ``pdf_pages_isolated`` with the limits of ``documents.pdf_limits``
(``NODESTRO_PDF_TIMEOUT_SECONDS``, ``NODESTRO_PDF_MAX_PAGES``, ``NODESTRO_PDF_MAX_CHARS``,
``NODESTRO_PDF_MAX_MEMORY_MB``). A PDF over a limit is refused with ``document.too_complex`` and nothing is stored.
The PostgreSQL cases need ``NODESTRO_TEST_POSTGRES`` (see ``postgres_support``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import time
from typing import Any

from orkexa import SQLiteIndex
import orkexa.retrieval
import pdf_support
import postgres_support
import pytest

from nodestro_runtime import documents
from nodestro_runtime.documents import DocumentError, pdf_limits
from nodestro_runtime.file_formats import FileFormatError, extract
from nodestro_runtime.knowledge import KnowledgeBase

LINES = [f"Page {number}: warranty, shipping and refunds of order {number}." for number in range(1, 6)]


@pytest.fixture(autouse=True)
def _default_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in documents.PDF_LIMITS_ENV.values():
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(params=["sqlite", "postgres"])
def knowledge(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[KnowledgeBase]:
    if request.param == "sqlite":
        base = KnowledgeBase(tmp_path / "nodestro.db")
        yield base
        base.close()
        return
    from nodestro_runtime.postgres_tables import knowledge_tables
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    try:
        database.migrate()
        base = KnowledgeBase(knowledge_tables(database, "alpha"))
        yield base
        base.close()
    finally:
        database.close()
        postgres_support.drop_schema(schema)


def _files(root: Path) -> dict[str, bytes]:
    """The files under ``root`` except SQLite's database files (their pages change with every read)."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith("nodestro.db")
    }


# ---- configuration -------------------------------------------------------------------------------


def test_the_limits_default_to_decision_o6() -> None:
    # MUP-ONA T-PDF-8
    limits = pdf_limits({})
    assert (limits.timeout, limits.max_pages, limits.max_chars, limits.max_memory_mb) == (30.0, 2000, 5_000_000, 512)
    blank = {variable: "  " for variable in documents.PDF_LIMITS_ENV.values()}
    assert pdf_limits(blank) == limits


def test_the_limits_come_from_the_environment() -> None:
    # MUP-ONA T-PDF-8
    limits = pdf_limits(
        {
            "NODESTRO_PDF_TIMEOUT_SECONDS": "2.5",
            "NODESTRO_PDF_MAX_PAGES": "40",
            "NODESTRO_PDF_MAX_CHARS": " 9000 ",
            "NODESTRO_PDF_MAX_MEMORY_MB": "256",
        }
    )
    assert (limits.timeout, limits.max_pages, limits.max_chars, limits.max_memory_mb) == (2.5, 40, 9000, 256)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("NODESTRO_PDF_TIMEOUT_SECONDS", "soon"),
        ("NODESTRO_PDF_TIMEOUT_SECONDS", "0"),
        ("NODESTRO_PDF_TIMEOUT_SECONDS", "-1"),
        ("NODESTRO_PDF_TIMEOUT_SECONDS", "inf"),
        ("NODESTRO_PDF_TIMEOUT_SECONDS", "nan"),
        ("NODESTRO_PDF_MAX_PAGES", "0"),
        ("NODESTRO_PDF_MAX_PAGES", "2.5"),
        ("NODESTRO_PDF_MAX_CHARS", "-5"),
        ("NODESTRO_PDF_MAX_CHARS", "many"),
        ("NODESTRO_PDF_MAX_MEMORY_MB", "15"),
        ("NODESTRO_PDF_MAX_MEMORY_MB", "512MB"),
    ],
)
def test_an_invalid_limit_is_refused_by_name(variable: str, value: str) -> None:
    # MUP-ONA T-PDF-8
    with pytest.raises(DocumentError) as raised:
        pdf_limits({variable: value})
    assert raised.value.code == "document.limits_invalid"
    assert raised.value.params == {"variable": variable}


def test_an_invalid_limit_refuses_the_upload_instead_of_reading_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-8
    monkeypatch.setenv("NODESTRO_PDF_MAX_PAGES", "none")
    base = KnowledgeBase(tmp_path / "nodestro.db")
    try:
        with pytest.raises(DocumentError) as raised:
            base.add(pdf_support.text_pdf(LINES), name="manual.pdf")
        assert raised.value.code == "document.limits_invalid"
        assert base.files() == []
    finally:
        base.close()


# ---- knowledge uploads ---------------------------------------------------------------------------


def test_a_pdf_over_the_page_limit_is_refused_and_nothing_is_stored(knowledge: KnowledgeBase, tmp_path: Path) -> None:
    # MUP-ONA T-PDF-8
    kept = knowledge.add(pdf_support.text_pdf(LINES), name="manual.pdf")
    files, before = knowledge.files(), _files(tmp_path)
    huge = pdf_support.pages_100k()
    started = time.monotonic()
    for _attempt in range(2):  # the same file again: the same answer, still nothing stored
        with pytest.raises(DocumentError) as raised:
            knowledge.add(huge, name="huge.pdf", media_type="application/pdf")
        assert raised.value.code == "document.too_complex"
        assert raised.value.params == {"name": "huge.pdf", "reason": "pages"}
    assert time.monotonic() - started < 20
    assert knowledge.files() == files
    assert [entry.id for entry in knowledge.store.list()] == [kept.id]
    assert _files(tmp_path) == before
    assert knowledge.search("many pages") == []
    assert {hit.source for hit in knowledge.search("warranty")} == {kept.id}


def test_a_pdf_over_the_time_budget_is_stopped_and_nothing_is_stored(
    knowledge: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-8
    monkeypatch.setenv("NODESTRO_PDF_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    with pytest.raises(DocumentError) as raised:
        knowledge.add(pdf_support.slow_content_stream(), name="slow.pdf")
    assert time.monotonic() - started < 1 + 2 + 5  # the budget, the grace period before SIGKILL, slack
    assert raised.value.code == "document.too_complex"
    assert raised.value.params == {"name": "slow.pdf", "reason": "timeout"}
    assert knowledge.files() == []


def test_a_pdf_over_the_character_limit_is_refused(knowledge: KnowledgeBase, monkeypatch: pytest.MonkeyPatch) -> None:
    # MUP-ONA T-PDF-8
    monkeypatch.setenv("NODESTRO_PDF_MAX_CHARS", "100")
    with pytest.raises(DocumentError) as raised:
        knowledge.add(pdf_support.text_pdf(LINES), name="manual.pdf")
    assert raised.value.params == {"name": "manual.pdf", "reason": "chars"}
    assert knowledge.files() == []


def test_a_pdf_within_the_limits_is_indexed_as_orkexa_indexes_it(knowledge: KnowledgeBase, tmp_path: Path) -> None:
    # MUP-ONA T-PDF-8: the text read in the limited process gives the chunks Index.add_file gave before
    data = pdf_support.text_pdf([*LINES[:2], "", *LINES[2:]])
    entry = knowledge.add(data, name="manual.pdf")
    assert entry.searchable and not entry.scanned and entry.chunks == len(LINES)

    reference = SQLiteIndex(tmp_path / "reference.db")
    path = tmp_path / "manual.pdf"
    path.write_bytes(data)
    reference.add_file(path, source=entry.id, title="manual.pdf")

    def chunks(hits: list[Any]) -> list[tuple[str, str | None, dict[str, str]]]:
        return sorted((hit.text, hit.title, hit.metadata) for hit in hits)

    found = knowledge.search("warranty", k=20)
    assert chunks(found) == chunks(reference.search("warranty", k=20))
    assert {hit.title for hit in found} == {f"manual.pdf, p. {number}" for number in (1, 2, 4, 5, 6)}
    assert {hit.file_id for hit in found} == {entry.id}
    reference.close()


def test_a_pdf_without_text_is_stored_as_a_scan(knowledge: KnowledgeBase) -> None:
    # MUP-ONA T-PDF-8
    entry = knowledge.add(pdf_support.text_pdf(["", ""]), name="scan.pdf")
    assert entry.scanned and not entry.searchable
    assert [item.id for item in knowledge.files()] == [entry.id]


# ---- input files (file_formats) ------------------------------------------------------------------


def test_an_input_pdf_over_the_page_limit_is_refused_with_the_same_code() -> None:
    # MUP-ONA T-PDF-9
    with pytest.raises(FileFormatError) as raised:
        extract("huge.pdf", pdf_support.pages_100k())
    assert raised.value.code == "document.too_complex"
    assert raised.value.params == {"name": "huge.pdf", "reason": "pages"}


def test_an_input_pdf_over_the_time_budget_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUP-ONA T-PDF-9
    monkeypatch.setenv("NODESTRO_PDF_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    with pytest.raises(FileFormatError) as raised:
        extract("slow.pdf", pdf_support.slow_content_stream())
    assert time.monotonic() - started < 1 + 2 + 5
    assert raised.value.params == {"name": "slow.pdf", "reason": "timeout"}


def test_input_files_and_knowledge_uploads_use_the_same_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # MUP-ONA T-PDF-9: both paths go through pdf_pages_isolated with the limits of the environment
    calls: list[tuple[str, dict[str, Any]]] = []
    isolated: Callable[..., list[str]] = orkexa.retrieval.pdf_pages_isolated

    def spy(data: bytes, **limits: Any) -> list[str]:
        calls.append(("isolated", limits))
        return isolated(data, **limits)

    def unlimited(*_args: Any, **_kwargs: Any) -> list[str]:
        raise AssertionError("a PDF was read in this process")

    monkeypatch.setattr(orkexa.retrieval, "pdf_pages_isolated", spy)
    monkeypatch.setattr(orkexa.retrieval, "pdf_pages", unlimited)
    monkeypatch.setattr(orkexa.retrieval, "_pdf_pages", unlimited)
    monkeypatch.setenv("NODESTRO_PDF_MAX_PAGES", "3")
    monkeypatch.setenv("NODESTRO_PDF_MAX_MEMORY_MB", "384")
    data = pdf_support.text_pdf(LINES)  # five pages

    with pytest.raises(FileFormatError) as from_input:
        extract("manual.pdf", data)
    base = KnowledgeBase(tmp_path / "nodestro.db")
    try:
        with pytest.raises(DocumentError) as from_knowledge:
            base.add(data, name="manual.pdf")
        assert base.files() == []

        assert from_input.value.code == from_knowledge.value.code == "document.too_complex"
        assert from_input.value.params == from_knowledge.value.params == {"name": "manual.pdf", "reason": "pages"}
        expected = {"timeout": 30.0, "max_memory_mb": 384, "max_pages": 3, "max_chars": 5_000_000}
        assert calls == [("isolated", expected), ("isolated", expected)]

        monkeypatch.setenv("NODESTRO_PDF_MAX_PAGES", "5")
        assert extract("manual.pdf", data).info["pages"] == 5
        assert base.add(data, name="manual.pdf").chunks == 5
    finally:
        base.close()


def test_the_runtime_no_longer_imports_orkexas_private_pdf_reader() -> None:
    # MUP-ONA T-PDF-9
    source = Path(documents.__file__).resolve().parent
    users = [path.name for path in source.rglob("*.py") if "_pdf_pages" in path.read_text(encoding="utf-8")]
    assert users == []
