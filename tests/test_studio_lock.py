"""scripts/studio_lock.sh: which studio lock a run installs with (CI-FAST paired lock, W0-1 lock lag).

``curl`` is replaced by a stub on ``PATH``: no network; the stub answers raw lock downloads by branch and the compare
API with a fixed ``ahead_by``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "studio_lock.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs bash")

STUB = """#!/usr/bin/env bash
# Stub curl: raw lock downloads by branch, the compare API with AHEAD_BY (empty: the request fails).
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; -H) shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
case "$url" in
  https://api.github.com/*) [ -n "$AHEAD_BY" ] || exit 22; echo "{\\"ahead_by\\": $AHEAD_BY}" ;;
  */master/platform/internal-server.txt) echo "lock of master" > "$out" ;;
  */platform/internal-server.txt)
    branch=${url#https://raw.githubusercontent.com/peterkarlhuber/nodestro-studio/}; branch=${branch%/platform/*}
    [ "$branch" = "$BRANCH_WITH_LOCK" ] || exit 22
    echo "lock of $branch" > "$out" ;;
  *) exit 22 ;;
esac
"""


def run(tmp_path: Path, *, event: str, message: str = "", head_ref: str = "", ahead: str = "",
        branch_with_lock: str = "claude/pair") -> tuple[str, str]:  # fmt: skip
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"head_commit": {"message": message}}))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_HEAD_REF": head_ref,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "AHEAD_BY": ahead,
        "BRANCH_WITH_LOCK": branch_with_lock,
    }
    out = tmp_path / "lock.txt"
    done = subprocess.run(["bash", str(SCRIPT), str(out)], env=env, capture_output=True, text=True, check=True)
    return out.read_text().strip(), done.stdout


MERGE = "Merge pull request #12 from peterkarlhuber/claude/pair\n\nTitle"


def test_a_pull_request_reads_its_paired_branch(tmp_path: Path) -> None:
    assert run(tmp_path, event="pull_request", head_ref="claude/pair")[0] == "lock of claude/pair"


def test_a_merge_push_reads_the_paired_branch_while_its_bump_is_open(tmp_path: Path) -> None:
    lock, output = run(tmp_path, event="push", message=MERGE, ahead="2")
    assert lock == "lock of claude/pair"
    assert "Lock lag" in output


@pytest.mark.parametrize("ahead", ["0", ""], ids=["bump merged", "compare failed"])
def test_a_merge_push_reads_master_once_the_bump_is_merged_or_unknown(tmp_path: Path, ahead: str) -> None:
    assert run(tmp_path, event="push", message=MERGE, ahead=ahead)[0] == "lock of master"


def test_a_push_without_a_merge_message_reads_master(tmp_path: Path) -> None:
    assert run(tmp_path, event="push", message="Fix a typo", ahead="2")[0] == "lock of master"


def test_the_nightly_reads_master(tmp_path: Path) -> None:
    assert run(tmp_path, event="schedule", message=MERGE, ahead="2")[0] == "lock of master"
