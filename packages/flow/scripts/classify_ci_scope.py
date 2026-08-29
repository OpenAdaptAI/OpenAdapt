"""Classify whether Flow CI must run the expensive required jobs.

Documentation, paper sources, licenses, notices, and issue templates do
not need the 40-minute test / e2e / Windows / AT-SPI jobs. Required checks
still report: gated jobs start, see ``code_changed=false``, and succeed
without installing anything.

Fail closed: an empty or unreadable path list runs the full suite.
"""

from __future__ import annotations

import os
import re
import sys

# Keep in sync with tests/test_ci_workflow_contract.py.
CHEAP_PATH = re.compile(
    r"^(?:"
    r"docs/.*"
    r"|paper/.*"
    r"|.+\.md"
    r"|LICENSE(?:\..*)?"
    r"|NOTICE(?:\..*)?"
    r"|\.github/ISSUE_TEMPLATE/.*"
    r")$"
)


def code_changed(paths: list[str]) -> bool:
    if not paths:
        return True
    return any(CHEAP_PATH.match(path) is None for path in paths)


def main() -> None:
    paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    changed = code_changed(paths)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"code_changed={'true' if changed else 'false'}\n")
    if changed:
        sys.stdout.write(
            "Code or workflow files changed. The full required suite will run.\n"
        )
    else:
        sys.stdout.write(
            "Only documentation, paper, license, or notice files changed. "
            "Expensive required jobs will report success without the suite.\n"
        )


if __name__ == "__main__":
    main()
