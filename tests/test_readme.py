"""The README's quickstart runs as written (offline, with the scripted client)."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"


def test_the_quickstart_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    assert blocks, "the README has no Python example"
    exec(compile(blocks[0], "README.md", "exec"), {"__name__": "readme"})
    assert capsys.readouterr().out.strip() == "succeeded Offer: 10 units, delivered Friday, 1,200 EUR."
