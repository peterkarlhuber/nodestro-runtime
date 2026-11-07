#!/usr/bin/env python3
"""ci-ok: did every mandatory pull-request check run on the PR head SHA and succeed?

CI-FAST W1, v2. The mandatory checks are listed in .github/ci-manifest.json. The workflow .github/workflows/ci-ok.yml
runs on `workflow_run: completed` of every listed workflow, so GitHub always takes its definition, this script and the
manifest from the DEFAULT branch: a pull request cannot change its own gate. Each completion starts one short
evaluation of the current state (no waiting, no polling): OK, NOT OK, or PENDING while listed workflows are still
running. The latest completed evaluation for a head SHA is the verdict. The result is a markdown table in
$GITHUB_STEP_SUMMARY plus one ::notice or ::warning line.

REPORT-ONLY: the exit code is 0 whatever the verdict, also on API errors (they are reported as NOT OK), unless
CI_OK_ENFORCE=1. --self-check is a lint and exits 1 on any problem.

The same file is used in every repository (canonical copy: nodestro-studio, design note
docs/architecture/ci-fast/ci-ok.md there). Standard library only, Python 3.10+.

Manifest keys read here:
  pull_request               {"<workflow file>": ["<exact job/check-run name>", ...]}  required on every PR
  pull_request_path_filtered {"<workflow file>": [...]}  required only when the workflow's paths filter matches
  path_filters               {"<workflow file>": {"paths": [...]} | {"paths_ignore": [...]}}  copied verbatim from
                             the workflow's on.pull_request filter; --self-check fails when they differ
  advisory                   ["<workflow file>/<job name>", ...]  continue-on-error jobs: reported, never counted

Modes:
  (default)      evaluate once: the workflow_run event payload ($GITHUB_EVENT_PATH), or --repo with --pr and/or
                 --head-sha for a local dry run; the manifest is read from --root (or --manifest-file)
  --self-check   lint the manifest against the workflow files under --root, including ci-ok.yml's workflow_run
                 list (no network); exits 1 on any problem
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
SELF_WORKFLOW = "ci-ok.yml"  # the gate's own workflow; it runs on workflow_run only, never on pull_request
GATE_NAME = "ci-ok"  # the gate's job and check name; any pull_request job of this name is an impostor
SCRIPT_PATH = ".github/scripts/ci_ok.py"
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


def _on_block(text: str) -> tuple[list[str], list[str]] | None:
    """The workflow's `on:` value: (inline trigger names, []) or ([], block lines). None when there is no `on:`."""
    lines = [_strip_comment(line) for line in text.splitlines()]
    for idx, line in enumerate(lines):
        m = re.match(r"^(?:on|\"on\"|'on'|true)\s*:(.*)$", line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline:
            return (_flow_list(inline) if inline.startswith("[") else [_unquote(inline)]), []
        return [], _block(lines, idx, 0)
    return None


def _trigger(text: str, trigger: str) -> tuple[bool, list[str]]:
    """(does the workflow have this trigger, the trigger's block lines)."""
    on = _on_block(text)
    if on is None:
        return False, []
    names, on_block = on
    if names:
        return trigger in names, []
    found = _key_value(on_block, trigger)
    if found is None:
        return False, []
    idx, inline = found
    if inline and inline not in ("{}", "null", "~"):
        raise YamlSubsetError(f"unsupported inline {trigger} value {inline!r}")
    return True, _block(on_block, idx, _indent(on_block[idx]))


def has_trigger(text: str, trigger: str) -> bool:
    return _trigger(text, trigger)[0]


def read_pull_request_filters(text: str) -> dict | None:
    """The workflow's on.pull_request path filters.

    None: the workflow has no pull_request trigger. {}: it runs on every pull request. Otherwise {"paths": [...]} or
    {"paths_ignore": [...]} (the manifest's spelling). Raises YamlSubsetError for shapes this reader does not handle.
    """
    present, pr_block = _trigger(text, "pull_request")
    if not present:
        return None
    result: dict = {}
    for yaml_key, manifest_key in (("paths", "paths"), ("paths-ignore", "paths_ignore")):
        kv = _key_value(pr_block, yaml_key)
        if kv is not None:
            result[manifest_key] = _list_value(pr_block, kv[0], kv[1])
    return result


def read_workflow_run_workflows(text: str) -> list[str] | None:
    """on.workflow_run.workflows of a workflow file (None when absent)."""
    present, block = _trigger(text, "workflow_run")
    if not present:
        return None
    kv = _key_value(block, "workflows")
    return None if kv is None else _list_value(block, kv[0], kv[1])


def workflow_name(text: str, filename: str) -> str:
    """The workflow's top-level `name:`; without one, GitHub shows (and workflow_run matches) the file path."""
    for line in text.splitlines():
        m = re.match(r"^name\s*:(.*)$", _strip_comment(line))
        if m and m.group(1).strip():
            return _unquote(m.group(1))
    return f"{WORKFLOWS_DIR}/{filename}"


_IMPOSTOR_RXS = (
    re.compile(r"^\s+[\"']?" + re.escape(GATE_NAME) + r"[\"']?\s*:\s*$"),  # a job id `ci-ok:`
    re.compile(r"^\s+(?:-\s+)?name\s*:\s*[\"']?" + re.escape(GATE_NAME) + r"[\"']?\s*$"),  # `name: ci-ok`
)


def defines_gate_name(text: str) -> bool:
    """Does this workflow text define a job (or step) called ci-ok? Only the gate's own workflow may."""
    return any(rx.match(_strip_comment(line)) for line in text.splitlines() for rx in _IMPOSTOR_RXS)


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


def listed_workflows(manifest: dict) -> set[str]:
    return set(manifest.get("pull_request", {})) | set(manifest.get("pull_request_path_filtered", {}))


# --------------------------------------------------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}" if status else f"GET {url} failed: {body[:200]}")
        self.status = status


class Api:
    """Tiny read-only REST client. Tests replace it with a fake that implements get(), or inject `opener`.

    Retries 5xx, network errors and rate limits within one overall deadline (default 180 s,
    $CI_OK_API_DEADLINE_SECONDS), so that a short evaluation stays short; after that the error propagates and is
    reported as NOT OK (api error).
    """

    def __init__(
        self,
        token: str,
        base_url: str = API_URL,
        *,
        deadline_seconds: float = 180.0,
        max_attempts: int = 4,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        clock=time.monotonic,
        now=time.time,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.calls = 0
        self.max_attempts = max_attempts
        self._opener, self._sleep, self._clock, self._now = opener, sleep, clock, now
        self.deadline = clock() + deadline_seconds

    def _rate_limit_pause(self, headers) -> float:
        """Retry-After first (secondary rate limits send it), then x-ratelimit-reset, else one minute."""
        retry_after = (headers.get("retry-after") or "").strip()
        if retry_after.isdigit():
            return max(float(retry_after), 1.0)
        reset = (headers.get("x-ratelimit-reset") or "").strip()
        if reset.isdigit():
            return max(float(reset) - self._now(), 1.0)
        return 60.0

    def get(self, path: str, raw: bool = False):
        url = path if path.startswith("http") else self.base_url + path
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-ok",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            try:
                with self._opener(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                    data = resp.read().decode("utf-8")
                    return data if raw else json.loads(data)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                hdrs = exc.headers or {}
                limited = exc.code == 429 or (
                    exc.code == 403 and ("rate limit" in body.lower() or bool(hdrs.get("retry-after")))
                )
                if exc.code < 500 and not limited:
                    raise ApiError(exc.code, url, body) from None
                error = ApiError(exc.code, url, body)
                pause = self._rate_limit_pause(hdrs) if limited else 5.0 * attempt
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error = ApiError(0, url, str(exc))
                pause = 5.0 * attempt
            if attempt >= self.max_attempts or self._clock() + pause > self.deadline:
                raise error
            print(f"::debug::ci-ok: {error}; retrying in {int(pause)} s", flush=True)
            self._sleep(pause)


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


def _file_names(files: list) -> list[str]:
    names = []
    for f in files:
        names.append(f["filename"])
        if f.get("previous_filename"):
            names.append(f["previous_filename"])
    return names


def fetch_changed_files(api, repo: str, pr_number: int, base_sha: str | None, head_sha: str) -> list[str]:
    """Changed files of the PR (renames contribute both names). Uses pulls/{n}/files and falls back to the compare
    API, which needs only contents: read (the ci-ok job has no pull-requests permission)."""
    try:
        files = paged(api, f"/repos/{repo}/pulls/{pr_number}/files", None, limit_pages=30)
    except ApiError as exc:
        if exc.status not in (403, 404) or not base_sha:
            raise
        files = api.get(f"/repos/{repo}/compare/{base_sha}...{head_sha}").get("files", [])
    return _file_names(files)


def fetch_changed_files_without_pr(api, repo: str, default_branch: str, head_sha: str) -> list[str] | None:
    """No PR number (fork PRs): approximate the PR's files by the default branch...head SHA comparison. None when
    the comparison is not possible; path-filtered workflows without a run then count as missing (fail closed)."""
    try:
        data = api.get(f"/repos/{repo}/compare/{urllib.parse.quote(default_branch, safe='')}...{head_sha}")
    except ApiError as exc:
        if exc.status in (404, 422):
            return None
        raise
    return _file_names(data.get("files", []))


@dataclass
class Snapshot:
    runs: dict = field(default_factory=dict)  # workflow file -> chosen run (dict)
    jobs: dict = field(default_factory=dict)  # (workflow file, job name) -> [job, ...] (several when names repeat)
    notes: dict = field(default_factory=dict)  # workflow file -> note about how its run was chosen
    ignored_runs: int = 0  # runs on this SHA that belong to other pull requests


def run_rank(run: dict, pr_number: int | None) -> int | None:
    """How well a run belongs to the evaluated PR: 0 listed (or no PR known), 1 lists no PR at all, None another PR."""
    if pr_number is None:
        return 0
    numbers = {p.get("number") for p in run.get("pull_requests") or []}
    if pr_number in numbers:
        return 0
    return None if numbers else 1


class Collector:
    """Reads the pull_request runs and their jobs for one head SHA (and, when known, one PR number)."""

    def __init__(self, api, repo: str, head_sha: str, pr_number: int | None = None):
        self.api, self.repo, self.head_sha, self.pr_number = api, repo, head_sha, pr_number

    def snapshot(self) -> Snapshot:
        runs = paged(
            self.api,
            f"/repos/{self.repo}/actions/runs?head_sha={self.head_sha}&event=pull_request",
            "workflow_runs",
            limit_pages=10,
        )
        best: dict = {}  # workflow -> (rank, created_at, id, run)
        ignored = 0
        for run in runs:
            if run.get("event") != "pull_request" or run.get("head_sha") != self.head_sha:
                continue
            path = run.get("path") or ""
            if not path.startswith(WORKFLOWS_DIR + "/"):
                continue
            workflow = path[len(WORKFLOWS_DIR) + 1 :].split("@", 1)[0]
            rank = run_rank(run, self.pr_number)
            if rank is None:
                # F5: a run of another PR with the same head SHA tested another merge commit; it never decides here.
                ignored += 1
                continue
            # A run attributed to this PR beats an unattributed one; among equals the newest wins (close/reopen).
            key = (-rank, run["created_at"], run["id"])
            if workflow not in best or key > best[workflow][0]:
                best[workflow] = (key, run)
        snap = Snapshot(ignored_runs=ignored)
        for workflow, (key, run) in best.items():
            snap.runs[workflow] = run
            if key[0] == -1:
                snap.notes[workflow] = f"run lists no pull request; taken for PR #{self.pr_number} by head SHA only"
            # filter=latest: the jobs of the most recent attempt, so a re-run replaces the failed attempt.
            jobs = paged(self.api, f"/repos/{self.repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
            for job in sorted(jobs, key=lambda j: j["id"]):
                snap.jobs.setdefault((workflow, job["name"]), []).append(job)
        return snap


# --------------------------------------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Row:
    workflow: str
    job: str
    state: str  # success | pending | <conclusion> | missing | not triggered (paths) | impostor | api error | ...
    counts: bool  # does this row decide the verdict?
    ok: bool | None  # True ok, False NOT ok, None undecided (pending)
    url: str = ""
    note: str = ""


@dataclass
class Evaluation:
    rows: list
    unlisted: list
    verdict: str  # OK | NOT OK | PENDING


def judge_jobs(jobs: list) -> tuple[str, bool | None, str, str]:
    """(state, ok, url, note) for all jobs of one name in the newest attempt. F6: every one of them must succeed."""
    note = f"{len(jobs)} jobs share this name; all must succeed" if len(jobs) > 1 else ""
    unfinished = [j for j in jobs if j.get("status") != "completed"]  # queued, in_progress, waiting, ...
    if unfinished:
        return "pending", None, unfinished[0].get("html_url", ""), note
    bad = [j for j in jobs if (j.get("conclusion") or "unknown") not in OK_CONCLUSIONS]
    if bad:
        return bad[0].get("conclusion") or "unknown", False, bad[0].get("html_url", ""), note
    return "success", True, jobs[0].get("html_url", ""), note


def _join(*notes: str) -> str:
    return "; ".join(n for n in notes if n)


def evaluate(manifest: dict, snap: Snapshot, changed_files: list[str] | None, problems=()) -> Evaluation:
    """Turn one snapshot into rows and a verdict. No waiting: anything unfinished is pending.

    changed_files None: unknown, so a path-filtered workflow without a run counts as missing (fail closed).
    problems: manifest problems (--self-check of the default branch); each one is a counted NOT ok row.
    """
    filters = manifest.get("path_filters", {})
    rows: list[Row] = []
    listed = set()
    for exp in expected_jobs(manifest):
        listed.add((exp.workflow, exp.job))
        counts = not exp.advisory
        run = snap.runs.get(exp.workflow)
        jobs = snap.jobs.get((exp.workflow, exp.job))
        note = _join(
            "advisory (continue-on-error), not counted" if exp.advisory else "", snap.notes.get(exp.workflow, "")
        )
        if exp.path_filtered and run is None and not jobs:
            wf_filters = filters.get(exp.workflow)
            if wf_filters is None:
                # F8: without its filter nobody can tell whether the workflow had to run: fail closed.
                rows.append(
                    Row(
                        exp.workflow,
                        exp.job,
                        "not run (path-filtered, no filter in manifest)",
                        counts,
                        False,
                        note=_join(note, "add path_filters for this workflow"),
                    )
                )
                continue
            if changed_files is not None and not workflow_triggered(wf_filters, changed_files):
                rows.append(Row(exp.workflow, exp.job, "not triggered (paths)", counts, True, note=note))
                continue
        if jobs:
            state, ok, url, job_note = judge_jobs(jobs)
            rows.append(Row(exp.workflow, exp.job, state, counts, ok, url, _join(note, job_note)))
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
        why = "no pull_request run of this workflow on the head SHA"
        if exp.path_filtered and changed_files is None:
            why += " (changed files unknown, so its paths filter cannot excuse it)"
        rows.append(Row(exp.workflow, exp.job, "missing", counts, False, note=note or why))

    for problem in problems:
        rows.append(Row(MANIFEST_PATH, "self-check", "inconsistent", True, False, note=problem))

    unlisted = []
    for (workflow, name), jobs in sorted(snap.jobs.items()):
        if name == GATE_NAME:
            # F1/F13: the real gate runs on workflow_run only, so a pull_request job named ci-ok is an impostor that
            # a required check named ci-ok could pick up. Always counted, always NOT ok.
            for j in jobs:
                rows.append(
                    Row(
                        workflow,
                        name,
                        "impostor",
                        True,
                        False,
                        j.get("html_url", ""),
                        "a pull_request job named ci-ok; the real gate runs on workflow_run only",
                    )
                )
            continue
        if (workflow, name) in listed:
            continue
        for j in jobs:
            state = j.get("conclusion") or j.get("status") or "unknown"
            unlisted.append(Row(workflow, name, state, False, None, j.get("html_url", ""), "unlisted"))

    counting = [r for r in rows if r.counts]
    if not counting:
        # F8: an empty (or all-advisory) manifest must not turn the gate into a silent OK.
        rows.append(Row(MANIFEST_PATH, "-", "no mandatory jobs", True, False, note="the manifest lists no counted job"))
        counting = rows[-1:]
    if any(r.ok is False for r in counting):
        verdict = "NOT OK"
    elif any(r.ok is None for r in counting):
        verdict = "PENDING"
    else:
        verdict = "OK"
    return Evaluation(rows, unlisted, verdict)


def error_evaluation(what: str, exc: BaseException | str) -> Evaluation:
    """F7: an error is a NOT OK verdict in the report, never a crash."""
    row = Row("-", "-", what, True, False, note=str(exc).replace("\n", " ")[:300])
    return Evaluation([row], [], "NOT OK")


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ci_span_minutes(snap: Snapshot) -> float | None:
    """Minutes from the first pull_request run's creation to the last job's completion on this head SHA."""
    starts = [_ts(r.get("created_at")) for r in snap.runs.values()]
    ends = [_ts(j.get("completed_at")) for jobs in snap.jobs.values() for j in jobs]
    starts, ends = [s for s in starts if s], [e for e in ends if e]
    if not starts or not ends:
        return None
    return round((max(ends) - min(starts)) / 60, 1)


def verdict_line(ev: Evaluation, label: str = "") -> str:
    counting = [r for r in ev.rows if r.counts]
    good = sum(1 for r in counting if r.ok)
    where = f" for {label}" if label else ""
    extra = f"; {len(ev.unlisted)} unlisted job(s)" if ev.unlisted else ""
    if ev.verdict == "OK":
        return (
            f"ci-ok{where}: OK ({good}/{len(counting)} mandatory jobs passed or were not triggered by their paths "
            f"filter{extra})"
        )
    bad = [f"{r.workflow}/{r.job}={r.state}" for r in counting if r.ok is not True]
    return f"ci-ok{where}: {ev.verdict} ({good}/{len(counting)} ok; not ok or unfinished: {', '.join(bad)}{extra})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(ev: Evaluation, notices: list[str], header: str, label: str) -> str:
    icon = {True: "pass", False: "FAIL", None: "pending"}
    lines = [f"## ci-ok (report-only): {ev.verdict} for {label}", "", header, ""]
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
    lines += ["", verdict_line(ev, label), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------------
# Self-check: manifest vs. workflow files (no network)
# --------------------------------------------------------------------------------------------------------------------


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} is not a JSON object")
    return manifest


def self_check(root: str, manifest: dict | None = None) -> list[str]:
    """Problems between the manifest and the workflow files under `root` (empty list = consistent)."""
    problems: list[str] = []
    if manifest is None:
        try:
            manifest = load_manifest(os.path.join(root, MANIFEST_PATH))
        except (OSError, ValueError) as exc:
            return [f"cannot read {MANIFEST_PATH}: {exc}"]
    wf_dir = os.path.join(root, WORKFLOWS_DIR)
    filters = manifest.get("path_filters", {})
    path_filtered = manifest.get("pull_request_path_filtered", {})
    listed_wfs = listed_workflows(manifest)
    if not any(jobs for jobs in [*manifest.get("pull_request", {}).values(), *path_filtered.values()]):
        problems.append("the manifest lists no mandatory pull_request job (an empty gate is always OK)")
    for name in sorted(path_filtered):
        if name not in filters:
            problems.append(f"{name} is path-filtered but has no path_filters entry (ci-ok could not tell if it ran)")
    texts: dict = {}
    file_filters: dict = {}
    try:
        names = sorted(os.listdir(wf_dir))
    except OSError as exc:
        return [*problems, f"cannot list {WORKFLOWS_DIR}: {exc}"]
    for name in names:
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(wf_dir, name), encoding="utf-8") as fh:
            texts[name] = fh.read()
        if name == SELF_WORKFLOW:
            continue
        if defines_gate_name(texts[name]):
            problems.append(f"{name} defines a job or step named {GATE_NAME!r}; only {SELF_WORKFLOW} may (impostor)")
        try:
            file_filters[name] = read_pull_request_filters(texts[name])
        except YamlSubsetError as exc:
            problems.append(f"{name}: cannot read on.pull_request ({exc})")
    for name, wf_filter in file_filters.items():
        if wf_filter is not None and name not in listed_wfs:
            problems.append(f"{name} runs on pull_request but is not in the manifest")
    for name in sorted(listed_wfs):
        if name not in file_filters:
            problems.append(f"{name} is in the manifest but {WORKFLOWS_DIR}/{name} does not exist (or is unreadable)")
            continue
        wf_filter = file_filters[name]
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
        if name in path_filtered and not wf_filter:
            problems.append(f"{name} is listed as path-filtered but its workflow has no paths filter")
    for name in sorted(set(filters) - listed_wfs):
        problems.append(f"path_filters has {name}, which is not a listed workflow")
    for ref in manifest.get("advisory", []):
        try:
            wf, job = split_job_ref(ref)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        all_jobs = manifest.get("pull_request", {}).get(wf, []) + path_filtered.get(wf, [])
        if job not in all_jobs:
            problems.append(f"advisory {ref!r} is not a listed job")
        elif wf in texts and "continue-on-error:" not in texts[wf]:
            problems.append(f"advisory {ref!r}: {wf} has no continue-on-error at all")
    problems += _check_gate_workflow(texts, listed_wfs)
    return problems


def _check_gate_workflow(texts: dict, listed_wfs: set[str]) -> list[str]:
    """ci-ok.yml must run on workflow_run of exactly the listed workflows (by their `name:`), never on a PR event."""
    text = texts.get(SELF_WORKFLOW)
    if text is None:
        return [f"{WORKFLOWS_DIR}/{SELF_WORKFLOW} does not exist"]
    problems = []
    try:
        for trigger in ("pull_request", "pull_request_target"):
            if has_trigger(text, trigger):
                problems.append(f"{SELF_WORKFLOW} runs on {trigger}: the pull request would control its own gate")
        workflows = read_workflow_run_workflows(text)
    except YamlSubsetError as exc:
        return [*problems, f"{SELF_WORKFLOW}: cannot read its on: block ({exc})"]
    if workflows is None:
        return [*problems, f"{SELF_WORKFLOW} has no on.workflow_run.workflows list"]
    expected = {workflow_name(texts[n], n) for n in listed_wfs if n in texts}
    missing, extra = sorted(expected - set(workflows)), sorted(set(workflows) - expected)
    if missing:
        problems.append(f"{SELF_WORKFLOW} workflow_run.workflows lacks {missing} (names of listed workflows)")
    if extra:
        problems.append(f"{SELF_WORKFLOW} workflow_run.workflows has {extra}, which name no listed workflow")
    names = [workflow_name(t, n) for n, t in texts.items() if n != SELF_WORKFLOW]
    dupes = sorted({n for n in names if names.count(n) > 1} & expected)
    if dupes:
        problems.append(f"several workflow files share the name {dupes}; workflow_run cannot tell them apart")
    return problems


# --------------------------------------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Target:
    head_sha: str
    pr_number: int | None = None
    base_sha: str | None = None
    notices: list = field(default_factory=list)
    attribute: bool = True  # count only runs whose pull_requests list pr_number (F5)

    @property
    def label(self) -> str:
        pr = f"PR #{self.pr_number}" if self.pr_number else "head SHA (no PR number)"
        return f"{pr} @ {self.head_sha[:12]}"


def _write_summary(env: dict, text: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="dry run: PR number (head and base SHA are read from the API)")
    p.add_argument("--head-sha", help="dry run: evaluate this head SHA (with --pr: instead of the PR's current head)")
    p.add_argument("--manifest-file", help="use this manifest instead of <root>/.github/ci-manifest.json (dry runs)")
    p.add_argument("--once", "--no-wait", dest="once", action="store_true", help="accepted for v1 compatibility")
    p.add_argument("--self-check", action="store_true", help="lint manifest and workflow files under --root")
    p.add_argument("--root", default=".", help="tree with .github (in CI: the default branch checkout)")
    p.add_argument("--json", help="also write the evaluations as JSON to this file")
    return p.parse_args(argv)


def targets_from(args, event: dict, api, repo: str) -> tuple[list[Target], str]:
    """What to evaluate, plus a reason when there is nothing to evaluate."""
    if args.pr or args.head_sha:
        if args.pr:
            pr = api.get(f"/repos/{repo}/pulls/{args.pr}")
            target = Target(args.head_sha or pr["head"]["sha"], args.pr, pr["base"]["sha"])
            if pr.get("state") != "open":
                # GitHub fills a run's pull_requests from the head branch's currently OPEN pull requests, so the
                # runs of a closed PR list another PR or none: attribute by head SHA alone in this dry run.
                target.attribute = False
                target.notices.append(
                    f"PR #{args.pr} is {pr.get('state')}: its runs no longer list it, so runs of any PR on this "
                    "head SHA count (dry run)."
                )
            return [target], ""
        return [Target(args.head_sha, notices=["Dry run by head SHA alone: runs of any PR on this SHA count."])], ""
    wr = event.get("workflow_run")
    if not wr:
        return [], "no workflow_run event payload and no --pr/--head-sha"
    if wr.get("event") != "pull_request":
        return [], f"the triggering run's event is {wr.get('event')!r}, not pull_request"
    head = wr["head_sha"]
    prs = [p for p in wr.get("pull_requests") or [] if p.get("number")]
    if not prs:
        return [
            Target(
                head,
                notices=[
                    "The triggering run lists no pull request (typical for a fork PR). Evaluated by head SHA alone: "
                    "pull_request runs of ANY PR on this SHA count, and changed files are approximated by comparing "
                    "the default branch with the head SHA."
                ],
            )
        ], ""
    return [Target(head, p["number"], (p.get("base") or {}).get("sha")) for p in prs], ""


def evaluate_target(api, repo: str, manifest: dict, problems: list[str], target: Target, default_branch) -> dict:
    notices = list(target.notices)
    if target.pr_number:
        changed = fetch_changed_files(api, repo, target.pr_number, target.base_sha, target.head_sha)
    else:
        changed = fetch_changed_files_without_pr(api, repo, default_branch(), target.head_sha)
        if changed is None:
            notices.append("Changed files unknown: path-filtered workflows without a run count as missing.")
    for path in (MANIFEST_PATH, SCRIPT_PATH, f"{WORKFLOWS_DIR}/{SELF_WORKFLOW}"):
        if changed and path in changed:
            notices.append(
                f"This PR changes {path}. ci-ok always runs the default branch's copy; the change takes effect "
                "after merge. A changed gate needs a reason in the PR description."
            )
    snap = Collector(api, repo, target.head_sha, target.pr_number if target.attribute else None).snapshot()
    if snap.ignored_runs:
        notices.append(f"{snap.ignored_runs} run(s) on this SHA belong to other pull requests and were ignored.")
    ev = evaluate(manifest, snap, changed, problems)
    files = "unknown" if changed is None else str(len(changed))
    return {"ev": ev, "notices": notices, "files": files, "span": ci_span_minutes(snap)}


def main(argv=None, env=None, api=None) -> int:
    args = parse_args(argv)
    env = dict(os.environ) if env is None else env
    enforce = env.get("CI_OK_ENFORCE") == "1"

    if args.self_check:
        problems = self_check(args.root)
        for p in problems:
            print(f"::error title=ci-ok self-check::{p}")
        print(f"ci-ok self-check: {'consistent' if not problems else f'{len(problems)} problem(s)'}")
        if problems:
            _write_summary(env, "### ci-ok self-check\n\n" + "\n".join(f"- {p}" for p in problems) + "\n")
        # F8: a lint, not a verdict: it fails on any problem, report-only or not.
        return 1 if problems else 0

    results: list[tuple[Target | None, dict]] = []
    event: dict = {}
    repo = args.repo or env.get("GITHUB_REPOSITORY") or ""
    targets: list[Target] = []
    try:
        if env.get("GITHUB_EVENT_PATH") and os.path.exists(env["GITHUB_EVENT_PATH"]):
            with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
                event = json.load(fh)
        if not repo:
            raise ValueError("no repository (pass --repo or set GITHUB_REPOSITORY)")
        if api is None:
            deadline = float(env.get("CI_OK_API_DEADLINE_SECONDS") or 180)
            api = Api(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "", deadline_seconds=deadline)
        targets, reason = targets_from(args, event, api, repo)
        if not targets:
            print(f"::notice title=ci-ok::Nothing to evaluate: {reason}.")
            return 1 if enforce and not event.get("workflow_run") else 0
    except Exception as exc:  # report-only: never crash (F7)
        results.append((None, {"ev": error_evaluation("error", exc), "notices": [], "files": "?", "span": None}))

    manifest, problems = None, []
    if targets:
        try:
            if args.manifest_file:
                manifest = load_manifest(args.manifest_file)
            else:
                manifest = load_manifest(os.path.join(args.root, MANIFEST_PATH))
            if os.path.isdir(os.path.join(args.root, WORKFLOWS_DIR)):
                problems = self_check(args.root, manifest)
        except (OSError, ValueError) as exc:
            manifest = None
            err = error_evaluation("no manifest", exc)
            results += [(t, {"ev": err, "notices": [], "files": "?", "span": None}) for t in targets]

    cache: dict = {}

    def default_branch() -> str:
        if "b" not in cache:
            cache["b"] = (event.get("repository") or {}).get("default_branch") or api.get(f"/repos/{repo}")[
                "default_branch"
            ]
        return cache["b"]

    if manifest is not None:
        for target in targets:
            try:
                result = evaluate_target(api, repo, manifest, problems, target, default_branch)
            except Exception as exc:  # F7: API errors (after retries) and anything unexpected are NOT OK, not a crash
                result = {
                    "ev": error_evaluation("api error", exc),
                    "notices": target.notices,
                    "files": "?",
                    "span": None,
                }
            results.append((target, result))

    if args.manifest_file:
        for _, r in results:
            r["notices"].insert(0, f"Dry run: manifest from the local file {args.manifest_file}.")
    wr = event.get("workflow_run") or {}
    trigger = (
        f"triggered by {wr.get('name')!r} run {wr.get('id')} attempt {wr.get('run_attempt')} ({wr.get('conclusion')})"
        if wr
        else "local dry run"
    )
    report_json = []
    for target, r in results:
        ev, label = r["ev"], target.label if target else "this event"
        header = (
            f"{label}, {r['files']} changed file(s), {getattr(api, 'calls', '?')} API call(s) so far, "
            f"CI span on this head {r['span']} min, {trigger}. The latest completed ci-ok run for a head SHA is "
            "its verdict. Mode: report-only (CI_OK_ENFORCE=1 makes NOT OK fail this job)."
        )
        report = render_markdown(ev, r["notices"], header, label)
        print(report)
        _write_summary(env, report)
        level = "notice" if ev.verdict in ("OK", "PENDING") else "warning"
        print(f"::{level} title=ci-ok::{verdict_line(ev, label)}")
        report_json.append(
            {
                "target": label,
                "verdict": ev.verdict,
                "ci_span_minutes": r["span"],
                "rows": [x.__dict__ for x in ev.rows],
                "unlisted": [x.__dict__ for x in ev.unlisted],
                "notices": r["notices"],
            }
        )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report_json, fh, indent=2)
    return 1 if enforce and any(r["ev"].verdict != "OK" for _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
