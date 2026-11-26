"""The writer of folder grants (N-10): a person grants and revokes folders for reading.

:class:`FolderGrants` stores a :class:`~nodestro_runtime.folders.FolderGrant` together with its access grant of the
access model v2 (:class:`~nodestro_runtime.tool_approvals.AccessRules`, resource ``folder``, action ``read``), so
duration, expiry, scope, mode, revocation and the audit (``access_granted``, ``access_grant_revoked``) are N-06's.
Only a :class:`~nodestro_runtime.tool_approvals.Person` writes, never inside a tool call. No tool, engine or model
module imports this module (a test checks it).

A folder is refused (``folder.not_grantable``, param ``reason``) when :class:`orkexa.fs.SafeRoot` refuses it as a root
(a drive or file-system root, the home folder or a folder above it, any user's home folder, a system folder, a blocked
credential folder, Nodestro's data folder or a folder inside it, a network path; also through a link) or when a global
safety limit of the access model applies to it (``limit:protected_folder``, ``limit:unresolvable_path``). See
``docs/folders.md``.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from datetime import datetime
import os
import tempfile
from typing import Literal

from orkexa.fs import FsError, SafeRoot
from pydantic import BaseModel

from . import activity
from .folders import COLLECTION, FolderGrant, FolderLimits, folders_enabled
from .problems import NodestroError
from .schemas import new_id, now
from .storage import DocumentStore
from .tool_approvals import AccessRules, Person
from .tool_policy import AccessMode, AccessRequest, AccessResource, AccessScope, Grant, safety_limit

#: Why a folder cannot be granted (``folder.not_grantable``, param ``reason``).
NotGrantable = Literal[
    "protected_root",
    "network_path",
    "invalid_path",
    "not_found",
    "not_a_directory",
    "link",
    "safety_limit",
    "temp_root",
]
_FS_REASONS: dict[str, NotGrantable] = {
    "fs.protected_root": "protected_root",
    "fs.network_path": "network_path",
    "fs.not_found": "not_found",
    "fs.not_a_directory": "not_a_directory",
    "fs.link": "link",
}


class FolderGrantView(BaseModel):
    """A folder grant with the state of its access grant, as the administration shows it."""

    folder: FolderGrant
    #: ``active``, ``revoked`` or ``expired`` (from the access grant, at the time of reading).
    status: Literal["active", "revoked", "expired"]
    scope: AccessScope
    agents: tuple[str, ...]
    mode: AccessMode
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class FolderAccessEntry(BaseModel):
    """One entry of a grant's access log (an activity event, without paths)."""

    at: datetime
    action: str
    actor: str | None = None
    agent: str | None = None
    outcome: str
    code: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None


#: The shared temporary folders themselves: every program writes there, so they are never granted. A folder
#: inside one may be granted (reason ``temp_root``).
TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp", "/private/var/folders")


def temp_roots() -> set[str]:
    """The canonical spellings (``os.path.normcase``) of the temporary folders that cannot be granted: the
    :data:`TEMP_ROOTS` that exist here and this process's temporary folder."""
    found = {os.path.normcase(os.path.realpath(path)) for path in TEMP_ROOTS if os.name != "nt"}
    found.add(os.path.normcase(os.path.realpath(tempfile.gettempdir())))
    return found


#: The activity event of a new folder grant; its ``code`` is :data:`DEFAULT_EXCLUDES_OFF` when an administrator
#: turned the default excludes off.
GRANTED = "folder.granted"
DEFAULT_EXCLUDES_OFF = "default_excludes_off"
#: The actions of the folder tools: a grant's access log (:meth:`FolderGrants.accesses`) lists only these.
TOOL_ACTIONS = ("folder.listed", "folder.read", "folder.searched")


def _refused(reason: NotGrantable) -> NodestroError:
    return NodestroError("folder.not_grantable", "This folder cannot be granted", reason=reason)


