"""Every entry in tests/nightly_only.txt names a test that exists."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import lanes

ROOT = Path(__file__).resolve().parent.parent


def test_every_listed_test_exists() -> None:
    entries = lanes.nightly_only()
    assert entries
    for entry in entries:
        file, _, name = entry.partition("::")
        path = ROOT / file
        assert path.is_file(), entry
        if name:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            }
            assert name.split("::")[0].split("[")[0] in names, entry
    assert len(entries) == len(set(entries))


def test_listed_ids_match_files_tests_and_cases() -> None:
    entries = ["tests/a.py", "tests/b.py::test_x"]
    assert lanes._listed("tests/a.py::test_any", entries)
    assert lanes._listed("tests/b.py::test_x", entries)
    assert lanes._listed("tests/b.py::test_x[case-1]", entries)
    assert not lanes._listed("tests/b.py::test_xy", entries)
    assert not lanes._listed("tests/ab.py::test_any", entries)


def test_live_entries_leave_the_main_lane_too(tmp_path: Path) -> None:
    listed = tmp_path / "nightly_only.txt"
    listed.write_text(
        "# comment\ntests/slow.py  # 9s\nlive tests/live.py::test_cdp  # 134s\n",
        encoding="utf-8",
    )
    assert lanes.nightly_only(listed) == ["tests/slow.py", "tests/live.py::test_cdp"]
    assert lanes.live_only(listed) == ["tests/live.py::test_cdp"]
    assert set(lanes.live_only()) <= set(lanes.nightly_only())
    assert (
        "tests/test_browser_attach.py::"
        "test_live_cdp_attach_records_compiles_and_leaves_browser_running_three_trials"
    ) in lanes.live_only()


@pytest.mark.parametrize(
    "lane,dropped",
    [
        ("fast", ["tests/slow.py::test_a", "tests/live.py::test_cdp"]),
        ("main", ["tests/live.py::test_cdp"]),
        ("full", []),
        (None, []),
    ],
)
def test_each_lane_leaves_out_its_entries(monkeypatch, tmp_path, lane, dropped):
    listed = tmp_path / "nightly_only.txt"
    listed.write_text("tests/slow.py\nlive tests/live.py::test_cdp\n")
    if lane is None:
        monkeypatch.delenv("OPENADAPT_TEST_LANE", raising=False)
    else:
        monkeypatch.setenv("OPENADAPT_TEST_LANE", lane)

    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    class Hook:
        def pytest_deselected(self, items) -> None:
            seen.extend(item.nodeid for item in items)

    class Config:
        hook = Hook()

    seen: list[str] = []
    names = ["tests/slow.py::test_a", "tests/live.py::test_cdp", "tests/kept.py::t"]
    items = [Item(name) for name in names]
    lanes.deselect(Config(), items, listed)
    assert seen == dropped
    assert [item.nodeid for item in items] == [n for n in names if n not in dropped]
