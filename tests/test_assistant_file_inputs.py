"""Input files attached to a Nodestro Assistant conversation: kept while the conversation holds them, never removed
by the clean-up, and handed to the model with the conversation's own wording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orkexa import ModelCapabilities

from nodestro_runtime.file_delivery import INTRO, RunFile, deliver, files_tool
from nodestro_runtime.input_files import InputFiles
from nodestro_runtime.storage import Store

#: Uploaded files never ask knowledge (only knowledge files of a run do).
NO_KNOWLEDGE: Any = None


def test_files_of_a_conversation_are_protected_from_the_cleanup(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    inputs = InputFiles(store, tmp_path / "inputs")
    held = inputs.add(b"Kosten: 1200", name="notiz.txt", origin="assistant", uploaded_by="user-1")
    loose = inputs.add(b"lose", name="lose.txt")
    store.put_raw("assistant_conversations", "chat_1", f'{{"id": "chat_1", "files": [{{"id": "{held.id}"}}]}}')

    assert inputs.references()[held.id] == {"conversation"}
    later = datetime.now(UTC) + timedelta(days=400)
    result = inputs.cleanup(older_than_days=1, at=later)
    assert result.removed == 1 and result.kept_in_use == 1
    assert held.id in inputs and loose.id not in inputs
    assert inputs.get(held.id).origin == "assistant" and inputs.get(held.id).uploaded_by == "user-1"

    # The conversation is gone: its files are released like the files of a deleted run.
    store.delete("assistant_conversations", "chat_1")
    assert inputs.release([held.id]) == 1
    assert held.id not in inputs


def test_the_delivery_names_the_conversation_instead_of_a_run(tmp_path: Path) -> None:
    inputs = InputFiles(Store(tmp_path / "db.sqlite"), tmp_path / "inputs")
    item = inputs.add(b"Miete;1200\n", name="kosten.csv", origin="assistant")
    files = [
        RunFile(
            id=item.id,
            name=item.name,
            source="input",
            format=item.format,
            group=item.group,
            size=item.size,
            chars=item.chars,
            outline=item.outline,
        )
    ]
    capabilities = ModelCapabilities()
    default = deliver(files, capabilities, inputs, NO_KNOWLEDGE)
    assert default.blocks[0]["text"].startswith(INTRO)
    assert default.tool is not None and "this run" in (default.tool.description or "")

    own = deliver(
        files,
        capabilities,
        inputs,
        NO_KNOWLEDGE,
        intro="Files of this conversation.",
        subject="this conversation",
    )
    assert own.blocks[0]["text"].startswith("Files of this conversation.")
    assert own.tool is not None
    assert "this conversation" in (own.tool.description or "") and "this run" not in (own.tool.description or "")
    assert [item.mode for item in own.provided] == ["text"]
    tool = files_tool(files, capabilities, inputs, subject="this conversation")
    assert "this conversation" in (tool.description or "")
