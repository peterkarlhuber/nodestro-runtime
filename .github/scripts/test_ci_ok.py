"""Unit tests for ci_ok.py (CI-FAST W1, v2). Standard library only, no network: the HTTP layer is a fake.

Run: python3 -I -m unittest discover -s .github/scripts -p test_ci_ok.py   (or: python3 .github/scripts/test_ci_ok.py).
`python3 -m unittest .github/scripts/test_ci_ok.py` does not work: unittest turns the path into the module name
".github.scripts.test_ci_ok", and the leading dot is an empty package name.
"""

from __future__ import annotations

import email.message
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_ok

REPO = "o/r"
HEAD = "h" * 40
BASE = "b" * 40


class FakeApi:
    """Answers GET paths from a dict; values may be callables or ApiError instances (raised)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, path: str, raw: bool = False):
        self.calls.append(path)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                value = self.routes[prefix]
                value = value() if callable(value) else value
                if isinstance(value, ci_ok.ApiError):
                    raise value
                return json.dumps(value) if raw else value
        raise ci_ok.ApiError(404, path)


def run(
    rid,
    workflow,
    *,
    status="completed",
    conclusion="success",
    created="2026-11-07T10:00:00Z",
    attempt=1,
    event="pull_request",
    head=HEAD,
    updated="2026-11-07T10:30:00Z",
    prs=(7,),
    branch="feature",
    repo_id=1,
    pr_heads=None,
):
    """pr_heads: {number: (head ref, head repo id)} for listed PRs; without it a listed PR carries no head."""
    pr_heads = pr_heads or {}
    return {
        "id": rid,
        "path": f".github/workflows/{workflow}",
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": updated,
        "run_attempt": attempt,
        "event": event,
        "head_sha": head,
        "html_url": f"https://x/runs/{rid}",
        "head_branch": branch,
        "head_repository": {"id": repo_id},
        "pull_requests": [
            {
                "number": n,
                **({"head": {"ref": pr_heads[n][0], "repo": {"id": pr_heads[n][1]}}} if n in pr_heads else {}),
            }
            for n in prs
        ],
    }


def job(jid, name, *, status="completed", conclusion="success", completed="2026-11-07T10:20:00Z"):
    return {
        "id": jid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completed_at": completed,
        "html_url": f"https://x/jobs/{jid}",
    }


def routes_for(runs: list, jobs_by_run: dict, files=("src/a.py",)) -> dict:
    r = {
        f"/repos/{REPO}/actions/runs?head_sha={HEAD}": {"workflow_runs": runs},
        f"/repos/{REPO}/pulls/7/files": [{"filename": f} for f in files],
        f"/repos/{REPO}/pulls/8/files": [{"filename": f} for f in files],
        f"/repos/{REPO}/pulls/7": {"number": 7, "state": "open", "head": {"sha": HEAD}, "base": {"sha": BASE}},
        f"/repos/{REPO}/compare/master...{HEAD}": {"files": [{"filename": f} for f in files]},
    }
    for rid, jobs in jobs_by_run.items():
        r[f"/repos/{REPO}/actions/runs/{rid}/jobs"] = {"jobs": jobs}
    return r


MANIFEST = {
    "version": 1,
    "pull_request": {"ci.yml": ["lint", "test (3.11)", "engines (webkit)"], "docker.yml": ["docker"]},
    "path_filters": {"docker.yml": {"paths": ["docker/**", ".github/workflows/docker.yml"]}},
    "advisory": ["ci.yml/engines (webkit)"],
}
GREEN = [job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")]
LINT_TEST = [job(1, "lint"), job(2, "test (3.11)")]


def snapshot(routes: dict, pr=7, head_ref=None, head_repo_id=None) -> ci_ok.Snapshot:
    return ci_ok.Collector(FakeApi(routes), REPO, HEAD, pr, head_ref, head_repo_id).snapshot()


def evaluate(routes, manifest=MANIFEST, files=("src/a.py",), pr=7, problems=(), head_ref=None, head_repo_id=None):
    snap = snapshot(routes, pr, head_ref, head_repo_id)
    return ci_ok.evaluate(manifest, snap, None if files is None else list(files), problems)


def states(ev):
    return {(r.workflow, r.job): r.state for r in ev.rows}


class GlobSemantics(unittest.TestCase):
    def m(self, pattern, path):
        return ci_ok.matches_filter([pattern], path)

    def test_single_star_does_not_cross_slash(self):
        self.assertTrue(self.m("*.py", "a.py"))
        self.assertFalse(self.m("*.py", "dir/a.py"))
        self.assertTrue(self.m("docs/*", "docs/a.md"))
        self.assertFalse(self.m("docs/*", "docs/sub/a.md"))

    def test_double_star_crosses_slash(self):
        self.assertTrue(self.m("frontend/**", "frontend/src/app/x.ts"))
        self.assertTrue(self.m("**.js", "a/b/c.js"))
        self.assertTrue(self.m("**/README.md", "README.md"))
        self.assertTrue(self.m("**/README.md", "a/b/README.md"))
        self.assertFalse(self.m("frontend/**", "frontend2/x.ts"))
        self.assertTrue(self.m("docs/**/*.md", "docs/a.md"))
        self.assertTrue(self.m("docs/**/*.md", "docs/x/y/a.md"))

    def test_literal_paths_and_dots(self):
        self.assertTrue(self.m(".github/workflows/docker.yml", ".github/workflows/docker.yml"))
        self.assertFalse(self.m("pyproject.toml", "pyprojectXtoml"))
        self.assertFalse(self.m("nodestro", "nodestro/x"))
        self.assertTrue(self.m("nodestro", "nodestro"))

    def test_question_mark_and_plus_quantify_the_preceding_character(self):
        self.assertTrue(self.m("*.jsx?", "a.js"))
        self.assertTrue(self.m("*.jsx?", "a.jsx"))
        self.assertFalse(self.m("*.jsx?", "a.jsxx"))
        self.assertTrue(self.m("*.js+", "a.jsss"))
        self.assertFalse(self.m("*.js+", "a.j"))

    def test_character_class(self):
        self.assertTrue(self.m("v[12].txt", "v1.txt"))
        self.assertFalse(self.m("v[12].txt", "v3.txt"))

    def test_negation_last_match_wins(self):
        pats = ["sub/**", "!sub/docs/**", "sub/docs/keep.md"]
        self.assertTrue(ci_ok.matches_filter(pats, "sub/a.py"))
        self.assertFalse(ci_ok.matches_filter(pats, "sub/docs/a.md"))
        self.assertTrue(ci_ok.matches_filter(pats, "sub/docs/keep.md"))

    def test_workflow_triggered_paths_and_paths_ignore(self):
        f = {"paths": ["frontend/**"]}
        self.assertTrue(ci_ok.workflow_triggered(f, ["README.md", "frontend/a.ts"]))
        self.assertFalse(ci_ok.workflow_triggered(f, ["README.md"]))
        self.assertFalse(ci_ok.workflow_triggered(f, []))
        g = {"paths_ignore": ["docs/**", "*.md"]}
        self.assertFalse(ci_ok.workflow_triggered(g, ["docs/x.md", "README.md"]))
        self.assertTrue(ci_ok.workflow_triggered(g, ["docs/x.md", "src/a.py"]))
        self.assertTrue(ci_ok.workflow_triggered({}, []))


class YamlReader(unittest.TestCase):
    def test_block_lists_with_quotes_and_comments(self):
        text = (
            "name: X\non:\n  pull_request:\n    paths:\n      - \"a/**\"   # why\n      - 'b.txt'\n      - c\n"
            "  push:\n    branches: [master]\n    paths:\n      - nope\njobs:\n  x: {}\n"
        )
        self.assertEqual(ci_ok.read_pull_request_filters(text), {"paths": ["a/**", "b.txt", "c"]})

    def test_flow_list_and_paths_ignore(self):
        text = 'on:\n  push:\n    paths: ["x"]\n  pull_request:\n    paths-ignore: ["docs/**", "*.md"]\n'
        self.assertEqual(ci_ok.read_pull_request_filters(text), {"paths_ignore": ["docs/**", "*.md"]})

    def test_sequence_at_key_indent(self):
        text = "on:\n  pull_request:\n    paths:\n    - a\n    - b\n    types: [opened]\n"
        self.assertEqual(ci_ok.read_pull_request_filters(text), {"paths": ["a", "b"]})

    def test_unfiltered_and_absent_triggers(self):
        self.assertEqual(ci_ok.read_pull_request_filters("on:\n  pull_request:\n  push:\n"), {})
        self.assertEqual(ci_ok.read_pull_request_filters("on: [push, pull_request]\n"), {})
        self.assertEqual(ci_ok.read_pull_request_filters("on: pull_request\n"), {})
        self.assertIsNone(ci_ok.read_pull_request_filters("on:\n  push:\n  workflow_dispatch:\n"))
        self.assertIsNone(ci_ok.read_pull_request_filters("on:\n  pull_request_target:\n"))


class GateWorkflowReader(unittest.TestCase):
    def test_workflow_run_list_block_and_flow(self):
        flow = "name: ci-ok\non:\n  workflow_run:\n    workflows: [CI, macOS frontend]\n    types: [completed]\n"
        self.assertEqual(ci_ok.read_workflow_run_workflows(flow), ["CI", "macOS frontend"])
        block = 'on:\n  workflow_run:\n    types: [completed]\n    workflows:\n      - "CI"\n      - Docker  # x\n'
        self.assertEqual(ci_ok.read_workflow_run_workflows(block), ["CI", "Docker"])
        self.assertIsNone(ci_ok.read_workflow_run_workflows("on:\n  pull_request:\n"))

    def test_has_trigger(self):
        self.assertTrue(ci_ok.has_trigger("on: [push, pull_request_target]\n", "pull_request_target"))
        self.assertFalse(ci_ok.has_trigger("on:\n  workflow_run:\n    workflows: [CI]\n", "pull_request"))

    def test_workflow_name_defaults_to_the_path(self):
        self.assertEqual(ci_ok.workflow_name('name: "macOS frontend"  # c\non: push\n', "m.yml"), "macOS frontend")
        self.assertEqual(
            ci_ok.workflow_name("on: push\njobs:\n  a:\n    name: x\n", "m.yml"), ".github/workflows/m.yml"
        )

    def test_gate_name_detection(self):
        self.assertTrue(ci_ok.defines_gate_name("jobs:\n  ci-ok:\n    runs-on: x\n"))
        self.assertTrue(ci_ok.defines_gate_name("jobs:\n  fake:\n    name: 'ci-ok'\n"))
        self.assertFalse(ci_ok.defines_gate_name("jobs:\n  lint:\n    name: ci-ok-lint\n  # ci-ok:\n"))


class Verdicts(unittest.TestCase):
    def base_routes(self, ci_jobs, ci_run=None, extra_runs=(), extra_jobs=None):
        runs = [ci_run or run(1, "ci.yml"), *extra_runs]
        jobs = {1: ci_jobs, **(extra_jobs or {})}
        return routes_for(runs, jobs)

    def test_all_success_is_ok_and_docker_not_triggered(self):
        ev = evaluate(self.base_routes(GREEN))
        self.assertEqual(ev.verdict, "OK")
        self.assertEqual(states(ev)[("docker.yml", "docker")], "not triggered (paths)")

    def test_path_filtered_workflow_that_should_have_run_is_missing(self):
        ev = evaluate(self.base_routes(GREEN), files=["docker/Dockerfile"])
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertEqual(states(ev)[("docker.yml", "docker")], "missing")

    def test_path_filtered_workflow_with_unknown_files_fails_closed(self):
        ev = evaluate(self.base_routes(GREEN), files=None)
        self.assertEqual(states(ev)[("docker.yml", "docker")], "missing")
        self.assertIn("changed files unknown", ev.rows[-1].note)

    def test_path_filtered_workflow_that_ran_anyway_is_judged(self):
        routes = self.base_routes(
            GREEN, extra_runs=[run(2, "docker.yml")], extra_jobs={2: [job(9, "docker", conclusion="failure")]}
        )
        ev = evaluate(routes)
        self.assertEqual(states(ev)[("docker.yml", "docker")], "failure")
        self.assertEqual(ev.verdict, "NOT OK")

    def test_missing_job_in_a_finished_run(self):
        ev = evaluate(self.base_routes([job(1, "lint"), job(3, "engines (webkit)")]))
        self.assertEqual(states(ev)[("ci.yml", "test (3.11)")], "missing")
        self.assertEqual(ev.verdict, "NOT OK")

    def test_missing_job_in_a_cancelled_or_unapproved_run(self):
        ev = evaluate(self.base_routes([], ci_run=run(1, "ci.yml", conclusion="action_required")))
        self.assertEqual(states(ev)[("ci.yml", "lint")], "missing (run action_required)")
        self.assertEqual(ev.verdict, "NOT OK")

    def test_workflow_without_any_run_is_missing_at_once(self):
        ev = evaluate(routes_for([], {}))
        self.assertEqual(states(ev)[("ci.yml", "lint")], "missing")
        self.assertEqual(ev.verdict, "NOT OK")

    def test_skipped_cancelled_neutral_timed_out_are_not_ok(self):
        for conclusion in ("skipped", "cancelled", "neutral", "timed_out", "action_required", "stale", "failure"):
            with self.subTest(conclusion=conclusion):
                ev = evaluate(
                    self.base_routes(
                        [job(1, "lint", conclusion=conclusion), job(2, "test (3.11)"), job(3, "engines (webkit)")]
                    )
                )
                self.assertEqual(ev.verdict, "NOT OK")
                self.assertEqual(states(ev)[("ci.yml", "lint")], conclusion)

    def test_names_must_match_exactly(self):
        ev = evaluate(self.base_routes([job(1, "Lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")]))
        self.assertEqual(states(ev)[("ci.yml", "lint")], "missing")
        self.assertEqual([(r.workflow, r.job) for r in ev.unlisted], [("ci.yml", "Lint")])

    def test_same_job_name_in_another_workflow_does_not_count(self):
        routes = routes_for(
            [run(1, "ci.yml"), run(2, "other.yml")],
            {1: [job(2, "test (3.11)"), job(3, "engines (webkit)")], 2: [job(4, "lint")]},
        )
        ev = evaluate(routes)
        self.assertEqual(states(ev)[("ci.yml", "lint")], "missing")
        self.assertIn(("other.yml", "lint"), [(r.workflow, r.job) for r in ev.unlisted])

    def test_runs_of_other_shas_and_events_are_ignored(self):
        routes = routes_for(
            [run(1, "ci.yml", head="x" * 40), run(2, "ci.yml", event="push")],
            {1: [job(1, "lint")], 2: [job(2, "lint")]},
        )
        self.assertEqual(states(evaluate(routes))[("ci.yml", "lint")], "missing")

    def test_advisory_failure_does_not_count(self):
        ev = evaluate(
            self.base_routes([job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)", conclusion="failure")])
        )
        self.assertEqual(ev.verdict, "OK")
        row = next(r for r in ev.rows if r.job == "engines (webkit)")
        self.assertFalse(row.counts)
        self.assertFalse(row.ok)

    def test_unlisted_job_is_a_warning_not_a_failure(self):
        ev = evaluate(self.base_routes([*GREEN, job(4, "brand-new-job", conclusion="failure")]))
        self.assertEqual(ev.verdict, "OK")
        self.assertEqual([r.job for r in ev.unlisted], ["brand-new-job"])

    def test_rerun_uses_the_latest_attempt(self):
        # The jobs endpoint is asked with filter=latest; it returns the re-run attempt's jobs.
        routes = self.base_routes(
            [job(11, "lint"), job(12, "test (3.11)"), job(13, "engines (webkit)")], ci_run=run(1, "ci.yml", attempt=2)
        )
        api = FakeApi(routes)
        snap = ci_ok.Collector(api, REPO, HEAD, 7).snapshot()
        self.assertTrue(any("filter=latest" in c for c in api.calls))
        self.assertEqual(ci_ok.evaluate(MANIFEST, snap, ["a"]).verdict, "OK")

    def test_newest_run_of_a_workflow_supersedes_older_runs(self):
        routes = routes_for(
            [run(1, "ci.yml", created="2026-11-07T10:00:00Z"), run(2, "ci.yml", created="2026-11-07T11:00:00Z")],
            {1: [job(1, "lint", conclusion="failure")], 2: [job(2, "lint"), job(3, "test (3.11)")]},
        )
        self.assertEqual(evaluate(routes).verdict, "OK")

    def test_unfinished_is_pending_without_waiting(self):
        routes = self.base_routes(
            [job(1, "lint", status="in_progress", conclusion=None), job(2, "test (3.11)"), job(3, "engines (webkit)")],
            ci_run=run(1, "ci.yml", status="in_progress"),
        )
        ev = evaluate(routes)
        self.assertEqual(ev.verdict, "PENDING")
        self.assertEqual(states(ev)[("ci.yml", "lint")], "pending")

    def test_failure_beats_pending(self):
        routes = self.base_routes(
            [job(1, "lint", conclusion="failure"), job(2, "test (3.11)", status="queued", conclusion=None)],
            ci_run=run(1, "ci.yml", status="in_progress"),
        )
        self.assertEqual(evaluate(routes).verdict, "NOT OK")

    # F8: manifest states that used to fail open.
    def test_path_filtered_without_filters_in_manifest_is_not_ok(self):
        manifest = {"pull_request": {"ci.yml": ["lint"]}, "pull_request_path_filtered": {"upgrade.yml": ["upgrade"]}}
        ev = evaluate(routes_for([run(1, "ci.yml")], {1: [job(1, "lint")]}), manifest=manifest)
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertIn("no filter in manifest", states(ev)[("upgrade.yml", "upgrade")])

    def test_empty_manifest_is_not_ok(self):
        for manifest in ({"pull_request": {}}, {"pull_request": {"ci.yml": ["a"]}, "advisory": ["ci.yml/a"]}):
            with self.subTest(manifest=manifest):
                ev = evaluate(routes_for([run(1, "ci.yml")], {1: [job(1, "a")]}), manifest=manifest)
                self.assertEqual(ev.verdict, "NOT OK")
                self.assertIn("no mandatory jobs", [r.state for r in ev.rows])

    def test_manifest_problems_are_not_ok(self):
        ev = evaluate(self.base_routes(GREEN), problems=["ci.yml drifted"])
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertEqual(states(ev)[(ci_ok.MANIFEST_PATH, "self-check")], "inconsistent")


class DuplicateNames(unittest.TestCase):
    """F6: several jobs with one name in the newest attempt must all succeed."""

    def ev(self, lints):
        return evaluate(routes_for([run(1, "ci.yml")], {1: [*lints, job(8, "test (3.11)")]}))

    def test_one_failure_among_duplicates_is_not_ok(self):
        ev = self.ev([job(1, "lint", conclusion="failure"), job(2, "lint")])
        self.assertEqual(ev.verdict, "NOT OK")
        row = next(r for r in ev.rows if r.job == "lint")
        self.assertEqual(row.state, "failure")
        self.assertIn("2 jobs share this name", row.note)
        # The order of the ids does not matter.
        self.assertEqual(self.ev([job(1, "lint"), job(2, "lint", conclusion="failure")]).verdict, "NOT OK")

    def test_all_duplicates_green_is_ok_and_one_pending_is_pending(self):
        self.assertEqual(self.ev([job(1, "lint"), job(2, "lint")]).verdict, "OK")
        self.assertEqual(self.ev([job(1, "lint"), job(2, "lint", status="queued", conclusion=None)]).verdict, "PENDING")


class Attribution(unittest.TestCase):
    """F5: only runs of the evaluated PR decide when the PR number is known."""

    def test_another_prs_run_on_the_same_sha_is_ignored(self):
        routes = routes_for(
            [
                run(1, "ci.yml", prs=(7,), created="2026-11-07T10:00:00Z"),
                run(2, "ci.yml", prs=(8,), created="2026-11-07T11:00:00Z"),
            ],
            {1: [job(1, "lint"), job(2, "test (3.11)")], 2: [job(3, "lint", conclusion="failure")]},
        )
        snap = snapshot(routes, pr=7)
        self.assertEqual(snap.runs["ci.yml"]["id"], 1)
        self.assertEqual(snap.ignored_runs, 1)
        self.assertEqual(ci_ok.evaluate(MANIFEST, snap, ["a"]).verdict, "OK")
        self.assertEqual(evaluate(routes, pr=8).verdict, "NOT OK")

    def test_attributed_run_beats_a_newer_unattributed_run(self):
        routes = routes_for(
            [
                run(1, "ci.yml", prs=(7,), created="2026-11-07T10:00:00Z"),
                run(2, "ci.yml", prs=(), created="2026-11-07T11:00:00Z"),
            ],
            {1: [job(1, "lint"), job(2, "test (3.11)")], 2: [job(3, "lint", conclusion="failure")]},
        )
        self.assertEqual(evaluate(routes).verdict, "OK")

    def test_unattributed_run_is_used_with_a_note_when_nothing_else_exists(self):
        routes = routes_for([run(1, "ci.yml", prs=())], {1: [job(1, "lint"), job(2, "test (3.11)")]})
        ev = evaluate(routes)
        self.assertEqual(ev.verdict, "OK")
        self.assertIn("by head SHA only", next(r for r in ev.rows if r.job == "lint").note)

    def test_without_a_pr_number_every_run_on_the_sha_counts(self):
        routes = routes_for([run(1, "ci.yml", prs=(8,))], {1: [job(1, "lint"), job(2, "test (3.11)")]})
        self.assertEqual(evaluate(routes, pr=None).verdict, "OK")


class SameBranchAttribution(unittest.TestCase):
    """GitHub lists a run's PRs from its head branch's currently open PRs. After PR #7 is merged its runs list none,
    or a follow-up PR (#82) opened from the same branch (live: nodestro-server #81/#82, ci-ok run 43387975413). The
    triggering run's head branch and head repository identify PR #7's runs; another branch or a fork never does (F5)."""

    def eval_same_branch(self, runs, jobs):
        return evaluate(routes_for(runs, jobs), head_ref="feature", head_repo_id=1)

    def test_follow_up_pr_on_the_same_branch_after_merge_all_green_is_ok(self):
        ev = self.eval_same_branch([run(1, "ci.yml", prs=(82,), pr_heads={82: ("feature", 1)})], {1: LINT_TEST})
        self.assertEqual(ev.verdict, "OK")
        note = next(r for r in ev.rows if r.job == "lint").note
        self.assertIn("lists #82 from the same head branch", note)
        self.assertIn("head branch feature", note)

    def test_follow_up_pr_on_the_same_branch_after_merge_one_failure_is_not_ok(self):
        jobs = [job(1, "lint"), job(2, "test (3.11)", conclusion="failure")]
        ev = self.eval_same_branch([run(1, "ci.yml", prs=(82,), pr_heads={82: ("feature", 1)})], {1: jobs})
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertEqual(states(ev)[("ci.yml", "test (3.11)")], "failure")

    def test_run_listing_no_pr_on_the_same_branch_is_the_prs_run(self):
        # A re-run (attempt 2) of a PR whose run now lists no PR at all, with no new PR on the branch.
        ev = self.eval_same_branch([run(1, "ci.yml", prs=(), attempt=2)], {1: LINT_TEST})
        self.assertEqual(ev.verdict, "OK")
        self.assertIn("lists no pull request; taken for PR #7 by head SHA and head branch feature", ev.rows[0].note)
        failed = [job(1, "lint", conclusion="failure"), job(2, "test (3.11)")]
        self.assertEqual(self.eval_same_branch([run(1, "ci.yml", prs=(), attempt=2)], {1: failed}).verdict, "NOT OK")

    def test_pr_from_another_branch_or_a_fork_is_still_ignored(self):
        for heads, branch, repo_id in (
            ({8: ("other", 1)}, "other", 1),  # another branch of this repository
            ({8: ("feature", 2)}, "feature", 2),  # a fork's branch of the same name
            ({8: ("other", 1)}, "feature", 1),  # a run on our branch that lists a PR from another branch
            ({}, "feature", 1),  # a listed PR without head information is never ours
        ):
            with self.subTest(heads=heads, branch=branch, repo_id=repo_id):
                runs = [run(1, "ci.yml", prs=(8,), pr_heads=heads, branch=branch, repo_id=repo_id)]
                snap = snapshot(routes_for(runs, {1: LINT_TEST}), head_ref="feature", head_repo_id=1)
                self.assertEqual(snap.runs, {})
                self.assertEqual(snap.ignored_runs, 1)
                self.assertEqual(ci_ok.evaluate(MANIFEST, snap, ["a"]).verdict, "NOT OK")

    def test_another_prs_failing_run_does_not_beat_the_same_branch_run(self):
        runs = [
            run(1, "ci.yml", prs=(82,), pr_heads={82: ("feature", 1)}, created="2026-11-07T10:00:00Z"),
            run(2, "ci.yml", prs=(8,), pr_heads={8: ("other", 1)}, branch="other", created="2026-11-07T11:00:00Z"),
        ]
        ev = self.eval_same_branch(runs, {1: LINT_TEST, 2: [job(3, "lint", conclusion="failure")]})
        self.assertEqual(ev.verdict, "OK")

    def test_without_the_head_branch_the_follow_up_prs_run_is_ignored(self):
        # Fail closed: when the event does not name the head branch and repository, F5 applies unchanged.
        runs = [run(1, "ci.yml", prs=(82,), pr_heads={82: ("feature", 1)})]
        self.assertEqual(evaluate(routes_for(runs, {1: LINT_TEST})).verdict, "NOT OK")


class Impostors(unittest.TestCase):
    """F1/F13: the real gate never runs on pull_request; a pull_request job named ci-ok is an impostor."""

    def test_impostor_job_in_any_workflow_is_not_ok(self):
        for workflow in ("evil.yml", ci_ok.SELF_WORKFLOW):
            with self.subTest(workflow=workflow):
                routes = routes_for(
                    [run(1, "ci.yml"), run(2, workflow)], {1: GREEN, 2: [job(9, "ci-ok"), job(10, "helper")]}
                )
                ev = evaluate(routes)
                self.assertEqual(ev.verdict, "NOT OK")
                self.assertEqual(states(ev)[(workflow, "ci-ok")], "impostor")
                # F13: other jobs of the gate's own workflow file are no longer hidden.
                self.assertIn((workflow, "helper"), [(r.workflow, r.job) for r in ev.unlisted])


class ChangedFiles(unittest.TestCase):
    def test_changed_files_fall_back_to_compare(self):
        routes = routes_for([], {})
        routes[f"/repos/{REPO}/pulls/7/files"] = ci_ok.ApiError(403, "x")
        routes[f"/repos/{REPO}/compare/{BASE}...{HEAD}"] = {
            "files": [{"filename": "new.py", "previous_filename": "old.py"}]
        }
        self.assertEqual(ci_ok.fetch_changed_files(FakeApi(routes), REPO, 7, BASE, HEAD), ["new.py", "old.py"])

    def test_pagination_is_manual(self):
        pages = {1: [{"filename": f"f{i}"} for i in range(100)], 2: [{"filename": "last"}]}
        api = FakeApi({})
        api.get = lambda path, raw=False: pages[int(path.rsplit("page=", 1)[1])]
        self.assertEqual(len(ci_ok.fetch_changed_files(api, REPO, 7, BASE, HEAD)), 101)

    def test_without_pr_compare_default_branch_or_unknown(self):
        self.assertEqual(
            ci_ok.fetch_changed_files_without_pr(FakeApi(routes_for([], {})), REPO, "master", HEAD), ["src/a.py"]
        )
        self.assertIsNone(ci_ok.fetch_changed_files_without_pr(FakeApi({}), REPO, "master", HEAD))


def http_error(code, headers=None, body=b"{}"):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x", code, "err", msg, io.BytesIO(body))


class ApiRetries(unittest.TestCase):
    """F7: retries within a deadline, Retry-After honoured; the error propagates (and main reports it)."""

    def api(self, answers, deadline=180.0):
        sleeps: list[float] = []
        clock = {"t": 0.0}

        def opener(req, timeout):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(json.dumps(answer).encode())

        def sleep(s):
            sleeps.append(s)
            clock["t"] += s

        api = ci_ok.Api(
            "t",
            "https://api",
            deadline_seconds=deadline,
            opener=opener,
            sleep=sleep,
            clock=lambda: clock["t"],
            now=lambda: 1000.0,
        )
        return api, sleeps

    def test_retry_after_is_honoured(self):
        api, sleeps = self.api([http_error(403, {"Retry-After": "7"}, b"secondary"), {"ok": 1}])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(api.get("/x"), {"ok": 1})
        self.assertEqual(sleeps, [7.0])

    def test_rate_limit_reset_and_429(self):
        api, sleeps = self.api([http_error(429, {"x-ratelimit-reset": "1030"}), {"ok": 1}])
        with redirect_stdout(io.StringIO()):
            api.get("/x")
        self.assertEqual(sleeps, [30.0])

    def test_server_errors_retry_then_raise(self):
        api, sleeps = self.api([http_error(502)] * 4)
        with redirect_stdout(io.StringIO()), self.assertRaises(ci_ok.ApiError) as cm:
            api.get("/x")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(len(sleeps), 3)

    def test_wait_beyond_the_deadline_raises_at_once(self):
        api, sleeps = self.api([http_error(403, {"Retry-After": "600"}, b"secondary")], deadline=60)
        with self.assertRaises(ci_ok.ApiError):
            api.get("/x")
        self.assertEqual(sleeps, [])

    def test_client_errors_are_not_retried(self):
        api, sleeps = self.api([http_error(404)])
        with self.assertRaises(ci_ok.ApiError):
            api.get("/x")
        self.assertEqual(sleeps, [])

    def test_network_error_is_an_api_error(self):
        api, _ = self.api([urllib.error.URLError("down")] * 4)
        with redirect_stdout(io.StringIO()), self.assertRaises(ci_ok.ApiError):
            api.get("/x")


# A consistent default-branch tree for MANIFEST.
CI_WF = "name: CI\non:\n  push:\n  pull_request:\njobs:\n  engines:\n    continue-on-error: true\n"
DOCKER_WF = (
    'name: Docker\non:\n  pull_request:\n    paths:\n      - "docker/**"\n      - .github/workflows/docker.yml\n'
)
GATE_WF = (
    "name: ci-ok\non:\n  workflow_run:\n    workflows: [CI, Docker]\n    types: [completed]\njobs:\n  ci-ok:\n"
    "    name: ci-ok\n"
)
NIGHTLY_WF = "name: Nightly\non:\n  schedule:\n    - cron: '0 1 * * *'\n"


def make_tree(test: unittest.TestCase, manifest, workflows: dict) -> str:
    root = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, root)
    os.makedirs(os.path.join(root, ".github", "workflows"))
    if manifest is not None:
        with open(os.path.join(root, ci_ok.MANIFEST_PATH), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
    for name, text in workflows.items():
        with open(os.path.join(root, ".github", "workflows", name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def good_tree(test, manifest=MANIFEST, **overrides):
    workflows = {"ci.yml": CI_WF, "docker.yml": DOCKER_WF, "ci-ok.yml": GATE_WF, "nightly.yml": NIGHTLY_WF}
    workflows.update(overrides)
    return make_tree(test, manifest, {k: v for k, v in workflows.items() if v is not None})


def workflow_run_event(prs=(7,), event="pull_request", head_branch=None, head_repo_id=None):
    wr = {
        "id": 99,
        "name": "CI",
        "run_attempt": 1,
        "conclusion": "success",
        "event": event,
        "head_sha": HEAD,
        "pull_requests": [{"number": n, "base": {"sha": BASE}, "head": {"sha": HEAD}} for n in prs],
    }
    if head_branch is not None:
        wr["head_branch"] = head_branch
        wr["head_repository"] = {"id": head_repo_id}
    return {"workflow_run": wr, "repository": {"default_branch": "master"}}


class Main(unittest.TestCase):
    def main(self, routes, *, event=None, env=None, argv=None, root=None, api=None):
        root = root or good_tree(self)
        env = {"GITHUB_REPOSITORY": REPO, **(env or {})}
        with tempfile.TemporaryDirectory() as tmp:
            env["GITHUB_STEP_SUMMARY"] = os.path.join(tmp, "summary.md")
            if event is not None:
                env["GITHUB_EVENT_PATH"] = os.path.join(tmp, "event.json")
                with open(env["GITHUB_EVENT_PATH"], "w", encoding="utf-8") as fh:
                    json.dump(event, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                code = ci_ok.main(["--root", root, *(argv or [])], env=env, api=api or FakeApi(routes))
            summary = ""
            if os.path.exists(env["GITHUB_STEP_SUMMARY"]):
                with open(env["GITHUB_STEP_SUMMARY"], encoding="utf-8") as fh:
                    summary = fh.read()
            return code, out.getvalue(), summary

    def failing_routes(self):
        return routes_for([run(1, "ci.yml")], {1: [job(1, "lint", conclusion="failure")]})

    def green_routes(self):
        return routes_for([run(1, "ci.yml")], {1: GREEN})

    def test_workflow_run_event_ok_prints_one_notice(self):
        code, out, summary = self.main(self.green_routes(), event=workflow_run_event())
        self.assertEqual(code, 0)
        self.assertIn("::notice title=ci-ok::ci-ok for PR #7 @ hhhhhhhhhhhh: OK", out)
        self.assertNotIn("::warning", out)
        self.assertIn("| ci.yml | lint |", summary)
        self.assertIn("triggered by 'CI' run 99", summary)

    def test_report_only_exits_zero_on_not_ok_with_one_warning(self):
        code, out, summary = self.main(self.failing_routes(), event=workflow_run_event())
        self.assertEqual(code, 0)
        self.assertEqual(out.count("::warning"), 1)
        self.assertIn("::warning title=ci-ok::ci-ok for PR #7", out)
        self.assertIn("NOT OK", summary)

    def test_enforce_exits_one_on_not_ok(self):
        code, _, _ = self.main(self.failing_routes(), event=workflow_run_event(), env={"CI_OK_ENFORCE": "1"})
        self.assertEqual(code, 1)

    def test_ok_exits_zero_even_when_enforced(self):
        code, _, _ = self.main(self.green_routes(), event=workflow_run_event(), env={"CI_OK_ENFORCE": "1"})
        self.assertEqual(code, 0)

    def test_pending_is_a_notice(self):
        routes = routes_for([run(1, "ci.yml", status="in_progress")], {1: [job(1, "lint", status="queued")]})
        code, out, _ = self.main(routes, event=workflow_run_event())
        self.assertEqual(code, 0)
        self.assertIn("::notice title=ci-ok::ci-ok for PR #7 @ hhhhhhhhhhhh: PENDING", out)

    def test_non_pull_request_completion_is_skipped(self):
        api = FakeApi({})
        code, out, summary = self.main({}, event=workflow_run_event(event="push"), api=api)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to evaluate", out)
        self.assertEqual(api.calls, [])
        self.assertEqual(summary, "")

    def test_fork_pr_falls_back_to_the_head_sha(self):
        api = FakeApi(routes_for([run(1, "ci.yml", prs=())], {1: GREEN}))
        code, out, summary = self.main({}, event=workflow_run_event(prs=()), api=api)
        self.assertEqual(code, 0)
        self.assertIn("lists no pull request", summary)
        self.assertIn("head SHA (no PR number)", summary)
        self.assertIn(f"/repos/{REPO}/compare/master...{HEAD}", api.calls)
        self.assertIn(": OK", out)

    def test_several_prs_on_one_run_get_one_report_each(self):
        routes = routes_for([run(1, "ci.yml", prs=(7, 8))], {1: GREEN})
        _, out, summary = self.main(routes, event=workflow_run_event(prs=(7, 8)))
        self.assertIn("PR #7", summary)
        self.assertIn("PR #8", summary)
        self.assertEqual(out.count("::notice title=ci-ok::"), 2)

    def test_merged_pr_whose_runs_list_a_follow_up_pr_on_the_same_branch(self):
        # Live: nodestro-server #81 merged at 07:45:20, #82 opened from the same branch at 07:56:01, and the ci-ok run
        # queued at 07:45:03 started at 07:56:26: both runs listed #82 and v2 reported every check as missing.
        event = workflow_run_event(head_branch="feature", head_repo_id=1)
        heads = {82: ("feature", 1)}
        green = routes_for([run(1, "ci.yml", prs=(82,), pr_heads=heads)], {1: GREEN})
        green[f"/repos/{REPO}/pulls/7"]["state"] = "closed"
        _, out, summary = self.main(green, event=event)
        self.assertIn("::notice title=ci-ok::ci-ok for PR #7 @ hhhhhhhhhhhh: OK", out)
        self.assertIn("lists #82 from the same head branch", summary)
        self.assertNotIn("belong to other pull requests", summary)
        failed = routes_for(
            [run(1, "ci.yml", prs=(82,), pr_heads=heads)],
            {1: [*GREEN[:1], job(2, "test (3.11)", conclusion="failure")]},
        )
        _, out, _ = self.main(failed, event=event)
        self.assertIn("::warning title=ci-ok::ci-ok for PR #7 @ hhhhhhhhhhhh: NOT OK", out)
        self.assertIn("ci.yml/test (3.11)=failure", out)
        other = routes_for([run(1, "ci.yml", prs=(8,), pr_heads={8: ("other", 1)}, branch="other")], {1: GREEN})
        _, out, summary = self.main(other, event=event)
        self.assertIn("NOT OK", out)
        self.assertIn("1 run(s) on this SHA belong to other pull requests and were ignored.", summary)

    def test_api_error_is_not_ok_and_exits_zero(self):
        routes = self.green_routes()
        routes[f"/repos/{REPO}/actions/runs?head_sha={HEAD}"] = ci_ok.ApiError(502, "runs")
        code, out, summary = self.main(routes, event=workflow_run_event())
        self.assertEqual(code, 0)
        self.assertIn("api error", summary)
        self.assertIn("::warning title=ci-ok::", out)
        code, _, _ = self.main(routes, event=workflow_run_event(), env={"CI_OK_ENFORCE": "1"})
        self.assertEqual(code, 1)

    def test_unexpected_exception_is_not_ok_and_exits_zero(self):
        routes = self.green_routes()
        routes[f"/repos/{REPO}/actions/runs?head_sha={HEAD}"] = lambda: {"no workflow_runs key": 1}
        code, _, summary = self.main(routes, event=workflow_run_event())
        self.assertEqual(code, 0)
        self.assertIn("api error", summary)

    def test_missing_manifest_is_not_ok_and_exits_zero(self):
        root = good_tree(self, manifest=None)
        code, out, summary = self.main(self.green_routes(), event=workflow_run_event(), root=root)
        self.assertEqual(code, 0)
        self.assertIn("no manifest", summary)
        self.assertIn("::warning", out)

    def test_inconsistent_default_branch_manifest_is_not_ok(self):
        root = good_tree(self, **{"ci-ok.yml": GATE_WF.replace("[CI, Docker]", "[CI]")})
        _, _, summary = self.main(self.green_routes(), event=workflow_run_event(), root=root)
        self.assertIn("inconsistent", summary)
        self.assertIn("NOT OK", summary)

    def test_change_to_the_gate_gets_a_notice(self):
        routes = routes_for([run(1, "ci.yml")], {1: GREEN}, files=(ci_ok.MANIFEST_PATH, "src/a.py"))
        _, _, summary = self.main(routes, event=workflow_run_event())
        self.assertIn("This PR changes .github/ci-manifest.json", summary)

    def test_no_repository_or_event_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out:
            code = ci_ok.main(["--root", tmp], env={"GITHUB_STEP_SUMMARY": os.path.join(tmp, "s.md")}, api=FakeApi({}))
        self.assertEqual(code, 0)
        self.assertIn("NOT OK", out.getvalue())

    def test_dry_run_by_pr_number(self):
        code, out, _ = self.main(self.green_routes(), argv=["--repo", REPO, "--pr", "7", "--once"])
        self.assertEqual(code, 0)
        self.assertIn("PR #7", out)
        self.assertIn("local dry run", out)

    def test_dry_run_of_a_closed_pr_attributes_by_head_sha(self):
        # Live behavior seen on studio #152: a closed PR's runs list the branch's current open PR instead.
        routes = routes_for([run(1, "ci.yml", prs=(155,))], {1: GREEN})
        routes[f"/repos/{REPO}/pulls/7"] = {
            "number": 7,
            "state": "closed",
            "head": {"sha": HEAD},
            "base": {"sha": BASE},
        }
        _, out, summary = self.main(routes, argv=["--repo", REPO, "--pr", "7"])
        self.assertIn(": OK", out)
        self.assertIn("is closed", summary)
        open_routes = routes_for([run(1, "ci.yml", prs=(155,))], {1: GREEN})
        self.assertIn("NOT OK", self.main(open_routes, argv=["--repo", REPO, "--pr", "7"])[1])

    def test_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            self.main(self.failing_routes(), event=workflow_run_event(), argv=["--json", path])
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        self.assertEqual(data[0]["verdict"], "NOT OK")


class SelfCheck(unittest.TestCase):
    def test_consistent(self):
        self.assertEqual(ci_ok.self_check(good_tree(self)), [])

    def test_filter_drift_is_reported(self):
        root = good_tree(self, **{"docker.yml": DOCKER_WF.replace("docker/**", "dock/**")})
        problems = ci_ok.self_check(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("docker.yml: path_filters", problems[0])

    def test_unlisted_pull_request_workflow_and_missing_file(self):
        root = good_tree(self, **{"docker.yml": None, "new.yml": CI_WF.replace("CI", "New")})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("new.yml runs on pull_request but is not in the manifest", problems)
        self.assertIn("docker.yml is in the manifest but", problems)

    def test_advisory_must_be_listed_and_continue_on_error(self):
        manifest = dict(MANIFEST, advisory=["ci.yml/engines (webkit)", "ci.yml/ghost"])
        root = good_tree(self, manifest, **{"ci.yml": "name: CI\non:\n  pull_request:\n"})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("'ci.yml/ghost' is not a listed job", problems)
        self.assertIn("has no continue-on-error", problems)

    # F8
    def test_empty_manifest_fails(self):
        root = good_tree(self, {"pull_request": {"ci.yml": []}}, **{"docker.yml": None})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("lists no mandatory pull_request job", problems)

    def test_path_filtered_without_filter_entry_fails(self):
        manifest = {"pull_request": {"ci.yml": ["lint"]}, "pull_request_path_filtered": {"docker.yml": ["docker"]}}
        problems = "\n".join(ci_ok.self_check(good_tree(self, manifest)))
        self.assertIn("docker.yml is path-filtered but has no path_filters entry", problems)

    def test_workflow_run_list_must_match_the_manifest_names(self):
        root = good_tree(self, **{"ci-ok.yml": GATE_WF.replace("[CI, Docker]", "[CI, Nightly]")})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("lacks ['Docker']", problems)
        self.assertIn("has ['Nightly']", problems)
        renamed = good_tree(self, **{"docker.yml": DOCKER_WF.replace("name: Docker", "name: Docker build")})
        self.assertIn("lacks ['Docker build']", "\n".join(ci_ok.self_check(renamed)))

    def test_gate_must_not_run_on_pull_request_and_must_exist(self):
        root = good_tree(self, **{"ci-ok.yml": "name: ci-ok\non:\n  pull_request:\n"})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("ci-ok.yml runs on pull_request", problems)
        self.assertIn("no on.workflow_run.workflows", problems)
        self.assertIn("does not exist", "\n".join(ci_ok.self_check(good_tree(self, **{"ci-ok.yml": None}))))

    def test_impostor_workflow_fails(self):
        root = good_tree(self, **{"ci.yml": CI_WF + "  ci-ok:\n    runs-on: x\n"})
        self.assertIn("impostor", "\n".join(ci_ok.self_check(root)))

    def test_duplicate_workflow_names_fail(self):
        root = good_tree(self, **{"other.yml": "name: Docker\non:\n  push:\n"})
        self.assertIn("share the name ['Docker']", "\n".join(ci_ok.self_check(root)))

    def test_unreadable_manifest_fails(self):
        root = good_tree(self, manifest=None)
        self.assertIn("cannot read", ci_ok.self_check(root)[0])

    def test_self_check_exit_code_fails_without_enforce(self):
        bad = good_tree(self, **{"docker.yml": None})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ci_ok.main(["--self-check", "--root", bad], env={}), 1)
            self.assertEqual(ci_ok.main(["--self-check", "--root", good_tree(self)], env={}), 0)


if __name__ == "__main__":
    unittest.main()
