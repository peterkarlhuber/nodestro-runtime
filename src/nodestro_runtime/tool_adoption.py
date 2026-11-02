"""Adopting AI-generated tool code as one's own: a confirmed, audited action on the desktop only (ADR-0006, R-4).

AI-generated code is ``untrusted`` and runs only in an isolating sandbox. On the desktop, the local owner or an
administrator may take over one version of such a tool as their own code after seeing its code and tests and a
warning (the studio's confirmation dialog). The tool then counts as ``user-authored``: on the desktop it may run in
the subprocess sandbox like code the person wrote. Nothing else changes:

* Off the desktop (server, Nodestro Local Docker, a tenant's workspace) every adoption is refused with
  ``tool.adoption_desktop_only``; their trust policy is not touched.
* The adoption is bound to the :func:`~nodestro_runtime.tool_provenance.content_hash` of the code and tests that were
  shown; ``tool.adoption_stale`` when the tool changed in between.
* A later version that is not a person's own (an AI change) ends the adoption: the tool is ``untrusted`` again
  (:meth:`~nodestro_runtime.tool_library.ToolLibrary.adoption`). Editing it in the editor keeps it.
* Every adoption, every refused attempt and every revocation is written to the tool audit trail
  (:data:`nodestro_runtime.tool_approvals.AUDIT`): who, when, the tool, its version and the content hash.
"""

from __future__ import annotations

from collections.abc import Callable

from .problems import NodestroError
from .storage import DocumentStore
from .tool_approvals import ToolApprovalEvent, record_audit
from .tool_library import ToolLibrary
from .tool_provenance import ToolAdoption


def adopt_tool(
    tools: ToolLibrary, store: DocumentStore, name: str, *, actor: str, role: str, expected_hash: str
) -> ToolAdoption:
    """Adopt ``name`` (:meth:`ToolLibrary.adopt`) and audit the outcome, also a refusal (``tool_adoption_refused``
    with the problem code; the error is raised again)."""
    try:
        adoption = tools.adopt(name, actor=actor, role=role, expected_hash=expected_hash)
    except NodestroError as exc:
        record_audit(
            store,
            ToolApprovalEvent(
                kind="tool_adoption_refused",
                actor=actor,
                tool=name,
                outcome=exc.code,
                details={"role": role, "content_hash": expected_hash[:128]},
            ),
        )
        raise
    record_audit(
        store,
        ToolApprovalEvent(
            kind="tool_adopted",
            actor=actor,
            tool=name,
            outcome="adopted",
            details={"role": role, "version": adoption.version, "content_hash": adoption.content_hash},
        ),
    )
    return adoption


def audit_revocation(store: DocumentStore) -> Callable[[ToolAdoption, int], None]:
    """A :attr:`ToolLibrary.on_adoption_revoked` callback that writes ``tool_adoption_revoked`` to the trail."""

    def revoked(adoption: ToolAdoption, version: int) -> None:
        record_audit(
            store,
            ToolApprovalEvent(
                kind="tool_adoption_revoked",
                actor="system",
                tool=adoption.name,
                outcome="ai_change",
                details={
                    "adopted_version": adoption.version,
                    "adopted_by": adoption.actor,
                    "content_hash": adoption.content_hash,
                    "new_version": version,
                },
            ),
        )

    return revoked


__all__ = ["adopt_tool", "audit_revocation"]
