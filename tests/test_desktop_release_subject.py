"""A dispatched Desktop release waits on a commit that has its own checks.

Only commits on main's first-parent history get a Packages push run. A commit
from inside a merged branch never does, so choosing it made the release gate
wait 60 minutes for a run that cannot exist, then refuse (2026-10-20, 0.21.1).
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/desktop-release.yml"


def test_dispatch_subject_follows_first_parent_history() -> None:
    lines = [
        line.strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('subject="$(git log')
    ]
    assert lines, "the release gate no longer chooses a subject commit with git log"
    for line in lines:
        assert "--first-parent" in line, line
