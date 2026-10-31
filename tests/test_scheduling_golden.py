"""Golden traces: the engine schedules exactly as it did before it used the orkexa.flow kernel.

See :mod:`scheduling_traces` for what is compared and how the traces were recorded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime import engine
from nodestro_runtime.testing import ScriptedClient
from nodestro_runtime.workspace import Workspace

from scheduling_traces import GOLDEN, Scripted, play, scenarios

CASES = scenarios()


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    if os.environ.get("NODESTRO_WRITE_GOLDEN"):
        return {}
    return json.loads(GOLDEN.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


_written: dict[str, Any] = {}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
async def test_trace_matches_the_recorded_one(
    case: dict[str, Any], recorded: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ScriptedClient({})  # never asked: agents are scripted below the provider
    workspace = Workspace(tmp_path / "data", client_factory=lambda _provider: fake, integrations=[])
    try:
        scripted = Scripted(workspace)
        monkeypatch.setattr(engine, "execute_agent", scripted)
        trace = await play(workspace, scripted, case)
    finally:
        workspace.close()
    if os.environ.get("NODESTRO_WRITE_GOLDEN"):
        _written[case["name"]] = trace
        GOLDEN.write_text(json.dumps(_written, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert trace == recorded[case["name"]]


def test_every_recorded_trace_has_a_scenario(recorded: dict[str, Any]) -> None:
    if os.environ.get("NODESTRO_WRITE_GOLDEN"):
        pytest.skip("recording")
    assert set(recorded) == {case["name"] for case in CASES}
