"""scripts/check_file_size_ratchet.py keeps the largest files from growing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_file_size_ratchet.py"
_spec = importlib.util.spec_from_file_location("check_file_size_ratchet", SCRIPT)
assert _spec and _spec.loader
ratchet = importlib.util.module_from_spec(_spec)
# dataclasses look the module up in sys.modules while the class is built.
sys.modules[_spec.name] = ratchet
_spec.loader.exec_module(ratchet)


def _repo(tmp_path: Path, table: str, files: dict[str, int]) -> Path:
    doc = tmp_path / ratchet.DOC
    doc.parent.mkdir(parents=True)
    doc.write_text(
        f"# Plan\n\n{ratchet.BEGIN}\n| File | Line budget |\n|---|---:|\n"
        f"{table}{ratchet.END}\n",
        encoding="utf-8",
    )
    for name, lines in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n" * lines, encoding="utf-8")
    return tmp_path


def test_listed_files_are_within_budget() -> None:
    failures, _notes = ratchet.check(ROOT)
    assert failures == []


def test_growth_past_budget_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 11})
    failures, _notes = ratchet.check(root)
    assert len(failures) == 1
    assert "a.py: 11 lines, budget 10 (+1)" in failures[0]
    assert ratchet.main(["--root", str(root)]) == 1


def test_equal_passes_and_shrink_asks_to_lower(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, "| `a.py` | 10 |\n| `b.py` | 10 |\n", {"a.py": 10, "b.py": 7}
    )
    failures, notes = ratchet.check(root)
    assert failures == []
    assert notes == [
        f"b.py: 7 lines, budget 10. Lower the budget to 7 in {ratchet.DOC}."
    ]
    assert ratchet.main(["--root", str(root)]) == 0


def test_missing_listed_file_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, "| `gone.py` | 10 |\n", {})
    failures, _notes = ratchet.check(root)
    assert len(failures) == 1 and "missing" in failures[0]


@pytest.mark.parametrize(
    "doc_text",
    [
        "no markers at all",
        f"{ratchet.BEGIN}\n| File | Line budget |\n{ratchet.END}",
        f"{ratchet.BEGIN}\n| `a.py` | 1 |\n| `a.py` | 2 |\n{ratchet.END}",
    ],
    ids=["no-markers", "empty-table", "duplicate-row"],
)
def test_malformed_table_is_refused(doc_text: str) -> None:
    with pytest.raises(ValueError):
        ratchet.parse_budgets(doc_text)


def test_plan_lists_the_ten_largest_files() -> None:
    budgets = ratchet.parse_budgets((ROOT / ratchet.DOC).read_text(encoding="utf-8"))
    paths = {budget.path for budget in budgets}
    assert "packages/flow/openadapt_flow/runtime/replayer.py" in paths
    assert len(paths) == 10
