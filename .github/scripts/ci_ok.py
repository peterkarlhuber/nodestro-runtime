#!/usr/bin/env python3
"""ci-ok: did every mandatory pull-request check run on the PR head SHA and succeed?

CI-FAST W1. The mandatory checks are listed in .github/ci-manifest.json. The manifest is read from the PR's BASE
commit, so that a pull request cannot weaken its own gate. The script polls the Actions API until every expected job
has completed (or a timeout is hit), writes a markdown table to $GITHUB_STEP_SUMMARY and prints a one-line verdict.

REPORT-ONLY: the exit code is 0 even when the verdict is NOT OK, unless CI_OK_ENFORCE=1.

The same file is used in every repository (canonical copy: nodestro-studio, design note
docs/architecture/ci-fast/ci-ok.md there). Standard library only, Python 3.10+.

Manifest keys read here:
  pull_request               {"<workflow file>": ["<exact job/check-run name>", ...]}  required on every PR
  pull_request_path_filtered {"<workflow file>": [...]}  required only when the workflow's paths filter matches
  path_filters               {"<workflow file>": {"paths": [...]} | {"paths_ignore": [...]}}  copied verbatim from
                             the workflow's on.pull_request filter; --self-check fails when they differ
  advisory                   ["<workflow file>/<job name>", ...]  continue-on-error jobs: reported, never counted

Modes:
  (default)      evaluate a pull request (event payload from $GITHUB_EVENT_PATH, or --repo/--pr)
  --once         evaluate one snapshot, do not wait (for dry runs against finished PRs)
  --self-check   compare the manifest with the workflow files under --root (no network)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

MANIFEST_PATH = ".github/ci-manifest.json"
WORKFLOWS_DIR = ".github/workflows"
SELF_WORKFLOW = "ci-ok.yml"  # this check's own workflow: never expected, never "unlisted"
API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")

OK_CONCLUSIONS = {"success"}
# Everything else that a completed job can conclude counts as NOT ok, including skipped and neutral: a mandatory
# check that did not actually run is not a passed check.


# --------------------------------------------------------------------------------------------------------------------
# GitHub path-filter glob semantics
# --------------------------------------------------------------------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one GitHub Actions filter pattern into a regex that must match the whole path.

    Cheat sheet (docs.github.com, "Filter pattern cheat sheet"): `*` matches any characters except `/`, `**` matches
    any characters including `/`, `?` matches zero or one of the preceding character, `+` one or more of the preceding
    character, `[...]` is a character class. A leading `!` (negation) is handled by the caller.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    # "**/" also matches zero directories: "**/a.txt" matches "a.txt".
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("?")  # zero or one of the preceding character (the regex quantifier)
        elif c == "+":
            out.append("+")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append("[" + pattern[i + 1 : j].replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    if out and out[0] in ("?", "+"):  # a quantifier without a preceding character: treat it literally
        out[0] = re.escape(out[0])
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def matches_filter(patterns: list[str], path: str) -> bool:
    """True when `path` is selected by an ordered pattern list; a later `!pattern` un-selects (last match wins)."""
    selected = False
    for raw in patterns:
        negated = raw.startswith("!")
        pat = raw[1:] if negated else raw
        if glob_to_regex(pat).match(path):
            selected = not negated
    return selected


def workflow_triggered(filters: dict, changed_files: list[str]) -> bool:
    """Would a pull_request workflow with these path filters run for these changed files?

    `paths`: runs when at least one file is selected. `paths_ignore`: runs unless every file is ignored.
    """
    if "paths" in filters:
        return any(matches_filter(filters["paths"], f) for f in changed_files)
    if "paths_ignore" in filters:
        return any(not matches_filter(filters["paths_ignore"], f) for f in changed_files)
    return True


# --------------------------------------------------------------------------------------------------------------------
# Minimal YAML reader for the on.pull_request block (enough for --self-check; no third-party YAML parser)
# --------------------------------------------------------------------------------------------------------------------


class YamlSubsetError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    quote = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx].rstrip()
    return line.rstrip()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _flow_list(value: str) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise YamlSubsetError(f"expected a flow list, got {value!r}")
    items, cur, quote = [], "", None
    for ch in value[1:-1]:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur)
    return [_unquote(i) for i in items if i.strip()]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], start: int, parent_indent: int) -> list[str]:
    """The lines after `start` that are indented deeper than `parent_indent` (blank lines removed)."""
    out = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if _indent(line) <= parent_indent:
            break
        out.append(line)
    return out


def _key_value(block: list[str], key: str) -> tuple[int, str] | None:
    """Index and inline value of `key:` among the block's direct children (the shallowest indent)."""
    if not block:
        return None
    base = min(_indent(line) for line in block)
    rx = re.compile(r"^(?:%s|\"%s\"|'%s')\s*:(.*)$" % ((re.escape(key),) * 3))
    for idx, line in enumerate(block):
        if _indent(line) == base:
            m = rx.match(line.strip())
            if m:
                return idx, m.group(1).strip()
    return None


