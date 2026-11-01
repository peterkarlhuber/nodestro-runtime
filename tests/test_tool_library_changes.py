"""Tool libraries of several processes on one tool directory (S-2 pilot finding F1).

Each library stands for one process: a change or deletion in one is seen by the others at their next lookup, and a
tool an agent already holds refuses to run once its version is no longer the current approved one.
"""

from __future__ import annotations

from pathlib import Path

from orkexa import ToolExecutionError
from orkexa.dynamic import SubprocessSandbox
import pytest

from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, PinnedTool, ToolLibrary
from nodestro_runtime.tool_provenance import MemoryProvenance

NAME = "celsius_to_fahrenheit"
CHANGED = CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 1032")
CHANGED_TESTS = (
    "from tool import celsius_to_fahrenheit\n\n\ndef test_it():\n    assert celsius_to_fahrenheit(0) == 1032\n"
)


#: Which versions a person wrote in the editor; shared like the database the processes share.
AUTHORED = MemoryProvenance()


def library(path: Path) -> ToolLibrary:
    return ToolLibrary(path, sandbox=SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True), provenance=AUTHORED)


def save(target: ToolLibrary, code: str = CUSTOM_TOOL_TEMPLATE, tests: str = CUSTOM_TESTS_TEMPLATE) -> int:
    result, info = target.save_custom(code, tests, NAME, trust="user-authored")
    assert result.ok and info is not None and info.version is not None, result
    return info.version


def test_a_change_in_one_process_is_seen_by_the_others(tmp_path: Path) -> None:
    api, worker = library(tmp_path / "tools"), library(tmp_path / "tools")
    assert NAME not in worker
    save(api)
    assert NAME in worker and worker.get(NAME).version == 1
    assert worker.run(NAME, {"celsius": 100}) == ("212.0", False)
    save(api, CHANGED, CHANGED_TESTS)
    assert worker.get(NAME).version == 2 and worker.get(NAME).code == CHANGED
    assert worker.run(NAME, {"celsius": 100}) == ("1212.0", False)
    api.delete(NAME)
    assert NAME not in worker and NAME not in [info.name for info in worker.all()]
    with pytest.raises(KeyError):
        worker.get(NAME)
    with pytest.raises(KeyError):
        worker.tool(NAME)


def test_versions_stay_in_order_across_processes(tmp_path: Path) -> None:
    first, second = library(tmp_path / "tools"), library(tmp_path / "tools")
    assert save(first) == 1
    assert save(second, CHANGED, CHANGED_TESTS) == 2  # not a second version 1 over the first one's file
    assert save(first) == 3
    assert [record.status for record in second.registry.versions(NAME)] == ["revoked", "revoked", "approved"]


@pytest.mark.parametrize("change", ["save", "delete"])
def test_a_held_tool_refuses_once_its_version_is_not_current(tmp_path: Path, change: str) -> None:
    api, worker = library(tmp_path / "tools"), library(tmp_path / "tools")
    save(api)
    held = worker.tool(NAME)
    assert isinstance(held, PinnedTool) and held.version == 1
    assert held.execute({"celsius": 100}) == "212.0"
    if change == "save":
        save(api, CHANGED, CHANGED_TESTS)
    else:
        api.delete(NAME)
    with pytest.raises(ToolExecutionError, match="changed or deleted"):
        held.execute({"celsius": 100})
    with pytest.raises(ToolExecutionError, match="changed or deleted"):
        held(celsius=100)  # a direct call goes through the same check


def test_a_held_tool_with_another_sandbox_is_bound_too(tmp_path: Path) -> None:
    api, worker = library(tmp_path / "tools"), library(tmp_path / "tools")
    save(api)
    held = worker.tool(NAME, sandbox=SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True))
    assert isinstance(held, PinnedTool)
    save(api, CHANGED, CHANGED_TESTS)
    with pytest.raises(ToolExecutionError, match="changed or deleted"):
        held.execute({"celsius": 100})


def test_a_tampered_record_is_not_current(tmp_path: Path) -> None:
    api = library(tmp_path / "tools")
    save(api)
    held = api.tool(NAME)
    record = tmp_path / "tools" / NAME / "v1.json"
    record.write_text(record.read_text(encoding="utf-8").replace("+ 32", "+ 33"), encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="changed or deleted"):
        held.execute({"celsius": 100})
    assert NAME not in api  # the registry skips a record whose code does not match its hash


def test_a_library_in_memory_is_unchanged(tmp_path: Path) -> None:
    memory = ToolLibrary(None, sandbox=SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True))
    save(memory)
    held = memory.tool(NAME)
    assert not isinstance(held, PinnedTool)
    assert held.execute({"celsius": 100}) == "212.0"
