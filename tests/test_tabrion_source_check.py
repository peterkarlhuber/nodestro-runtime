"""DC-NR-1 (dependency confusion): ``scripts/check_tabrion_source.py`` is nodestro-studio's, unchanged.

Origin: ``https://github.com/peterkarlhuber/nodestro-studio``, ``scripts/check_tabrion_source.py`` at commit
``bf51022f64e3885c193bdbcc151c839f45e9f3df`` (TABRION DC-NS-2). Never edit this copy: change the studio's, copy it
unchanged, then update the origin commit and the SHA-256 below (the same hash in nodestro-studio, nodestro-runtime,
nodestro-api, orkexa and tabrion-studio). CI runs it in the job ``tabrion source (poisoned index)``: the pinned
install must not ask a poisoned index for ``tabrion``. That zero-request assertion is the proof; the installed
``direct_url.json`` only detects an accidental index install, since a wheel can forge it. The test below shows both
offline: an install by range takes a poisoned wheel whose forged ``direct_url.json`` passes the origin check, and
only the requests turn the script red.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_tabrion_source.py"
#: SHA-256 of ``scripts/check_tabrion_source.py``, the same in every repository that carries it.
SCRIPT_SHA256 = "8496bd63327dbee897966bd9d99296d479e2c1c0c55baa9018ee8dba50ab177f"
URL = "https://github.com/peterkarlhuber/tabrion.git"
SHA = "d5821faa8742dea012abda3b663549c038d76543"
HAS_PIP = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, check=False).returncode == 0


def test_the_script_is_the_shared_copy() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == SCRIPT_SHA256


@pytest.mark.skipif(not HAS_PIP, reason="this interpreter has no pip")
def test_an_install_by_range_takes_the_forged_tabrion_and_only_the_requests_are_red(tmp_path: Path) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text(f"tabrion @ git+{URL}@{SHA}\n", encoding="utf-8")
    target = tmp_path / "site"
    install = [sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--target", str(target)]
    script = [sys.executable, "-I", str(SCRIPT), "--lock", str(lock), "--path", str(target), "--"]
    result = subprocess.run([*script, *install, "tabrion>=0.1,<0.2"], capture_output=True, text=True, check=False)
    out = result.stdout
    assert result.returncode == 1, out
    assert "ok   control: pip install --dry-run tabrion chooses the poisoned 0.1.0" in out
    # pip kept the wheel's forged direct_url.json: the origin check passes (detection, not proof).
    assert f"ok   tabrion 0.1.0: direct_url.json names {URL}@{SHA[:12]}" in out
    assert "FAIL the index was asked for a poisoned name: GET /simple/tabrion/" in out
    assert out.splitlines()[-1] == "check_tabrion_source: RED (2 problem(s))"