class FolderGrants:
    """The folder grants of one tenant (collection ``folder_grants``). ``data_dir`` is Nodestro's data folder."""

    def __init__(self, store: DocumentStore, data_dir: str, *, clock: Callable[[], datetime] = now) -> None:
        self.store = store
        self.data_dir = data_dir
        self.clock = clock
        self.rules = AccessRules(store, clock=clock)

    def grant(
        self,
        *,
        name: str,
        path: str,
        by: Person,
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
        limits: FolderLimits | None = None,
        index: bool = False,
        default_excludes: bool = True,
        scope: AccessScope = "all",
        agents: Sequence[str] = (),
        mode: AccessMode = "supervised",
        expires_at: datetime | None = None,
    ) -> FolderGrantView:
        """Grant ``path`` (absolute) for reading under ``name``. ``expires_at`` makes it an ``until`` grant,
        otherwise it holds until revoked. ``default_excludes=False`` lets the agents read the common secret files
        (:data:`~nodestro_runtime.folders.DEFAULT_EXCLUDES`), which every grant leaves out unless an administrator
        turns this off explicitly."""
        if not folders_enabled():
            raise NodestroError("folder.disabled", "Folder grants are not switched on for this application")
        if not isinstance(by, Person):
            raise NodestroError("access.person_required", "Only a person can change grants and denies")
        shown = name.strip()
        if not shown or len(shown) > 80 or not shown.isprintable() or "/" in shown or "\\" in shown:
            raise NodestroError("folder.invalid_name", "A folder name has 1 to 80 printable characters, no slash")
        if not os.path.isabs(path):
            raise _refused("invalid_path")
        try:
            root = SafeRoot(path, include=include, exclude=exclude, protected=[self.data_dir])
        except FsError as exc:
            raise _refused(_FS_REASONS.get(exc.code, "invalid_path")) from None
        except ValueError:
            raise NodestroError("folder.invalid_pattern", "An include or exclude pattern is not valid") from None
        if os.path.normcase(root.path) in temp_roots():
            raise _refused("temp_root")
        for spelling in dict.fromkeys([path, root.path]):
            try:
                request = AccessRequest(
                    resource=AccessResource(type="folder", id=spelling),
                    action="read",
                    effect="read_internal",
                    protected_folders=(self.data_dir,),
                )
            except ValueError:
                raise _refused("invalid_path") from None
            if safety_limit(request) is not None:
                raise _refused("safety_limit")
        with self.store.transaction():  # the access grant and the folder grant are stored together or not at all
            # Checked inside the transaction (it holds the write lock), so two grants of one name cannot both pass.
            self.store.lock(COLLECTION, "name")
            if any(view.status == "active" and view.folder.name.casefold() == shown.casefold() for view in self.list()):
                raise NodestroError("folder.name_taken", "An active folder grant already has this name")
            access = self.rules.grant(
                resource=AccessResource(type="folder", id=root.path),
                actions=["read"],
                duration="until" if expires_at is not None else "until_revoked",
                by=by,
                scope=scope,
                agents=agents,
                mode=mode,
                expires_at=expires_at,
            )
            folder = FolderGrant(
                id=new_id("fgr"),
                name=shown,
                path=root.path,
                identity=root.identity,
                include=tuple(include),
                exclude=tuple(exclude),
                limits=limits or FolderLimits(),
                index=index,
                default_excludes=default_excludes,
                grant_id=access.id,
                created_by=by.id,
                created_at=access.created_at,
            )
            self.store.put(COLLECTION, folder.id, folder)
        # The grant in the activity log (N-13); turning the default excludes off is named (N-10 re-review).
        activity.add(
            self.store,
            "file",
            GRANTED,
            actor=by.id,
            resource_type="folder_grant",
            resource_id=folder.id,
            code=None if default_excludes else DEFAULT_EXCLUDES_OFF,
        )
        return self._view(folder, access)

    def revoke(self, folder_id: str, *, by: Person) -> FolderGrantView:
        """Revoke a folder grant (its access grant): every later call of the folder tools is refused, a running
        search stops at its next folder. Revoking twice changes nothing."""
        folder = self._folder(folder_id)
        return self._view(folder, self.rules.revoke_grant(folder.grant_id, by=by))

    def get(self, folder_id: str) -> FolderGrantView:
        folder = self._folder(folder_id)
        return self._view(folder, self._access(folder))

    def list(self) -> list[FolderGrantView]:
        """Every folder grant, newest first, ended ones included."""
        access = {grant.id: grant for grant in self.rules.grants()}
        found = [self._view(folder, access.get(folder.grant_id)) for folder in self.store.list(COLLECTION, FolderGrant)]
        return sorted(found, key=lambda view: (view.folder.created_at, view.folder.id), reverse=True)

    def accesses(self, folder_id: str, *, limit: int = 200) -> builtins.list[FolderAccessEntry]:
        """The access log of a grant, newest first: every call of the folder tools on it (activity log, N-13)."""
        folder = self._folder(folder_id)
        events = activity.query(
            self.store, actions=TOOL_ACTIONS, resource_type="folder_grant", resource_id=folder.id, limit=limit
        )
        return [
            FolderAccessEntry(
                at=event.at,
                action=event.action,
                actor=event.actor,
                agent=event.agent,
                outcome=event.outcome,
                code=event.code,
                run_id=event.run_id,
                conversation_id=event.conversation_id,
            )
            for event in events
        ]

    def _folder(self, folder_id: str) -> FolderGrant:
        found = self.store.get(COLLECTION, folder_id, FolderGrant)
        if found is None:
            raise NodestroError("folder.not_found", "There is no such folder grant", id=folder_id)
        return found

    def _access(self, folder: FolderGrant) -> Grant | None:
        return next((grant for grant in self.rules.grants() if grant.id == folder.grant_id), None)

    def _view(self, folder: FolderGrant, access: Grant | None) -> FolderGrantView:
        if access is None:  # never happens unless the store was edited by hand: the folder counts as revoked
            return FolderGrantView(folder=folder, status="revoked", scope="all", agents=(), mode="supervised")
        status: Literal["active", "revoked", "expired"] = "active"
        if access.status != "active" or (access.revoked_at is not None and access.revoked_at <= self.clock()):
            status = "revoked"
        elif not access.active_at(self.clock()):
            status = "expired"
        return FolderGrantView(
            folder=folder,
            status=status,
            scope=access.scope,
            agents=access.agents,
            mode=access.mode,
            expires_at=access.expires_at,
            revoked_at=access.revoked_at,
            revoked_by=access.revoked_by,
        )


__all__ = [
    "DEFAULT_EXCLUDES_OFF",
    "GRANTED",
    "TEMP_ROOTS",
    "TOOL_ACTIONS",
    "FolderAccessEntry",
    "FolderGrantView",
    "FolderGrants",
    "NotGrantable",
    "temp_roots",
]
