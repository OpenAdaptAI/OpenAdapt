#!/usr/bin/env python3
"""Measure CI cost per merged pull request: wall time and job-minutes.

For each of the last N merged pull requests it reads, from the GitHub API:

- the pull-request runs on the PR's final head commit (event pull_request);
- the merge-queue runs for that PR (event merge_group, branch
  gh-readonly-queue/main/pr-<N>-<sha>).

Wall time is from the first run's start to the last job's finish. Job-minutes
sum every job's own start-to-finish time, which is the runner time the PR
used. Skipped jobs count zero.

    python scripts/ci/measure_pr_ci.py --prs 20 --json ci-before.json

Needs the gh CLI, signed in with read access to the repository.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from datetime import datetime

REPO = "OpenAdaptAI/OpenAdapt"


def _gh_json(*args: str):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def _pages(path: str, key: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", f".{key}[]"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def measure(runs: list[dict]) -> dict:
    """Wall minutes, job-minutes and job count for a set of workflow runs."""
    starts, ends, minutes, jobs = [], [], 0.0, 0
    for run in runs:
        start = _time(run.get("run_started_at") or run.get("created_at"))
        if start:
            starts.append(start)
        for job in _pages(f"repos/{REPO}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs"):
            began, done = _time(job.get("started_at")), _time(job.get("completed_at"))
            if not began or not done or job.get("conclusion") == "skipped":
                continue
            minutes += (done - began).total_seconds() / 60
            jobs += 1
            ends.append(done)
    wall = (max(ends) - min(starts)).total_seconds() / 60 if starts and ends else 0.0
    return {"runs": len(runs), "jobs": jobs, "wall_min": round(wall, 1), "job_min": round(minutes, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prs", type=int, default=20)
    parser.add_argument("--json", help="write per-PR rows here")
    args = parser.parse_args()

    prs = _gh_json(
        "pr", "list", "--repo", REPO, "--state", "merged", "--limit", str(args.prs),
        "--json", "number,headRefOid,mergedAt,title",
    )  # fmt: skip
    oldest = min(pr["mergedAt"] for pr in prs)[:10]
    queue = _pages(
        f"repos/{REPO}/actions/runs?event=merge_group&created=>={oldest}&per_page=100",
        "workflow_runs",
    )
    rows = []
    for pr in prs:
        pr_runs = _pages(
            f"repos/{REPO}/actions/runs?event=pull_request&head_sha={pr['headRefOid']}&per_page=100",
            "workflow_runs",
        )
        queue_runs = [
            run
            for run in queue
            if re.search(rf"/pr-{pr['number']}-", run.get("head_branch") or "")
        ]
        rows.append(
            {
                "pr": pr["number"],
                "merged_at": pr["mergedAt"],
                "pr_lane": measure(pr_runs),
                "queue": measure(queue_runs),
            }
        )
        print(rows[-1], flush=True)

    def median(key: str, field: str) -> float:
        values = [row[key][field] for row in rows if row[key]["runs"]]
        return round(statistics.median(values), 1) if values else 0.0

    summary = {
        "prs": len(rows),
        "pr_wall_min_median": median("pr_lane", "wall_min"),
        "pr_job_min_median": median("pr_lane", "job_min"),
        "queue_wall_min_median": median("queue", "wall_min"),
        "queue_job_min_median": median("queue", "job_min"),
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "rows": rows}, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
