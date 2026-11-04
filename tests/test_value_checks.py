"""Key figures that must come from a tool (``source="tool"``) and the deterministic ``table_totals`` tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.builtin_tools import number_of, parse_tables, table_totals
from nodestro_runtime.schemas import OutputConfig, OutputField, RunIn, ToolCallRecord, Workflow, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, parsed, tool_use
from nodestro_runtime.value_checks import correction_prompt, unverified_fields
from nodestro_runtime.workspace import Workspace

LOG = """Plant: Example Plant North, filling line L2
Period: weeks 14-15, total planned production time 14,400 minutes

| No. | Machine | Duration (min) | Fault code |
|---|---|---|---|
| 1 | Labeller | 45 | E-17 |
| 2 | Filler | 120 | F-03 |
| 3 | Labeller | 30 | E-17 |
| 4 | Packer | 25 | P-08 |
| 5 | Labeller | 50 | E-17 |
| 6 | Filler | 90 | F-11 |
| 7 | Labeller | 40 | E-17 |
| 8 | Packer | 35 | P-08 |
| 9 | Filler | 60 | F-03 |
| 10 | Labeller | 55 | E-17 |
| 11 | Filler | 95 | F-11 |
| 12 | Packer | 20 | P-02 |
"""
TABLE = LOG[LOG.index("| No.") :]
SHARE = 665 / 14400 * 100  # 4.618...


# ---- table_totals ------------------------------------------------------------------------------------------------


def test_table_totals_adds_up_every_row_and_ranks_the_groups() -> None:
    result = table_totals.function(TABLE, "Duration", group_by="Machine", reference_total=14400)
    assert result["rows"] == 12 and result["total"] == 665
    assert result["share_of_reference_percent"] == pytest.approx(SHARE)
    assert [(group["group"], group["total"]) for group in result["groups"]] == [
        ("Filler", 365),
        ("Labeller", 220),
        ("Packer", 80),
    ]
    assert result["groups"][-1]["cumulative_share_percent"] == pytest.approx(100)


def test_table_totals_reads_csv_and_european_numbers() -> None:
    csv = "Anlage;Dauer (min)\nFüller;1.234,5\nPacker;14 400\nEtikettierer;45 min\n"
    result = table_totals.function(csv, "Dauer")
    assert result["total"] == pytest.approx(1234.5 + 14400 + 45)
    assert number_of("1,500") == 1500 and number_of("1,5") == 1.5 and number_of("4.618") == 4.618
    assert number_of("n/a") is None


def test_table_totals_explains_an_unknown_or_ambiguous_column() -> None:
    from orkexa import ToolResultError

    with pytest.raises(ToolResultError, match=r"no column 'Shift'.*'Machine'"):
        table_totals.function(TABLE, "Shift")
    with pytest.raises(ToolResultError, match="no table"):
        table_totals.function("just words", "x")


def test_a_markdown_table_is_found_inside_a_longer_text() -> None:
    [(header, rows)] = parse_tables(LOG)
    assert header == ["No.", "Machine", "Duration (min)", "Fault code"] and len(rows) == 12


# ---- the check ---------------------------------------------------------------------------------------------------

OUTPUT = OutputConfig(
    mode="structured",
    fields=[
        OutputField(name="analysis"),
        OutputField(name="downtime_share_percent", type="number", source="tool"),
    ],
)


def _call(name: str, payload: dict[str, Any], output: Any) -> ToolCallRecord:
    return ToolCallRecord(id="c", name=name, input=payload, output=str(output))


def _totals(table: str) -> ToolCallRecord:
    payload = {"table": table, "value_column": "Duration", "reference_total": 14400}
    return _call("table_totals", payload, table_totals.function(table, "Duration", reference_total=14400))


def test_a_value_rounded_from_a_tool_result_is_accepted() -> None:
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 4.62}, [_totals(TABLE)], LOG) == []
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 4.6}, [_totals(TABLE)], LOG) == []
    calculator = _call("calculator", {"expression": "665/14400*100"}, SHARE)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 4.618}, [calculator], LOG) == []


def test_a_value_no_tool_computed_is_refused() -> None:
    # The share a small model reported without its tool in the live benchmark (RT-050).
    [failed] = unverified_fields(OUTPUT, {"downtime_share_percent": 2.83}, [_totals(TABLE)], LOG)
    assert (failed.field, failed.reason) == ("downtime_share_percent", "not_from_tool")
    [missing] = unverified_fields(OUTPUT, {"analysis": "x"}, [], LOG)
    assert missing.reason == "missing"
    failing_tool = ToolCallRecord(id="c", name="calculator", input={}, output="4.62", is_error=True)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 4.62}, [failing_tool], LOG)[0].reason == (
        "not_from_tool"
    )


def test_a_total_over_a_shortened_table_is_refused() -> None:
    shortened = "\n".join(TABLE.splitlines()[:-3])  # the last three rows left out
    record = _totals(shortened)
    value = round(table_totals.function(shortened, "Duration", reference_total=14400)["share_of_reference_percent"], 2)
    [failed] = unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG)
    assert (failed.reason, failed.rows_passed, failed.rows_in_input) == ("table_incomplete", 9, 12)
    assert "had 9 rows, the input has 12" in correction_prompt("Task", [failed])


def test_fields_without_the_source_are_not_checked() -> None:
    plain = OutputConfig(mode="structured", fields=[OutputField(name="score", type="number")])
    assert unverified_fields(plain, {"score": 7}, [], "") == []


# ---- in a run ----------------------------------------------------------------------------------------------------


def _flow(*, tools: list[str]) -> WorkflowIn:
    agent = {
        "name": "Analyst",
        "system_prompt": "Analyst",
        "task_template": "{{input}}",
        "tools": tools,
        "output": {
            "mode": "structured",
            "fields": [{"name": "downtime_share_percent", "type": "number", "source": "tool"}],
        },
    }
    return WorkflowIn.model_validate(
        {
            "name": "Downtime",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "a", "type": "agent", "label": "A", "agent": agent},
                {
                    "id": "gate",
                    "type": "condition",
                    "condition": {
                        "source": "a",
                        "rules": [{"id": "above", "field": "downtime_share_percent", "operator": "gt", "value": 3}],
                    },
                },
                {"id": "out", "type": "output"},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "a"},
                {"id": "e2", "source": "a", "target": "gate"},
                {"id": "e3", "source": "gate", "target": "out", "source_handle": "above"},
            ],
        }
    )


def _share(value: float) -> Any:
    return parsed(lambda model: model(downtime_share_percent=value))


TOTALS_CALL = {"table": TABLE, "value_column": "Duration", "reference_total": 14400}


async def test_a_run_decides_on_the_value_the_tool_computed(tmp_path: Path) -> None:
    fake = ScriptedClient({"Analyst": [tool_use("table_totals", TOTALS_CALL), _share(4.62)]})
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(_flow(tools=["table_totals"]))
        done = await workspace.wait((await workspace.run(saved, RunIn(input=LOG))).id, timeout=20)
        assert done.status == "succeeded", done.error
        assert done.nodes["out"].status == "succeeded"


async def test_an_estimated_value_gets_one_correction_and_then_fails_the_step(tmp_path: Path) -> None:
    fake = ScriptedClient({"Analyst": [_share(2.83), _share(2.83)]})
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(_flow(tools=["table_totals"]))
        done = await workspace.wait((await workspace.run(saved, RunIn(input=LOG))).id, timeout=20)
        assert done.status == "failed"
        step = done.nodes["a"]
        assert step.error_code == "run.value_unverified"
        assert step.error_params["field"] == "downtime_share_percent"
        # The gate never decided on the unchecked figure.
        assert done.nodes["gate"].status != "succeeded"
        # The second request told the agent why its value was not accepted.
        second = fake.messages.calls[1]
        assert "not the result of a tool call" in str(second["messages"])


async def test_the_correction_can_compute_the_value(tmp_path: Path) -> None:
    fake = ScriptedClient({"Analyst": [_share(2.83), tool_use("table_totals", TOTALS_CALL), _share(4.62)]})
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(_flow(tools=["table_totals"]))
        done = await workspace.wait((await workspace.run(saved, RunIn(input=LOG))).id, timeout=20)
        assert done.status == "succeeded", done.error


def test_the_check_names_a_checked_value_without_any_tool(tmp_path: Path) -> None:
    from nodestro_runtime.preflight import preflight

    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        saved = workspace.save_workflow(_flow(tools=[]))
        check = preflight(workspace, Workflow.model_validate(saved.model_dump()))
        codes = {(item.code, item.node_id) for item in check.problems}
        assert ("check.value_source_no_tool", "a") in codes
        saved = workspace.save_workflow(_flow(tools=["table_totals"]))
        check = preflight(workspace, Workflow.model_validate(saved.model_dump()))
        assert "check.value_source_no_tool" not in {item.code for item in check.problems}
