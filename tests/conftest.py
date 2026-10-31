"""Shared test setup.

The built-in Linux sandbox (:mod:`nodestro_runtime.builtin_sandbox`) is available on some development machines and
CI runners and not on others. Unless a test module is marked ``real_sandbox_status``, every test sees a computer
without it, so tests of the rules for computers without isolation behave the same everywhere. Tests of the computer
with the sandbox set the status themselves or are in a marked module.
"""

from __future__ import annotations

import pytest

from nodestro_runtime.builtin_sandbox import LinuxSandbox, SandboxStatus

_UNAVAILABLE = SandboxStatus(False, "untested-system", "not available in this test", "", 0)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_sandbox_status: use the computer's real built-in sandbox status")


@pytest.fixture(autouse=True)
def _builtin_sandbox_status(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("real_sandbox_status") is None:
        monkeypatch.setattr(LinuxSandbox, "status", staticmethod(lambda refresh=False: _UNAVAILABLE))
