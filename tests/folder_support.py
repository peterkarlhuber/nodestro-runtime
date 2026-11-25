"""Test support for folder grants (N-10, N-63): a workspace with the switch on, a place to grant folders from, and the
adapter that runs Orkexa's folder grant cases through Nodestro's folder tools."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Literal

from orkexa import Tool
from orkexa.errors import ToolInputError
from orkexa.testing.conformance import DATA_FOLDER, Request

from nodestro_runtime.folder_grants import FolderGrants
from nodestro_runtime.folder_tools import FolderToolError, list_folder, read_file, search_files
from nodestro_runtime.folders import FOLDERS_ENV, FolderCaller, FolderLimits, FolderReader
from nodestro_runtime.schemas import now
from nodestro_runtime.tool_approvals import AccessRules, Person
from nodestro_runtime.tool_policy import AccessResource
from nodestro_runtime.tool_spec import BUILTIN_SPECS, CallCounter, RunServices, bind_tool
from nodestro_runtime.workspace import Workspace

PERSON = Person("local")
_OPERATIONS = {"folder.listed": "list", "folder.read": "read", "folder.searched": "search"}


def grant_base(tmp_path: Path) -> Iterator[Path]:
    """A folder to grant folders from. On macOS the temporary folder lies in /private/var, a system folder of the access
    model (``tool_policy.SYSTEM_FOLDERS``) that no grant opens, so the folders go below the home folder there."""
    if sys.platform != "darwin":
        yield tmp_path
        return
    base = Path(tempfile.mkdtemp(prefix="nodestro-folder-tests-", dir=Path.home()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def reader(workspace: Workspace, caller: FolderCaller | None = None, **kwargs: Any) -> FolderReader:
    return FolderReader(
        workspace.store,
        records=AccessRules(workspace.store).current,
        protected=str(workspace.data_dir.resolve()),
        caller=caller or FolderCaller(actor="local"),
        clock=now,
        **kwargs,
    )


def bound(tool: Tool, folders: FolderReader | None) -> Tool:
    """``tool`` as a run binds it (spec, call limit, time limit), with ``folders`` as the run's reader."""
    assert tool.name is not None
    spec = BUILTIN_SPECS[tool.name].model_copy(update={"max_calls_per_run": None})
    return bind_tool(tool, spec, None, resolve=None, counter=CallCounter(), services=RunServices(folders=folders))


class ToolFolders:
    """:class:`orkexa.testing.conformance.FolderAccess` through the folder tools of one workspace: grants are written
    by :class:`FolderGrants` as a person, reads go through the bound tools, the access log is the activity log."""

    def __init__(
        self, request: Request | None, *, of: ToolFolders | None = None, caller: FolderCaller | None = None
    ) -> None:
        if of is None:
            assert request is not None
            self.workspace = Workspace(request.workdir / DATA_FOLDER, integrations=[])
            self.grants = FolderGrants(self.workspace.store, str(self.workspace.data_dir.resolve()))
            self.names: dict[str, str] = {}
            self.owner = True
        else:  # another caller of the same workspace (as_caller)
            self.workspace, self.grants, self.names, self.owner = of.workspace, of.grants, of.names, False
        folders = reader(self.workspace, caller)
        self.tools = {tool.name: bound(tool, folders) for tool in (list_folder, read_file, search_files)}

    def close(self) -> None:
        if self.owner:
            self.workspace.close()

    def as_caller(self, agent: str, *, unattended: bool = False) -> ToolFolders:
        mode: Literal["supervised", "unattended"] = "unattended" if unattended else "supervised"
        return ToolFolders(None, of=self, caller=FolderCaller(agent=agent, mode=mode, actor="local"))

    def deny(self, path: str) -> None:
        AccessRules(self.workspace.store).deny(
            resource=AccessResource(type="folder", id=path), actions=["read"], by=PERSON
        )

    def grant(
        self,
        path: str,
        *,
        include: Any = (),
        exclude: Any = (),
        max_file_bytes: int | None = None,
        expires_at: datetime | None = None,
        agents: Any = (),
        unattended: bool = False,
    ) -> str:
        limits = FolderLimits() if max_file_bytes is None else FolderLimits(max_file_bytes=max_file_bytes)
        name = f"folder{len(self.names) + 1}"
        view = self.grants.grant(
            name=name,
            path=path,
            by=PERSON,
            include=tuple(include),
            exclude=tuple(exclude),
            limits=limits,
            expires_at=expires_at,
            scope="agents" if agents else "all",
            agents=tuple(agents),
            mode="unattended" if unattended else "supervised",
        )
        self.names[view.folder.id] = name
        return view.folder.id

    def revoke(self, grant: str) -> None:
        self.grants.revoke(grant, by=PERSON)

    def _call(self, tool: str, **arguments: Any) -> Any:
        return json.loads(self.tools[tool].execute(arguments))

    def list(self, grant: str, path: str = "") -> list[str]:
        found = self._call("list_folder", folder=self.names[grant], path=path)
        return [entry["path"] for entry in found["entries"]]

    def read(self, grant: str, path: str | bytes) -> str:
        shown = path if isinstance(path, str) else path.decode("utf-8", "surrogateescape")
        try:
            found = self._call("read_file", folder=self.names[grant], path=shown)
        except ToolInputError:
            # A path that is not valid text: the tool's input check refuses it before any file is touched (tool
            # input is JSON text, so a model cannot send it at all).
            if not any("\ud800" <= char <= "\udfff" for char in shown):
                raise
            raise FolderToolError("fs.invalid_path", "the path is not valid text") from None
        text = str(found["text"])
        # The text between the data markers of read_file.
        return text.split("\n", 1)[1].rsplit("\n", 1)[0]

    def search(self, grant: str, text: str) -> list[str]:
        found = self._call("search_files", folder=self.names[grant], query=text)
        return sorted({match["path"] for match in found["matches"]})

    def accesses(self, grant: str) -> list[tuple[str, str]]:
        entries = reversed(self.grants.accesses(grant, limit=10_000))
        return [(_OPERATIONS[entry.action], entry.outcome) for entry in entries]


def tool_folders(request: Request) -> ToolFolders:
    return ToolFolders(request)


__all__ = ["FOLDERS_ENV", "PERSON", "ToolFolders", "bound", "grant_base", "reader", "tool_folders"]
