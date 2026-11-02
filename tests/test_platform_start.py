"""Nodestro Runtime imports and starts on every supported operating system (Linux, macOS, Windows).

These tests run on the CI job ``platforms`` on real macOS and Windows runners, and on Linux in the normal suite. They
guard against platform-specific code that runs at import time (``ctypes`` lookups of Linux-only functions such as
``prctl``, POSIX-only modules such as ``resource`` or ``fcntl``) and so stops the whole application from starting
on a computer where only one sandbox backend is missing. A missing sandbox backend must show up as "not available",
and untrusted code must still be refused.

``test_import_without_linux_kernel_functions`` also simulates macOS and Windows on Linux: it starts an interpreter in
which ``prctl`` does not exist and (for Windows) ``resource`` and ``fcntl`` cannot be imported and
``ctypes.CDLL(None)`` fails, imports every module there, and then checks under the other system's ``sys.platform``
that the built-in sandbox reports "platform" and untrusted code is refused.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import pkgutil
import subprocess
import sys
import textwrap

from orkexa.dynamic import SubprocessSandbox
import pytest

import nodestro_runtime
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.sandbox_policy import SandboxPolicy

pytestmark = pytest.mark.real_sandbox_status  # the computer's real sandbox status (see conftest.py)

#: Modules that may need an optional dependency that the platform job does not install.
_OPTIONAL = {
    "nodestro_runtime.storage_postgres",
    "nodestro_runtime.postgres_import",
    "nodestro_runtime.postgres_tables",
}


def _modules() -> list[str]:
    found = ["nodestro_runtime"]
    for info in pkgutil.walk_packages(nodestro_runtime.__path__, "nodestro_runtime."):
        if ".migrations." in info.name and info.name.count(".") > 2:
            continue
        found.append(info.name)
    return sorted(found)


@pytest.mark.parametrize("name", _modules())
def test_every_module_imports(name: str) -> None:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if name in _OPTIONAL and exc.name in {"psycopg", "psycopg_pool", "cryptography"}:
            pytest.skip(f"optional dependency {exc.name} is not installed")
        raise


def test_workspace_starts_and_reports_the_sandbox(tmp_path: Path) -> None:
    from nodestro_runtime.workspace import Workspace

    with Workspace(tmp_path / "data") as workspace:
        settings = workspace.settings()
        status = settings.builtin_sandbox
        if not sys.platform.startswith("linux"):
            # The built-in sandbox is Linux-only: it is reported as missing, never as available.
            assert status.available is False
            assert status.reason == "platform"
        assert settings.sandbox == "auto"  # nothing chosen yet
        assert settings.sandbox_selection.os in {"linux", "macos", "windows"}
        if not sys.platform.startswith("linux") and not settings.docker_available:
            assert settings.sandbox_selection.blocked
            assert settings.sandbox_selection.reason == "no-isolation"


def test_untrusted_code_is_refused_without_isolation() -> None:
    """Without the built-in sandbox and Docker, untrusted code is refused, never run in a host subprocess."""
    from nodestro_runtime.builtin_sandbox import LinuxSandbox

    policy = SandboxPolicy("desktop")
    sandbox = SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True)
    if LinuxSandbox.status().available:
        pytest.skip("this computer has the built-in sandbox")
    with pytest.raises(NodestroError) as caught:
        policy.require("untrusted", sandbox, name="probe")
    assert caught.value.code == "sandbox.isolation_required"
    assert policy.allows("user-authored", sandbox)


def test_user_authored_code_runs_in_the_subprocess_sandbox() -> None:
    """The desktop runs a person's own tool code in a host subprocess on every system (ADR-0006)."""
    code = (
        "def add(a: int, b: int) -> int:\n"
        '    """Add two numbers.\n\n    Args:\n        a: First.\n        b: Second.\n\n'
        '    Returns:\n        The sum.\n    """\n'
        "    return a + b\n"
    )
    sandbox = SubprocessSandbox(timeout=30.0, acknowledge_unsafe=True)
    assert sandbox.call(code, "add", {"a": 2, "b": 3}) == "5"


_SIMULATE = textwrap.dedent(
    """
    import builtins, ctypes, json, sys
    target = sys.argv[1]
    real_import = builtins.__import__
    blocked = {"resource", "fcntl"} if target == "win32" else set()

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    for name in blocked:
        sys.modules.pop(name, None)

    real_cdll = ctypes.CDLL

    class FakeLibc:
        def __init__(self, *args, **kwargs):
            if target == "win32" and (not args or args[0] is None):
                raise TypeError("expected str, bytes or os.PathLike object, not NoneType")
            self._real = real_cdll(*args, **kwargs)

        def __getattr__(self, name):
            if name in {"prctl", "syscall"} and target != "linux":
                raise AttributeError(f"dlsym(RTLD_DEFAULT, {name}): symbol not found")
            return getattr(self._real, name)

    ctypes.CDLL = FakeLibc
    import pkgutil, importlib
    import nodestro_runtime
    failed = {}
    for info in pkgutil.walk_packages(nodestro_runtime.__path__, "nodestro_runtime."):
        if ".migrations." in info.name:
            continue
        try:
            importlib.import_module(info.name)
        except ModuleNotFoundError as exc:
            if exc.name in {"psycopg", "psycopg_pool", "cryptography"}:
                continue
            failed[info.name] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            failed[info.name] = f"{type(exc).__name__}: {exc}"
    # Only now the other system's name: the standard library itself reads sys.platform while it imports.
    sys.platform = target
    from nodestro_runtime.builtin_sandbox import LinuxSandbox
    from nodestro_runtime.sandbox_confine import landlock_abi
    from nodestro_runtime.sandbox_policy import SandboxPolicy
    from orkexa.dynamic import SubprocessSandbox
    status = LinuxSandbox.status(refresh=True)
    try:
        SandboxPolicy("desktop").require("untrusted", SubprocessSandbox(acknowledge_unsafe=True))
        refused = ""
    except Exception as exc:
        refused = getattr(exc, "code", type(exc).__name__)
    print(json.dumps({"failed": failed, "available": status.available, "reason": status.reason,
                      "abi": landlock_abi(), "refused": refused}))
    """
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="simulates the other systems on Linux")
@pytest.mark.parametrize("target", ["darwin", "win32"])
def test_import_without_linux_kernel_functions(target: str, tmp_path: Path) -> None:
    script = tmp_path / "simulate.py"
    script.write_text(_SIMULATE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), target], capture_output=True, text=True, timeout=300, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["failed"] == {}
    assert report["available"] is False
    assert report["reason"] == "platform"
    assert report["abi"] == 0
    assert report["refused"] == "sandbox.isolation_required"
