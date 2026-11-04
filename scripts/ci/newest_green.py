#!/usr/bin/env python3
"""Find the green main run a package release must come from.

Every release follows one rule (desktop-release.yml set it first): take the
newest commit on main's own history (first parent) that changed the package,
and require that commit's latest successful push run on main, with the
package's own jobs run and passed. The release then builds main's tip. Nothing
changed the package after that commit, so the package's tree is the one that
run tested.

    python scripts/ci/newest_green.py flow --tip "$GITHUB_SHA" \\
        --github-output "$GITHUB_OUTPUT"

It writes `sha` (the commit) and `run_id` (the green run), or exits 1 and
says why: no commit changed the package, the commit's run is still going, or
it failed and needs a re-run. Needs git history (fetch-depth: 0) and the gh
CLI with GH_TOKEN (actions: read).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# package -> (paths that make a commit change it, workflow, job-name prefixes
# that must each have passed in that workflow's run)
PACKAGES: dict[str, tuple[list[str], str, list[str]]] = {
    "types": (["packages/types"], "packages.yml", ["types ("]),
    "flow": (
        ["packages/flow"],
        "packages.yml",
        ["flow core lint", "flow core unit", "flow core critical"],
    ),
    "agent": (["packages/agent"], "packages.yml", ["agent shim"]),
    "capture": (
        ["packages/capture"],
        "packages.yml",
        ["capture (linux", "capture lint"],
    ),
    "privacy": (["packages/privacy"], "packages.yml", ["privacy (", "privacy lint"]),
    "tray": (["packages/tray"], "packages.yml", ["tray (", "tray lint"]),
    "program-graph": (["packages/program-graph"], "packages.yml", ["program-graph"]),
    "desktop": (
        ["packages/desktop"],
        "packages.yml",
        ["desktop test / ", "desktop build / "],
    ),
    "launcher": (
        ["openadapt", "pyproject.toml", "uv.lock", "hatch_build.py"],
        "main.yml",
        ["run-ci ("],
    ),
}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}


def subject(tip: str, paths: list[str]) -> str | None:
    out = subprocess.run(
        ["git", "log", "-1", "--first-parent", "--format=%H", tip, "--", *paths],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out or None


def select_run(runs: list[dict], sha: str, repo: str) -> dict | None:
    """The newest successful push run on this repository's main for `sha`."""
    green = [
        run
        for run in runs
        if run.get("head_sha") == sha
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("conclusion") == "success"
        and (run.get("repository") or {}).get("full_name") == repo
    ]
    return max(green, key=lambda run: run["updated_at"]) if green else None


def job_problems(jobs: list[dict], prefixes: list[str]) -> list[str]:
    """Why these jobs do not prove the package, or [] when they do.

    A green run whose package jobs were skipped proves nothing about the
    package, so each prefix needs at least one passed job and none failed.
    """
    problems = []
    for prefix in prefixes:
        mine = [job for job in jobs if job["name"].startswith(prefix)]
        if not any(job.get("conclusion") == "success" for job in mine):
            problems.append(f"no passed job named '{prefix}...'")
        problems += [
            f"'{job['name']}' ended {job['conclusion']}"
            for job in mine
            if job.get("conclusion") in FAILED
        ]
    return problems


def _gh(path: str, key: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", f".{key}[]"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("package", choices=sorted(PACKAGES))
    parser.add_argument("--tip", default="HEAD")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    paths, workflow, prefixes = PACKAGES[args.package]

    sha = subject(args.tip, paths)
    if not sha:
        print(f"No commit on main changes {args.package}.", file=sys.stderr)
        return 1
    runs = _gh(
        f"repos/{repo}/actions/workflows/{workflow}/runs"
        f"?head_sha={sha}&event=push&branch=main&per_page=50",
        "workflow_runs",
    )
    run = select_run(runs, sha, repo)
    if run is None:
        states = sorted({f"{r.get('status')}/{r.get('conclusion')}" for r in runs}) or [
            "none"
        ]
        print(
            f"Refusing to release {args.package}: {sha[:12]}, the newest commit that "
            f"changed it, has no successful {workflow} push run on main (runs: "
            f"{', '.join(states)}). Wait for it, or re-run it until it passes.",
            file=sys.stderr,
        )
        return 1
    problems = job_problems(
        _gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100", "jobs"),
        prefixes,
    )
    if problems:
        print(
            f"Refusing to release {args.package}: run {run['id']} for {sha[:12]} "
            f"passed but {'; '.join(problems)}.",
            file=sys.stderr,
        )
        return 1
    print(f"{args.package}: {sha} passed in run {run['html_url']}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"sha={sha}\nrun_id={run['id']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
