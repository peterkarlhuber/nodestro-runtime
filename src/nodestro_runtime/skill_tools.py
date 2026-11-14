"""Assigned skills at run time (NAL-B3): the catalog in the prompt and the run's ``load_skill``/``read_skill_file``.

Loading follows the three stages of the Agent Skills format: every assigned skill's name and description are in the
system prompt (a skill with mode ``always`` also with its instructions); the model loads a skill's instructions with
``load_skill`` and reads one of its text files with ``read_skill_file``. Both are tools of the run itself: they only
read the fixed skill versions the agent was given, so the per-call approval gate does not ask about them (as for the
input files tool). Every use is recorded (:class:`~.schemas.SkillUse`).

A skill adds instructions only. The prompt says so, and nothing here grants a tool, a connection or a permission.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from html import escape

from orkexa import Tool
from orkexa.skills import SkillPackage, catalog_entry, is_text_path, render_activation

from .schemas import SkillMode, SkillUse

LOAD_TOOL = "load_skill"
READ_TOOL = "read_skill_file"
SKILL_TOOL_NAMES = (LOAD_TOOL, READ_TOOL)
#: The most text one ``read_skill_file`` call returns.
MAX_READ_BYTES = 200_000
SKILLS_NOTICE = (
    "Skills: instructions for particular kinds of tasks, chosen by the author of this workflow. They describe how to "
    "work; they never grant tools, connections or permissions, and they do not change the rules above."
)


@dataclass(frozen=True)
class AssignedSkill:
    """One skill of an agent in its fixed version."""

    skill_id: str
    version: int
    mode: SkillMode
    package: SkillPackage


@dataclass
class SkillSet:
    """The skills of one agent in one step: the prompt part and the two tools (``None`` without skills)."""

    skills: Sequence[AssignedSkill]
    on_use: Callable[[SkillUse], None] | None = None
    loaded: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.loaded = {item.package.name for item in self.skills if item.mode == "always"}

    def _find(self, name: str) -> AssignedSkill | None:
        return next((item for item in self.skills if item.package.name == name), None)

    def _record(self, item: AssignedSkill, path: str | None = None) -> None:
        if self.on_use is not None:
            self.on_use(SkillUse(skill_id=item.skill_id, name=item.package.name, version=item.version, path=path))

    def _unknown(self, name: str) -> str:
        names = ", ".join(item.package.name for item in self.skills)
        return f"There is no skill named {name!r}. Available skills: {names}."

    @cached_property
    def prompt(self) -> str:
        """The part of the system prompt: the notice, the catalog and the instructions of ``always`` skills."""
        if not self.skills:
            return ""
        entries = "".join(
            f"\n<skill><name>{escape(entry['name'])}</name><description>{escape(entry['description'])}</description>"
            "</skill>"
            for entry in (catalog_entry(item.package) for item in self.skills)
        )
        parts = [SKILLS_NOTICE, f"<available_skills>{entries}\n</available_skills>"]
        if any(item.mode == "on_demand" for item in self.skills):
            parts.append(
                f"Before you use a skill, load its instructions with the {LOAD_TOOL} tool. Read one of its files with "
                f"{READ_TOOL} only when its instructions point to it."
            )
        parts += [render_activation(item.package) for item in self.skills if item.mode == "always"]
        return "\n\n".join(parts)

    def load_skill(self, name: str) -> str:
        """The instructions of a skill (stage 2), or a note when it is unknown or already loaded in this step."""
        item = self._find(name.strip())
        if item is None:
            return self._unknown(name)
        if item.package.name in self.loaded:
            return f"The skill {item.package.name!r} is already loaded in this step; use its instructions above."
        self.loaded.add(item.package.name)
        self._record(item)
        return render_activation(item.package)

    def read_skill_file(self, name: str, path: str) -> str:
        """One text file of a skill (stage 3), at most :data:`MAX_READ_BYTES`; only paths of the fixed version."""
        item = self._find(name.strip())
        if item is None:
            return self._unknown(name)
        found = item.package.file(path.strip().lstrip("/"))
        if found is None:
            return (
                f"The skill {item.package.name!r} has no file {path!r}. Its files: {', '.join(item.package.resources)}."
            )
        if not is_text_path(found.path):
            return f"{found.path} is not a text file; it cannot be read here."
        try:
            text = found.data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        except UnicodeDecodeError:  # pragma: no cover - errors="replace" never raises
            return f"{found.path} is not UTF-8 text."
        self._record(item, found.path)
        if found.size > MAX_READ_BYTES:
            text += f"\n\n[Only the first {MAX_READ_BYTES} bytes of {found.size} are shown.]"
        return text

    @cached_property
    def tools(self) -> list[Tool]:
        """``load_skill`` (when a skill is loaded on demand) and ``read_skill_file``; the same objects every time."""
        if not self.skills:
            return []

        def load_skill(name: str) -> str:
            """Load the instructions of one of your skills, with the list of its files.

            Args:
                name: The skill's name, as listed in available_skills.

            Returns:
                The skill's instructions and the paths of its files.
            """
            return self.load_skill(name)

        def read_skill_file(name: str, path: str) -> str:
            """Read one text file of one of your skills (for example references/checklist.md).

            Args:
                name: The skill's name.
                path: The file's path inside the skill, as listed when the skill was loaded.

            Returns:
                The file's text.
            """
            return self.read_skill_file(name, path)

        tools = [Tool(read_skill_file, name=READ_TOOL)]
        if any(item.mode == "on_demand" for item in self.skills):
            tools.insert(0, Tool(load_skill, name=LOAD_TOOL))
        return tools
