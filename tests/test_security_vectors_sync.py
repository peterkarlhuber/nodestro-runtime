"""Axentra's copy of the shared security test vectors is the canonical corpus (MUP-ONA MP-A8, T-VEC-4).

Axentra vendors ``tests/vectors/security-vectors.json`` with ``tests/vectors/SOURCE.md`` (origin, version, SHA-256) and
has no reference to Nodestro, so the comparison lives only here. ``NODESTRO_AXENTRA_SRC`` names the root of an axentra
checkout: the CI job ``security-vectors-sync`` checks out axentra's ``master`` anonymously (it is public; no secret)
and sets it together with ``NODESTRO_REQUIRE_VECTOR_SYNC=1``, which turns a missing checkout into a failure. Without
the variable (the main test matrix, a developer machine) the check is skipped with the reason that
``tests/ci/skipped-test.txt`` lists. Locally:
``NODESTRO_AXENTRA_SRC=../axentra pytest tests/test_security_vectors_sync.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re

import pytest

CANONICAL = Path(__file__).parent / "vectors" / "security-vectors.json"
COPY = Path("tests") / "vectors" / "security-vectors.json"
SOURCE = Path("tests") / "vectors" / "SOURCE.md"
#: The skip reason where no axentra checkout is given; tests/ci/skipped-test.txt lists it.
SYNC_SKIP = "axentra copy checked by the CI job security-vectors-sync (MUP-ONA A8)"
INSTRUCTIONS = (
    "\nThe canonical corpus (nodestro-runtime tests/vectors/security-vectors.json) and Axentra's copy differ. Copy the "
    "canonical file unchanged to axentra tests/vectors/security-vectors.json and record its version and SHA-256 "
    "(sha256sum) in axentra tests/vectors/SOURCE.md in an axentra pull request; merge that before the runtime change "
    "(tests/vectors/README.md, 'Changing the corpus'). Never edit the copy by hand."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded(source_text: str) -> tuple[str | None, str | None]:
    """The ``Version`` and ``SHA-256`` lines of a SOURCE.md (``None`` for a missing line)."""
    version = re.search(r"^Version: `(\d+)`$", source_text, re.MULTILINE)
    checksum = re.search(r"^SHA-256: `([0-9a-f]{64})`$", source_text, re.MULTILINE)
    return (version.group(1) if version else None, checksum.group(1) if checksum else None)


def sync_problems(canonical: Path, axentra: Path) -> list[str]:
    """Why axentra's copy under ``axentra`` (a checkout root) is not the canonical corpus; empty when it is."""
    expected_sha = sha256(canonical)
    expected_version = str(json.loads(canonical.read_text(encoding="utf-8"))["version"])
    version, checksum = recorded((axentra / SOURCE).read_text(encoding="utf-8"))
    problems = []
    if checksum is None or version is None:
        problems.append(f"{SOURCE} names no 'Version: `<n>`' or 'SHA-256: `<hex>`' line")
    if checksum is not None and checksum != expected_sha:
        problems.append(f"{SOURCE} records SHA-256 {checksum}, the canonical file has {expected_sha}")
    if version is not None and version != expected_version:
        problems.append(f"{SOURCE} records version {version}, the canonical file is version {expected_version}")
    copy = axentra / COPY
    if not copy.is_file():
        problems.append(f"{COPY} is missing")
    elif sha256(copy) != expected_sha:
        problems.append(f"{COPY} has SHA-256 {sha256(copy)}, the canonical file has {expected_sha}")
    return problems


def axentra_checkout(environ: Mapping[str, str]) -> Path:
    """The axentra checkout from ``NODESTRO_AXENTRA_SRC``; fails when it holds no vendored corpus, skips without it
    (fails instead with ``NODESTRO_REQUIRE_VECTOR_SYNC=1``)."""
    given = environ.get("NODESTRO_AXENTRA_SRC", "")
    if given:
        root = Path(given)
        if not (root / SOURCE).is_file():
            pytest.fail(f"NODESTRO_AXENTRA_SRC={given} holds no {SOURCE} (the root of an axentra checkout)")
        return root
    if environ.get("NODESTRO_REQUIRE_VECTOR_SYNC") == "1":
        pytest.fail("NODESTRO_REQUIRE_VECTOR_SYNC=1 but NODESTRO_AXENTRA_SRC names no axentra checkout")
    pytest.skip(SYNC_SKIP)


# MUP-ONA T-VEC-4
def test_the_axentra_copy_is_the_canonical_corpus() -> None:
    root = axentra_checkout(os.environ)
    problems = sync_problems(CANONICAL, root)
    assert problems == [], "\n".join(problems) + INSTRUCTIONS


# ---- the check itself, without a checkout --------------------------------------------------------------------------


def _vendor(root: Path, content: bytes, *, version: str, checksum: str) -> Path:
    (root / COPY).parent.mkdir(parents=True, exist_ok=True)
    (root / COPY).write_bytes(content)
    (root / SOURCE).write_text(
        f"# Copy\n\nOrigin: somewhere\nVersion: `{version}`\nSHA-256: `{checksum}`\n", encoding="utf-8"
    )
    return root


# MUP-ONA T-VEC-4
def test_a_faithful_copy_passes_and_every_drift_is_reported(tmp_path: Path) -> None:
    content = CANONICAL.read_bytes()
    version = str(json.loads(content)["version"])
    good = _vendor(tmp_path / "good", content, version=version, checksum=sha256(CANONICAL))
    assert sync_problems(CANONICAL, good) == []

    edited = _vendor(tmp_path / "edited", content + b"\n", version=version, checksum=sha256(CANONICAL))
    assert [p for p in sync_problems(CANONICAL, edited) if "has SHA-256" in p]

    stale = _vendor(tmp_path / "stale", content, version="0", checksum="0" * 64)
    found = sync_problems(CANONICAL, stale)
    assert any("records SHA-256" in p for p in found) and any("records version 0" in p for p in found)

    (stale / COPY).unlink()
    assert f"{COPY} is missing" in sync_problems(CANONICAL, stale)

    (good / SOURCE).write_text("# Copy without the lines\n", encoding="utf-8")
    assert any("names no" in p for p in sync_problems(CANONICAL, good))


# MUP-ONA T-VEC-4
def test_a_mismatch_fails_with_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _vendor(tmp_path, b"{}", version="0", checksum="0" * 64)
    monkeypatch.setenv("NODESTRO_AXENTRA_SRC", str(root))
    with pytest.raises(AssertionError, match=r"(?s)records SHA-256.*Copy the canonical file unchanged.*SOURCE\.md"):
        test_the_axentra_copy_is_the_canonical_corpus()


# MUP-ONA T-VEC-4
def test_a_required_sync_fails_instead_of_skipping(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="NODESTRO_AXENTRA_SRC names no axentra checkout"):
        axentra_checkout({"NODESTRO_REQUIRE_VECTOR_SYNC": "1"})
    with pytest.raises(pytest.fail.Exception, match="holds no"):
        axentra_checkout({"NODESTRO_AXENTRA_SRC": str(tmp_path)})  # a wrong path fails even when not required
    with pytest.raises(pytest.skip.Exception, match=re.escape(SYNC_SKIP)):
        axentra_checkout({})
    root = _vendor(tmp_path / "axentra", b"{}", version="1", checksum="0" * 64)
    assert axentra_checkout({"NODESTRO_REQUIRE_VECTOR_SYNC": "1", "NODESTRO_AXENTRA_SRC": str(root)}) == root
