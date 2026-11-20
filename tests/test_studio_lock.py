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
# Stub curl: raw lock downloads by branch; the compare API answers COMPARE_STATUS (default 200) with AHEAD_BY and logs
# the Authorization header it got to AUTH_LOG. COMPARE_STATUS=000 is a network failure.
out=""; url=""; write=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) write=$2; shift 2 ;;
    -H) case "$2" in Authorization:*) echo "$2" >> "$AUTH_LOG" ;; esac; shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  https://api.github.com/*)
    status=${COMPARE_STATUS:-200}
    [ "$status" != 000 ] || { echo "curl: (6) Could not resolve host" >&2; echo 000; exit 6; }
    body="{\\"message\\": \\"API rate limit exceeded\\"}"
    [ "$status" != 200 ] || body="{\\"ahead_by\\": $AHEAD_BY}"
    if [ -n "$out" ]; then echo "$body" > "$out"; else echo "$body"; fi
    [ -z "$write" ] || printf '%s' "$status" ;;
  */master/platform/internal-server.txt) echo "lock of master" > "$out" ;;
  */platform/internal-server.txt)
    branch=${url#https://raw.githubusercontent.com/peterkarlhuber/nodestro-studio/}; branch=${branch%/platform/*}
    [ "$branch" = "$BRANCH_WITH_LOCK" ] || exit 22
    echo "lock of $branch" > "$out" ;;
  *) exit 22 ;;
esac
"""


def run(tmp_path: Path, *, event: str, message: str = "", head_ref: str = "", ahead: str = "0",
        branch_with_lock: str = "claude/pair", status: str = "200", token: str = "") -> tuple[str, str]:  # fmt: skip
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
        "COMPARE_STATUS": status,
        "AUTH_LOG": str(tmp_path / "auth.log"),
    }
    env.pop("GH_TOKEN", None)
    if token:
        env["GH_TOKEN"] = token
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


def test_a_merge_push_reads_master_once_the_bump_is_merged(tmp_path: Path) -> None:
    lock, output = run(tmp_path, event="push", message=MERGE, ahead="0")
    assert lock == "lock of master"
    assert "Lock lag" not in output


def test_the_compare_request_uses_the_workflow_token(tmp_path: Path) -> None:
    run(tmp_path, event="push", message=MERGE, ahead="2", token="t0ken")
    assert (tmp_path / "auth.log").read_text().strip() == "Authorization: Bearer t0ken"


def test_without_a_token_the_compare_request_is_anonymous(tmp_path: Path) -> None:
    lock, _ = run(tmp_path, event="push", message=MERGE, ahead="2")
    assert lock == "lock of claude/pair"
    assert not (tmp_path / "auth.log").exists()


def test_a_gone_branch_reads_master_without_a_warning(tmp_path: Path) -> None:
    lock, output = run(tmp_path, event="push", message=MERGE, status="404")
    assert lock == "lock of master"
    assert "::warning" not in output
    assert "is gone" in output


@pytest.mark.parametrize("status", ["403", "500", "000"], ids=["rate limit", "server error", "no network"])
def test_an_api_error_reads_master_with_a_warning_naming_the_cause(tmp_path: Path, status: str) -> None:
    lock, output = run(tmp_path, event="push", message=MERGE, ahead="2", status=status, token="t0ken")
    assert lock == "lock of master"
    assert f"::warning title=Lock lag unknown::GitHub answered HTTP {status}" in output
    assert "t0ken" not in output


def test_an_api_error_without_a_token_says_so(tmp_path: Path) -> None:
    _, output = run(tmp_path, event="push", message=MERGE, status="403")
    assert "(no GH_TOKEN)" in output
    assert "API rate limit exceeded" in output


def test_a_push_without_a_merge_message_reads_master(tmp_path: Path) -> None:
    assert run(tmp_path, event="push", message="Fix a typo", ahead="2")[0] == "lock of master"


def test_the_nightly_reads_master(tmp_path: Path) -> None:
    assert run(tmp_path, event="schedule", message=MERGE, ahead="2")[0] == "lock of master"
