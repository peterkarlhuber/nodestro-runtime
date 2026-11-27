"""The data-only extension store ``<data>/extensions/<sha256>/`` (RT-098, ``docs/extension-isolation.md``).

Foreign packages enter only through :meth:`ExtensionStore.install`, are checked before anything is written, appear
atomically, are never put on ``sys.path`` or imported in the host, and run only in the (here: fake) Docker sandbox.
Removal, revocation and updates never let an invalid package run again. The real engine's isolation of store packages
is tested in ``test_extension_isolation.py`` (``Real`` takes its package from a real store).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any
import zipfile

from orkexa.errors import ToolExecutionError
import pytest
from test_extension_isolation import (  # noqa: F401 - docker_cli is an autouse fixture of these tests too
    MODULE,
    Containers,
    build_wheel,
    docker_cli,
    wheel_members,
)

from nodestro_runtime import activity, extension_classes
from nodestro_runtime.extension_sandbox import ExtensionSandbox, PackageError
from nodestro_runtime.extension_store import INDEX, MAX_WHEEL_BYTES, STAGING, ExtensionStore, StoreError
from nodestro_runtime.extensions import EXTENSION_TOOLS_ENV, HOST_INSTALLED, Extensions, discover, find_packages
from nodestro_runtime.profile import RuntimeProfile
from nodestro_runtime.workspace import Workspace


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "imported-in-host.txt"


@pytest.fixture
def store(tmp_path: Path) -> ExtensionStore:
    return ExtensionStore(tmp_path / "data" / "extensions")


def install(store: ExtensionStore, wheel: bytes, **options: Any) -> dict[str, Any]:
    return store.install(
        wheel, **{"sha256": sha(wheel), "origin": "https://example.org/acme", "actor": "local", **options}
    )


def folders(store: ExtensionStore) -> list[str]:
    """What the store folder holds besides its index and the (empty) staging and trash folders."""
    if not store.root.exists():
        return []
    found = []
    for child in store.root.iterdir():
        if child.name in (STAGING, ".trash"):
            found += [f"{child.name}/{item.name}" for item in child.iterdir()]
        elif child.name != INDEX:
            found.append(child.name)
    return sorted(found)


def nothing_imported(marker: Path, *, contained: bool = False) -> None:
    """The package was never imported in the host. ``contained``: its code ran in a :class:`FakeContainer` (a child
    process on this machine, tests only), which may have left the marker."""
    assert contained or not marker.exists()
    assert MODULE not in sys.modules
    assert importlib.util.find_spec(MODULE) is None  # not importable from the host's search path
    assert not [entry for entry in sys.path if "extensions" in Path(entry).parts]


# ---- intake --------------------------------------------------------------------------------------------------------


def test_a_checked_wheel_is_kept_read_only_by_its_hash_and_never_imported(store: ExtensionStore, marker: Path) -> None:
    wheel = build_wheel(wheel_members(marker=marker))
    entry = install(store, wheel)
    assert entry["sha256"] == sha(wheel) and entry["distribution"] == "acme-ext" and entry["version"] == "1.0"
    assert entry["entries"] == [["acme", f"{MODULE}:integration"]] and entry["origin"] == "https://example.org/acme"
    folder = store.root / sha(wheel)
    assert folders(store) == [sha(wheel)]
    assert stat.S_IMODE((folder / MODULE / "__init__.py").stat().st_mode) == 0o444
    assert stat.S_IMODE(folder.stat().st_mode) == 0o555 and stat.S_IMODE(store.root.stat().st_mode) == 0o700
    [package] = store.packages()
    assert (package.name, package.entry, package.distribution, package.sha256) == (
        "acme",
        f"{MODULE}:integration",
        "acme-ext",
        sha(wheel),
    )
    assert list(package.read_files()) == [f"{MODULE}/__init__.py"]
    assert store.listing()["packages"][0]["sha256"] == sha(wheel)
    # Never in the host: not imported, not on the search path, not found by the host's discovery.
    nothing_imported(marker)
    assert discover() == [] or all(item.name != "acme" for item in discover())
    assert all(item.distribution != "acme-ext" for item in find_packages())


def _special(name: str, mode: int = stat.S_IFREG | 0o644) -> zipfile.ZipInfo:
    item = zipfile.ZipInfo(name)
    item.create_system = 3
    item.external_attr = mode << 16
    return item


def _bzip2(name: str) -> zipfile.ZipInfo:
    item = _special(name)
    item.compress_type = zipfile.ZIP_BZIP2
    return item


def _encrypted(wheel: bytes, name: str) -> bytes:
    """``wheel`` with the encryption flag set on ``name`` in the central directory (zipfile cannot write one)."""
    data = bytearray(wheel)
    start = 0
    while (start := data.find(b"PK\x01\x02", start)) >= 0:
        length = int.from_bytes(data[start + 28 : start + 30], "little")
        if data[start + 46 : start + 46 + length] == name.encode():
            data[start + 8] |= 0x1
        start += 4
    return bytes(data)


INFO = "acme_ext-1.0.dist-info"


def _tampered_record(members: dict[str, bytes]) -> bytes:
    wheel = build_wheel(members)
    source = zipfile.ZipFile(__import__("io").BytesIO(wheel))
    out = __import__("io").BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == f"{MODULE}/__init__.py":
                content += b"\nTAMPERED = True\n"
            archive.writestr(item.filename, content)
    return out.getvalue()


#: name -> (how to make the wheel from the package's members, the refusal reason).
REFUSED: dict[str, tuple[Any, str]] = {
    "not a zip": (lambda m: b"not a wheel", "archive"),
    "absolute path": (lambda m: build_wheel(m, special=[(_special("/etc/evil.py"), b"x")]), "unsafe_path"),
    "parent path": (lambda m: build_wheel(m, special=[(_special(f"{MODULE}/../../evil.py"), b"x")]), "unsafe_path"),
    "backslash": (lambda m: build_wheel(m, special=[(_special(f"{MODULE}\\evil.py"), b"x")]), "unsafe_path"),
    "drive": (lambda m: build_wheel(m, special=[(_special("C:/evil.py"), b"x")]), "unsafe_path"),
    "same name in another case": (
        lambda m: build_wheel(m, special=[(_special(f"{MODULE.upper()}/__init__.py"), b"x")]),
        "unsafe_path",
    ),
    "symbolic link": (
        lambda m: build_wheel(m, special=[(_special(f"{MODULE}/link.py", stat.S_IFLNK | 0o777), b"/etc/passwd")]),
        "unsafe_member",
    ),
    "block device": (
        lambda m: build_wheel(m, special=[(_special(f"{MODULE}/disk", stat.S_IFBLK | 0o600), b"")]),
        "unsafe_member",
    ),
    "fifo": (
        lambda m: build_wheel(m, special=[(_special(f"{MODULE}/pipe", stat.S_IFIFO | 0o600), b"")]),
        "unsafe_member",
    ),
    "other compression": (lambda m: build_wheel(m, special=[(_bzip2(f"{MODULE}/b.py"), b"x = 1\n")]), "unsafe_member"),
    "encrypted": (lambda m: _encrypted(build_wheel(m), f"{MODULE}/__init__.py"), "unsafe_member"),
    "no RECORD": (lambda m: build_wheel(m, record=False), "record"),
    "member not in RECORD": (
        lambda m: _with_extra_member(build_wheel(m), f"{MODULE}/extra.py", b"x = 1\n"),
        "record",
    ),
    "changed after RECORD": (_tampered_record, "hash_mismatch"),
    "pth file": (lambda m: build_wheel({**m, "acme.pth": b"import os; os.system('id')\n"}), "unsafe_member"),
    "startup module": (lambda m: build_wheel({**m, "sitecustomize.py": b"print(1)\n"}), "unsafe_member"),
    "native code": (lambda m: build_wheel({**m, f"{MODULE}/fast.so": b"\x7fELF"}), "unsafe_member"),
    "data part (scripts)": (
        lambda m: build_wheel({**m, "acme_ext-1.0.data/scripts/acme": b"#!/bin/sh\nid\n"}),
        "data_part",
    ),
    "two dist-info folders": (
        lambda m: build_wheel({**m, "other-1.0.dist-info/METADATA": b"Name: other\nVersion: 1.0\n"}),
        "manifest",
    ),
    "not pure": (
        lambda m: build_wheel(
            {**m, f"{INFO}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-linux_x86_64\n"}
        ),
        "not_pure",
    ),
    "metadata of another name": (
        lambda m: build_wheel(
            {**m, f"{INFO}/METADATA": b"Metadata-Version: 2.1\nName: nodestro-integrations\nVersion: 1.0\n"}
        ),
        "manifest",
    ),
    "a dependency": (
        lambda m: build_wheel({**m, f"{INFO}/METADATA": m[f"{INFO}/METADATA"] + b"Requires-Dist: requests>=2\n"}),
        "dependency",
    ),
    "console script": (
        lambda m: build_wheel(
            {**m, f"{INFO}/entry_points.txt": m[f"{INFO}/entry_points.txt"] + b"[console_scripts]\nacme = x:y\n"}
        ),
        "manifest",
    ),
    "no entry point": (lambda m: build_wheel({**m, f"{INFO}/entry_points.txt": b""}), "manifest"),
    "entry point of a missing module": (
        lambda m: build_wheel(
            {**m, f"{INFO}/entry_points.txt": b"[nodestro.integrations]\nacme = other_mod:integration\n"}
        ),
        "manifest",
    ),
    "too large": (lambda m: b"PK" + b"\0" * MAX_WHEEL_BYTES, "too_large"),
    "too many members": (
        lambda m: build_wheel({**m, **{f"{MODULE}/m{index}.py": b"" for index in range(1001)}}),
        "too_large",
    ),
}


def _with_extra_member(wheel: bytes, name: str, content: bytes) -> bytes:
    import io

    out = io.BytesIO(wheel)
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr(name, content)
    return out.getvalue()


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_an_unsafe_or_inconsistent_wheel_is_refused_before_anything_is_written(
    tmp_path: Path, store: ExtensionStore, marker: Path, case: str
) -> None:
    make, reason = REFUSED[case]
    wheel = make(wheel_members(marker=marker))
    with pytest.raises(StoreError) as caught:
        install(store, wheel)
    assert caught.value.code == "extension_store.refused" and caught.value.params["reason"] == reason, caught.value
    assert folders(store) == [] and store.packages() == []
    nothing_imported(marker)


def test_the_expected_hash_and_the_origin_are_required(store: ExtensionStore, marker: Path) -> None:
    wheel = build_wheel(wheel_members(marker=marker))
    for options, reason in (
        ({"sha256": "0" * 64}, "hash_mismatch"),
        ({"sha256": ""}, "hash_mismatch"),
        ({"origin": "  "}, "origin"),
        ({"origin": "a\nb"}, "origin"),
    ):
        with pytest.raises(StoreError) as caught:
            install(store, wheel, **options)
        assert caught.value.params["reason"] == reason
    assert folders(store) == []


def test_optional_and_provided_dependencies_are_accepted(store: ExtensionStore, marker: Path) -> None:
    extra = 'Requires-Dist: pydantic>=2\nRequires-Dist: pytest; extra == "test"\nRequires-Dist: orkexa\n'
    wheel = build_wheel(wheel_members(marker=marker, metadata_extra=extra))
    assert install(store, wheel)["sha256"] == sha(wheel)


# ---- atomic intake ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["while writing", "before the commit"])
def test_an_interrupted_intake_never_leaves_a_valid_package(
    tmp_path: Path, store: ExtensionStore, marker: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A crash at any point before the index is replaced leaves nothing valid; the next open removes the rest."""
    wheel = build_wheel(wheel_members(marker=marker))

    class Crash(BaseException):
        pass

    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise Crash

    if where == "while writing":
        monkeypatch.setattr("nodestro_runtime.extension_store.os.fsync", crash)
    else:
        monkeypatch.setattr(ExtensionStore, "_commit", crash)
    with pytest.raises(Crash):
        install(store, wheel)
    monkeypatch.undo()
    # A crash that even skipped the cleanup: a folder named by the hash but not listed, and a staging folder.
    (store.root / STAGING / "left-over").mkdir(parents=True, exist_ok=True)
    (store.root / sha(wheel)).mkdir(exist_ok=True)
    (store.root / sha(wheel) / "half.py").write_text("x = 1\n")
    reopened = ExtensionStore(store.root)
    assert reopened.packages() == [] and reopened.listing()["packages"] == []
    assert folders(reopened) == []
    assert install(reopened, wheel)["sha256"] == sha(wheel)  # a clean retry works


