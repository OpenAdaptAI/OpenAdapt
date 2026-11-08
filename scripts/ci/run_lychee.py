#!/usr/bin/env python3
"""Run an installed Lychee with the workflow's existing report/empty policies."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--fail-if-empty", action="store_true")
    parser.add_argument("--job-summary", action="store_true")
    args = parser.parse_args(argv)
    report = Path("lychee/out.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    # A previous report cannot stand in for output from this invocation.
    report.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "lychee",
            "--mode",
            "task",
            "--format",
            "markdown",
            "--output",
            str(report),
            *shlex.split(os.environ["LYCHEE_ARGS"]),
        ],
        check=False,
    )
    if not report.is_file():
        return result.returncode or 1
    text = report.read_text()
    empty = args.fail_if_empty and re.search(r"Total\s+\|\s+0", text) is not None
    if empty:
        text += "No links were found; check the input paths or explicitly allow an empty check.\n"
    if "GITHUB_OUTPUT" in os.environ:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"exit_code={result.returncode}\n")
    if "GITHUB_REPOSITORY" in os.environ and "GITHUB_RUN_ID" in os.environ:
        url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}?check_suite_focus=true"
        report.write_text(text + f"\n[Full Github Actions output]({url})\n")
    print(text)
    if args.job_summary:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("w") as summary:
            summary.write(text)
    # The upstream action gives its explicit empty-input refusal precedence.
    return 1 if empty else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
