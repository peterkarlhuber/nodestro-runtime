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


def test_a_calculator_call_with_the_value_itself_backs_nothing() -> None:
    # A household check reported a surplus of 463 that no tool computed; echoing it through the calculator must not
    # make it "checked", while the whole calculation does.
    echo = _call("calculator", {"expression": "463"}, 463.0)
    [failed] = unverified_fields(OUTPUT, {"downtime_share_percent": 463}, [echo], LOG)
    assert failed.reason == "not_from_tool"
    computed = _call("calculator", {"expression": "4605 - 2422 - 1720"}, 463.0)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 463}, [computed], LOG) == []
    assert "written out from the figures" in correction_prompt("Task", [failed])


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


# ---- what a made-up figure cannot pass (audit round 3, RT3-01) ----------------------------------------------------


def _calc(expression: str) -> ToolCallRecord:
    from nodestro_runtime.builtin_tools import calculator

    return _call("calculator", {"expression": expression}, calculator.function(expression))


@pytest.mark.parametrize("expression", ["46_3", "0x1cf", "0o717", "4.63e2", "round(463.2)", "abs(-463)", "463.0"])
def test_a_calculator_that_only_repeats_or_rounds_one_number_backs_nothing(expression: str) -> None:
    [failed] = unverified_fields(OUTPUT, {"downtime_share_percent": 463}, [_calc(expression)], LOG)
    assert failed.reason == "not_from_tool"


def test_table_totals_backs_what_it_computed_not_what_it_repeats() -> None:
    # The reference total and the names come back in the result, but the tool did not compute them.
    echo = _call(
        "table_totals",
        {"table": TABLE, "value_column": "Duration", "reference_total": 7.5},
        table_totals.function(TABLE, "Duration", reference_total=7.5),
    )
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 7.5}, [echo], LOG)[0].reason == "not_from_tool"
    named = "| No. | 7.5 |\n|---|---|\n| 1 | 2 |"
    record = _call("table_totals", {"table": named, "value_column": "7.5"}, table_totals.function(named, "7.5"))
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 7.5}, [record], "")[0].reason == "not_from_tool"
    # Row counts and group shares are computed and still count.
    grouped = _call(
        "table_totals",
        {"table": TABLE, "value_column": "Duration", "group_by": "Machine"},
        table_totals.function(TABLE, "Duration", group_by="Machine"),
    )
    assert unverified_fields(OUTPUT, {"downtime_share_percent": round(365 / 665 * 100, 2)}, [grouped], LOG) == []
    assert unverified_fields(OUTPUT, {"downtime_share_percent": 12}, [grouped], LOG) == []


def _over(table: str) -> tuple[ToolCallRecord, float]:
    value = table_totals.function(table, "Duration", reference_total=14400)["share_of_reference_percent"]
    return _totals(table), round(value, 2)


def test_a_total_over_a_table_with_changed_or_added_rows_is_refused() -> None:
    changed = TABLE.replace("| 120 |", "| 420 |")
    record, value = _over(changed)
    [failed] = unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG)
    assert (failed.reason, failed.rows_passed, failed.rows_in_input) == ("table_changed", 12, 12)
    added = TABLE + "| 13 | Filler | 300 | F-03 |\n"
    record, value = _over(added)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG)[0].reason == "table_changed"
    assert "rows were added or changed" in correction_prompt("Task", [failed])


def test_a_table_with_renamed_or_fewer_columns_is_compared_by_its_values() -> None:
    lines = TABLE.splitlines()
    renamed = "\n".join(["| No. | Machine | Duration (mins) | Fault code |", *lines[1:-3]])  # and rows left out
    record, value = _over(renamed)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG)[0].reason == "table_incomplete"
    made_up = "| No. | Machine | Duration (mins) |\n|---|---|---|\n| 1 | Filler | 665 |"
    record, value = _over(made_up)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG)[0].reason == "table_changed"
    # Only the columns it needs, every row: the same values, so the total counts.
    two = "| Machine | Duration (min) |\n|---|---|\n" + "\n".join(
        f"| {row.split('|')[2].strip()} | {row.split('|')[3].strip()} |" for row in lines[2:]
    )
    record, value = _over(two)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG) == []


def test_the_same_table_written_differently_still_counts() -> None:
    csv = "No.;Machine;Duration (min);Fault code\n" + "\n".join(
        ";".join(cell.strip() for cell in row.strip("|").split("|")) for row in TABLE.splitlines()[2:]
    )
    record, value = _over(csv)
    assert unverified_fields(OUTPUT, {"downtime_share_percent": value}, [record], LOG) == []


async def test_a_run_refuses_a_figure_the_tool_only_repeated(tmp_path: Path) -> None:
    echo = {"table": TABLE, "value_column": "Duration", "reference_total": 2.83}
    fake = ScriptedClient(
        {"Analyst": [tool_use("table_totals", echo), _share(2.83), tool_use("table_totals", echo), _share(2.83)]}
    )
    with Workspace(tmp_path / "data", client_factory=lambda _p: fake, integrations=[]) as workspace:
        saved = workspace.save_workflow(_flow(tools=["table_totals"]))
        done = await workspace.wait((await workspace.run(saved, RunIn(input=LOG))).id, timeout=20)
        assert done.status == "failed"
        assert done.nodes["a"].error_code == "run.value_unverified"
        assert done.nodes["a"].error_params["reason"] == "not_from_tool"