def _list_value(block: list[str], idx: int, inline: str) -> list[str]:
    if inline:
        return _flow_list(inline)
    key_indent = _indent(block[idx])
    items = []
    for line in block[idx + 1 :]:
        stripped = line.strip()
        if _indent(line) < key_indent or (_indent(line) == key_indent and not stripped.startswith("-")):
            break
        if not stripped.startswith("-"):
            raise YamlSubsetError(f"unsupported list item {stripped!r}")
        items.append(_unquote(stripped[1:]))
    return items


def read_pull_request_filters(text: str) -> dict | None:
    """The workflow's on.pull_request path filters.

    None: the workflow has no pull_request trigger. {}: it runs on every pull request. Otherwise {"paths": [...]} or
    {"paths_ignore": [...]} (the manifest's spelling). Raises YamlSubsetError for shapes this reader does not handle.
    """
    lines = [_strip_comment(line) for line in text.splitlines()]
    for idx, line in enumerate(lines):
        m = re.match(r"^(?:on|\"on\"|'on'|true)\s*:(.*)$", line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline:
            names = _flow_list(inline) if inline.startswith("[") else [_unquote(inline)]
            return {} if "pull_request" in names else None
        on_block = _block(lines, idx, 0)
        found = _key_value(on_block, "pull_request")
        if found is None:
            return None
        pr_idx, pr_inline = found
        if pr_inline and pr_inline not in ("{}", "null", "~"):
            raise YamlSubsetError(f"unsupported inline pull_request value {pr_inline!r}")
        pr_block = _block(on_block, pr_idx, _indent(on_block[pr_idx]))
        result: dict = {}
        for yaml_key, manifest_key in (("paths", "paths"), ("paths-ignore", "paths_ignore")):
            kv = _key_value(pr_block, yaml_key)
            if kv is not None:
                result[manifest_key] = _list_value(pr_block, kv[0], kv[1])
        return result
    return None


# --------------------------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Expected:
    workflow: str
    job: str
    path_filtered: bool = False
    advisory: bool = False


def split_job_ref(ref: str) -> tuple[str, str]:
    # Workflow file names have no "/"; job names may ("systems / system (ubuntu-24.04)").
    workflow, sep, job = ref.partition("/")
    if not sep or not job:
        raise ValueError(f"advisory entry {ref!r} is not '<workflow file>/<job name>'")
    return workflow, job


def expected_jobs(manifest: dict) -> list[Expected]:
    advisory = {split_job_ref(a) for a in manifest.get("advisory", [])}
    filters = manifest.get("path_filters", {})
    out: list[Expected] = []
    for key, path_filtered in (("pull_request", False), ("pull_request_path_filtered", True)):
        for workflow, jobs in manifest.get(key, {}).items():
            for job in jobs:
                out.append(Expected(workflow, job, path_filtered or workflow in filters, (workflow, job) in advisory))
    return out


# --------------------------------------------------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


class Api:
    """Tiny read-only REST client. Tests replace it with a fake that implements get()."""

    def __init__(self, token: str, base_url: str = API_URL):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.calls = 0

    def get(self, path: str, raw: bool = False):
        url = path if path.startswith("http") else self.base_url + path
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-ok",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last: Exception | None = None
        for attempt in range(4):
            self.calls += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                    data = resp.read().decode("utf-8")
                    return data if raw else json.loads(data)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                limited = exc.code == 429 or (exc.code == 403 and "rate limit" in body.lower())
                if exc.code < 500 and not limited:
                    raise ApiError(exc.code, url, body) from None
                last = ApiError(exc.code, url, body)
                if limited:
                    # GITHUB_TOKEN allows 1,000 requests per hour per repository: wait for the reset (at most 10 min).
                    reset = exc.headers.get("x-ratelimit-reset") or ""
                    pause = float(reset) - time.time() if reset.isdigit() else 60.0
                    print(f"::warning title=ci-ok::API rate limit hit; pausing {int(max(pause, 5))} s", flush=True)
                    time.sleep(min(max(pause, 5.0), 600.0))
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed after retries: {last}")


def paged(api, path: str, key: str | None, limit_pages: int = 50) -> list:
    """Manual &page=N pagination (per_page=100), until a short page."""
    sep = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, limit_pages + 1):
        data = api.get(f"{path}{sep}per_page=100&page={page}")
        chunk = data[key] if key else data
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return items


