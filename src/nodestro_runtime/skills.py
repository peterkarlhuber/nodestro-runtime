"""The skill library (NAL-B3): skills in the open Agent Skills format, stored in immutable versions.

A skill is a package (``SKILL.md`` plus ``references/``, ``assets/``, ``scripts/``) read and checked by
:mod:`orkexa.skills`. Every save that changes a package's files stores a new :class:`~.schemas.SkillVersion` with a
digest over all paths and bytes; file contents are stored once per SHA-256 (``skill_blobs``), so versions share
unchanged files. The latest state of each skill is a :class:`~.schemas.Skill` in ``skills``. Names are unique in a
workspace (the specification ties a skill's name to its folder).

A skill only adds text to an agent's prompt. It never grants tools or permissions (``allowed-tools`` is shown and
ignored), and its scripts are stored and shown but never run.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import hashlib
import re

from orkexa.skills import (
    SkillIssue,
    SkillLimits,
    SkillPackage,
    package_digest,
    parse_skill,
    validate,
)

from .problems import NodestroError
from .sanitize import credential_kinds
from .schemas import Skill, SkillFileRef, SkillStatus, SkillVersion, now
from .storage import DocumentStore

SKILLS = "skills"
VERSIONS = "skill_versions"
BLOBS = "skill_blobs"
LIMITS = SkillLimits()


def _key(skill_id: str, version: int) -> str:
    return f"{skill_id}:{version:06d}"


def _warning_issue(code: str, message: str, *, path: str, **params: str | int) -> SkillIssue:
    return SkillIssue(severity="warning", code=code, message=message, path=path, params={"path": path, **params})


def check(package: SkillPackage) -> list[SkillIssue]:
    """The findings of the specification (:func:`orkexa.skills.validate`) plus a warning for each text file that
    seems to contain a credential (a skill is shared and exported; it should never carry one). Every file that is
    UTF-8 text is scanned, scripts and configuration files included (internal review L5)."""
    issues = validate(package, limits=LIMITS)
    for item in package.files:
        try:
            text = item.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        kinds = credential_kinds(text)
        if kinds:
            issues.append(
                _warning_issue(
                    "skill.secret_suspected",
                    f"{item.path} seems to contain a credential ({', '.join(kinds)}); remove it",
                    path=item.path,
                    kinds=", ".join(kinds),
                )
            )
    return issues


_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


def _language(package: SkillPackage) -> str | None:
    """``metadata.language`` when it looks like a language tag, else ``None`` (it is only a label)."""
    value = package.metadata.get("language", "").strip()
    return value if _LANGUAGE.fullmatch(value) else None


def is_import(origin: str) -> bool:
    return origin.startswith("import:")


def errors_of(issues: list[SkillIssue]) -> list[SkillIssue]:
    return [issue for issue in issues if issue.severity == "error"]


def _raise_invalid(issues: list[SkillIssue]) -> None:
    errors = errors_of(issues)
    if errors:
        first = errors[0]
        raise NodestroError(
            "skill.invalid",
            f"The skill is not valid: {first.message}",
            reason=first.code,
            count=len(errors),
        )


class Skills:
    """The skill library of a workspace, on any :class:`~nodestro_runtime.storage.DocumentStore`."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    # ---- reading ---------------------------------------------------------------------------------------------

    def all(self) -> list[Skill]:
        return sorted((skill for _key, skill in self.store.items(SKILLS, Skill)), key=lambda item: item.name)

    def get(self, skill_id: str) -> Skill | None:
        return self.store.get(SKILLS, skill_id, Skill)

    def by_name(self, name: str) -> Skill | None:
        return next((skill for skill in self.all() if skill.name == name), None)

    def versions(self, skill_id: str) -> list[SkillVersion]:
        """Oldest first."""
        return self.store.list_prefix(VERSIONS, f"{skill_id}:", SkillVersion)

    def version(self, skill_id: str, version: int) -> SkillVersion:
        found = self.store.get(VERSIONS, _key(skill_id, version), SkillVersion)
        if found is None:
            raise KeyError(f"{skill_id} v{version}")
        return found

    def package(self, skill_id: str, version: int) -> SkillPackage:
        """The package of one version with its exact bytes. Raises :class:`KeyError` for an unknown version."""
        stored = self.version(skill_id, version)
        files: dict[str, bytes] = {}
        for ref in stored.files:
            raw = self.store.get_raw(BLOBS, ref.sha256)
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), str):
                raise KeyError(f"{skill_id} v{version}: {ref.path}")
            data = base64.b64decode(raw["data"])
            # A stored file must still be the bytes its version names (a restored backup is not trusted; L4).
            if hashlib.sha256(data).hexdigest() != ref.sha256:
                raise KeyError(f"{skill_id} v{version}: {ref.path} does not match its hash")
            files[ref.path] = data
        package = parse_skill(files, limits=LIMITS).model_copy(update={"directory": stored.name})
        if package_digest(package) != stored.digest:
            raise KeyError(f"{skill_id} v{version}: the files do not match the version's digest")
        return package

    def resolver(self) -> Callable[[str, int], tuple[Skill, SkillPackage]]:
        """What an agent builder resolves an assigned skill with (:attr:`AgentBuilder.skill_source`)."""

        def resolve(skill_id: str, version: int) -> tuple[Skill, SkillPackage]:
            skill = self.get(skill_id)
            if skill is None:
                raise KeyError(skill_id)
            return skill, self.package(skill_id, version)

        return resolve

    # ---- writing ---------------------------------------------------------------------------------------------

    def _store_version(
        self, skill_id: str, number: int, package: SkillPackage, *, origin: str, note: str, created_by: str
    ) -> SkillVersion:
        refs = []
        for item in package.files:
            refs.append(SkillFileRef(path=item.path, size=item.size, sha256=item.sha256))
            self.store.put_raw_if_absent(BLOBS, item.sha256, {"data": base64.b64encode(item.data).decode("ascii")})
        version = SkillVersion(
            skill_id=skill_id,
            version=number,
            digest=package_digest(package),
            name=package.name,
            description=package.description.strip(),
            license=package.license,
            compatibility=package.compatibility,
            metadata=package.metadata,
            allowed_tools=package.allowed_tools,
            files=refs,
            origin=origin,
            note=note.strip()[:200],
            created_by=created_by,
        )
        self.store.put(VERSIONS, _key(skill_id, number), version)
        return version

    def _check_name(self, name: str, skill_id: str | None) -> None:
        other = self.by_name(name)
        if other is not None and other.id != skill_id:
            raise NodestroError("skill.name_taken", f"A skill named {name!r} already exists", name=name)

    def create(self, package: SkillPackage, *, origin: str = "user", note: str = "", created_by: str = "") -> Skill:
        """Store a new skill as version 1. Raises :class:`NodestroError` (``skill.invalid``, ``skill.name_taken``)."""
        _raise_invalid(check(package))
        with self.store.transaction():
            self.store.lock(SKILLS, "names")
            self._check_name(package.name, None)
            skill = Skill(
                name=package.name,
                description=package.description.strip(),
                language=_language(package),
                origin=origin,
                has_scripts=package.has_scripts,
            )
            self._store_version(skill.id, 1, package, origin=origin, note=note, created_by=created_by)
            self.store.put(SKILLS, skill.id, skill)
        return skill

    def save(
        self, skill_id: str, package: SkillPackage, *, origin: str | None = None, note: str = "", created_by: str = ""
    ) -> Skill:
        """Store a changed package as the skill's next version; an unchanged one returns the skill as it is.

        ``origin`` is where the new content came from (an import: ``import:<sha256>``); without it the version keeps
        the skill's origin. Imported content marks the skill as imported for good, so the warnings about imported
        skills never get lost by editing or importing into another skill (internal review M2). Raises
        :class:`KeyError` for an unknown skill."""
        _raise_invalid(check(package))
        with self.store.transaction():
            self.store.lock(SKILLS, "names")
            self.store.lock(VERSIONS, skill_id)
            current = self.get(skill_id)
            if current is None:
                raise KeyError(skill_id)
            latest = self.version(skill_id, current.version)
            if package_digest(package) == latest.digest:
                return current
            self._check_name(package.name, skill_id)
            number = current.version + 1
            source = origin or current.origin
            self._store_version(skill_id, number, package, origin=source, note=note, created_by=created_by)
            changed = current.model_copy(
                update={
                    "name": package.name,
                    "description": package.description.strip(),
                    "origin": source if is_import(source) else current.origin,
                    "language": _language(package),
                    "has_scripts": package.has_scripts,
                    "version": number,
                    "updated_at": now(),
                }
            )
            self.store.put(SKILLS, skill_id, changed)
        return changed

    def set_status(self, skill_id: str, status: SkillStatus) -> Skill:
        """Activate, disable or archive a skill. A disabled or archived skill cannot be assigned or run."""
        with self.store.transaction():
            current = self.get(skill_id)
            if current is None:
                raise KeyError(skill_id)
            changed = current.model_copy(update={"status": status, "updated_at": now()})
            self.store.put(SKILLS, skill_id, changed)
        return changed

    def delete(self, skill_id: str) -> bool:
        """Delete a skill with all its versions; ``False`` if there was none. Blobs another version still uses stay.

        The caller makes sure no agent uses the skill (or archives it instead)."""
        with self.store.transaction():
            if self.get(skill_id) is None:
                return False
            dropped = self.versions(skill_id)
            for item in dropped:
                self.store.delete(VERSIONS, _key(skill_id, item.version))
            self.store.delete(SKILLS, skill_id)
            used = {ref.sha256 for _key, other in self.store.items(VERSIONS, SkillVersion) for ref in other.files}
            for sha in {ref.sha256 for item in dropped for ref in item.files} - used:
                self.store.delete(BLOBS, sha)
        return True
