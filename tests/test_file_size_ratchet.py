"""scripts/check_file_size_ratchet.py keeps the largest files from growing."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import warnings
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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def _base(root: Path) -> Path:
    """Commit ``root`` as the base a pull request merges into."""
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _write(root: Path, name: str, lines: int) -> None:
    (root / name).write_text("x\n" * lines, encoding="utf-8")


def test_listed_files_are_within_budget() -> None:
    """The real table, under the rule this CI event gets from ``policy``."""
    base, report_only = ratchet.policy(os.environ)
    failures, _notes = ratchet.check(ROOT, base)
    if report_only:
        # A push to main reports; the pull request that grew a file failed.
        for failure in failures:
            warnings.warn(failure, stacklevel=1)
        return
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


@pytest.mark.parametrize(
    "event,expected",
    [
        ("pull_request", ("HEAD^1", False)),
        ("merge_group", ("HEAD^1", False)),
        ("push", (None, True)),
        ("schedule", (None, False)),
        ("workflow_dispatch", (None, False)),
        (None, (None, False)),
    ],
)
def test_policy_per_event(event: str | None, expected) -> None:
    env = {} if event is None else {"GITHUB_EVENT_NAME": event}
    assert ratchet.policy(env) == expected


def test_a_change_that_does_not_touch_an_over_budget_file_passes(
    tmp_path: Path,
) -> None:
    # Two pull requests each fit; together they left a.py over budget on main.
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 12, "b.txt": 1}))
    _write(root, "b.txt", 5)
    failures, notes = ratchet.check(root, "HEAD")
    assert failures == []
    assert "did not grow it" in notes[0]
    assert ratchet.main(["--root", str(root), "--base", "HEAD"]) == 0
    # The absolute check still sees it, and --report-only only reports it.
    assert len(ratchet.check(root)[0]) == 1
    assert ratchet.main(["--root", str(root)]) == 1
    assert ratchet.main(["--root", str(root), "--report-only"]) == 0


@pytest.mark.parametrize("before", [10, 12])
def test_a_change_that_grows_a_file_past_its_budget_fails(
    tmp_path: Path, before: int
) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": before}))
    _write(root, "a.py", 13)
    failures, _notes = ratchet.check(root, "HEAD")
    assert len(failures) == 1 and "a.py: 13 lines, budget 10 (+3)" in failures[0]
    assert ratchet.main(["--root", str(root), "--base", "HEAD"]) == 1


def test_growth_within_budget_passes(tmp_path: Path) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 5}))
    _write(root, "a.py", 10)
    assert ratchet.check(root, "HEAD")[0] == []


def test_a_new_file_over_its_budget_fails(tmp_path: Path) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 10}))
    doc = root / ratchet.DOC
    doc.write_text(
        doc.read_text().replace("| `a.py` | 10 |", "| `a.py` | 10 |\n| `n.py` | 1 |")
    )
    _write(root, "n.py", 2)
    failures, _notes = ratchet.check(root, "HEAD")
    assert len(failures) == 1 and failures[0].startswith("n.py: 2 lines")


def test_a_budget_row_edited_below_the_file_fails(tmp_path: Path) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 12 |\n", {"a.py": 12}))
    doc = root / ratchet.DOC
    doc.write_text(doc.read_text().replace("| 12 |", "| 11 |"))
    failures, _notes = ratchet.check(root, "HEAD")
    assert len(failures) == 1 and "a.py: 12 lines, budget 11" in failures[0]


def test_removing_a_listed_file_fails_only_the_change_that_removed_it(
    tmp_path: Path,
) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 10}))
    (root / "a.py").unlink()
    failures, _notes = ratchet.check(root, "HEAD")
    assert len(failures) == 1 and "missing" in failures[0]
    _git(root, "commit", "-q", "-am", "removed without its row")
    failures, notes = ratchet.check(root, "HEAD")
    assert failures == [] and "Already missing on the base" in notes[0]


def test_an_unreadable_base_is_refused(tmp_path: Path) -> None:
    root = _base(_repo(tmp_path, "| `a.py` | 10 |\n", {"a.py": 10}))
    with pytest.raises(ValueError, match="not a commit"):
        ratchet.check(root, "no-such-ref")
    assert ratchet.main(["--root", str(root), "--base", "no-such-ref"]) == 2