def fetch_manifest(api, repo: str, ref: str) -> dict | None:
    try:
        text = api.get(f"/repos/{repo}/contents/{MANIFEST_PATH}?ref={ref}", raw=True)
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise
    return json.loads(text)


def load_base_manifest(
    api, repo: str, base_sha: str, head_sha: str, changed_files: list[str]
) -> tuple[dict, list[str]]:
    """The manifest from the PR's base commit, plus notices for the reader."""
    notices = []
    manifest = fetch_manifest(api, repo, base_sha)
    if manifest is None:
        manifest = fetch_manifest(api, repo, head_sha)
        if manifest is None:
            raise SystemExit(
                f"::error::{MANIFEST_PATH} exists neither at the base {base_sha} nor at the head {head_sha}"
            )
        notices.append(f"The base commit has no {MANIFEST_PATH}; the PR head's manifest is used (bootstrap only).")
    elif MANIFEST_PATH in changed_files:
        notices.append(
            f"This PR changes {MANIFEST_PATH}. ci-ok evaluates the BASE manifest; the change takes effect after merge. "
            "A changed list needs a reason in the PR description."
        )
    for script in (".github/scripts/ci_ok.py", f"{WORKFLOWS_DIR}/{SELF_WORKFLOW}"):
        if script in changed_files:
            notices.append(f"This PR changes {script}; the base commit's copy decides this PR.")
    return manifest, notices


def fetch_changed_files(api, repo: str, pr_number: int, base_sha: str, head_sha: str) -> list[str]:
    """Changed files of the PR (renames contribute both names). Uses pulls/{n}/files and falls back to the compare
    API, which needs only contents: read (the ci-ok job has no pull-requests permission)."""
    try:
        files = paged(api, f"/repos/{repo}/pulls/{pr_number}/files", None, limit_pages=30)
    except ApiError as exc:
        if exc.status not in (403, 404):
            raise
        files = api.get(f"/repos/{repo}/compare/{base_sha}...{head_sha}").get("files", [])
    names = []
    for f in files:
        names.append(f["filename"])
        if f.get("previous_filename"):
            names.append(f["previous_filename"])
    return names


@dataclass
class Snapshot:
    runs: dict = field(default_factory=dict)  # workflow file -> latest run (dict)
    jobs: dict = field(default_factory=dict)  # (workflow file, job name) -> job (dict)


