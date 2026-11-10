"""The fast and main lanes leave out tests listed in tests/nightly_only.txt.

The `ci` check on a pull request sets OPENADAPT_TEST_LANE=fast, so its Flow
shards skip the slow end-to-end and browser-heavy tests. A push to main sets
OPENADAPT_TEST_LANE=main and skips only the entries marked `live `: live
browser or OS tests whose timing a busy shared runner cannot hold. The nightly
run (nightly.yml) sets `full`, and a local run leaves the variable unset; both
run everything. Each entry in the list is a file (tests/test_x.py) or one test
(tests/test_x.py::test_y, which also covers its parametrized cases).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

LIST = Path(__file__).with_name("nightly_only.txt")
LIVE = "live "


def _entries(path: Path) -> list[str]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def nightly_only(path: Path = LIST) -> list[str]:
    """Every listed test, live or not: what the fast lane leaves out."""
    return [entry.removeprefix(LIVE) for entry in _entries(path)]


def live_only(path: Path = LIST) -> list[str]:
    """The entries marked `live `: what the main lane also leaves out."""
    return [
        entry.removeprefix(LIVE) for entry in _entries(path) if entry.startswith(LIVE)
    ]


def _listed(nodeid: str, entries: list[str]) -> bool:
    return any(
        nodeid == entry or nodeid.startswith((entry + "::", entry + "["))
        for entry in entries
    )


def deselect(
    config: pytest.Config, items: list[pytest.Item], path: Path = LIST
) -> None:
    lane = os.environ.get("OPENADAPT_TEST_LANE")
    if lane == "fast":
        entries = nightly_only(path)
    elif lane == "main":
        entries = live_only(path)
    else:
        return
    dropped = [item for item in items if _listed(item.nodeid, entries)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item in items if not _listed(item.nodeid, entries)]
