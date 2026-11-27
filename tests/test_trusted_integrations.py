"""Nodestro's own integration is imported only after its checks (N-22, review F1, ``trusted_integrations``).

Also run by the Platforms workflow (Windows, macOS): the checks compare real paths, which differ there (short names,
case, ``/private`` on macOS).
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

import pytest
from trusted_dist_support import OCR_SOURCE, TOP, clean_imports, place_runtime, write_distribution  # noqa: F401 - a fixture
from trusted_dist_support import entries as trusted_entries

from nodestro_runtime import trusted_integrations
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
def test_a_verified_copy_of_the_trusted_distribution_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place_runtime(monkeypatch, tmp_path / "site")
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
def test_every_trust_check_refuses_the_import(tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    place_runtime(monkeypatch, site)
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
def test_a_copy_earlier_on_the_path_neither_hides_nor_replaces_the_verified_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every distribution is looked at (no de-duplication by name); the module is served from the verified files only,
    never from another copy on ``sys.path`` or from cached bytecode next to it."""
    place_runtime(monkeypatch, tmp_path / "real")
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


# ---- R1: where the trusted distribution may come from, and a closed import path (owner decision on R1) -------------


@pytest.mark.usefixtures("clean_imports")
def test_a_forged_self_consistent_copy_ahead_on_the_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: a complete copy with its own ``.dist-info`` and a ``RECORD`` whose hashes match its own files, placed ahead
    of the runtime's installation (``PYTHONPATH``, the user's site-packages, the working directory), passes every file
    check but is not installed next to the runtime: refused and listed, the genuine copy is the one imported."""
    marker = tmp_path / "imported-in-host.txt"
    evil = OCR_SOURCE.replace("GENUINE = True", f"GENUINE = False\nopen({str(marker)!r}, 'w').write('x')")
    forged = write_distribution(tmp_path / "forged", files={f"{TOP}/__init__.py": "", f"{TOP}/ocr.py": evil})
    real = write_distribution(tmp_path / "real")
    place_runtime(monkeypatch, tmp_path / "real")
    sys.path[:0] = [str(tmp_path / "forged"), str(tmp_path / "real")]
    try:
        [found] = discover(trusted_entries(forged, real))
        [package] = find_packages(trusted_entries(forged, real))
    finally:
        sys.path.remove(str(tmp_path / "forged"))
        sys.path.remove(str(tmp_path / "real"))
    assert found.name == "tesseract-ocr" and sys.modules[TOP + ".ocr"].GENUINE is True and not marker.exists()
    assert "not installed next to nodestro_runtime" in package.rejected


@pytest.mark.usefixtures("clean_imports")
def test_a_verified_copy_away_from_the_running_runtime_is_refused(tmp_path: Path) -> None:
    """Without the test's placement the real :func:`installation_home` decides: a copy in another folder is never
    imported, however well it passes the file checks."""
    assert trusted_integrations.installation_home() != Path(os.path.realpath(tmp_path / "site"))
    info = write_distribution(tmp_path / "site")
    assert discover(trusted_entries(info)) == []
    [package] = find_packages(trusted_entries(info))
    assert "not installed next to nodestro_runtime" in package.rejected and TOP + ".ocr" not in sys.modules


@pytest.mark.usefixtures("clean_imports")
def test_an_open_import_path_refuses_the_host_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verified copy next to the runtime is still refused, and listed as invalid with the reason, while the
    import path is not closed; the workspace's data directory reaches the check."""
    site = Path(os.path.realpath(tmp_path / "site"))
    info = write_distribution(site)
    seen: list[object] = []

    def problems(home: Path, *, data_dir: object = None, path: object = None) -> list[str]:
        seen.append(data_dir)
        return ["/somewhere is writable by every user"]

    monkeypatch.setattr(trusted_integrations, "installation_home", lambda: site)
    monkeypatch.setattr(trusted_integrations, "environment_problems", problems)
    assert discover(trusted_entries(info), data_dir=tmp_path / "data") == []
    [package] = find_packages(trusted_entries(info), data_dir=tmp_path / "data")
    assert "the import path is not closed: /somewhere is writable by every user" in package.rejected
    assert TOP + ".ocr" not in sys.modules and seen and set(seen) == {tmp_path / "data"}
    extensions = Extensions(installed=True, packages=[package])
    assert extensions.packages_status()[0]["reason"] == "invalid"


def test_the_workspace_hands_its_data_directory_to_the_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nodestro_runtime import workspace as workspace_module

    seen: list[object] = []
    monkeypatch.setattr(workspace_module, "discover", lambda **options: seen.append(options["data_dir"]) or [])
    monkeypatch.setattr(workspace_module, "find_packages", lambda **options: seen.append(options["data_dir"]) or [])
    workspace = workspace_module.Workspace(tmp_path / "data")
    workspace.close()
    assert seen == [tmp_path / "data", tmp_path / "data"]


def _folder(path: Path, mode: int = 0o755) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return Path(os.path.realpath(path))


def _foreign_owner(monkeypatch: pytest.MonkeyPatch, folder: Path, *, same_user: bool, same_group: bool) -> None:
    """Make this process look like another user (or group) than the owner of ``folder``. Root-owned folders are
    administered and accepted, so a folder of root is first given to an unprivileged id (only root can do that)."""
    if os.stat(folder).st_uid == 0:
        os.chown(folder, 4242, 4242)
    info = os.stat(folder)
    euid = info.st_uid if same_user else info.st_uid + 1
    egid = info.st_gid if same_group else info.st_gid + 1
    monkeypatch.setattr(trusted_integrations, "_ids", lambda: (euid, egid))


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes and owners (Windows: ACLs, refused below)")


