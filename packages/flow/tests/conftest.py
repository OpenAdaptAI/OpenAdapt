"""Shared test setup: make the repo root importable without pip install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.repo_tree_guard import (  # noqa: E402 - needs the sys.path line above
    capture_baseline,
    check_and_report,
)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot the tracked-file status before the first test runs."""

    capture_baseline(session.config, REPO_ROOT)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when the run dirtied tracked repository files.

    See :mod:`tests.repo_tree_guard` for why this matters and how to opt out.
    """

    check_and_report(session, exitstatus, REPO_ROOT)


@pytest.fixture(autouse=True)
def _isolate_durable_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test's monotonic run authority independent and disposable."""

    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        str(tmp_path / ".durable-authority.sqlite3"),
    )
