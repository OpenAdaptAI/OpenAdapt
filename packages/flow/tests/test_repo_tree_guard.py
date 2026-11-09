"""Tests for the guard that fails a run which dirties tracked files.

The guard itself is what stops a test from writing into the checkout (the
``docs/showcase-openemr/bundle`` incident), so it needs its own coverage:
a guard that silently stops working is worse than none.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.repo_tree_guard import (
    OPT_OUT_ENV,
    format_failure,
    guard_is_enabled,
    new_dirty_entries,
    tracked_status,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Guard Test",
            "GIT_AUTHOR_EMAIL": "guard@example.invalid",
            "GIT_COMMITTER_NAME": "Guard Test",
            "GIT_COMMITTER_EMAIL": "guard@example.invalid",
        },
    )


def _repo_with_tracked_file(root: Path) -> Path:
    """Create a git repo holding one committed file and return that file."""

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    tracked = root / "golden.txt"
    tracked.write_text("original\n")
    _git(root, "add", "golden.txt")
    _git(root, "commit", "--quiet", "-m", "seed")
    return tracked


# -- opt-out -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", " 1 "])
def test_opt_out_values_disable_the_guard(value: str) -> None:
    assert guard_is_enabled({OPT_OUT_ENV: value}) is False


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_other_values_leave_the_guard_on(value: str) -> None:
    assert guard_is_enabled({OPT_OUT_ENV: value}) is True


def test_guard_is_on_by_default() -> None:
    assert guard_is_enabled({}) is True


# -- tracked_status ----------------------------------------------------------


def test_tracked_status_is_empty_for_a_clean_repo(tmp_path: Path) -> None:
    _repo_with_tracked_file(tmp_path / "repo")
    assert tracked_status(tmp_path / "repo") == ()


def test_tracked_status_reports_a_modified_tracked_file(tmp_path: Path) -> None:
    tracked = _repo_with_tracked_file(tmp_path / "repo")
    tracked.write_text("mutated\n")
    status = tracked_status(tmp_path / "repo")
    assert status is not None
    assert any("golden.txt" in entry for entry in status)


def test_tracked_status_reports_a_deleted_tracked_file(tmp_path: Path) -> None:
    tracked = _repo_with_tracked_file(tmp_path / "repo")
    tracked.unlink()
    status = tracked_status(tmp_path / "repo")
    assert status is not None
    assert any(entry.strip().startswith("D") for entry in status)


def test_tracked_status_ignores_untracked_files(tmp_path: Path) -> None:
    _repo_with_tracked_file(tmp_path / "repo")
    (tmp_path / "repo" / "scratch.log").write_text("build output\n")
    assert tracked_status(tmp_path / "repo") == ()


def test_tracked_status_is_unknown_outside_a_repo(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    # Only assert the contract when the directory really is outside a work
    # tree: on some machines tmp_path could sit inside one.
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:  # pragma: no cover - environment dependent
        pytest.skip("tmp_path is inside a git work tree")
    assert tracked_status(outside) is None


# -- new_dirty_entries -------------------------------------------------------


def test_new_dirty_entries_reports_only_what_the_run_added() -> None:
    before = (" M already_dirty.py",)
    after = (" M already_dirty.py", " M docs/showcase-openemr/bundle/templates/a.png")
    assert new_dirty_entries(before, after) == (
        " M docs/showcase-openemr/bundle/templates/a.png",
    )


def test_new_dirty_entries_is_empty_when_nothing_changed() -> None:
    same = (" M already_dirty.py",)
    assert new_dirty_entries(same, same) == ()


def test_new_dirty_entries_ignores_a_path_that_became_clean() -> None:
    assert new_dirty_entries((" M a.py",), ()) == ()


@pytest.mark.parametrize(
    ("before", "after"),
    [(None, (" M a.py",)), ((" M a.py",), None), (None, None)],
)
def test_an_unknown_snapshot_never_accuses(
    before: tuple[str, ...] | None,
    after: tuple[str, ...] | None,
) -> None:
    assert new_dirty_entries(before, after) == ()


# -- format_failure ----------------------------------------------------------


def test_format_failure_names_the_paths_and_the_opt_out() -> None:
    message = format_failure((" M docs/showcase-openemr/bundle/README.md",))
    assert "docs/showcase-openemr/bundle/README.md" in message
    assert OPT_OUT_ENV in message
    assert "tmp_path" in message


def test_format_failure_truncates_a_long_list() -> None:
    entries = tuple(f" M file_{index}.png" for index in range(30))
    message = format_failure(entries, limit=5)
    assert "30 newly dirty tracked path(s)" in message
    assert "... and 25 more" in message
    assert "file_5.png" not in message


# -- end to end --------------------------------------------------------------

_CONFTEST = """
import sys
from pathlib import Path

sys.path.insert(0, {repo_root!r})

from tests.repo_tree_guard import capture_baseline, check_and_report

ROOT = Path(__file__).resolve().parent


def pytest_sessionstart(session):
    capture_baseline(session.config, ROOT)


def pytest_sessionfinish(session, exitstatus):
    check_and_report(session, exitstatus, ROOT)
"""

_WELL_BEHAVED_TEST = """
def test_writes_to_its_own_directory(tmp_path):
    (tmp_path / "golden.txt").write_text("copy")
"""

_MISBEHAVING_TEST = """
from pathlib import Path


def test_writes_into_the_checkout():
    Path(__file__).resolve().parent.joinpath("golden.txt").write_text("mutated\\n")
"""


def _run_inner_pytest(root: Path, *, opt_out: bool) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.pop(OPT_OUT_ENV, None)
    if opt_out:
        env[OPT_OUT_ENV] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
    )


def _prepare(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "repo"
    _repo_with_tracked_file(root)
    (root / "conftest.py").write_text(
        textwrap.dedent(_CONFTEST).format(repo_root=str(REPO_ROOT))
    )
    (root / "test_inner.py").write_text(textwrap.dedent(body))
    _git(root, "add", "conftest.py", "test_inner.py")
    _git(root, "commit", "--quiet", "-m", "tests")
    return root


def test_a_test_that_writes_into_the_checkout_fails_the_run(tmp_path: Path) -> None:
    root = _prepare(tmp_path, _MISBEHAVING_TEST)

    completed = _run_inner_pytest(root, opt_out=False)

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout, completed.stdout
    assert "repository tree guard" in completed.stdout
    assert "changed during the test run" in completed.stdout
    assert "golden.txt" in completed.stdout


def test_a_well_behaved_test_run_passes(tmp_path: Path) -> None:
    root = _prepare(tmp_path, _WELL_BEHAVED_TEST)

    completed = _run_inner_pytest(root, opt_out=False)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "repository tree guard" not in completed.stdout


def test_the_opt_out_environment_variable_disables_the_guard(tmp_path: Path) -> None:
    root = _prepare(tmp_path, _MISBEHAVING_TEST)

    completed = _run_inner_pytest(root, opt_out=True)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "repository tree guard" not in completed.stdout
