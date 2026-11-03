"""The fast lane leaves out the tests listed in tests/nightly_only.txt.

The `ci` check on a pull request sets OPENADAPT_TEST_LANE=fast, so its Flow
shards skip the slow end-to-end and browser-heavy tests. Main and the nightly
run (nightly.yml) leave the variable unset and run everything. Each entry in
the list is a file (tests/test_x.py) or one test (tests/test_x.py::test_y,
which also covers its parametrized cases).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

LIST = Path(__file__).with_name("nightly_only.txt")


def nightly_only(path: Path = LIST) -> list[str]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


def _listed(nodeid: str, entries: list[str]) -> bool:
    return any(
        nodeid == entry or nodeid.startswith((entry + "::", entry + "["))
        for entry in entries
    )


def deselect(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("OPENADAPT_TEST_LANE") != "fast":
        return
    entries = nightly_only()
    dropped = [item for item in items if _listed(item.nodeid, entries)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item in items if not _listed(item.nodeid, entries)]