class Collector:
    """Reads runs and jobs for one head SHA; caches the jobs of completed runs to spare the rate limit."""

    def __init__(self, api, repo: str, head_sha: str):
        self.api, self.repo, self.head_sha = api, repo, head_sha
        self._cache: dict = {}

    def snapshot(self) -> Snapshot:
        runs = paged(
            self.api,
            f"/repos/{self.repo}/actions/runs?head_sha={self.head_sha}&event=pull_request",
            "workflow_runs",
            limit_pages=10,
        )
        latest: dict = {}
        for run in runs:
            if run.get("event") != "pull_request" or run.get("head_sha") != self.head_sha:
                continue
            path = run.get("path") or ""
            if not path.startswith(WORKFLOWS_DIR + "/"):
                continue
            workflow = path[len(WORKFLOWS_DIR) + 1 :].split("@", 1)[0]
            # Several runs of one workflow on one SHA (e.g. close/reopen): the newest supersedes the older ones.
            if workflow not in latest or (run["created_at"], run["id"]) > (
                latest[workflow]["created_at"],
                latest[workflow]["id"],
            ):
                latest[workflow] = run
        snap = Snapshot(runs=latest)
        for workflow, run in latest.items():
            key = (run["id"], run.get("run_attempt"), run.get("updated_at"))
            if run.get("status") == "completed" and key in self._cache:
                jobs = self._cache[key]
            else:
                # filter=latest: the jobs of the most recent attempt, so a re-run replaces the failed attempt.
                jobs = paged(self.api, f"/repos/{self.repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
                if run.get("status") == "completed":
                    self._cache[key] = jobs
            for job in sorted(jobs, key=lambda j: j["id"]):
                snap.jobs[(workflow, job["name"])] = job
        return snap


# --------------------------------------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Row:
    workflow: str
    job: str
    state: str  # success | pending | <conclusion> | missing | not triggered (paths) | ...
    counts: bool  # does this row decide the verdict?
    ok: bool | None  # True ok, False NOT ok, None undecided (pending)
    url: str = ""
    note: str = ""


@dataclass
class Evaluation:
    rows: list
    unlisted: list
    verdict: str  # OK | NOT OK | PENDING
    decided: bool  # nothing required is pending (or a required job already failed, with fail_fast)


def evaluate(
    manifest: dict,
    snap: Snapshot,
    changed_files: list[str] | None,
    *,
    grace_over: bool,
    final: bool,
    fail_fast: bool = True,
    final_label: str = "timed out (ci-ok)",
) -> Evaluation:
    """Turn a snapshot into rows and a verdict.

    grace_over: runs had enough time to appear; a workflow without any run now counts as missing.
    final: no more waiting (timeout or --once); anything still pending counts as NOT ok, labelled final_label.
    """
    filters = manifest.get("path_filters", {})
    rows: list[Row] = []
    listed = set()
    for exp in expected_jobs(manifest):
        listed.add((exp.workflow, exp.job))
        counts = not exp.advisory
        run = snap.runs.get(exp.workflow)
        job = snap.jobs.get((exp.workflow, exp.job))
        note = "advisory (continue-on-error), not counted" if exp.advisory else ""
        if exp.path_filtered and run is None and job is None:
            wf_filters = filters.get(exp.workflow)
            if wf_filters is None:
                rows.append(
                    Row(
                        exp.workflow,
                        exp.job,
                        "not run (path-filtered, no filter in manifest)",
                        counts,
                        True,
                        note="cannot tell whether it should have run; add path_filters",
                    )
                )
                continue
            if changed_files is not None and not workflow_triggered(wf_filters, changed_files):
                rows.append(Row(exp.workflow, exp.job, "not triggered (paths)", counts, True, note=note))
                continue
        if job is not None:
            url = job.get("html_url", "")
            if job.get("status") != "completed":  # queued, in_progress, waiting, pending, requested
                rows.append(Row(exp.workflow, exp.job, "pending", counts, None, url, note))
            else:
                conclusion = job.get("conclusion") or "unknown"
                rows.append(Row(exp.workflow, exp.job, conclusion, counts, conclusion in OK_CONCLUSIONS, url, note))
            continue
        if run is not None:
            if run.get("status") != "completed":
                rows.append(Row(exp.workflow, exp.job, "pending", counts, None, run.get("html_url", ""), note))
            else:
                rc = run.get("conclusion") or "unknown"
                state = "missing" if rc in OK_CONCLUSIONS else f"missing (run {rc})"
                rows.append(
                    Row(
                        exp.workflow,
                        exp.job,
                        state,
                        counts,
                        False,
                        run.get("html_url", ""),
                        note or "the run finished without a job of this exact name",
                    )
                )
            continue
        if grace_over:
            rows.append(
                Row(
                    exp.workflow,
                    exp.job,
                    "missing",
                    counts,
                    False,
                    note=note or "no pull_request run of this workflow on the head SHA",
                )
            )
        else:
            rows.append(Row(exp.workflow, exp.job, "pending", counts, None, note=note or "no run yet"))

    if final:
        for row in rows:
            if row.ok is None:
                row.state, row.ok = final_label, False

    unlisted = []
    for (workflow, name), job in sorted(snap.jobs.items()):
        if workflow == SELF_WORKFLOW or (workflow, name) in listed:
            continue
        state = job.get("conclusion") or job.get("status") or "unknown"
        unlisted.append(Row(workflow, name, state, False, None, job.get("html_url", ""), "unlisted"))

    counting = [r for r in rows if r.counts]
    failed = [r for r in counting if r.ok is False]
    pending = [r for r in counting if r.ok is None]
    if failed:
        verdict = "NOT OK"
        decided = fail_fast or not pending
    elif pending:
        verdict, decided = "PENDING", False
    else:
        verdict, decided = "OK", True
    return Evaluation(rows, unlisted, verdict, decided)


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ci_span_minutes(snap: Snapshot) -> float | None:
    """Minutes from the first pull_request run's creation to the last job's completion on this head SHA: how long a
    ci-ok job that started with the other workflows would have had to wait (the runner-slot cost)."""
    starts = [_ts(r.get("created_at")) for w, r in snap.runs.items() if w != SELF_WORKFLOW]
    ends = [_ts(j.get("completed_at")) for (w, _), j in snap.jobs.items() if w != SELF_WORKFLOW]
    starts, ends = [s for s in starts if s], [e for e in ends if e]
    if not starts or not ends:
        return None
    return round((max(ends) - min(starts)) / 60, 1)


def verdict_line(ev: Evaluation) -> str:
    counting = [r for r in ev.rows if r.counts]
    good = sum(1 for r in counting if r.ok)
    if ev.verdict == "OK":
        return f"ci-ok: OK ({good}/{len(counting)} mandatory jobs passed or were not triggered by their paths filter)"
    bad = [f"{r.workflow}/{r.job}={r.state}" for r in counting if r.ok is not True]
    return f"ci-ok: {ev.verdict} ({good}/{len(counting)} ok; not ok or unfinished: {', '.join(bad)})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(ev: Evaluation, notices: list[str], header: str) -> str:
    icon = {True: "pass", False: "FAIL", None: "pending"}
    lines = [f"## ci-ok (report-only): {ev.verdict}", "", header, ""]
    for n in notices:
        lines.append(f"> {n}")
    if notices:
        lines.append("")
    lines += ["| Workflow | Job | Result | Counts | Note |", "|---|---|---|---|---|"]
    for r in ev.rows:
        result = f"[{r.state}]({r.url})" if r.url else r.state
        counted = icon[r.ok] if r.counts else "advisory"
        lines.append(f"| {_cell(r.workflow)} | {_cell(r.job)} | {_cell(result)} | {counted} | {_cell(r.note)} |")
    for r in ev.unlisted:
        result = f"[{r.state}]({r.url})" if r.url else r.state
        lines.append(f"| {_cell(r.workflow)} | {_cell(r.job)} | {_cell(result)} | unlisted | not in the manifest |")
    lines += ["", verdict_line(ev), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------------
# Self-check: manifest vs. workflow files (no network)
# --------------------------------------------------------------------------------------------------------------------


def self_check(root: str) -> list[str]:
    """Problems between the manifest and the workflow files under `root` (empty list = consistent)."""
    problems: list[str] = []
    with open(os.path.join(root, MANIFEST_PATH), encoding="utf-8") as fh:
        manifest = json.load(fh)
    wf_dir = os.path.join(root, WORKFLOWS_DIR)
    filters = manifest.get("path_filters", {})
    listed_wfs = set(manifest.get("pull_request", {})) | set(manifest.get("pull_request_path_filtered", {}))
    file_filters: dict = {}
    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")) or name == SELF_WORKFLOW:
            continue
        with open(os.path.join(wf_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        try:
            file_filters[name] = (read_pull_request_filters(text), text)
        except YamlSubsetError as exc:
            problems.append(f"{name}: cannot read on.pull_request ({exc})")
    for name, (wf_filter, _) in file_filters.items():
        if wf_filter is not None and name not in listed_wfs:
            problems.append(f"{name} runs on pull_request but is not in the manifest")
    for name in sorted(listed_wfs):
        if name not in file_filters:
            problems.append(f"{name} is in the manifest but {WORKFLOWS_DIR}/{name} does not exist (or is unreadable)")
            continue
        wf_filter = file_filters[name][0]
        if wf_filter is None:
            problems.append(f"{name} is in the manifest but has no pull_request trigger")
            continue
        if wf_filter and filters.get(name) != wf_filter:
            problems.append(
                f"{name}: path_filters in the manifest {filters.get(name)!r} differ from the workflow's "
                f"on.pull_request filter {wf_filter!r}"
            )
        if not wf_filter and name in filters:
            problems.append(f"{name}: the manifest has path_filters but the workflow runs on every pull request")
        if name in manifest.get("pull_request_path_filtered", {}) and not wf_filter:
            problems.append(f"{name} is listed as path-filtered but its workflow has no paths filter")
    for name in sorted(set(filters) - listed_wfs):
        problems.append(f"path_filters has {name}, which is not a listed workflow")
    for ref in manifest.get("advisory", []):
        try:
            wf, job = split_job_ref(ref)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        all_jobs = manifest.get("pull_request", {}).get(wf, []) + manifest.get("pull_request_path_filtered", {}).get(
            wf, []
        )
        if job not in all_jobs:
            problems.append(f"advisory {ref!r} is not a listed job")
        elif wf in file_filters and "continue-on-error:" not in file_filters[wf][1]:
            problems.append(f"advisory {ref!r}: {wf} has no continue-on-error at all")
    return problems


# --------------------------------------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------------------------------------


def _env_number(env: dict, name: str, default: float) -> float:
    value = env.get(name, "")
    return float(value) if value.strip() else default


def _write_summary(env: dict, text: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="PR number (default: from $GITHUB_EVENT_PATH)")
    p.add_argument("--head-sha", help="override the PR head SHA")
    p.add_argument("--base-sha", help="override the PR base SHA")
    p.add_argument("--manifest-file", help="use this local manifest instead of the base commit's (dry runs only)")
    p.add_argument("--once", "--no-wait", dest="once", action="store_true", help="one snapshot, no waiting")
    p.add_argument("--timeout-minutes", type=float, help="default $CI_OK_TIMEOUT_MINUTES or 90")
    p.add_argument("--poll-seconds", type=float, help="default $CI_OK_POLL_SECONDS or 60")
    p.add_argument("--grace-minutes", type=float, help="time for runs to appear; default $CI_OK_GRACE_MINUTES or 10")
    p.add_argument("--self-check", action="store_true", help="compare manifest and workflow files under --root")
    p.add_argument("--root", default=".", help="repository root for --self-check")
    p.add_argument("--json", help="also write the evaluation as JSON to this file")
    return p.parse_args(argv)


def main(argv=None, env=None, api=None, sleep=time.sleep, clock=time.monotonic) -> int:
    args = parse_args(argv)
    env = dict(os.environ) if env is None else env
    enforce = env.get("CI_OK_ENFORCE") == "1"

    if args.self_check:
        problems = self_check(args.root)
        for p in problems:
            print(f"::warning title=ci-ok manifest drift::{p}")
        print(f"ci-ok self-check: {'consistent' if not problems else f'{len(problems)} problem(s)'}")
        if problems:
            _write_summary(env, "### ci-ok self-check\n\n" + "\n".join(f"- {p}" for p in problems) + "\n")
        return 1 if problems and enforce else 0

    repo = args.repo or env.get("GITHUB_REPOSITORY")
    event = {}
    if env.get("GITHUB_EVENT_PATH") and os.path.exists(env["GITHUB_EVENT_PATH"]):
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
            event = json.load(fh)
    pr = event.get("pull_request") or {}
    pr_number = args.pr or pr.get("number")
    if not repo or not pr_number:
        print("::error::ci-ok needs a repository and a pull request (run on pull_request, or pass --repo and --pr)")
        return 2
    if api is None:
        api = Api(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "")
    if not pr:
        pr = api.get(f"/repos/{repo}/pulls/{pr_number}")
    head_sha = args.head_sha or pr["head"]["sha"]
    base_sha = args.base_sha or pr["base"]["sha"]

    changed = fetch_changed_files(api, repo, pr_number, base_sha, head_sha)
    if args.manifest_file:
        with open(args.manifest_file, encoding="utf-8") as fh:
            manifest = json.load(fh)
        notices = [f"Dry run: manifest from the local file {args.manifest_file}, not from the base commit."]
    else:
        manifest, notices = load_base_manifest(api, repo, base_sha, head_sha, changed)
    for n in notices:
        print(f"::notice title=ci-ok::{n}")

    timeout = 60 * (
        args.timeout_minutes if args.timeout_minutes is not None else _env_number(env, "CI_OK_TIMEOUT_MINUTES", 90)
    )
    poll = args.poll_seconds if args.poll_seconds is not None else _env_number(env, "CI_OK_POLL_SECONDS", 60)
    grace = 60 * (args.grace_minutes if args.grace_minutes is not None else _env_number(env, "CI_OK_GRACE_MINUTES", 10))
    fail_fast = env.get("CI_OK_FAIL_FAST", "1") != "0"

    collector = Collector(api, repo, head_sha)
    start = clock()
    while True:
        elapsed = clock() - start
        final = args.once or elapsed >= timeout
        snap = collector.snapshot()
        ev = evaluate(
            manifest,
            snap,
            changed,
            grace_over=args.once or elapsed >= grace,
            final=final,
            fail_fast=fail_fast,
            final_label="not finished (--once)" if args.once else "timed out (ci-ok)",
        )
        if ev.decided or final:
            break
        pending = [f"{r.workflow}/{r.job}" for r in ev.rows if r.counts and r.ok is None]
        print(
            f"[{int(elapsed // 60)} min] waiting for {len(pending)} job(s): {', '.join(pending[:8])}"
            f"{' ...' if len(pending) > 8 else ''}",
            flush=True,
        )
        sleep(poll)

    header = (
        f"PR #{pr_number}, head `{head_sha[:12]}`, base `{base_sha[:12]}`, {len(changed)} changed file(s), "
        f"waited {int((clock() - start) // 60)} min, {getattr(api, 'calls', '?')} API call(s), "
        f"CI span on this head {ci_span_minutes(snap)} min. "
        "Mode: report-only (CI_OK_ENFORCE=1 makes NOT OK fail this job)."
    )
    report = render_markdown(ev, notices, header)
    print(report)
    _write_summary(env, report)
    for r in ev.unlisted:
        print(f"::warning title=ci-ok unlisted job::{r.workflow}/{r.job} ran on this PR but is not in {MANIFEST_PATH}")
    if ev.verdict != "OK":
        print(f"::warning title=ci-ok::{verdict_line(ev)}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "verdict": ev.verdict,
                    "ci_span_minutes": ci_span_minutes(snap),
                    "rows": [r.__dict__ for r in ev.rows],
                    "unlisted": [r.__dict__ for r in ev.unlisted],
                    "notices": notices,
                },
                fh,
                indent=2,
            )
    return 1 if (ev.verdict != "OK" and enforce) else 0


if __name__ == "__main__":
    sys.exit(main())