@posix_only
def test_a_closed_import_path_has_no_problems(tmp_path: Path) -> None:
    home = _folder(tmp_path / "install" / "site-packages")
    stdlib = _folder(tmp_path / "install" / "lib")
    assert (
        trusted_integrations.environment_problems(home, path=[str(stdlib), str(home)], data_dir=tmp_path / "data") == []
    )


@posix_only
def test_the_user_site_packages_on_the_path_is_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _folder(tmp_path / "site-packages")
    user = _folder(tmp_path / "home" / ".local" / "lib" / "site-packages")
    monkeypatch.setattr(trusted_integrations.site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(trusted_integrations.site, "getusersitepackages", lambda: str(user))
    problems = trusted_integrations.environment_problems(home, path=[str(user), str(home)])
    assert problems == [f"the user's site-packages {user} is on the import path"]
    monkeypatch.setattr(trusted_integrations.site, "ENABLE_USER_SITE", False)  # -s, -I or a venv
    assert trusted_integrations.environment_problems(home, path=[str(user), str(home)]) == []


@posix_only
def test_the_data_directory_on_the_path_is_a_problem(tmp_path: Path) -> None:
    data = _folder(tmp_path / "data")
    env = _folder(data / "python" / "envs" / "workflow-1" / "lib")
    home = _folder(tmp_path / "site-packages")
    assert trusted_integrations.environment_problems(home, path=[str(env)], data_dir=data) == [
        f"{env} lies in the data directory"
    ]
    inside = _folder(data / "site-packages")
    assert trusted_integrations.environment_problems(inside, path=[], data_dir=data) == [
        f"{inside} lies in the data directory"
    ]


@posix_only
def test_folders_writable_by_every_user_are_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _folder(tmp_path / "site-packages")
    shared = _folder(tmp_path / "shared", 0o777)
    sticky = _folder(tmp_path / "sticky", 0o1777)
    below = _folder(sticky / "mine")
    try:
        problems = trusted_integrations.environment_problems(home, path=[str(shared), str(sticky), str(below)])
        assert problems == [f"{shared} is writable by every user", f"{sticky} is writable by every user"]
        # The working directory (an empty or relative entry) is checked like any other folder.
        monkeypatch.chdir(shared)
        assert trusted_integrations.environment_problems(home, path=["", "lib"]) == [
            f"{shared} is writable by every user"
        ]
    finally:
        shared.chmod(0o755)
        sticky.chmod(0o755)


@posix_only
def test_folders_of_another_user_or_a_foreign_group_are_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The installation belongs to root here: this process only pretends to be another user than the owner of
    # ``theirs``, which would also make a home in ``tmp_path`` someone else's.
    root_home = Path(os.path.realpath("/usr"))
    monkeypatch.setattr(trusted_integrations, "_writable", lambda path: True)
    theirs = _folder(tmp_path / "theirs")
    _foreign_owner(monkeypatch, theirs, same_user=False, same_group=True)
    assert trusted_integrations.environment_problems(root_home, path=[str(theirs)]) == [
        f"{theirs} belongs to another user"
    ]
    home = _folder(tmp_path / "site-packages")
    group = _folder(tmp_path / "group", 0o775)
    _foreign_owner(monkeypatch, group, same_user=True, same_group=False)
    assert trusted_integrations.environment_problems(home, path=[str(group)]) == [
        f"{group} is writable by a group of other users"
    ]
    _foreign_owner(monkeypatch, group, same_user=True, same_group=True)  # the user's own group
    assert trusted_integrations.environment_problems(home, path=[str(group)]) == []


@posix_only
def test_a_writable_entry_of_a_sealed_installation_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container images: the installation belongs to root, the process runs as another user. A folder it can
    write on the import path (its home or data volume as the working directory, a ``PYTHONPATH`` entry) would let
    tool output become host code."""
    home = _folder(tmp_path / "opt" / "site-packages")
    volume = _folder(tmp_path / "var" / "lib" / "nodestro")
    monkeypatch.setattr(trusted_integrations, "_writable", lambda path: not str(path).startswith(str(home.parent)))
    assert trusted_integrations.environment_problems(home, path=[str(home), str(volume)]) == [
        f"{volume} is writable by this process, its installation {home} is not"
    ]
    missing = volume / "not-yet"  # whoever can create it counts
    assert trusted_integrations.environment_problems(home, path=[str(missing)]) == [
        f"{volume} is writable by this process, its installation {home} is not"
    ]
    monkeypatch.setattr(trusted_integrations, "_writable", lambda path: True)  # a user-owned installation
    assert trusted_integrations.environment_problems(home, path=[str(home), str(volume)]) == []


def test_without_readable_permissions_the_host_import_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: ACLs are not read, so a closed import path is not proven and the trusted import stays refused."""
    monkeypatch.setattr(trusted_integrations, "_POSIX_MODES", False)
    assert trusted_integrations.environment_problems(tmp_path, path=[]) == [
        "the permissions of the import path cannot be checked on this system"
    ]
