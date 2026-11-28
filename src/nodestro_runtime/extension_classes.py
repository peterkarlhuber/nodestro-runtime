"""Tool classes of extension tools and their later release (N-22, G-6 items 17, 18 and 22). Nothing is released.

A package's own statement about its tools never lowers their capability (:func:`~.extension_sandbox.
floor_capability`), so a class cannot be derived from it. What a tool in the Docker sandbox can do is bounded by what
the container is given instead, its :class:`Grants`:

- no grant (the only kind that exists today): no network, no host file, no secret; the tool's only effect is its
  result: class ``read`` with reach ``internal`` (:func:`effective_class`);
- a folder grant (planned, through ``orkexa.fs`` and the host's :class:`~nodestro_runtime.folder_grants.FolderGrant`,
  never a mount): ``read`` or ``write`` with reach ``internal``;
- a network grant (planned, only the granted hosts through the egress guard, like the MCP and web tools): reach
  ``external``.

A class is offered by default (without the development switch) and counts with its effective capability only after it
is listed in :data:`RELEASES` with the evidence of gate G-6: the passed catalog (``docs/extension-isolation.md``), the
internal review and the independent review. :data:`RELEASES` is empty and stays empty in this version; every grant
other than none is refused (:func:`require_supported`), because no grant path exists yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .tool_policy import UNKNOWN, Access, Reach, ToolCapability

#: ``(access, reach)``.
ToolClass = tuple[Access, Reach]


@dataclass(frozen=True)
class Grants:
    """What the container of one extension tool is given. Default (and today the only possibility): nothing."""

    #: Host names the tool may reach through the egress guard.
    network: tuple[str, ...] = ()
    #: ``(granted folder id, "read" | "write")`` reached through ``orkexa.fs`` on the host.
    folders: tuple[tuple[str, Literal["read", "write"]], ...] = field(default=())

    @property
    def none(self) -> bool:
        return not self.network and not self.folders


NO_GRANTS = Grants()


def effective_class(grants: Grants) -> ToolClass:
    """The class the container's grants bound a tool to (see the module documentation)."""
    access: Access = "write" if any(mode == "write" for _folder, mode in grants.folders) else "read"
    reach: Reach = "external" if grants.network else "internal"
    return access, reach


#: The G-6 catalog items (``docs/extension-isolation.md``) each class needs beyond the common ones: 17 the network
#: grant, 18 the folder grant. A package with dependencies of its own needs item 21 (a dependency image) whatever its
#: class; the store refuses such packages (:data:`~.extension_store.PROVIDED`).
PREREQUISITES: dict[ToolClass, frozenset[int]] = {
    ("read", "internal"): frozenset(),
    ("write", "internal"): frozenset({18}),
    ("read", "external"): frozenset({17}),
    ("write", "external"): frozenset({17, 18}),
}
#: The items not implemented: a class that needs one of them cannot be released (:class:`ClassRelease`).
MISSING_ITEMS: frozenset[int] = frozenset({17, 18, 21})


@dataclass(frozen=True)
class ClassRelease:
    """The release of one class after gate G-6; every reference is required."""

    tool_class: ToolClass
    #: The passed G-6 catalog for this class on the real engines (report or CI run).
    evidence: str
    internal_review: str
    independent_review: str

    def __post_init__(self) -> None:
        if not all(part.strip() for part in (self.evidence, self.internal_review, self.independent_review)):
            raise ValueError("a class release needs the G-6 evidence, the internal and the independent review")
        missing = sorted(PREREQUISITES.get(self.tool_class, MISSING_ITEMS) & MISSING_ITEMS)
        if missing:
            raise ValueError(f"the class {self.tool_class} needs G-6 item(s) {missing}, which are not implemented")


#: The released classes: none. Adding one is a reviewed change after gate G-6, never a setting.
RELEASES: tuple[ClassRelease, ...] = ()


def released(tool_class: ToolClass, releases: Sequence[ClassRelease] = RELEASES) -> ClassRelease | None:
    return next((item for item in releases if item.tool_class == tool_class), None)


def require_supported(grants: Grants) -> None:
    """Refuse every grant: no network or folder grant path exists for extension tools yet (default no access)."""
    if not grants.none:
        raise ValueError("extension tools get no network or folder grant in this version")


def class_capability(
    stated: Mapping[str, Any] | None,
    grants: Grants = NO_GRANTS,
    releases: Sequence[ClassRelease] = RELEASES,
) -> ToolCapability:
    """The capability an extension tool counts with: at least :data:`~nodestro_runtime.tool_policy.UNKNOWN` (raised by
    a stated ``destructive`` only) unless its effective class is released, then that class (still raised by a stated
    ``destructive``)."""
    require_supported(grants)
    destructive = (stated or {}).get("access") == "destructive"
    release = released(effective_class(grants), releases)
    if release is None:
        if destructive:
            return ToolCapability(access="destructive", reach="external", source="author")
        return UNKNOWN
    access, reach = release.tool_class
    return ToolCapability(access="destructive" if destructive else access, reach=reach, source="nodestro")


__all__ = [
    "MISSING_ITEMS",
    "NO_GRANTS",
    "PREREQUISITES",
    "RELEASES",
    "ClassRelease",
    "Grants",
    "ToolClass",
    "class_capability",
    "effective_class",
    "released",
    "require_supported",
]
