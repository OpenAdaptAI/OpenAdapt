#!/usr/bin/env python3
"""Keep one "main is red" issue in step with the nightly full run.

The nightly workflow runs every package's full suite against main. Its last
job runs this script, which reads the run's jobs from the GitHub API:

- When a job failed, it opens the issue (label `main-is-red`) or, if one is
  open, rewrites its body and comments with the failing jobs.
- When every job passed and the issue is open, it comments and closes it.

It needs the `gh` CLI with GH_TOKEN (issues: write, actions: read) and the
GITHUB_* variables Actions sets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable

LABEL = "main-is-red"
TITLE = "main is red"
FAILED = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def failing_jobs(jobs: Iterable[dict]) -> list[dict]:
    """Jobs that finished without passing, in the order the API lists them."""
    return [job for job in jobs if job.get("conclusion") in FAILED]


def render(failed: list[dict], run_url: str, sha: str) -> str:
    lines = [
        f"The [nightly full run]({run_url}) on main ({sha[:12]}) failed in "
        f"{len(failed)} job{'s' if len(failed) != 1 else ''}:",
        "",
    ]
    lines += [
        f"- [{job['name']}]({job['html_url']}): {job['conclusion']}" for job in failed
    ]
    lines += [
        "",
        "The nightly run updates this issue and closes it when a full run passes.",
    ]
    return "\n".join(lines) + "\n"


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return result.stdout


def _jobs(repo: str, run_id: str, attempt: str) -> list[dict]:
    out = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        "--jq",
        ".jobs[]",
    )
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _open_issue(repo: str) -> int | None:
    out = _gh(
        "issue", "list", "--repo", repo, "--state", "open", "--label", LABEL,
        "--json", "number", "--jq", "[.[].number] | min // empty",
    )  # fmt: skip
    return int(out) if out.strip() else None


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    sha = os.environ["GITHUB_SHA"]
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{run_id}"

    failed = failing_jobs(_jobs(repo, run_id, attempt))
    issue = _open_issue(repo)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")

    if failed:
        body = render(failed, run_url, sha)
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(body)
        if issue is None:
            _gh("label", "create", LABEL, "--repo", repo, "--force", "--color", "B60205",
                "--description", "The nightly full run on main failed")  # fmt: skip
            _gh("issue", "create", "--repo", repo, "--title", TITLE, "--label", LABEL,
                "--body", body)  # fmt: skip
            print(f"opened '{TITLE}' with {len(failed)} failing jobs")
        else:
            _gh("issue", "edit", str(issue), "--repo", repo, "--body", body)
            _gh("issue", "comment", str(issue), "--repo", repo, "--body", body)
            print(f"updated #{issue} with {len(failed)} failing jobs")
        return 0

    print("every job passed")
    if issue is not None:
        _gh("issue", "close", str(issue), "--repo", repo, "--comment",
            f"The [nightly full run]({run_url}) on main ({sha[:12]}) passed.")  # fmt: skip
        print(f"closed #{issue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
