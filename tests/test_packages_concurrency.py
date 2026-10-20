"""Packages runs on main: only runs the Desktop release never reads are cancelled.

desktop-release.yml waits for the Packages push run of the newest first-parent
commit that changed packages/desktop (#1398). That run must never be cancelled.
Any other main run may be superseded by a newer push.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "packages.yml"
)


def _concurrency() -> dict[str, str]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["concurrency"]


def test_desktop_changes_keep_their_own_uncancelled_group() -> None:
    group = " ".join(_concurrency()["group"].split())
    cancel = " ".join(_concurrency()["cancel-in-progress"].split())
    desktop = "!contains(toJSON(github.event.commits), 'packages/desktop/')"
    # Only a push without a Desktop change joins the shared, cancellable group.
    assert (
        f"github.event_name == 'push' && github.event.head_commit && {desktop} && 'main-without-desktop'"
        in group
    )
    assert group.endswith("|| github.sha }}")
    assert (
        f"github.event_name == 'push' && github.event.head_commit != null && {desktop}"
        in cancel
    )
    # Merge queue groups and dispatches are never cancelled.
    assert "merge_group" not in cancel and "workflow_dispatch" not in cancel
