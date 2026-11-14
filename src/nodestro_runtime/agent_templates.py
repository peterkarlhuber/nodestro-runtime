"""The saved versions of each agent template (NAL-B1): every save that changes a template is kept.

A template is stored in the ``templates`` collection in its latest version; each version is also stored in
``agent_template_versions`` and is never changed afterwards. A template saved before versions existed has none
stored: its stored state counts as version 1 (``origin`` ``user``), and its next save first stores that state as
``v1``, so nothing is lost and no migration has to run at start-up. Reading the latest version and storing the next
one is one transaction under the lock of the template's versions, so two saves never get the same number.
"""

from __future__ import annotations

from .schemas import AgentConfig, AgentTemplate, AgentTemplateVersion, TemplateMeta, now
from .storage import DocumentStore

TEMPLATES = "templates"
_COLLECTION = "agent_template_versions"


def _key(template_id: str, version: int) -> str:
    return f"{template_id}:{version:06d}"


def _content(agent: AgentConfig, meta: TemplateMeta) -> str:
    return (
        AgentConfig.model_validate(agent.model_dump()).model_dump_json()
        + TemplateMeta.model_validate(meta.model_dump()).model_dump_json()
    )


def _first(template: AgentTemplate) -> AgentTemplateVersion:
    """The version a template stored before versions existed (or never changed since) stands for."""
    return AgentTemplateVersion(
        template_id=template.id,
        version=template.version,
        agent=template.agent,
        meta=template.meta,
        created_at=template.updated_at,
    )


class AgentTemplates:
    """Agent templates with their versions, on any :class:`~nodestro_runtime.storage.DocumentStore`."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def get(self, template_id: str) -> AgentTemplate | None:
        return self.store.get(TEMPLATES, template_id, AgentTemplate)

    def versions(self, template_id: str) -> list[AgentTemplateVersion]:
        """Oldest first; a template without stored versions has its current state as its only version."""
        stored = self.store.list_prefix(_COLLECTION, f"{template_id}:", AgentTemplateVersion)
        if stored:
            return stored
        template = self.get(template_id)
        return [_first(template)] if template is not None else []

    def version(self, template_id: str, version: int) -> AgentTemplateVersion:
        for item in self.versions(template_id):
            if item.version == version:
                return item
        raise KeyError(f"v{version}")

    def create(
        self, agent: AgentConfig, meta: TemplateMeta | None = None, *, note: str = "", created_by: str = ""
    ) -> AgentTemplate:
        template = AgentTemplate(agent=agent, meta=meta or TemplateMeta())
        with self.store.transaction():
            self.store.put(TEMPLATES, template.id, template)
            self.store.put(
                _COLLECTION,
                _key(template.id, 1),
                AgentTemplateVersion(
                    template_id=template.id,
                    version=1,
                    agent=template.agent,
                    meta=template.meta,
                    note=note.strip()[:200],
                    created_at=template.created_at,
                    created_by=created_by,
                ),
            )
        return template

    def save(
        self,
        template_id: str,
        agent: AgentConfig,
        meta: TemplateMeta | None = None,
        *,
        note: str = "",
        created_by: str = "",
    ) -> AgentTemplate:
        """Store a changed template as its next version and return it; an unchanged one is returned as it is.

        ``meta`` ``None`` keeps the stored meta. Raises :class:`KeyError` for an unknown template."""
        with self.store.transaction():
            self.store.lock(_COLLECTION, template_id)
            current = self.get(template_id)
            if current is None:
                raise KeyError(template_id)
            meta = meta if meta is not None else current.meta
            if _content(agent, meta) == _content(current.agent, current.meta):
                return current
            stored = self.store.list_prefix(_COLLECTION, f"{template_id}:", AgentTemplateVersion)
            if not stored:
                first = _first(current)
                self.store.put(_COLLECTION, _key(template_id, first.version), first)
                stored = [first]
            number = stored[-1].version + 1
            changed = AgentTemplate(
                agent=agent,
                meta=meta,
                id=template_id,
                version=number,
                created_at=current.created_at,
                updated_at=now(),
            )
            self.store.put(
                _COLLECTION,
                _key(template_id, number),
                AgentTemplateVersion(
                    template_id=template_id,
                    version=number,
                    agent=agent,
                    meta=meta,
                    note=note.strip()[:200],
                    created_at=changed.updated_at,
                    created_by=created_by,
                ),
            )
            self.store.put(TEMPLATES, template_id, changed)
        return changed

    def delete(self, template_id: str) -> bool:
        """Delete a template with all its versions; ``False`` if there was none."""
        with self.store.transaction():
            if self.get(template_id) is None:
                return False
            for item in self.store.list_prefix(_COLLECTION, f"{template_id}:", AgentTemplateVersion):
                self.store.delete(_COLLECTION, _key(template_id, item.version))
            self.store.delete(TEMPLATES, template_id)
        return True
