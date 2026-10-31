"""Output contracts of workflows (Adaptive Orchestration M3, teams).

A result node may declare a **contract**: the fields the workflow promises to deliver, in the same shape
as an agent's structured output (:class:`~nodestro_runtime.schemas.OutputField`). A workflow with a
contract can be used as a team: a parent workflow that runs it as a sub-workflow gets an object with
exactly these fields, so decisions after it can rely on them.

The contract is checked when the result node runs: the result must be an object (or JSON text of one)
with every required field in the declared type. Fields that are not in the contract are dropped. A
result that does not fit fails the result node with ``run.output_contract`` and so the run; a parent
never receives an unchecked result.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from .agent_builder import output_model
from .graph import data_sources
from .problems import NodestroError
from .schemas import Node, OutputConfig, OutputField, Workflow, WorkflowIn

__all__ = [
    "ContractError",
    "apply_contract",
    "contract_model",
    "contract_of",
    "contract_problems",
    "merged_input",
    "source_gaps",
]


class ContractError(ValueError):
    """A result that does not fit the contract; ``fields`` names the missing or invalid fields."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__(", ".join(fields))
        self.fields = fields


def contract_model(fields: list[OutputField]) -> type[BaseModel]:
    """The Pydantic model of a contract (raises :class:`NodestroError` for invalid field names)."""
    model = output_model(OutputConfig(mode="structured", name="Contract", fields=fields))
    assert model is not None
    return model


def contract_problems(fields: list[OutputField]) -> list[tuple[str, str, dict[str, Any]]]:
    """Configuration errors of a contract: invalid or repeated field names."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    names = [item.name for item in fields]
    for name in sorted({name for name in names if names.count(name) > 1}):
        found.append(
            ("workflow.contract_field_duplicate", f"The contract names the field {name} twice", {"field": name})
        )
    for item in fields:
        try:
            contract_model([item])
        except NodestroError as error:
            found.append(("workflow.contract_field_name", str(error), {"field": item.name}))
    return found


def contract_of(workflow: Workflow | WorkflowIn) -> list[OutputField]:
    """The contract a workflow promises: the one of its result node, empty without one.

    A workflow with several result nodes delivers an object per result node, so it has no contract.
    """
    outputs = [node for node in workflow.nodes if node.type == "output"]
    if len(outputs) != 1 or outputs[0].output is None:
        return []
    return list(outputs[0].output.contract)


def _parsed(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def apply_contract(fields: list[OutputField], value: Any) -> dict[str, Any]:
    """``value`` checked against the contract: exactly its fields, or :class:`ContractError`."""
    value = _parsed(value)
    if not isinstance(value, dict):
        raise ContractError([item.name for item in fields if item.required] or [item.name for item in fields])
    try:
        checked = contract_model(fields).model_validate(value)
    except ValidationError as error:
        names = sorted({str(issue["loc"][0]) for issue in error.errors() if issue.get("loc")})
        raise ContractError(names) from None
    return checked.model_dump(mode="json")


def merged_input(inputs: list[tuple[str, Any]]) -> Any:
    """What a result node with a contract checks: its single input, or the objects of several merged."""
    if len(inputs) == 1:
        return inputs[0][1]
    merged: dict[str, Any] = {}
    for _name, value in inputs:
        value = _parsed(value)
        if isinstance(value, dict):
            merged.update(value)
    return merged


def source_gaps(workflow: Workflow | WorkflowIn, node: Node) -> list[tuple[str, str, dict[str, Any]]]:
    """Warnings for a result node whose sources cannot be seen to fulfil its contract.

    An agent with plain text output may still answer with JSON, so this is a warning, not an error; the
    check at run time decides.
    """
    if node.output is None or not node.output.contract:
        return []
    nodes = {item.id: item for item in workflow.nodes}
    required = {item.name for item in node.output.contract if item.required}
    found: list[tuple[str, str, dict[str, Any]]] = []
    offered: set[str] = set()
    for source_id in data_sources(workflow, node.id):
        source = nodes.get(source_id)
        if source is None or source.type != "agent" or source.agent is None:
            return []  # a sub-workflow or decision passes something on that is not known here
        if source.agent.output.mode != "structured":
            found.append(
                (
                    "check.contract_text_source",
                    f"{source.label or source.id} answers in plain text; the contract can only be met by JSON",
                    {"name": source.label or source.id},
                )
            )
            return found
        offered |= {item.name for item in source.agent.output.fields}
    missing = sorted(required - offered)
    if missing:
        found.append(
            (
                "check.contract_field_missing",
                f"No agent before the result delivers {', '.join(missing)}",
                {"fields": ", ".join(missing)},
            )
        )
    return found
