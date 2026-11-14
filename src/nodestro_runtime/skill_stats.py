"""How skills were used (NAL-B4): offered, loaded and read per skill, with the outcome of the steps that loaded them.

Computed from stored runs only (``NodeRun.skills`` and ``NodeRun.skill_selection``), so it needs no extra records and
reflects exactly what the runs saw. The outcome of a step that loaded a skill is its status and, where the agent has
a quality check (``evaluation``), whether the check passed: an indication, not proof that the skill helped.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import Field

from .schemas import NodeRun, Run, _Model

__all__ = ["SkillStats", "skill_stats"]


class SkillStats(_Model):
    """The use of one skill (by name) in the runs looked at."""

    name: str
    #: Steps that offered it in their catalog (``always`` skills included).
    offered: int = 0
    #: Steps that loaded its instructions, and file reads.
    loaded: int = 0
    files_read: int = 0
    #: Distinct runs with a step that loaded it.
    runs: int = 0
    #: Of the steps that loaded it: succeeded, failed, and quality checks passed / done.
    succeeded: int = 0
    failed: int = 0
    checks_passed: int = 0
    checks: int = 0
    #: Steps that offered it as recommended by an orchestrator.
    recommended: int = 0
    versions: list[int] = Field(default_factory=list)
    last_used: datetime | None = None


def _steps(state: NodeRun) -> Iterable[NodeRun]:
    """A node's latest execution and its earlier ones (loops)."""
    yield state
    yield from state.earlier


def skill_stats(runs: Iterable[Run]) -> list[SkillStats]:
    """The use of every skill that any of ``runs`` offered or loaded, most loaded first, then by name."""
    found: dict[str, SkillStats] = {}
    run_sets: dict[str, set[str]] = {}

    def entry(name: str) -> SkillStats:
        return found.setdefault(name, SkillStats(name=name))

    for run in runs:
        for state in run.nodes.values():
            for step in _steps(state):
                selection = step.skill_selection
                if selection is not None:
                    for name in selection.offered:
                        entry(name).offered += 1
                    for name in selection.recommended:
                        entry(name).recommended += 1
                loaded: set[str] = set()
                for use in step.skills:
                    stats = entry(use.name)
                    if use.version not in stats.versions:
                        stats.versions.append(use.version)
                    if use.path is None:
                        stats.loaded += 1
                        loaded.add(use.name)
                    else:
                        stats.files_read += 1
                    when = step.finished_at or step.started_at or run.created_at
                    if stats.last_used is None or when > stats.last_used:
                        stats.last_used = when
                for name in loaded:
                    stats = entry(name)
                    run_sets.setdefault(name, set()).add(run.id)
                    if step.status == "succeeded":
                        stats.succeeded += 1
                    elif step.status == "failed":
                        stats.failed += 1
                    if step.evaluation is not None:
                        stats.checks += 1
                        stats.checks_passed += int(step.evaluation.passed)
    for name, ids in run_sets.items():
        found[name].runs = len(ids)
    for stats in found.values():
        stats.versions.sort()
    return sorted(found.values(), key=lambda item: (-item.loaded, item.name))
