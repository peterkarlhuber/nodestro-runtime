"""What a tool may do: the capability statement of every tool (S-1, decision D10).

One shared model for built-in, own (``custom``), generated and MCP tools, for the provider's server tools and for
the tools of orchestrated agents (evolution plan S-1/S-2, adaptive orchestration AO-11, MCP WP-5; see the design in
nodestro-studio ``docs/evolution/s-tool-approvals.md``). There is no second permission model next to it.

A :class:`ToolCapability` says

- ``access``: ``read`` (changes nothing anywhere), ``write`` (creates or changes data) or ``destructive`` (deletes,
  pays, sends or cannot be undone);
- ``reach``: ``internal`` (stays inside Nodestro) or ``external`` (talks to systems outside it);
- ``source``: who made the statement: ``nodestro`` (fixed for built-in and provider tools), ``author`` (the person
  who saved the tool), ``admin`` (an administrator's classification, e.g. of an MCP tool), ``derived`` (computed
  from where the tool runs), ``folder`` (fixed by Nodestro for the tools that read a granted folder, N-10: what they
  return is content of a granted source) or ``default`` (nobody said anything).

A missing or unknown statement means ``write`` + ``external`` (:data:`UNKNOWN`): only Nodestro or a person can make
a tool look harmless, never its own code, its description or a model. The statement is security information, not
proof of harmless behavior: a ``read`` + ``external`` tool can still send data out through its arguments.

Asking before a call (S-2) builds on it: a tool *binding* of an agent says when to ask (:class:`ToolBinding`), and
:func:`decide` turns capability, binding and the run's situation into allow, deny or ask, without any I/O. Waiting
for the answer is :mod:`nodestro_runtime.tool_approvals`.

The access model v2 (N-06, behind :data:`~nodestro_runtime.tool_approvals.ACCESS_ENV`, default off) extends it with
stored :class:`Grant` and :class:`Deny` records about a resource (a tool, folder, workflow, MCP server, connection or
cloud provider), global safety limits that are code (:data:`SAFETY_LIMITS`), and :func:`evaluate`: safety limit, then
deny, then grant, otherwise a missing grant, with the kind of denial and its source. Besides tool calls, workflow
starts, MCP servers, connections and cloud models are checked (:func:`site_access`, :func:`related_access`), and
:func:`explain` and :func:`who_may` say who may do what and why (BER-1). See ``docs/access-grants.md``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
import itertools
import logging
import os
import posixpath
import re
from typing import Any, Literal
import unicodedata

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .sandbox_policy import Isolation

Access = Literal["read", "write", "destructive"]
Reach = Literal["internal", "external"]
CapabilitySource = Literal["nodestro", "author", "admin", "derived", "folder", "default"]


class ToolCapability(BaseModel):
    """What a tool may do (see the module documentation)."""

    model_config = ConfigDict(frozen=True)

    access: Access
    reach: Reach
    source: CapabilitySource

    @property
    def reads_only(self) -> bool:
        return self.access == "read"


#: Nobody made a statement: assume the worst that is not destructive.
UNKNOWN = ToolCapability(access="write", reach="external", source="default")

_READ_INTERNAL = ToolCapability(access="read", reach="internal", source="nodestro")
#: The folder tools (N-10): read-only, on this computer, and their source is a granted folder.
_FOLDER_READ = ToolCapability(access="read", reach="internal", source="folder")

#: The built-in tools of Nodestro itself (``builtin_tools.BUILTIN_TOOLS``). Tools that extensions add are not in
#: here: without their own statement they count as :data:`UNKNOWN`.
BUILTIN_CAPABILITIES: dict[str, ToolCapability] = {
    "calculator": _READ_INTERNAL,
    "current_datetime": _READ_INTERNAL,
    "days_between": _READ_INTERNAL,
    "text_statistics": _READ_INTERNAL,
    "table_totals": _READ_INTERNAL,
    # NAL-A2: they read from the internet (the query or the address leaves the computer), never write.
    "search_web": ToolCapability(access="read", reach="external", source="nodestro"),
    "fetch_web_page": ToolCapability(access="read", reach="external", source="nodestro"),
    # NAL-A3: leaves a message in Nodestro's own inbox; nothing leaves Nodestro.
    "notify_user": ToolCapability(access="write", reach="internal", source="nodestro"),
    # NAL-A4: drafts are files of the run; nothing leaves Nodestro (sending is NAL-A5).
    "draft_email": ToolCapability(access="write", reach="internal", source="nodestro"),
    "draft_webhook": ToolCapability(access="write", reach="internal", source="nodestro"),
    # N-10: they read granted folders on this computer, never write, never leave it; their output is folder content.
    "list_folder": _FOLDER_READ,
    "read_file": _FOLDER_READ,
    "search_files": _FOLDER_READ,
}

#: The provider's server tools (``catalog.SERVER_TOOLS``). They run at the provider and never pass Nodestro's tool
#: hook, so they cannot be held per call.
SERVER_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "web_search": ToolCapability(access="read", reach="external", source="nodestro"),
    "web_fetch": ToolCapability(access="read", reach="external", source="nodestro"),
    "code_execution": ToolCapability(access="write", reach="internal", source="nodestro"),
}

#: Sandboxes without any network (ADR-0006): a container, or the built-in Linux sandbox (seccomp, no sockets).
_NO_NETWORK: frozenset[Isolation] = frozenset({"container", "kernel"})
#: The isolations without network (code in them cannot reach outside this computer).
NO_NETWORK = _NO_NETWORK


def parse_capability(value: Any, *, source: CapabilitySource | None = None) -> ToolCapability | None:
    """A stored statement (a :class:`ToolCapability` or its JSON); ``None`` when it is missing or not valid.

    ``source`` replaces the stored source, so a statement can never claim to come from someone else (e.g. a tool
    file that says ``"source": "nodestro"``).
    """
    if value is None:
        return None
    if isinstance(value, dict) and source is not None:
        value = {**value, "source": source}
    try:
        found = value if isinstance(value, ToolCapability) else ToolCapability.model_validate(value)
    except (TypeError, ValueError):
        return None
    return found if source is None else found.model_copy(update={"source": source})


def capability_of(
    *,
    name: str,
    kind: str,
    stated: Any = None,
    stated_by: CapabilitySource = "author",
    isolation: Isolation | None = None,
) -> ToolCapability:
    """The capability of one tool.

    - ``builtin``: Nodestro's own statement; other trusted tools (extensions) count as unknown unless ``stated``.
    - ``mcp``: the administrator's classification in ``stated``; ``reach`` is always ``external``.
    - ``custom`` and ``generated``: the author's statement in ``stated``; without one :data:`UNKNOWN`.
    - Without network (``isolation`` is a container or the built-in sandbox), ``reach`` is ``internal`` and the
      statement is marked ``derived`` when it was not ``internal`` already. ``access`` is never derived.
    """
    if kind == "builtin" and name in BUILTIN_CAPABILITIES:
        return BUILTIN_CAPABILITIES[name]
    if kind == "mcp":
        found = parse_capability(stated, source="admin") or UNKNOWN
        return found.model_copy(update={"reach": "external"})
    found = parse_capability(stated, source=stated_by) or UNKNOWN
    if kind in ("custom", "generated") and isolation in _NO_NETWORK and found.reach == "external":
        return ToolCapability(access=found.access, reach="internal", source="derived")
    return found


def server_tool_capability(key: str) -> ToolCapability:
    """The capability of a provider server tool; unknown keys count as :data:`UNKNOWN`."""
    return SERVER_TOOL_CAPABILITIES.get(key, UNKNOWN)


# ---- S-2: when to ask before a call ---------------------------------------------------------------------------

AskMode = Literal["auto", "always", "never"]
GateAction = Literal["allow", "deny", "ask"]
#: The run triggers where a pre-grant can stand in for a person (the scopes of :class:`ToolPreGrant`).
PreGrantScope = Literal["schedule", "batch", "benchmark"]


class ToolBinding(BaseModel):
    """When an agent asks before calling one of its tools.

    - ``auto`` (every new binding): ask when the tool does more than read.
    - ``always``: ask for every call.
    - ``never``: do not ask. Counts only for ``read`` tools, for ``write`` tools with a ``reason`` and for legacy
      exemptions; never for ``destructive`` tools. Otherwise it is treated as ``auto``.

    A **legacy exemption** (``legacy``) is a binding that existed before S-2; it holds only for the tool version it
    was bound to (``bound_version``): a new version of the tool ends it.
    """

    model_config = ConfigDict(frozen=True)

    ask: AskMode = "auto"
    reason: str | None = Field(default=None, min_length=10, max_length=500)
    legacy: bool = False
    bound_version: int | None = None


#: A binding nobody configured: a new binding.
NEW_BINDING = ToolBinding()


class ToolPreGrant(BaseModel):
    """A person's advance approval of one tool for one unattended scope (D10c): narrow, traceable, revocable.

    Matches only the tool name, its version (own and generated tools) and the scope; voided by a new tool version,
    by its expiry and by revocation. Never covers ``destructive`` or MCP tools."""

    model_config = ConfigDict(frozen=True)

    id: str
    tool: str
    version: int | None = None
    scope: PreGrantScope
    scope_id: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


# ---- N-06: grants, denies and global safety limits (access model v2) --------------------------------------------

ResourceType = Literal["tool", "folder", "workflow", "mcp_server", "connection", "cloud_provider"]
#: What is done to a resource (spec 7.1): ``submit`` hands content to a cloud model.
AccessAction = Literal["read", "write", "move", "trash", "delete", "execute", "send", "submit"]
#: For which agents a record holds: one agent, a list of agents, or every agent.
AccessScope = Literal["agent", "agents", "all"]
#: ``once`` (one use), ``conversation`` (one conversation), ``until`` (an expiry date), ``until_revoked``.
GrantDuration = Literal["once", "conversation", "until", "until_revoked"]
DenyDuration = Literal["until", "until_revoked"]
#: ``supervised``: a person is in the chat; ``unattended``: background, schedule.
AccessMode = Literal["supervised", "unattended"]
#: The five effect classes of the spec (7.1), as combinations of the two axes ``access`` x ``reach`` (no second
#: schema): ``destructive`` is destructive whatever its reach.
EffectClass = Literal["read_internal", "write_internal", "read_external", "write_external", "destructive"]
#: The three kinds of a refusal (spec 7.6). Only a ``missing_grant`` can be lifted by a person's confirmation.
DenialKind = Literal["missing_grant", "denied", "safety_limit"]

#: Global safety limits: code, not data. No grant lifts them, and only the setting named here (if any) changes them.
SAFETY_LIMITS: dict[str, str] = {
    "offline": "Offline mode blocks every way out of the computer (the offline setting)",
    "destructive_unattended": "A destructive action never runs without a person watching",
    "destructive_per_call": "A destructive action needs a person's confirmation for each call; no grant covers it",
    "protected_folder": "System folders and Nodestro's own data folder are never open to agents",
    "unresolvable_path": "A folder that is not an absolute path is never open to agents",
}

#: System folders no grant opens (compared case-insensitively, component by component). The Windows folders named by
#: the environment (``SystemRoot``, ``ProgramFiles`` ...) are added at run time (:func:`system_folders`). Nodestro's
#: own data folder is protected as well; the caller names it (``AccessRequest.protected_folders``, required for folder
#: requests), since only it knows where it is.
SYSTEM_FOLDERS: tuple[str, ...] = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/db",
    "/var/lib",
    "/var/root",
    "/Library",
    "/System",
    "/private/etc",
    "/private/var",
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
)
#: Environment variables that name Windows system folders (on another drive than ``C:`` too).
_SYSTEM_FOLDER_ENV = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
#: Host names of the own computer in a UNC path (``\\localhost\C$`` is drive ``C:``).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "."})
#: A Windows path: a drive, or two leading separators of either kind (UNC, ``\\?\``, ``\\.\``; also ``//host/...``,
#: which Windows reads as UNC; on POSIX such a path is taken as UNC too, which fails closed).
_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})")
_DRIVE = re.compile(r"^[A-Za-z]:\\")
_ADMIN_SHARE = re.compile(r"^[a-z]\$$")
#: An 8.3 short name (``PROGRA~1``): another spelling of a long name that only the file system can resolve.
_SHORT_NAME = re.compile(r"~\d")


def system_folders() -> tuple[str, ...]:
    """:data:`SYSTEM_FOLDERS` and the Windows system folders the environment names."""
    found = [os.environ.get(name, "") for name in _SYSTEM_FOLDER_ENV]
    return (*SYSTEM_FOLDERS, *(path for path in found if path))


def _system_parts() -> list[tuple[str, ...]] | None:
    """The components of every system folder that names a folder on this system, or ``None`` when a system folder
    the environment names cannot be compared (an 8.3 short name such as ``C:\\PROGRA~1``, a device path, a path
    without a drive): then no folder can be told apart from it, so every folder is refused (fail closed) and a
    warning is logged once per value. On Windows a POSIX folder (``/bin``) names none (it would be relative to the
    current drive), so it is left out (logged at debug level) instead of making every folder unresolvable; on POSIX
    the Windows folders still compare (fail closed for UNC-like spellings)."""
    found: list[tuple[str, ...]] = []
    for path in SYSTEM_FOLDERS:
        try:
            found.append(folder_parts(path))
        except ValueError:
            _log.debug("System folder %s names no folder on this system; left out", path)
    for name in _SYSTEM_FOLDER_ENV:
        value = os.environ.get(name, "")
        if not value:
            continue
        try:
            found.append(folder_parts(value))
        except ValueError:
            if (name, value) not in _WARNED:
                _WARNED.add((name, value))
                _log.warning("The system folder in %s cannot be compared (%r); every folder is refused", name, value)
            return None
    return found


_log = logging.getLogger(__name__)
_WARNED: set[tuple[str, str]] = set()


_EFFECTS: dict[tuple[str, str], EffectClass] = {
    ("read", "internal"): "read_internal",
    ("write", "internal"): "write_internal",
    ("read", "external"): "read_external",
    ("write", "external"): "write_external",
}


def effect_class(access: Access, reach: Reach) -> EffectClass:
    """The effect class of the two axes (spec 7.1): read/write x internal/external, or destructive."""
    if access == "destructive":
        return "destructive"
    return _EFFECTS[(access, reach)]


def effect_of(capability: ToolCapability) -> EffectClass:
    return effect_class(capability.access, capability.reach)


def folder_parts(path: str) -> tuple[str, ...]:
    r"""The normalized components of an absolute folder path, for comparing folders component by component.

    Unicode NFC (a name typed on macOS and on Linux compares equal), ``.`` and ``..`` resolved lexically, repeated
    separators dropped. Raises :class:`ValueError` for a path that is not absolute or cannot be compared by name: one
    with leading or trailing white space, a NUL, a Windows device path (``\\.\``) or an 8.3 short name.

    Windows paths (``C:\...``, UNC, ``\\?\``) accept both separators, lose trailing dots and spaces per component (as
    Windows does) and are lower-cased throughout (denies and limits compare case-folded on top); ``\\?\C:\x`` is
    ``C:\x``, and an administrative share of the own computer (``\\localhost\C$``) is its drive. Two leading
    separators of either kind always mean a Windows path (``//host/share`` is UNC), and on Windows a path without a
    drive or share is refused. This is a comparison of names, not a guard on the file system: links, hard links and
    junctions are resolved by the caller before it asks (``os.path.realpath``), and the file-system guard itself is
    N-10."""
    if not path or path != path.strip():
        raise ValueError("A folder path must not start or end with white space")
    text = unicodedata.normalize("NFC", path)
    if "\x00" in text:
        raise ValueError("A folder path must not contain NUL")
    if _WINDOWS.match(text):
        return _windows_parts(text)
    if os.name == "nt":
        # On Windows ``\Windows`` or ``/Windows`` is relative to the current drive: it names no fixed folder.
        raise ValueError("On Windows a folder path names a drive or a UNC share")
    if not text.startswith("/"):
        raise ValueError("A folder path must be absolute")
    normal = posixpath.normpath(text)
    return ("/", *(part for part in normal.split("/") if part))


def _windows_parts(text: str) -> tuple[str, ...]:
    text = text.replace("/", "\\")
    if text.startswith("\\\\.\\"):
        raise ValueError("A Windows device path cannot be compared by name")
    if text.startswith("\\\\?\\"):
        rest = text[4:]
        if rest[:4].casefold() == "unc\\":
            text = "\\\\" + rest[4:]
        elif _DRIVE.match(rest):
            text = rest
        else:
            raise ValueError("A Windows device path cannot be compared by name")
    if text.startswith("\\\\"):
        host, _, rest = text[2:].partition("\\")
        share, _, rest = rest.partition("\\")
        host, share = host.lower(), share.rstrip(" .").lower()
        if not host or not share:
            raise ValueError("A UNC path names a host and a share")
        head = f"{share[0]}:" if _ADMIN_SHARE.match(share) and host in _LOCAL_HOSTS else f"//{host}/{share}"
    else:
        head, rest = text[:2].lower(), text[3:]
    parts: list[str] = []
    for part in rest.split("\\"):
        if part == "..":
            if parts:
                parts.pop()
            continue
        if part in ("", "."):
            continue
        name = part.rstrip(" .")
        if not name or ":" in name or _SHORT_NAME.search(name):
            raise ValueError("A Windows path component cannot be compared by name")
        # ``lower()``, not ``casefold()``: Windows compares names letter by letter, so ``straße`` and ``STRASSE`` are
        # two folders and a grant for one must not cover the other. Denies and limits still compare case-folded.
        parts.append(name.lower())
    return (head, *parts)


def _drive_form(parts: tuple[str, ...]) -> tuple[str, ...]:
    """An administrative share of any host (``//host/c$``) as its drive, for the protected-folder check only."""
    head = parts[0]
    if head.startswith("//"):
        share = head.rsplit("/", 1)[-1]
        if _ADMIN_SHARE.match(share):
            return (f"{share[0]}:", *parts[1:])
    return parts


def _within(inner: tuple[str, ...], outer: tuple[str, ...], *, fold: bool) -> bool:
    """Whether the folder ``inner`` is ``outer`` or lies below it (whole components, never a string prefix)."""
    if len(inner) < len(outer):
        return False
    if fold:
        return all(a.casefold() == b.casefold() for a, b in zip(inner, outer, strict=False))
    return inner[: len(outer)] == outer


class AccessResource(BaseModel):
    """What a record or a request is about: a type and its id (a tool name, an absolute folder path, a workflow id,
    an MCP server id, a connection id, a provider name)."""

    model_config = ConfigDict(frozen=True)

    type: ResourceType
    id: str = Field(min_length=1, max_length=4096)


#: The longest an unattended grant for more than reading may last (spec 7.2: narrow and dated, D10).
MAX_UNATTENDED_GRANT = timedelta(days=30)


class ToolFacts(BaseModel):
    """The tool a grant was given for: its version, classification and MCP definition hash. A tool grant matches only
    while the tool is still exactly this one, like a pre-grant (:func:`matching_pre_grant`): a new version, a changed
    classification or another MCP definition ends it."""

    model_config = ConfigDict(frozen=True)

    version: int | None = None
    access: Access
    reach: Reach
    definition_hash: str | None = None

    @classmethod
    def of(cls, capability: ToolCapability, *, version: int | None, definition_hash: str | None = None) -> ToolFacts:
        return cls(version=version, access=capability.access, reach=capability.reach, definition_hash=definition_hash)


class _AccessRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    resource: AccessResource
    actions: tuple[AccessAction, ...] = Field(min_length=1)
    scope: AccessScope = "all"
    #: The agents of ``agent`` (exactly one) or ``agents`` (one or more); empty for ``all``.
    agents: tuple[str, ...] = ()
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None

    @model_validator(mode="after")
    def _check_scope(self) -> _AccessRecord:
        wanted = {"agent": len(self.agents) == 1, "agents": len(self.agents) >= 1, "all": not self.agents}
        if not wanted[self.scope]:
            raise ValueError(f"scope {self.scope} does not fit {len(self.agents)} agents")
        return self

    def active_at(self, at: datetime) -> bool:
        """Not revoked and not expired at ``at``: checked at every evaluation, so a change applies at the next one."""
        if getattr(self, "status", "active") != "active":
            return False
        if self.revoked_at is not None and self.revoked_at <= at:
            return False
        return self.expires_at is None or self.expires_at > at

    def _addresses(self, request: AccessRequest, *, fold: bool, agent_unknown: bool) -> bool:
        if request.action not in self.actions or self.resource.type != request.resource.type:
            return False
        if self.scope != "all" and (request.agent not in self.agents if request.agent else not agent_unknown):
            return False
        if self.resource.type != "folder":
            return self.resource.id == request.resource.id
        try:
            return _within(folder_parts(request.resource.id), folder_parts(self.resource.id), fold=fold)
        except ValueError:
            return False


class Grant(_AccessRecord):
    """A person's permission (spec 7.1): resource, actions, scope, duration and mode. Written only by a person
    (through the rule store of :mod:`~nodestro_runtime.tool_approvals`), never by a model. Holds for a folder and
    every folder below it; a subfolder can be granted without its parent. A tool grant holds only for the tool as it
    was (``tool``)."""

    duration: GrantDuration
    #: ``supervised`` covers only calls a person watches; ``unattended`` covers background calls as well.
    mode: AccessMode = "supervised"
    #: The conversation of a ``conversation`` grant.
    conversation_id: str | None = None
    #: ``active``; ``used`` after the one use of a ``once`` grant; ``revoked``. Use and revocation are both one
    #: compare-and-set from ``active``, so exactly one of them wins.
    status: Literal["active", "used", "revoked"] = "active"
    #: The tool as it was granted (required for a tool grant to match).
    tool: ToolFacts | None = None
    granted_by: str


class Deny(_AccessRecord):
    """An explicit block (spec 7.1), addressed like a :class:`Grant`. Holds for a folder and every folder below it,
    also against a grant of a subfolder; folders compare case-insensitively here, so a deny never misses because of
    the spelling. A tool deny holds for every version of the tool. ``mode`` ``None`` blocks in both modes."""

    duration: DenyDuration = "until_revoked"
    mode: AccessMode | None = None
    status: Literal["active", "revoked"] = "active"
    denied_by: str


class AccessRequest(BaseModel):
    """One access to evaluate. A folder request names the protected data folder (``protected_folders``)."""

    model_config = ConfigDict(frozen=True)

    resource: AccessResource
    action: AccessAction
    effect: EffectClass
    agent: str | None = None
    mode: AccessMode = "supervised"
    conversation_id: str | None = None
    #: The conversation or task holds content from a granted source (folder, knowledge, memory; N-08).
    shared_content: bool = False
    offline: bool = False
    #: Folders no grant opens besides :func:`system_folders` (Nodestro's own data folder); required for folders.
    protected_folders: tuple[str, ...] = ()
    #: The tool as it is now (tool requests); a tool grant must name the same.
    tool: ToolFacts | None = None

    @model_validator(mode="after")
    def _check_folder(self) -> AccessRequest:
        if self.resource.type == "folder" and not self.protected_folders:
            raise ValueError("A folder request names the protected data folder (protected_folders)")
        return self


class AccessDecision(BaseModel):
    """The outcome of :func:`evaluate`: allowed or not, the kind of denial (spec 7.6) and its source:
    ``grant:<id>``, ``deny:<id>``, ``limit:<name>`` (a key of :data:`SAFETY_LIMITS`), ``default:<rule>`` or ``none``.
    The source is for the person; a model never gets it (N-07)."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    kind: DenialKind | None = None
    source: str = "none"

    @property
    def code(self) -> str | None:
        """The problem code of a refusal: ``access.missing_grant``, ``access.denied`` or ``access.safety_limit``."""
        return None if self.kind is None else _DENIAL_CODES[self.kind]

    @property
    def record_id(self) -> str | None:
        kind, _, found = self.source.partition(":")
        return found if kind in ("grant", "deny") else None


_DENIAL_CODES: dict[str, str] = {
    "missing_grant": "access.missing_grant",
    "denied": "access.denied",
    "safety_limit": "access.safety_limit",
}

#: Durations in the order a grant is preferred: a ``once`` grant is used only when nothing else covers the request.
_DURATION_ORDER = {"until_revoked": 0, "until": 1, "conversation": 2, "once": 3}


def safety_limit(request: AccessRequest) -> str | None:
    """The global safety limit that blocks ``request`` (a key of :data:`SAFETY_LIMITS`), or ``None``."""
    effect = "destructive" if request.action == "delete" else request.effect
    if request.offline and (effect in ("read_external", "write_external") or request.resource.type == "cloud_provider"):
        return "offline"
    if effect == "destructive" and request.mode == "unattended":
        return "destructive_unattended"
    if request.resource.type == "folder":
        try:
            parts = folder_parts(request.resource.id)
            protected = [folder_parts(path) for path in request.protected_folders]
        except ValueError:
            return "unresolvable_path"
        system = _system_parts()
        if system is None:
            return "unresolvable_path"
        protected += system
        forms = {parts, _drive_form(parts)}
        if any(_within(form, folder, fold=True) for form in forms for folder in protected):
            return "protected_folder"
    return None


def _grant_applies(grant: Grant, request: AccessRequest, effect: EffectClass) -> bool:
    if not grant._addresses(request, fold=False, agent_unknown=False):
        return False
    if request.resource.type == "tool" and (grant.tool is None or grant.tool != request.tool):
        return False  # another version, classification or MCP definition than the one granted
    if request.mode == "unattended":
        if grant.mode != "unattended":
            return False
        # Unattended writing, external calls and cloud models need a narrow, dated grant (spec 7.2, D10).
        if effect != "read_internal":
            if grant.scope == "all" or grant.duration not in ("until", "once"):
                return False
            if grant.expires_at is not None and grant.expires_at - grant.created_at > MAX_UNATTENDED_GRANT:
                return False
    if grant.duration == "conversation":
        return request.conversation_id is not None and grant.conversation_id == request.conversation_id
    return True


def _deny_applies(deny: Deny, request: AccessRequest) -> bool:
    if deny.mode is not None and deny.mode != request.mode:
        return False
    # A deny for some agents also holds when the caller cannot name the agent (fail closed).
    return deny._addresses(request, fold=True, agent_unknown=True)


def _default_rule(request: AccessRequest, effect: EffectClass) -> str | None:
    """Accesses the spec allows without a grant (7.2); every other access needs one."""
    if request.shared_content:
        return None
    if request.resource.type == "tool" and effect == "read_external":
        return "read_external_without_shared_content"
    if request.resource.type == "cloud_provider" and request.action == "submit":
        return "cloud_without_shared_content"
    return None


def evaluate(
    request: AccessRequest, *, grants: Sequence[Grant], denies: Sequence[Deny], at: datetime
) -> AccessDecision:
    """Evaluate one access (no I/O): a safety limit, then a deny, then a grant, otherwise a missing grant.

    Destructive actions are never covered by a grant (only by a person's confirmation of the one call). Revoked and
    expired records do not count, so a revocation applies at the next evaluation. ``at`` must carry a time zone."""
    if at.tzinfo is None:
        raise ValueError("evaluate() needs a time with a time zone")
    limit = safety_limit(request)
    if limit is not None:
        return AccessDecision(allowed=False, kind="safety_limit", source=f"limit:{limit}")
    for deny in sorted(denies, key=lambda item: (item.created_at, item.id)):
        if deny.active_at(at) and _deny_applies(deny, request):
            return AccessDecision(allowed=False, kind="denied", source=f"deny:{deny.id}")
    effect: EffectClass = "destructive" if request.action == "delete" else request.effect
    if effect == "destructive":
        return AccessDecision(allowed=False, kind="missing_grant", source="limit:destructive_per_call")
    for grant in sorted(grants, key=lambda item: (_DURATION_ORDER[item.duration], item.created_at, item.id)):
        if grant.active_at(at) and _grant_applies(grant, request, effect):
            return AccessDecision(allowed=True, source=f"grant:{grant.id}")
    rule = _default_rule(request, effect)
    if rule is not None:
        return AccessDecision(allowed=True, source=f"default:{rule}")
    return AccessDecision(allowed=False, kind="missing_grant")


# ---- N-06: the call sites beyond tool calls, and "who may do what and why" (BER-1) ---------------------------------

#: The action each resource is used with where the runtime checks it today (``docs/access-grants.md``, "Call sites").
#: Folders are checked by the file-system guard (N-10), which names its own actions.
SITE_ACTIONS: dict[str, AccessAction] = {
    "tool": "execute",
    "workflow": "execute",
    "mcp_server": "execute",
    "connection": "execute",
    "cloud_provider": "submit",
}
#: The effect class a request has when the call site has no tool to take it from (spec 7.1). A workflow start is
#: ``read_internal``: what the workflow then does is checked at each of its calls (spec 7.2 "by its strongest
#: effect"), so starting it trips no effect-based limit. A cloud model is reached like a web search (spec 7.2, 7.4).
_SITE_EFFECTS: dict[str, EffectClass] = {
    "workflow": "read_internal",
    "cloud_provider": "read_external",
}
_FOLDER_EFFECTS: dict[str, EffectClass] = {"read": "read_internal"}


def effect_for(resource_type: ResourceType, action: AccessAction, *, tool: ToolFacts | None = None) -> EffectClass:
    """The effect class of ``action`` on a resource of ``resource_type``, as the call sites evaluate it.

    ``delete`` is always destructive. A tool, MCP server or connection takes the effect of the tool it is used
    through (``tool``; without one :data:`UNKNOWN`, the worst that is not destructive; MCP servers and connections
    always reach outside). Folders read internally and otherwise write internally."""
    if action == "delete":
        return "destructive"
    if resource_type in _SITE_EFFECTS:
        return _SITE_EFFECTS[resource_type]
    if resource_type == "folder":
        return _FOLDER_EFFECTS.get(action, "write_internal")
    access: Access = tool.access if tool is not None else UNKNOWN.access
    reach: Reach = tool.reach if tool is not None else UNKNOWN.reach
    if resource_type in ("mcp_server", "connection"):
        reach = "external"
    return effect_class(access, reach)


def site_records(
    request: AccessRequest, grants: Sequence[Grant], denies: Sequence[Deny]
) -> tuple[list[Grant], list[Deny]]:
    """The records that count for ``request`` at its call site, so the enforcement and the explanation agree.

    - A tool call ignores ``once`` grants: one confirmed call is the per-call approval of S-2 itself.
    - A workflow start without an agent is a person's or a trigger's (a top-level run): only records for all agents
      count. A sub-workflow or team start names the calling agent, and then every record counts."""
    found_grants, found_denies = list(grants), list(denies)
    if request.resource.type == "tool":
        found_grants = [grant for grant in found_grants if grant.duration != "once"]
    if request.resource.type == "workflow" and request.agent is None:
        found_grants = [grant for grant in found_grants if grant.scope == "all"]
        found_denies = [deny for deny in found_denies if deny.scope == "all"]
    return found_grants, found_denies


#: What the access model v2 does at a call site:
#:
#: - ``refused``: a deny, a safety limit or (cloud models with shared content, folders) a missing grant refuses it;
#: - ``granted``: a grant allows it where a grant counts (a tool call that S-2 would ask about, except MCP tools; a
#:   cloud model with shared content; a folder);
#: - ``allowed``: nothing refuses it and nothing more is asked (a workflow start, a cloud model by default);
#: - ``per_call_rules``: the access model does not refuse it, and the per-call rules of S-2 decide (tools, MCP
#:   servers and connections without a grant that counts there).
SiteOutcome = Literal["refused", "granted", "allowed", "per_call_rules"]


def site_outcome(resource_type: ResourceType, found: AccessDecision, *, tool_kind: str | None = None) -> SiteOutcome:
    """What :func:`evaluate`'s ``found`` means at the call site of ``resource_type`` (the table of
    ``docs/access-grants.md``). The gate, the start and model checks and :func:`explain` all use it, so they agree.

    ``tool_kind`` is the kind of the tool (``mcp``: no grant replaces the question before an MCP call)."""
    if found.kind in ("denied", "safety_limit"):
        return "refused"
    granted = found.allowed and found.source.startswith("grant:")
    if resource_type == "tool":
        return "granted" if granted and tool_kind != "mcp" else "per_call_rules"
    if resource_type in ("mcp_server", "connection"):
        return "per_call_rules"  # only their denies and limits count; their grants are noted, not enforced
    if resource_type == "workflow":
        return "allowed"  # a missing grant never refuses a start; what it does is checked at each call
    if not found.allowed:
        return "refused"
    return "granted" if granted else "allowed"


class ToolNow(BaseModel):
    """A tool as it is now, for :func:`who_may`: its facts (``None``: it is gone), its kind, and the resources a call
    of it uses (``related``: its MCP server, and a connection if the caller knows the agent's), as the gate passes them
    to :func:`access_refusal`."""

    model_config = ConfigDict(frozen=True)

    facts: ToolFacts | None = None
    kind: str = "custom"
    related: tuple[AccessResource, ...] = ()


def _local_call(related: Sequence[AccessResource], local_servers: Collection[str]) -> bool:
    """Whether a call goes through a local (``stdio``) MCP server: strict offline does not refuse it."""
    return any(item.type == "mcp_server" and item.id in local_servers for item in related)


def _first_refusal(checked: Sequence[AccessDecision]) -> AccessDecision | None:
    """The refusal of a tool call among the evaluations of the tool and its related resources: a safety limit wins
    over a deny, and the tool's own (first) over a related one of the same kind."""
    for kind in ("safety_limit", "denied"):
        found = next((item for item in checked if item.kind == kind), None)
        if found is not None:
            return found
    return None


def _related_request(resource: AccessResource, tool: ToolFacts | None, request: AccessRequest) -> AccessRequest:
    """The request for the MCP server or connection a tool call (``request``) uses (:func:`related_access`)."""
    return AccessRequest(
        resource=resource,
        action=SITE_ACTIONS[resource.type],
        effect=effect_for(resource.type, "execute", tool=tool),
        agent=request.agent,
        mode=request.mode,
        conversation_id=request.conversation_id,
        offline=request.offline,
    )


def _evaluate_at_site(
    request: AccessRequest, grants: Sequence[Grant], denies: Sequence[Deny], at: datetime
) -> AccessDecision:
    found_grants, found_denies = site_records(request, grants, denies)
    return evaluate(request, grants=found_grants, denies=found_denies, at=at)


class AccessExplanation(BaseModel):
    """Why an access is allowed or refused at its call site (BER-1, spec 7.3 (8)): the request, what the access model
    does there (``outcome``) and what decided it.

    For the person and the administration only; a model never gets it (N-07)."""

    model_config = ConfigDict(frozen=True)

    resource: AccessResource
    action: AccessAction
    effect: EffectClass
    agent: str | None = None
    mode: AccessMode = "supervised"
    #: ``False`` exactly when the call site refuses the access (``outcome`` ``refused``).
    allowed: bool
    outcome: SiteOutcome
    #: The kind of refusal (spec 7.6) and its problem code; ``None`` unless refused.
    kind: DenialKind | None = None
    code: str | None = None
    #: What :func:`evaluate` found: ``grant:<id>``, ``deny:<id>``, ``limit:<name>``, ``default:<rule>`` or ``none``.
    source: str = "none"
    #: The deciding record, if a record decided.
    grant: Grant | None = None
    deny: Deny | None = None
    #: The grant decided but does not count at this call site (MCP servers, connections, MCP tools, workflows).
    grant_enforced: bool = False
    #: The deciding safety limit (a key of :data:`SAFETY_LIMITS`) and its text.
    limit: str | None = None
    limit_text: str | None = None
    #: The default rule of spec 7.2 that :func:`evaluate` applied.
    default_rule: str | None = None
    #: Other active records that address the request but did not decide it (a grant behind a deny, a second grant).
    also: tuple[str, ...] = ()
    #: Active tool grants for this tool that no longer match it (another version, classification or MCP definition).
    stale: tuple[str, ...] = ()


def explain(
    request: AccessRequest,
    *,
    grants: Sequence[Grant],
    denies: Sequence[Deny],
    at: datetime,
    tool_kind: str | None = None,
    related: Sequence[AccessResource] = (),
    local_servers: Collection[str] = (),
) -> AccessExplanation:
    """:func:`evaluate` with its reasons, as the request's call site applies it (no I/O): the records count as there
    (:func:`site_records`), and ``outcome`` says what happens there (:func:`site_outcome`).

    A tool request names the tool as it is now (``request.tool``), its kind (``tool_kind``) and the MCP server and
    connection a call uses (``related``): a deny or a safety limit on one of them refuses the call, as in
    :func:`access_refusal`. ``local_servers`` are the local (``stdio``) MCP servers: strict offline does not refuse a
    call through one, nor the server itself."""
    all_denies = denies
    local = _local_call(related, local_servers) or (
        request.resource.type == "mcp_server" and request.resource.id in local_servers
    )
    if local and request.offline:
        request = request.model_copy(update={"offline": False})
    grants, denies = site_records(request, grants, denies)
    found = evaluate(request, grants=grants, denies=denies, at=at)
    if request.resource.type == "tool" and related:
        checked = [found]
        checked += [
            _evaluate_at_site(_related_request(item, request.tool, request), grants, all_denies, at) for item in related
        ]
        refusal = _first_refusal(checked)
        if refusal is not None:
            found = refusal
            denies = list(all_denies)
    outcome = site_outcome(request.resource.type, found, tool_kind=tool_kind)
    refused = outcome == "refused"
    kind, _, name = found.source.partition(":")
    grant = next((item for item in grants if kind == "grant" and item.id == name), None)
    deny = next((item for item in denies if kind == "deny" and item.id == name), None)
    effect: EffectClass = "destructive" if request.action == "delete" else request.effect
    also = [
        f"deny:{item.id}" for item in denies if item.id != name and item.active_at(at) and _deny_applies(item, request)
    ]
    also += [
        f"grant:{item.id}"
        for item in grants
        if item.id != name and item.active_at(at) and _grant_applies(item, request, effect)
    ]
    stale = [
        item.id
        for item in grants
        if request.resource.type == "tool"
        and item.active_at(at)
        and item.tool != request.tool
        and item._addresses(request, fold=False, agent_unknown=False)
    ]
    return AccessExplanation(
        resource=request.resource,
        action=request.action,
        effect=request.effect,
        agent=request.agent,
        mode=request.mode,
        allowed=not refused,
        outcome=outcome,
        kind=(found.kind or "missing_grant") if refused else None,
        code=(found.code or "access.missing_grant") if refused else None,
        source=found.source,
        grant=grant,
        deny=deny,
        grant_enforced=grant is not None and outcome == "granted",
        limit=name if kind == "limit" else None,
        limit_text=SAFETY_LIMITS.get(name) if kind == "limit" else None,
        default_rule=name if kind == "default" else None,
        also=tuple(also),
        stale=tuple(stale),
    )


def who_may(
    grants: Sequence[Grant],
    denies: Sequence[Deny],
    *,
    at: datetime,
    resource: AccessResource | None = None,
    agent: str | None = None,
    mode: AccessMode | None = None,
    offline: bool = False,
    protected_folders: Sequence[str] = (),
    tools: Mapping[str, ToolNow] | None = None,
    local_servers: Collection[str] = (),
) -> list[AccessExplanation]:
    """ "Who may do what and why" (BER-1): for every resource, action, agent and mode an active record names, what
    the call site does now and why (:func:`explain`). ``resource``, ``agent`` and ``mode`` narrow the answer.

    - A record for all agents is explained without an agent (as for an agent no other record names).
    - A tool is explained as it is now (``tools``: name -> :class:`ToolNow`; a grant for another version shows as
      ``stale``). Without ``tools`` it is explained as it was granted (the grant's ``tool``), with only a deny as an
      unknown tool. A tool's MCP server and connection (``ToolNow.related``) refuse it like at the gate.
    - ``local_servers`` are the local (``stdio``) MCP servers: strict offline refuses neither them nor their tools.
    - A folder request names ``protected_folders`` (Nodestro's own data folder); without one, folders are left out."""
    cases: dict[
        tuple[str, str, str, str | None, str], tuple[AccessRequest, str | None, tuple[AccessResource, ...]]
    ] = {}
    active: list[Grant | Deny] = [*(item for item in grants if item.active_at(at))]
    active += [item for item in denies if item.active_at(at)]
    for record in active:
        if resource is not None and record.resource != resource:
            continue
        if record.resource.type == "folder" and not protected_folders:
            continue
        agents: tuple[str | None, ...] = record.agents if record.scope != "all" else (None,)
        if agent is not None:
            agents = (agent,) if record.scope == "all" or agent in record.agents else ()
        record_mode = record.mode
        modes: tuple[AccessMode, ...] = (record_mode,) if record_mode is not None else ("supervised", "unattended")
        if mode is not None:
            modes = (mode,) if mode in modes else ()
        tool: ToolFacts | None = record.tool if isinstance(record, Grant) else None
        tool_kind: str | None = None
        related: tuple[AccessResource, ...] = ()
        if record.resource.type == "tool" and tools is not None:
            now_ = tools.get(record.resource.id)
            tool, tool_kind, related = (now_.facts, now_.kind, now_.related) if now_ is not None else (None, None, ())
        for action, who, how in itertools.product(record.actions, agents, modes):
            key = (record.resource.type, record.resource.id, action, who, how)
            if key in cases:
                continue
            request = AccessRequest(
                resource=record.resource,
                action=action,
                effect=effect_for(record.resource.type, action, tool=tool),
                agent=who,
                mode=how,
                conversation_id=record.conversation_id if isinstance(record, Grant) else None,
                offline=offline,
                protected_folders=tuple(protected_folders) if record.resource.type == "folder" else (),
                tool=tool if record.resource.type == "tool" else None,
            )
            cases[key] = (request, tool_kind, related)
    return [
        explain(
            request,
            grants=grants,
            denies=denies,
            at=at,
            tool_kind=tool_kind,
            related=related,
            local_servers=local_servers,
        )
        for request, tool_kind, related in cases.values()
    ]


def site_access(
    resource: AccessResource,
    access: AccessContext,
    *,
    attended: bool,
    at: datetime,
    agent: str | None = None,
    shared_content: bool = False,
) -> AccessExplanation:
    """:func:`explain` for a workflow start or a cloud model request (actions :data:`SITE_ACTIONS`). ``agent``
    overrides the context's agent (``None`` for a top-level start); ``shared_content`` says the conversation or run
    holds content from a granted source (N-08 provenance; also ``access.shared_content``): a cloud model then needs
    a grant. It refuses exactly when ``allowed`` is ``False``."""
    action = SITE_ACTIONS[resource.type]
    request = AccessRequest(
        resource=resource,
        action=action,
        effect=effect_for(resource.type, action),
        agent=agent,
        mode="supervised" if attended else "unattended",
        conversation_id=access.conversation_id,
        shared_content=shared_content or access.shared_content,
        offline=access.offline,
    )
    return explain(request, grants=access.grants, denies=access.denies, at=at)


class AccessContext(BaseModel):
    """What the access model v2 knows about a tool call (set only while its switch is on)."""

    model_config = ConfigDict(frozen=True)

    grants: tuple[Grant, ...] = ()
    denies: tuple[Deny, ...] = ()
    agent: str | None = None
    conversation_id: str | None = None
    offline: bool = False
    #: Nodestro's own data folder (for folder requests; tool calls do not use it).
    protected_folders: tuple[str, ...] = ()
    #: Local (``stdio``) MCP servers: strict offline does not refuse their tools (they run on this computer).
    local_servers: frozenset[str] = frozenset()
    #: N-08: the conversation or run holds content of a granted source (only while the provenance switch is on):
    #: every call that reaches outside then needs a grant (:func:`provenance_refusal`).
    shared_content: bool = False


def agent_ref(workflow_id: str, node_id: str) -> str:
    """How grants and denies name an agent node: ``<workflow id>/<node id>`` (until agents have their own ids)."""
    return f"{workflow_id}/{node_id}"


def tool_access(
    tool: str,
    capability: ToolCapability,
    access: AccessContext,
    *,
    attended: bool,
    at: datetime,
    version: int | None = None,
    definition_hash: str | None = None,
) -> AccessDecision:
    """:func:`evaluate` for a tool call: resource ``tool:<name>``, action ``execute``, the tool's effect class, and the
    tool as it is now (a grant for another version or classification does not match).

    ``once`` grants do not count here: one confirmed tool call is the per-call approval of S-2 itself."""
    request = AccessRequest(
        resource=AccessResource(type="tool", id=tool),
        action="execute",
        effect=effect_of(capability),
        agent=access.agent,
        mode="supervised" if attended else "unattended",
        conversation_id=access.conversation_id,
        shared_content=access.shared_content,
        offline=access.offline,
        tool=ToolFacts.of(capability, version=version, definition_hash=definition_hash),
    )
    grants, denies = site_records(request, access.grants, access.denies)
    return evaluate(request, grants=grants, denies=denies, at=at)


def related_access(
    resource: AccessResource,
    capability: ToolCapability,
    access: AccessContext,
    *,
    attended: bool,
    at: datetime,
) -> AccessDecision:
    """:func:`evaluate` for the MCP server or the connection a tool call uses (action ``execute``, the tool's effect
    class; MCP servers and connections always reach outside). Only its refusals count (:func:`access_refusal`): a
    grant on them never allows a call, so the MCP rules and the per-call questions stay as they are."""
    call = AccessRequest(
        resource=AccessResource(type="tool", id="-"),
        action="execute",
        effect=effect_of(capability),
        agent=access.agent,
        mode="supervised" if attended else "unattended",
        conversation_id=access.conversation_id,
        offline=access.offline,
    )
    request = _related_request(resource, ToolFacts.of(capability, version=None), call)
    return _evaluate_at_site(request, access.grants, access.denies, at)


def access_refusal(
    tool: str,
    capability: ToolCapability,
    context: CallContext,
    *,
    at: datetime,
    version: int | None = None,
    definition_hash: str | None = None,
    related: Sequence[AccessResource] = (),
) -> tuple[GateDecision | None, AccessDecision | None]:
    """The refusal by a deny or a safety limit, if any, and the evaluation of the tool (both ``None`` while the
    switch is off).

    ``related`` names the MCP server and the connection the call uses (:func:`related_access`): a deny or a safety
    limit on one of them refuses the call as well; a grant on them changes nothing. A safety limit wins over a deny,
    and the tool's own refusal over a related one of the same kind.

    Used before a call is decided and again after a person approved a waiting call (spec 7.3 (7)): a deny or a limit
    that appeared while the call waited still refuses it."""
    if context.access is None:
        return None, None
    access = context.access
    if _local_call(related, access.local_servers):
        # A local (stdio) MCP server runs on this computer: strict offline does not refuse its tools.
        access = access.model_copy(update={"offline": False})
    found = tool_access(
        tool,
        capability,
        access,
        attended=context.attended,
        at=at,
        version=version,
        definition_hash=definition_hash,
    )
    checked = [found]
    checked += [related_access(resource, capability, access, attended=context.attended, at=at) for resource in related]
    refused = _first_refusal(checked)
    if refused is not None:
        reason = _ACCESS_REASONS[refused.kind or "denied"].format(tool=tool)
        return GateDecision(action="deny", code=refused.code, reason=reason, access_source=refused.source), found
    return None, found


class CallContext(BaseModel):
    """Where a call happens."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    node_id: str
    #: The trigger of the root run (a sub-workflow inherits its parent's).
    trigger: str
    #: A person can answer a question while the run waits.
    attended: bool
    #: The tools an orchestrator granted (AO-11); ``None``: no grant limits the agent.
    grants: frozenset[str] | None = None
    #: Who set ``grants`` (node id or orchestrator step), recorded with a ``tool.not_granted`` denial.
    grant_scope: str | None = None
    #: The unattended scope of the run (e.g. ``("schedule", "sch_1")``) and the pre-grants stored for it.
    scope: tuple[PreGrantScope, str] | None = None
    pre_grants: tuple[ToolPreGrant, ...] = ()
    #: ADR-0007 D-7: approved read-only MCP tools may run unattended (only after the MCP security acceptance).
    mcp_unattended: bool = False
    #: N-06: the stored grants and denies (only while the access model v2 is switched on; ``None``: as before).
    access: AccessContext | None = None


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: GateAction
    code: str | None = None
    reason: str | None = None
    #: The pre-grant that allowed an unattended call.
    pre_grant_id: str | None = None
    #: N-06: the grant that allowed the call, or the deny or safety limit that refused it (``grant:<id>``, ...).
    access_source: str | None = None


ALLOW = GateDecision(action="allow")
ASK = GateDecision(action="ask")


def _deny(code: str, reason: str) -> GateDecision:
    return GateDecision(action="deny", code=code, reason=reason)


def wants_question(capability: ToolCapability, binding: ToolBinding | None, *, version: int | None = None) -> bool:
    """Whether a person would be asked before this call (the binding's rule; see :class:`ToolBinding`)."""
    binding = binding or NEW_BINDING
    if binding.legacy and (binding.ask != "never" or binding.bound_version is None or binding.bound_version != version):
        binding = NEW_BINDING  # an exemption never outlives the tool version it was bound to
    if binding.ask == "always":
        return True
    if binding.ask == "never":
        if capability.access == "destructive":
            return True
        # A write tool needs a recorded reason (or a legacy exemption) to go unasked.
        return capability.access == "write" and not binding.legacy and binding.reason is None
    return capability.access != "read"


def matching_pre_grant(
    pre_grants: Iterable[ToolPreGrant],
    *,
    tool: str,
    version: int | None,
    scope: tuple[PreGrantScope, str] | None,
    at: datetime,
) -> ToolPreGrant | None:
    if scope is None:
        return None
    for grant in pre_grants:
        if grant.tool != tool or (grant.scope, grant.scope_id) != scope:
            continue
        if grant.version is not None and grant.version != version:
            continue
        if version is not None and grant.version is None:
            continue  # a grant for "any version" would survive a changed tool
        if grant.revoked_at is not None and grant.revoked_at <= at:
            continue
        if grant.expires_at is not None and grant.expires_at <= at:
            continue
        return grant
    return None


def decide(
    *,
    tool: str,
    kind: str,
    capability: ToolCapability,
    binding: ToolBinding | None,
    context: CallContext,
    at: datetime,
    version: int | None = None,
    definition_hash: str | None = None,
    related: Sequence[AccessResource] = (),
) -> GateDecision:
    """Allow, deny or ask for one call (no I/O). The order: grants narrow first, then the binding decides whether to
    ask; an unattended run cannot ask, so it denies unless a matching pre-grant exists. An approval never changes the
    sandbox the call runs in.

    With the access model v2 (``context.access``, N-06) a safety limit or a deny refuses the call before anything
    else, and a matching grant allows a call that would otherwise be asked or refused for lack of a person. A grant
    never lifts an orchestrator's narrowing, an ``always`` binding, the MCP rules or a destructive tool's question, and
    holds only for the tool version, classification and MCP definition (``definition_hash``) it was given for. A deny
    or a safety limit on the MCP server or the connection the call uses (``related``) refuses it too; a grant on them
    never allows it."""
    refused, found = access_refusal(
        tool, capability, context, at=at, version=version, definition_hash=definition_hash, related=related
    )
    if refused is not None:
        return refused
    verdict = _decide_binding(
        tool=tool, kind=kind, capability=capability, binding=binding, context=context, at=at, version=version
    )
    if (
        found is not None
        and site_outcome("tool", found, tool_kind=kind) == "granted"
        and verdict.action != "allow"
        and verdict.code != "tool.not_granted"
        and (binding is None or binding.ask != "always")
    ):
        return GateDecision(action="allow", access_source=found.source)
    held = provenance_refusal(tool, capability, context, found, at=at, related=related)
    if held is not None and verdict.action != "deny":
        return held
    if (
        verdict.action == "allow"
        and found is not None
        and found.source.startswith("grant:")
        and context.access is not None
        and context.access.shared_content
        and capability.reach == "external"
    ):
        # N-08: the grant is what lets this call reach outside with granted content; the person sees which one.
        return verdict.model_copy(update={"access_source": found.source})
    return verdict


def egress_granted(
    capability: ToolCapability,
    context: CallContext,
    found: AccessDecision | None,
    *,
    at: datetime,
    related: Sequence[AccessResource] = (),
) -> bool:
    """N-08: whether a call may reach outside although its conversation or run holds content of a granted source.

    ``True`` when nothing holds it: the provenance switch is off (``context.access`` is ``None`` or has no
    ``shared_content``), the call stays inside (``reach`` ``internal``), or a grant covers it: one on the tool
    itself (``found``, as :func:`tool_access` evaluated it with the shared content), or one on the service the call
    uses (``related``: its MCP server or its connection). A grant on the service only satisfies this rule; it never
    lifts a question of S-2 or the MCP rules (:func:`decide`)."""
    access = context.access
    if access is None or not access.shared_content or capability.reach != "external":
        return True
    if found is not None and found.allowed and found.source.startswith("grant:"):
        return True
    for resource in related:
        service = related_access(resource, capability, access, attended=context.attended, at=at)
        if service.allowed and service.source.startswith("grant:"):
            return True
    return False


def refusal_of(
    resource: AccessResource, found: AccessDecision, *, action: AccessAction = "execute", effect: EffectClass
) -> AccessExplanation:
    """The explanation of a refusal where no call site of :func:`explain` fits (N-08: a server tool of a request that
    holds content of a granted source). ``found`` is what :func:`evaluate` said; a decision that allowed is refused
    as a missing grant (it needed a grant and found none)."""
    kind: DenialKind = found.kind if found.kind is not None else "missing_grant"
    name = found.source.partition(":")[2] if found.source.startswith("limit:") else None
    return AccessExplanation(
        resource=resource,
        action=action,
        effect=effect,
        allowed=False,
        outcome="refused",
        kind=kind,
        code=_DENIAL_CODES[kind],
        source=found.source if found.kind is not None else "none",
        limit=name,
        limit_text=SAFETY_LIMITS.get(name) if name else None,
    )


def provenance_refusal(
    tool: str,
    capability: ToolCapability,
    context: CallContext,
    found: AccessDecision | None,
    *,
    at: datetime,
    related: Sequence[AccessResource] = (),
) -> GateDecision | None:
    """N-08: what happens to a call that reaches outside from a conversation or run with content of a granted source
    and that no grant covers (:func:`egress_granted`); ``None`` when it may go on as S-2 decides.

    - A person watches: the call is asked (the existing approval card shows its arguments, e.g. a web search's
      query, before anything is sent). A confirmation lets this one call run, like a ``once`` grant.
    - Nobody watches: it is refused with ``access.missing_grant`` (no question can be answered)."""
    if egress_granted(capability, context, found, at=at, related=related):
        return None
    if context.attended:
        return ASK
    reason = _ACCESS_REASONS["missing_grant"].format(tool=tool)
    return GateDecision(
        action="deny", code="access.missing_grant", reason=reason, access_source=found.source if found else None
    )


#: What a refused call tells the agent: no path, no record id, no limit name (N-07; the person sees the source).
_ACCESS_REASONS: dict[str, str] = {
    "denied": "The tool {tool} is blocked for this agent",
    "safety_limit": "The tool {tool} is not allowed here by a fixed safety rule",
    "missing_grant": "The tool {tool} would send content from a granted source outside, and no permission covers it",
}


def _decide_binding(
    *,
    tool: str,
    kind: str,
    capability: ToolCapability,
    binding: ToolBinding | None,
    context: CallContext,
    at: datetime,
    version: int | None,
) -> GateDecision:
    """The rules of S-2 (unchanged by N-06)."""
    if context.grants is not None and tool not in context.grants:
        scope = context.grant_scope or "orchestrator"
        return _deny("tool.not_granted", f"The tool {tool} was not granted to this agent (by {scope})")
    if kind == "mcp" and not context.attended and not (context.mcp_unattended and capability.reads_only):
        return _deny("tool.approval_unattended", f"The MCP tool {tool} cannot run without a person watching")
    if kind == "mcp" and not capability.reads_only:
        # MCP is strict (D10c): a changing MCP call is asked every time, whatever the binding says. No ``never`` and
        # no legacy exemption applies, because such calls were refused outright before per-call approvals existed.
        return ASK
    if not wants_question(capability, binding, version=version):
        return ALLOW
    if context.attended:
        return ASK
    if kind != "mcp" and capability.access != "destructive":
        grant = matching_pre_grant(context.pre_grants, tool=tool, version=version, scope=context.scope, at=at)
        if grant is not None:
            return GateDecision(action="allow", pre_grant_id=grant.id)
    return _deny(
        "tool.approval_unattended",
        f"The tool {tool} needs a person's approval for each call, and nobody can answer in this run",
    )


__all__ = [
    "ALLOW",
    "ASK",
    "BUILTIN_CAPABILITIES",
    "MAX_UNATTENDED_GRANT",
    "NEW_BINDING",
    "SAFETY_LIMITS",
    "SERVER_TOOL_CAPABILITIES",
    "SITE_ACTIONS",
    "SYSTEM_FOLDERS",
    "UNKNOWN",
    "Access",
    "AccessAction",
    "AccessContext",
    "AccessDecision",
    "AccessExplanation",
    "AccessMode",
    "AccessRequest",
    "AccessResource",
    "AccessScope",
    "AskMode",
    "CallContext",
    "CapabilitySource",
    "DenialKind",
    "Deny",
    "DenyDuration",
    "EffectClass",
    "GateAction",
    "GateDecision",
    "Grant",
    "GrantDuration",
    "PreGrantScope",
    "Reach",
    "ResourceType",
    "SiteOutcome",
    "ToolBinding",
    "ToolCapability",
    "ToolFacts",
    "ToolNow",
    "ToolPreGrant",
    "access_refusal",
    "agent_ref",
    "capability_of",
    "decide",
    "effect_class",
    "effect_for",
    "effect_of",
    "egress_granted",
    "evaluate",
    "explain",
    "folder_parts",
    "matching_pre_grant",
    "parse_capability",
    "provenance_refusal",
    "refusal_of",
    "related_access",
    "safety_limit",
    "server_tool_capability",
    "site_access",
    "site_outcome",
    "site_records",
    "system_folders",
    "tool_access",
    "wants_question",
    "who_may",
]
