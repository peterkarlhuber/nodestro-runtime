"""Tool drafts: Python tools the Workflow Architect wrote, waiting for the user's decision.

A draft is code plus tests that were only checked statically. Nothing of it has run, not
even in the sandbox, and it is not registered: agents cannot use it yet. The user reviews
the code and decides explicitly:

* ``test`` runs the tests in the sandbox (the same check as for hand-written tools);
* ``approve`` runs the same check and registers the tool only if everything passes;
* ``reject`` deletes the draft.

A workflow whose agents name a pending draft cannot run: the workflow check reports
``config.tool_unapproved`` until the tool is approved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .problems import NodestroError, Problem
from .python_envs import PythonEnvironments
from .python_tools import check_with_dependencies, save_with_dependencies
from .schemas import new_id, now
from .storage import Store
from .tool_library import CheckResult, ToolInfo, ToolLibrary

_COLLECTION = "tool_drafts"

ToolDraftStatus = Literal["draft", "approved"]


class ToolDraft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tdr"))
    #: The tool name agents use (the function name).
    name: str
    requirement: str
    description: str = ""
    code: str = ""
    tests: str = ""
    #: Findings of the static check (no code was executed for it).
    static_errors: list[Problem] = Field(default_factory=list)
    #: Third-party packages the code needs (from its imports and ``# /// script`` header). They are
    #: not approved or installed by writing the draft; testing it asks for approval first.
    dependencies: list[str] = Field(default_factory=list)
    status: ToolDraftStatus = "draft"
    #: The architect job that wrote it and the agents that need it.
    job_id: str | None = None
    agents: list[str] = Field(default_factory=list)
    #: The latest sandbox test the user started, if any.
    check: CheckResult | None = None
    created_at: datetime = Field(default_factory=now)
    decided_at: datetime | None = None


class ToolDrafts:
    """Stores drafts in the Nodestro database and turns approved ones into registered tools."""

    def __init__(self, store: Store, tools: ToolLibrary) -> None:
        self.store = store
        self.tools = tools
        #: Set by the application: tests with third-party libraries run in the tool's environment.
        self.python: PythonEnvironments | None = None

    def all(self) -> list[ToolDraft]:
        return sorted(self.store.list(_COLLECTION, ToolDraft), key=lambda draft: draft.created_at, reverse=True)

    def pending(self) -> list[ToolDraft]:
        return [draft for draft in self.all() if draft.status == "draft"]

    def pending_names(self) -> set[str]:
        return {draft.name for draft in self.pending()}

    def get(self, draft_id: str) -> ToolDraft:
        draft = self.store.get(_COLLECTION, draft_id, ToolDraft)
        if draft is None:
            raise KeyError(draft_id)
        return draft

    def add(self, draft: ToolDraft) -> ToolDraft:
        self.store.put(_COLLECTION, draft.id, draft)
        return draft

    def test(self, draft_id: str) -> ToolDraft:
        """Run the draft's tests in the sandbox; the user asked for it explicitly. Nothing is registered."""
        draft = self._pending(draft_id)
        if self.python is not None:
            draft.check = check_with_dependencies(self.tools, self.python, draft.code, draft.tests, draft.name)
        else:
            draft.check = self.tools.check(draft.code, draft.tests, draft.name)
        self.store.put(_COLLECTION, draft.id, draft)
        return draft

    def approve(self, draft_id: str) -> tuple[ToolDraft, ToolInfo | None]:
        """Check in the sandbox and register the tool if everything passes (the user's approval)."""
        draft = self._pending(draft_id)
        if draft.name in self.tools:
            raise NodestroError("tool_draft.name_taken", f"A tool named {draft.name!r} exists already", name=draft.name)
        if self.python is not None:
            result, info = save_with_dependencies(
                self.tools, self.python, draft.code, draft.tests, draft.name, requirement=draft.requirement
            )
        else:
            result, info = self.tools.save_custom(draft.code, draft.tests, draft.name, requirement=draft.requirement)
        draft.check = result
        if info is not None:
            draft.status = "approved"
            draft.decided_at = now()
        self.store.put(_COLLECTION, draft.id, draft)
        return draft, info

    def reject(self, draft_id: str) -> None:
        self._pending(draft_id)
        self.store.delete(_COLLECTION, draft_id)

    def discard_job(self, job_id: str) -> int:
        """Delete the pending drafts of an architect job whose result was discarded."""
        removed = 0
        for draft in self.pending():
            if draft.job_id == job_id:
                self.store.delete(_COLLECTION, draft.id)
                removed += 1
        return removed

    def _pending(self, draft_id: str) -> ToolDraft:
        draft = self.get(draft_id)
        if draft.status != "draft":
            raise NodestroError("tool_draft.decided", "This tool draft was already approved")
        return draft
