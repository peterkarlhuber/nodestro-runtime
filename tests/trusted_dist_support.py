"""Installed-looking distributions for the trust checks of ``nodestro_runtime.trusted_integrations`` (N-22, F1)."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
import hashlib
from importlib import metadata
from pathlib import Path
import sys

import pytest

from nodestro_runtime import trusted_integrations

TOP = "nodestro_integrations"

OCR_SOURCE = """
from nodestro_runtime.extensions import Integration

GENUINE = True


def integration():
    return Integration(name="tesseract-ocr", version="9.9")
"""


def _digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def write_distribution(
    site: Path,
    *,
    name: str = "nodestro-integrations",
    folder: str | None = None,
    version: str = "0.2.0",
    entry: str = "tesseract-ocr = nodestro_integrations.ocr:integration",
    files: dict[str, str] | None = None,
    hashed: bool = True,
) -> Path:
    """A distribution as a wheel installs it into ``site``: ``files`` (relative path -> source), a ``.dist-info``
    folder (``folder``, default the one ``name`` and ``version`` give) with ``METADATA``, ``entry_points.txt`` and a
    ``RECORD`` with SHA-256 hashes (``hashed``). Returns the ``.dist-info`` folder."""
    files = files if files is not None else {f"{TOP}/__init__.py": "", f"{TOP}/ocr.py": OCR_SOURCE}
    info = site / (folder or f"{name.replace('-', '_').lower()}-{version}.dist-info")
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[nodestro.integrations]\n{entry}\n", encoding="utf-8")
    lines = []
    for relative, source in files.items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        data = path.read_bytes()
        lines.append(f"{relative},sha256={_digest(data)},{len(data)}" if hashed else f"{relative},,")
    lines += [f"{info.name}/METADATA,,", f"{info.name}/entry_points.txt,,", f"{info.name}/RECORD,,"]
    (info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return info


def entries(*infos: Path) -> Callable[[], list[metadata.EntryPoint]]:
    """The entry points of these distributions, in this order (each one carries its distribution)."""
    return lambda: [entry for info in infos for entry in metadata.PathDistribution(info).entry_points]


def _ours(name: str) -> bool:
    return name == TOP or name.startswith(TOP + ".") or name == "evilmod"


@pytest.fixture
def clean_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts as a fresh process would and leaves the process as it found it: an installed
    ``nodestro_integrations`` that earlier tests imported (a real discovery in the same worker) is set aside with its
    verified finder and restored afterwards, and what the test imported is removed."""
    monkeypatch.setattr(
        sys,
        "meta_path",
        [finder for finder in sys.meta_path if not isinstance(finder, trusted_integrations._VerifiedFinder)],
    )
    monkeypatch.setattr(trusted_integrations, "_FINDERS", {})
    for name in [name for name in sys.modules if _ours(name)]:
        monkeypatch.delitem(sys.modules, name)
    yield
    for name in [name for name in sys.modules if _ours(name)]:
        del sys.modules[name]