# ---- tampering after intake ------------------------------------------------------------------------------------------


def _writable(path: Path) -> None:
    for item in [path, *path.parents]:
        if item == path.parents[len(path.parents) - 1]:
            break
        os.chmod(item, 0o700 if item.is_dir() else 0o600)


TAMPER = {
    "changed file": lambda folder: (folder / MODULE / "__init__.py").write_text("TAMPERED = 1\n"),
    "added file": lambda folder: (folder / MODULE / "added.py").write_text("x = 1\n"),
    "removed file": lambda folder: (folder / INFO / "WHEEL").unlink(),
    "changed RECORD": lambda folder: (folder / INFO / "RECORD").write_text(f"{MODULE}/__init__.py,,\n"),
    "symbolic link": lambda folder: (
        (folder / MODULE / "__init__.py").unlink(),
        (folder / MODULE / "__init__.py").symlink_to("/etc/hostname"),
    ),
    "hard link": lambda folder: os.link(folder / MODULE / "__init__.py", folder.parent.parent / "outside"),
}


@pytest.mark.parametrize("case", sorted(TAMPER))
def test_a_package_changed_after_intake_is_never_handed_to_the_sandbox(
    store: ExtensionStore, marker: Path, case: str
) -> None:
    wheel = build_wheel(wheel_members(marker=marker))
    install(store, wheel)
    folder = store.root / sha(wheel)
    for path in [folder, *[Path(root) for root, _dirs, _files in os.walk(folder)]]:
        os.chmod(path, 0o700)
    for path in folder.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
    TAMPER[case](folder)
    [package] = store.packages()
    with pytest.raises(PackageError):
        package.read_files()
    extensions = Extensions(packages=store.packages())
    containers = Containers()
    os.environ[EXTENSION_TOOLS_ENV] = "1"
    try:
        assert extensions.load_packages(ExtensionSandbox(containers), reserved=()) == []
    finally:
        os.environ.pop(EXTENSION_TOOLS_ENV)
    assert extensions.packages_status()[0]["state"] == "locked"
    assert extensions.packages_status()[0]["reason"] == "invalid"
    assert containers.runs == []  # nothing was sent to a container


