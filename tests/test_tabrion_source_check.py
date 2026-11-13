"""DC-NR-1 (dependency confusion): ``scripts/check_tabrion_source.py`` is nodestro-studio's script, unchanged.

Origin: ``https://github.com/peterkarlhuber/nodestro-studio``, ``scripts/check_tabrion_source.py`` at commit
``f0b4d70b249d296b70dd01c74833000a4607c807`` (TABRION DC-NS-2). Never edit this copy: change the studio's, copy it
unchanged, then update the origin commit and the SHA-256 below (the same hash in nodestro-studio, nodestro-runtime,
nodestro-api and orkexa). CI runs it in the job ``tabrion source (poisoned index)``: the install with the studio
lock must not ask a poisoned index for ``tabrion``. The test below shows the other side offline: an install by
range does ask it, takes the poisoned release, and the script goes red.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_tabrion_source.py"
#: SHA-256 of ``scripts/check_tabrion_source.py``, the same in all four repositories.
SCRIPT_SHA256 = "f2516d5df948a554a3aa0261fd26611f00ec50cd62ffded710717aa2f980c40b"


def test_the_script_is_the_shared_copy() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == SCRIPT_SHA256


@pytest.mark.skipif(
    subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, check=False).returncode != 0,
    reason="this interpreter has no pip",
)
def test_an_install_by_range_takes_the_poisoned_tabrion_and_the_script_is_red(tmp_path: Path) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text(
        "tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@d5821faa8742dea012abda3b663549c038d76543\n",
        encoding="utf-8",
    )
    target = tmp_path / "site"
    install = [sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--target", str(target)]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--lock",
            str(lock),
            "--path",
            str(target),
            "--",
            *install,
            "tabrion>=0.1,<0.2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout
    assert (
        "check_tabrion_source: ok   control: pip install --dry-run tabrion chooses the poisoned 0.1.99" in result.stdout
    )
    assert "check_tabrion_source: FAIL the index was asked for a poisoned name: GET /simple/tabrion/" in result.stdout
    assert "check_tabrion_source: FAIL tabrion 0.1.99: not installed from a pinned commit" in result.stdout
    assert result.stdout.splitlines()[-1].startswith("check_tabrion_source: RED")
