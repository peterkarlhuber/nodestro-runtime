"""Unit tests for ci_ok.py (CI-FAST W1). Standard library only, no network: the HTTP layer is a fake.

Run: python3 -m unittest discover -s .github/scripts -p test_ci_ok.py   (or: python3 .github/scripts/test_ci_ok.py).
`python3 -m unittest .github/scripts/test_ci_ok.py` does not work: unittest turns the path into the module name
".github.scripts.test_ci_ok", and the leading dot is an empty package name.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_ok

REPO = "o/r"
HEAD = "h" * 40
BASE = "b" * 40


class FakeApi:
    """Answers GET paths from a dict; values may be callables (for answers that change between polls)."""

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
):
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


def routes_for(runs: list, jobs_by_run: dict, files=("src/a.py",), base_manifest=None, head_manifest=None) -> dict:
    r = {
        f"/repos/{REPO}/actions/runs?head_sha={HEAD}": {"workflow_runs": runs},
        f"/repos/{REPO}/pulls/7/files": [{"filename": f} for f in files],
        f"/repos/{REPO}/pulls/7": {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE}},
    }
    for rid, jobs in jobs_by_run.items():
        r[f"/repos/{REPO}/actions/runs/{rid}/jobs"] = {"jobs": jobs}
    if base_manifest is not None:
        r[f"/repos/{REPO}/contents/{ci_ok.MANIFEST_PATH}?ref={BASE}"] = base_manifest
    if head_manifest is not None:
        r[f"/repos/{REPO}/contents/{ci_ok.MANIFEST_PATH}?ref={HEAD}"] = head_manifest
    return r


MANIFEST = {
    "version": 1,
    "pull_request": {"ci.yml": ["lint", "test (3.11)", "engines (webkit)"], "docker.yml": ["docker"]},
    "path_filters": {"docker.yml": {"paths": ["docker/**", ".github/workflows/docker.yml"]}},
    "advisory": ["ci.yml/engines (webkit)"],
}


def snapshot(routes: dict) -> ci_ok.Snapshot:
    return ci_ok.Collector(FakeApi(routes), REPO, HEAD).snapshot()


def evaluate(routes, manifest=MANIFEST, files=("src/a.py",), **kw):
    kw.setdefault("grace_over", True)
    kw.setdefault("final", False)
    return ci_ok.evaluate(manifest, snapshot(routes), list(files), **kw)


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


class Verdicts(unittest.TestCase):
    def base_routes(self, ci_jobs, ci_run=None, extra_runs=(), extra_jobs=None):
        runs = [ci_run or run(1, "ci.yml"), *extra_runs]
        jobs = {1: ci_jobs, **(extra_jobs or {})}
        return routes_for(runs, jobs)

    def test_all_success_is_ok_and_docker_not_triggered(self):
        ev = evaluate(self.base_routes([job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")]))
        self.assertEqual(ev.verdict, "OK")
        self.assertEqual(states(ev)[("docker.yml", "docker")], "not triggered (paths)")
        self.assertTrue(ev.decided)

    def test_path_filtered_workflow_that_should_have_run_is_missing(self):
        routes = self.base_routes([job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")])
        ev = evaluate(routes, files=["docker/Dockerfile"])
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertEqual(states(ev)[("docker.yml", "docker")], "missing")
        # Before the grace period is over, the absent run is still pending.
        ev = evaluate(routes, files=["docker/Dockerfile"], grace_over=False)
        self.assertEqual(ev.verdict, "PENDING")

    def test_path_filtered_workflow_that_ran_anyway_is_judged(self):
        routes = self.base_routes(
            [job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")],
            extra_runs=[run(2, "docker.yml")],
            extra_jobs={2: [job(9, "docker", conclusion="failure")]},
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
        ev = evaluate(routes)
        self.assertEqual(states(ev)[("ci.yml", "lint")], "missing")

    def test_advisory_failure_does_not_count(self):
        ev = evaluate(
            self.base_routes([job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)", conclusion="failure")])
        )
        self.assertEqual(ev.verdict, "OK")
        row = next(r for r in ev.rows if r.job == "engines (webkit)")
        self.assertFalse(row.counts)
        self.assertFalse(row.ok)

    def test_unlisted_job_is_a_warning_not_a_failure(self):
        ev = evaluate(
            self.base_routes(
                [
                    job(1, "lint"),
                    job(2, "test (3.11)"),
                    job(3, "engines (webkit)"),
                    job(4, "brand-new-job", conclusion="failure"),
                ]
            )
        )
        self.assertEqual(ev.verdict, "OK")
        self.assertEqual([r.job for r in ev.unlisted], ["brand-new-job"])

    def test_own_workflow_is_never_unlisted(self):
        routes = self.base_routes(
            [job(1, "lint"), job(2, "test (3.11)"), job(3, "engines (webkit)")],
            extra_runs=[run(5, ci_ok.SELF_WORKFLOW, status="in_progress", conclusion=None)],
            extra_jobs={5: [job(50, "ci-ok", status="in_progress", conclusion=None)]},
        )
        self.assertEqual(evaluate(routes).unlisted, [])

    def test_rerun_uses_the_latest_attempt(self):
        # The jobs endpoint is asked with filter=latest; it returns the re-run attempt's jobs.
        routes = self.base_routes(
            [job(11, "lint"), job(12, "test (3.11)"), job(13, "engines (webkit)")], ci_run=run(1, "ci.yml", attempt=2)
        )
        api = FakeApi(routes)
        snap = ci_ok.Collector(api, REPO, HEAD).snapshot()
        self.assertTrue(any("filter=latest" in c for c in api.calls))
        self.assertEqual(ci_ok.evaluate(MANIFEST, snap, ["a"], grace_over=True, final=False).verdict, "OK")

    def test_newest_run_of_a_workflow_supersedes_older_runs(self):
        routes = routes_for(
            [run(1, "ci.yml", created="2026-11-07T10:00:00Z"), run(2, "ci.yml", created="2026-11-07T11:00:00Z")],
            {
                1: [job(1, "lint", conclusion="failure")],
                2: [job(2, "lint"), job(3, "test (3.11)"), job(4, "engines (webkit)")],
            },
        )
        self.assertEqual(evaluate(routes).verdict, "OK")

    def test_pending_then_final(self):
        routes = self.base_routes(
            [job(1, "lint", status="in_progress", conclusion=None), job(2, "test (3.11)"), job(3, "engines (webkit)")],
            ci_run=run(1, "ci.yml", status="in_progress"),
        )
        ev = evaluate(routes)
        self.assertEqual(ev.verdict, "PENDING")
        self.assertFalse(ev.decided)
        ev = evaluate(routes, final=True)
        self.assertEqual(ev.verdict, "NOT OK")
        self.assertEqual(states(ev)[("ci.yml", "lint")], "timed out (ci-ok)")

    def test_fail_fast_decides_before_everything_finished(self):
        routes = self.base_routes(
            [job(1, "lint", conclusion="failure"), job(2, "test (3.11)", status="queued", conclusion=None)],
            ci_run=run(1, "ci.yml", status="in_progress"),
        )
        self.assertTrue(evaluate(routes).decided)
        self.assertFalse(evaluate(routes, fail_fast=False).decided)

    def test_path_filtered_without_filters_in_manifest(self):
        manifest = {"pull_request": {}, "pull_request_path_filtered": {"upgrade.yml": ["upgrade"]}}
        ev = evaluate(routes_for([], {}), manifest=manifest)
        self.assertEqual(ev.verdict, "OK")
        self.assertIn("no filter in manifest", ev.rows[0].state)


class ManifestFromBase(unittest.TestCase):
    def test_base_manifest_wins_and_change_is_noticed(self):
        weak = {"pull_request": {"ci.yml": []}}
        api = FakeApi(routes_for([], {}, base_manifest=MANIFEST, head_manifest=weak))
        manifest, notices = ci_ok.load_base_manifest(api, REPO, BASE, HEAD, [ci_ok.MANIFEST_PATH])
        self.assertEqual(manifest, MANIFEST)
        self.assertTrue(any("changes .github/ci-manifest.json" in n for n in notices))

    def test_no_notice_when_unchanged(self):
        api = FakeApi(routes_for([], {}, base_manifest=MANIFEST))
        _, notices = ci_ok.load_base_manifest(api, REPO, BASE, HEAD, ["src/a.py"])
        self.assertEqual(notices, [])

    def test_bootstrap_falls_back_to_head(self):
        api = FakeApi(routes_for([], {}, head_manifest=MANIFEST))
        manifest, notices = ci_ok.load_base_manifest(api, REPO, BASE, HEAD, [ci_ok.MANIFEST_PATH])
        self.assertEqual(manifest, MANIFEST)
        self.assertIn("bootstrap", notices[0])

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


class Main(unittest.TestCase):
    def main(self, routes, env=None, argv=("--repo", REPO, "--pr", "7")):
        env = {"CI_OK_POLL_SECONDS": "1", "CI_OK_TIMEOUT_MINUTES": "1", "CI_OK_GRACE_MINUTES": "0", **(env or {})}
        with tempfile.TemporaryDirectory() as tmp:
            env["GITHUB_STEP_SUMMARY"] = os.path.join(tmp, "summary.md")
            out = io.StringIO()
            clock = iter(range(0, 10_000, 30))
            with redirect_stdout(out):
                code = ci_ok.main(
                    list(argv), env=env, api=FakeApi(routes), sleep=lambda s: None, clock=lambda: next(clock)
                )
            with open(env["GITHUB_STEP_SUMMARY"], encoding="utf-8") as fh:
                return code, out.getvalue(), fh.read()

    def failing_routes(self):
        return routes_for([run(1, "ci.yml")], {1: [job(1, "lint", conclusion="failure")]}, base_manifest=MANIFEST)

    def test_report_only_exits_zero_on_not_ok(self):
        code, out, summary = self.main(self.failing_routes())
        self.assertEqual(code, 0)
        self.assertIn("::warning title=ci-ok::ci-ok: NOT OK", out)
        self.assertIn("| ci.yml | lint |", summary)

    def test_enforce_exits_one_on_not_ok(self):
        code, _, _ = self.main(self.failing_routes(), env={"CI_OK_ENFORCE": "1"})
        self.assertEqual(code, 1)

    def test_ok_exits_zero_even_when_enforced(self):
        routes = routes_for([run(1, "ci.yml")], {1: [job(1, "lint"), job(2, "test (3.11)")]}, base_manifest=MANIFEST)
        code, out, _ = self.main(routes, env={"CI_OK_ENFORCE": "1"})
        self.assertEqual(code, 0)
        self.assertIn("ci-ok: OK", out)

    def test_waits_until_done_then_times_out(self):
        routes = routes_for(
            [run(1, "ci.yml", status="in_progress")],
            {1: [job(1, "lint", status="in_progress", conclusion=None)]},
            base_manifest=MANIFEST,
        )
        code, out, _ = self.main(routes)
        self.assertEqual(code, 0)
        self.assertIn("waiting for", out)
        self.assertIn("timed out (ci-ok)", out)

    def test_event_payload_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "event.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"pull_request": {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE}}}, fh)
            code, out, _ = self.main(
                self.failing_routes(), argv=(), env={"GITHUB_EVENT_PATH": path, "GITHUB_REPOSITORY": REPO}
            )
        self.assertEqual(code, 0)
        self.assertIn("PR #7", out)


class SelfCheck(unittest.TestCase):
    def tree(self, manifest: dict, workflows: dict) -> str:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, ".github", "workflows"))
        with open(os.path.join(root, ci_ok.MANIFEST_PATH), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        for name, text in workflows.items():
            with open(os.path.join(root, ".github", "workflows", name), "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    CI = "on:\n  pull_request:\njobs:\n  engines:\n    continue-on-error: true\n"
    DOCKER = 'on:\n  pull_request:\n    paths:\n      - "docker/**"\n      - .github/workflows/docker.yml\n'

    def test_consistent(self):
        root = self.tree(MANIFEST, {"ci.yml": self.CI, "docker.yml": self.DOCKER, "ci-ok.yml": self.CI})
        self.assertEqual(ci_ok.self_check(root), [])

    def test_filter_drift_is_reported(self):
        root = self.tree(MANIFEST, {"ci.yml": self.CI, "docker.yml": self.DOCKER.replace("docker/**", "dock/**")})
        problems = ci_ok.self_check(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("docker.yml: path_filters", problems[0])

    def test_unlisted_pull_request_workflow_and_missing_file(self):
        root = self.tree(MANIFEST, {"ci.yml": self.CI, "new.yml": self.CI})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("new.yml runs on pull_request but is not in the manifest", problems)
        self.assertIn("docker.yml is in the manifest but", problems)

    def test_advisory_must_be_listed_and_continue_on_error(self):
        manifest = dict(MANIFEST, advisory=["ci.yml/engines (webkit)", "ci.yml/ghost"])
        root = self.tree(manifest, {"ci.yml": "on:\n  pull_request:\n", "docker.yml": self.DOCKER})
        problems = "\n".join(ci_ok.self_check(root))
        self.assertIn("'ci.yml/ghost' is not a listed job", problems)
        self.assertIn("has no continue-on-error", problems)

    def test_self_check_is_report_only_unless_enforced(self):
        root = self.tree(MANIFEST, {"ci.yml": self.CI})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ci_ok.main(["--self-check", "--root", root], env={}), 0)
            self.assertEqual(ci_ok.main(["--self-check", "--root", root], env={"CI_OK_ENFORCE": "1"}), 1)


if __name__ == "__main__":
    unittest.main()