# ---- removal, revocation, update ------------------------------------------------------------------------------------


def _offered(store: ExtensionStore, containers: Containers, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv(EXTENSION_TOOLS_ENV, "1")
    extensions = Extensions(packages=store.packages())
    return {spec.id: tool for spec, tool in extensions.load_packages(ExtensionSandbox(containers), reserved=())}


@pytest.mark.parametrize("action", ["remove", "revoke", "update"])
def test_a_removed_revoked_or_replaced_package_never_runs_again(
    store: ExtensionStore, marker: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The check before every container: a tool listed before the change is refused afterwards, without a container,
    although its verified files are still in memory."""
    wheel = build_wheel(wheel_members(marker=marker))
    install(store, wheel)
    containers = Containers()
    tools = _offered(store, containers, monkeypatch)
    assert tools["echo_text"].execute({"text": "before"}) == "before"
    runs = len(containers.runs)
    if action == "remove":
        store.remove(sha(wheel), actor="local")
    elif action == "revoke":
        store.revoke(sha(wheel), actor="local")
    else:
        newer = build_wheel(wheel_members(marker=marker, version="1.1"))
        install(store, newer, replaces=sha(wheel))
        assert [item.version for item in store.packages()] == ["1.1"]
    with pytest.raises(ToolExecutionError, match=r"no longer installed|revoked"):
        tools["echo_text"].execute({"text": "after"})
    assert len(containers.runs) == runs  # no container started
    assert not (store.root / sha(wheel)).exists()
    nothing_imported(marker, contained=True)


def test_a_revoked_hash_is_never_installed_again(store: ExtensionStore, marker: Path) -> None:
    wheel = build_wheel(wheel_members(marker=marker))
    install(store, wheel)
    store.revoke(sha(wheel), actor="local")
    with pytest.raises(StoreError) as caught:
        install(store, wheel)
    assert caught.value.params["reason"] == "revoked"
    other = build_wheel(wheel_members(marker=marker, version="2.0"))
    store.revoke(sha(other), actor="local")  # revoked before it was ever installed
    with pytest.raises(StoreError):
        install(store, other)
    assert store.packages() == [] and [item["sha256"] for item in store.listing()["revoked"]] == sorted(
        [sha(wheel), sha(other)]
    )
    # A removed (not revoked) package may come back, checked again.
    third = build_wheel(wheel_members(marker=marker, version="3.0"))
    install(store, third)
    store.remove(sha(third), actor="local")
    install(store, third)


def test_one_distribution_at_a_time_and_updates_only_of_the_same(store: ExtensionStore, marker: Path) -> None:
    first = build_wheel(wheel_members(marker=marker))
    install(store, first)
    second = build_wheel(wheel_members(marker=marker, version="1.1"))
    with pytest.raises(StoreError, match="update it instead"):
        install(store, second)
    other = build_wheel(wheel_members(marker=marker, name="other-ext"))
    with pytest.raises(StoreError, match="same distribution"):
        install(store, other, replaces=sha(first))
    with pytest.raises(StoreError) as caught:
        install(store, second, replaces="f" * 64)
    assert caught.value.code == "extension_store.not_found"
    with pytest.raises(StoreError) as caught:
        store.remove("f" * 64, actor="local")
    assert caught.value.code == "extension_store.not_found"
    assert [item.sha256 for item in store.packages()] == [sha(first)]


def test_an_unreadable_index_lists_nothing(store: ExtensionStore, marker: Path) -> None:
    install(store, build_wheel(wheel_members(marker=marker)))
    os.chmod(store.root / INDEX, 0o600)
    (store.root / INDEX).write_text("{broken")
    assert store.packages() == []
    with pytest.raises(StoreError):
        store.listing()


# ---- the workspace: discovery from the store, the sandbox, the audit -------------------------------------------------


def _workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, containers: Containers, *, switch: bool, kind: str = "desktop"
) -> Workspace:
    if switch:
        monkeypatch.setenv(EXTENSION_TOOLS_ENV, "1")
    else:
        monkeypatch.delenv(EXTENSION_TOOLS_ENV, raising=False)
    monkeypatch.setattr(Workspace, "_extension_sandbox", lambda _self, timeout: containers(timeout))
    return Workspace(profile=RuntimeProfile(kind=kind, data_dir=tmp_path / "data"))  # type: ignore[arg-type]


def test_the_workspace_offers_store_packages_only_through_the_sandbox(
    tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExtensionStore(tmp_path / "data" / "extensions")
    wheel = build_wheel(wheel_members(marker=marker))
    install(store, wheel)
    containers = Containers()
    workspace = _workspace(tmp_path, monkeypatch, containers, switch=False)
    try:
        assert workspace.extension_store is not None and workspace.extension_store.root == store.root
        [status] = workspace.extensions.packages_status()
        assert (status["state"], status["reason"]) == ("locked", "off") and containers.runs == []
    finally:
        workspace.close()
    workspace = _workspace(tmp_path, monkeypatch, Containers(ready=False), switch=True)
    try:
        [status] = workspace.extensions.packages_status()
        assert (status["state"], status["reason"]) == ("locked", "not_secured")
    finally:
        workspace.close()
    workspace = _workspace(tmp_path, monkeypatch, containers, switch=True)
    try:
        [status] = workspace.extensions.packages_status()
        assert status["state"] == "sandboxed" and "echo_text" in status["tools"] and status["sha256"] == sha(wheel)
        assert {run["request"]["mode"] for run in containers.runs} == {"describe"}
    finally:
        workspace.close()
    nothing_imported(marker, contained=True)


def test_a_host_installed_package_stays_refused_next_to_the_store(
    tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HOST_INSTALLED refusal keeps holding: the same distribution in the host environment is refused and does not
    make the store's copy count twice."""
    from test_extension_isolation import entry_points_of, install_package

    info = install_package(tmp_path / "site", marker=marker)
    monkeypatch.setattr(
        "nodestro_runtime.workspace.find_packages", lambda **options: find_packages(entry_points_of(info), **options)
    )
    install(ExtensionStore(tmp_path / "data" / "extensions"), build_wheel(wheel_members(marker=marker)))
    workspace = _workspace(tmp_path, monkeypatch, Containers(), switch=True)
    try:
        statuses = {(item["detail"] or "")[:20]: item for item in workspace.extensions.packages_status()}
        host = next(item for item in workspace.extensions.packages_status() if item["detail"] == HOST_INSTALLED)
        assert host["reason"] == "invalid"
        assert [item["state"] for item in statuses.values()].count("sandboxed") == 1
    finally:
        workspace.close()
    nothing_imported(marker, contained=True)


def test_explicit_integrations_list_no_store_package(tmp_path: Path, marker: Path) -> None:
    """The embedding application chose its integrations: the store can be changed, its packages are not listed."""
    install(ExtensionStore(tmp_path / "data" / "extensions"), build_wheel(wheel_members(marker=marker)))
    workspace = Workspace(tmp_path / "data", integrations=[])
    try:
        assert workspace.extension_store is not None and len(workspace.extension_store.packages()) == 1
        assert workspace.extensions.packages_status() == []
    finally:
        workspace.close()
    nothing_imported(marker)


def test_intake_removal_and_revocation_are_in_the_activity_log(
    tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, monkeypatch, Containers(), switch=False)
    try:
        store = workspace.extension_store
        assert store is not None
        wheel = build_wheel(wheel_members(marker=marker))
        with pytest.raises(StoreError):
            store.install(wheel, sha256="0" * 64, origin="tests", actor="user-1")
        install(store, wheel, actor="user-1")
        store.remove(sha(wheel), actor="user-1")
        store.revoke(sha(wheel), actor="user-2")
        events = activity.query(workspace.store)
        found = sorted(
            (item.action, item.actor, item.outcome, item.code) for item in events if item.source == "extension"
        )
        assert found == [
            ("extension_package.installed", "user-1", "success", None),
            ("extension_package.refused", "user-1", "denied", "hash_mismatch"),
            ("extension_package.removed", "user-1", "success", None),
            ("extension_package.revoked", "user-2", "success", None),
        ]
        assert {item.resource_id for item in events if item.source == "extension" and item.outcome == "success"} == {
            sha(wheel)
        }
    finally:
        workspace.close()


# ---- G-6 items 17, 18, 21: the classes that depend on them stay locked -----------------------------------------------


def test_classes_whose_prerequisite_is_missing_cannot_be_released() -> None:
    assert extension_classes.RELEASES == ()
    assert frozenset({17, 18, 21}) == extension_classes.MISSING_ITEMS
    for tool_class, items in extension_classes.PREREQUISITES.items():
        if items & extension_classes.MISSING_ITEMS:
            with pytest.raises(ValueError, match="G-6 item"):
                extension_classes.ClassRelease(tool_class, "evidence", "internal", "independent")
    assert extension_classes.PREREQUISITES[("read", "internal")] == frozenset()
    assert extension_classes.PREREQUISITES[("write", "internal")] == frozenset({18})
    assert extension_classes.PREREQUISITES[("read", "external")] == frozenset({17})
    assert json.dumps(sorted(extension_classes.PREREQUISITES[("write", "external")])) == "[17, 18]"
