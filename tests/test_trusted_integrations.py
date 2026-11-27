"""Nodestro's own integration is imported only after its checks (N-22, review F1, ``trusted_integrations``).

Also run by the Platforms workflow (Windows, macOS): the checks compare real paths, which differ there (short names,
case, ``/private`` on macOS).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
from trusted_dist_support import OCR_SOURCE, TOP, clean_imports, write_distribution  # noqa: F401 - a fixture
from trusted_dist_support import entries as trusted_entries

from nodestro_runtime.extension_sandbox import ExtensionSandbox
from nodestro_runtime.extensions import EXTENSION_TOOLS_ENV, Extensions, discover, find_packages

# ---- Nodestro's own distribution: verified before it is imported (review F1) --------------------------------------


EVIL_SOURCE = """
MARKER = {marker!r}
with open(MARKER, "w") as handle:
    handle.write("imported in the host")
thing = None
"""


@pytest.mark.usefixtures("clean_imports")
def test_a_verified_copy_of_the_trusted_distribution_is_imported(tmp_path: Path) -> None:
    [found] = discover(trusted_entries(write_distribution(tmp_path / "site")))
    assert found.name == "tesseract-ocr" and found.version == "9.9"
    assert sys.modules["nodestro_integrations.ocr"].GENUINE is True


@pytest.mark.usefixtures("clean_imports")
def test_a_distribution_that_claims_the_trusted_name_is_never_imported(tmp_path: Path) -> None:
    """The review's case: another folder, ``Name: Nodestro_Integrations`` and an entry point of its own."""
    marker = tmp_path / "imported-in-host.txt"
    info = write_distribution(
        tmp_path / "site",
        name="Nodestro_Integrations",
        folder="totally_other-1.0.dist-info",
        version="1.0",
        entry="x = evilmod:thing",
        files={"evilmod.py": EVIL_SOURCE.format(marker=str(marker))},
    )
    sys.path.insert(0, str(tmp_path / "site"))
    try:
        assert discover(trusted_entries(info)) == []
        [package] = find_packages(trusted_entries(info))
    finally:
        sys.path.remove(str(tmp_path / "site"))
    assert not marker.exists() and "evilmod" not in sys.modules
    assert "claims to be nodestro-integrations" in package.rejected
    extensions = Extensions(installed=True, packages=[package])
    [status] = extensions.packages_status()
    assert (status["state"], status["reason"]) == ("locked", "invalid")
    never = ExtensionSandbox(lambda _timeout: pytest.fail("no container for a rejected package"))
    assert extensions.load_packages(never, reserved=(), environ={EXTENSION_TOOLS_ENV: "1"}) == []


@pytest.mark.parametrize("change", ["folder", "pinned-entry", "hash", "unhashed", "unlisted-module", "linked"])
@pytest.mark.usefixtures("clean_imports")
def test_every_trust_check_refuses_the_import(tmp_path: Path, change: str) -> None:
    site = tmp_path / "site"
    marker = tmp_path / "imported-in-host.txt"
    evil = OCR_SOURCE + f"\nopen({str(marker)!r}, 'w').write('x')\n"
    options: dict[str, Any] = {}
    if change == "folder":
        options["folder"] = "nodestro_integrations-0.1.0.dist-info"
    elif change == "pinned-entry":
        options["entry"] = "tesseract-ocr = nodestro_integrations.other:integration"
        options["files"] = {f"{TOP}/__init__.py": "", f"{TOP}/other.py": evil}
    elif change == "unhashed":
        options["hashed"] = False
    elif change == "unlisted-module":  # an editable install lists no package files
        options["files"] = {f"{TOP}/__init__.py": ""}
    info = write_distribution(site, **options)
    if change == "hash":
        (site / TOP / "ocr.py").write_text(evil, encoding="utf-8")
    if change == "unlisted-module":
        (site / TOP / "ocr.py").write_text(evil, encoding="utf-8")
    if change == "linked":
        real = tmp_path / "elsewhere"
        (site / TOP).rename(real)
        (site / TOP).symlink_to(real, target_is_directory=True)
    sys.path.insert(0, str(site))
    try:
        assert discover(trusted_entries(info)) == []
        [package] = find_packages(trusted_entries(info))
    finally:
        sys.path.remove(str(site))
    assert package.rejected and not marker.exists() and TOP + ".ocr" not in sys.modules


@pytest.mark.usefixtures("clean_imports")
def test_a_copy_earlier_on_the_path_neither_hides_nor_replaces_the_verified_one(tmp_path: Path) -> None:
    """Every distribution is looked at (no de-duplication by name); the module is served from the verified files only,
    never from another copy on ``sys.path`` or from cached bytecode next to it."""
    marker = tmp_path / "imported-in-host.txt"
    evil = OCR_SOURCE.replace("GENUINE = True", f"GENUINE = False\nopen({str(marker)!r}, 'w').write('x')")
    fake = write_distribution(tmp_path / "fake", hashed=False, files={f"{TOP}/__init__.py": "", f"{TOP}/ocr.py": evil})
    real = write_distribution(tmp_path / "real")
    cache = tmp_path / "real" / TOP / "__pycache__"
    cache.mkdir()
    import importlib._bootstrap_external as external

    code = compile(evil, str(tmp_path / "real" / TOP / "ocr.py"), "exec")
    data = external._code_to_hash_pyc(code, b"\0" * 8, checked=False)  # an unchecked pyc: loaded without the source
    (cache / f"ocr.{sys.implementation.cache_tag}.pyc").write_bytes(bytes(data))
    sys.path[:0] = [str(tmp_path / "fake"), str(tmp_path / "real")]
    try:
        [found] = discover(trusted_entries(fake, real))
        packages = find_packages(trusted_entries(fake, real))
    finally:
        sys.path.remove(str(tmp_path / "fake"))
        sys.path.remove(str(tmp_path / "real"))
    assert found.name == "tesseract-ocr" and sys.modules[TOP + ".ocr"].GENUINE is True and not marker.exists()
    assert [bool(package.rejected) for package in packages] == [True]  # the fake one, listed as invalid
