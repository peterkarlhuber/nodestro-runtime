"""Operating profiles: which product a runtime serves, decided once and validated before anything starts.

Nodestro runs as two products on one runtime:

- ``desktop`` (the default): one person, one data directory with SQLite, runs in this process. This is the
  behavior of every Nodestro release so far; :meth:`RuntimeProfile.desktop` and a :class:`Workspace` opened
  without a profile describe exactly that.
- ``server``: many users, PostgreSQL, authentication, tenant isolation, persistent workers and a container
  sandbox. None of these exist yet, so :meth:`RuntimeProfile.validate` refuses the server profile with
  ``profile.server_unavailable``. There is no switch that skips this check.

A profile is built once (from code with :meth:`RuntimeProfile.desktop`, or from the environment with
:meth:`RuntimeProfile.from_env`) and validated before any file or database is touched::

    profile = RuntimeProfile.from_env()  # NODESTRO_PROFILE, NODESTRO_DATA_DIR
    workspace = Workspace(profile=profile)

Problems are reported with stable codes: ``profile.invalid`` (a value that is not understood, or a data
directory that contradicts the profile) and ``profile.server_unavailable``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Literal, get_args

from .problems import NodestroError

ProfileKind = Literal["desktop", "server"]
#: Every profile, in the order they are named in messages.
PROFILE_KINDS: tuple[ProfileKind, ...] = get_args(ProfileKind)
#: The profile used when none is chosen.
DEFAULT_PROFILE: ProfileKind = "desktop"
#: Environment variables read by :meth:`RuntimeProfile.from_env`.
ENV_PROFILE = "NODESTRO_PROFILE"
ENV_DATA_DIR = "NODESTRO_DATA_DIR"
#: The data directory when none is given (unchanged since Nodestro 0.1).
DEFAULT_DATA_DIR = "~/.nodestro"
#: What the server profile needs before it can start; each item arrives with its own pull request.
SERVER_REQUIREMENTS = ("authentication", "postgresql", "tenant_isolation", "persistent_workers", "container_sandbox")
_READABLE_REQUIREMENTS = ", ".join(item.replace("_", " ") for item in SERVER_REQUIREMENTS)


def _invalid(problems: list[dict[str, str]]) -> NodestroError:
    message = "; ".join(item["reason"] for item in problems)
    return NodestroError("profile.invalid", f"Invalid runtime profile: {message}", problems=problems)


def _data_dir(value: Any, field: str) -> Path | dict[str, str]:
    """The data directory as given (``~`` expanded, not resolved), or the problem with it.

    An empty path is the current directory, as ``Path("")`` has always been for ``Workspace("")``."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, os.PathLike):
        text = os.fspath(value)
        if not isinstance(text, str):
            return {"field": field, "value": repr(value), "reason": f"{field} must be a text path"}
    else:
        return {"field": field, "value": repr(value), "reason": f"{field} must be a path"}
    if "\x00" in text:
        return {"field": field, "value": repr(text), "reason": f"{field} contains a NUL character"}
    return Path(text).expanduser()


def same_directory(first: Path, second: Path) -> bool:
    """Whether two paths name the same directory, decided from the text alone (no file system access)."""
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


@dataclass(frozen=True)
class RuntimeProfile:
    """The product a runtime serves and the data directory it works on; immutable.

    Construction checks the values (``profile.invalid``); :meth:`validate` additionally checks that the
    profile can be started in this version (``profile.server_unavailable``).
    """

    kind: ProfileKind
    data_dir: Path

    def __post_init__(self) -> None:
        problems: list[dict[str, str]] = []
        if self.kind not in PROFILE_KINDS:
            problems.append(
                {
                    "field": "kind",
                    "value": repr(self.kind),
                    "reason": f"unknown profile {self.kind!r} (choose {' or '.join(PROFILE_KINDS)})",
                }
            )
        data_dir = _data_dir(self.data_dir, "data_dir")
        if isinstance(data_dir, dict):
            problems.append(data_dir)
        else:
            object.__setattr__(self, "data_dir", data_dir)
        if problems:
            raise _invalid(problems)

    @classmethod
    def desktop(cls, data_dir: str | os.PathLike[str]) -> RuntimeProfile:
        """The desktop profile on ``data_dir``: the behavior of every Nodestro release so far."""
        return cls(kind="desktop", data_dir=data_dir)  # type: ignore[arg-type]  # normalized in __post_init__

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeProfile:
        """The profile chosen by ``NODESTRO_PROFILE`` and ``NODESTRO_DATA_DIR`` (``environ``: ``os.environ``).

        A missing or empty variable means its default (``desktop``, ``~/.nodestro``); surrounding spaces
        are ignored and the profile name is case-insensitive. Any other value is refused with
        ``profile.invalid``, listing every invalid variable at once; nothing falls back to a default.
        """
        environ = os.environ if environ is None else environ
        problems: list[dict[str, str]] = []
        raw_kind = environ.get(ENV_PROFILE, "")
        name = raw_kind.strip().lower() or DEFAULT_PROFILE
        kind: ProfileKind = DEFAULT_PROFILE
        if name in PROFILE_KINDS:
            kind = name
        else:
            problems.append(
                {
                    "field": ENV_PROFILE,
                    "value": raw_kind,
                    "reason": f"{ENV_PROFILE}={raw_kind!r} is not a profile (choose {' or '.join(PROFILE_KINDS)})",
                }
            )
        raw_dir = environ.get(ENV_DATA_DIR, "")
        data_dir = _data_dir(raw_dir.strip() or DEFAULT_DATA_DIR, ENV_DATA_DIR)
        if isinstance(data_dir, dict):
            problems.append(data_dir)
        if problems:
            raise _invalid(problems)
        assert isinstance(data_dir, Path)
        return cls(kind=kind, data_dir=data_dir)

    def matches(self, data_dir: str | os.PathLike[str]) -> RuntimeProfile:
        """This profile, after checking that ``data_dir`` (given separately) is its data directory.

        Raises ``profile.invalid`` when the two contradict each other; nothing is read from or written to disk.
        """
        given = _data_dir(data_dir, "data_dir")
        if isinstance(given, dict):
            raise _invalid([given])
        if not same_directory(given, self.data_dir):
            raise _invalid(
                [
                    {
                        "field": "data_dir",
                        "value": str(given),
                        "reason": f"the data directory {str(given)!r} contradicts the profile's {str(self.data_dir)!r}",
                    }
                ]
            )
        return self

    def validate(self) -> RuntimeProfile:
        """This profile, if this version of Nodestro can start it; ``profile.server_unavailable`` otherwise."""
        if self.kind == "server":
            raise NodestroError(
                "profile.server_unavailable",
                f"The server profile cannot be started yet: it needs {_READABLE_REQUIREMENTS}, which this version "
                "does not provide; use the desktop profile",
                missing=list(SERVER_REQUIREMENTS),
            )
        return self


__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_PROFILE",
    "ENV_DATA_DIR",
    "ENV_PROFILE",
    "PROFILE_KINDS",
    "SERVER_REQUIREMENTS",
    "ProfileKind",
    "RuntimeProfile",
    "same_directory",
]
