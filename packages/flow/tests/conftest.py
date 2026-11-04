"""Shared test setup: make the repo root importable without pip install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import lanes  # noqa: E402 - needs the sys.path line above
from tests._approver_keys import approver  # noqa: E402,F401 - shared fixture
from tests.repo_tree_guard import (  # noqa: E402 - needs the sys.path line above
    capture_baseline,
    check_and_report,
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """In the fast lane, leave out the tests in tests/nightly_only.txt."""

    lanes.deselect(config, items)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot the tracked-file status before the first test runs."""

    capture_baseline(session.config, REPO_ROOT)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when the run dirtied tracked repository files.

    See :mod:`tests.repo_tree_guard` for why this matters and how to opt out.
    """

    check_and_report(session, exitstatus, REPO_ROOT)


@pytest.fixture(autouse=True)
def _teach_authorization_env(monkeypatch: pytest.MonkeyPatch):
    """Lets tests/_teach_authorization_support.py pin an approver root for one test."""

    from tests import _teach_authorization_support as support

    support._MONKEYPATCH[:] = [monkeypatch]
    yield
    support._MONKEYPATCH.clear()
    support._AUTHORIZERS.clear()


@pytest.fixture(autouse=True)
def _isolate_durable_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test's monotonic run authority independent and disposable."""

    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        str(tmp_path / ".durable-authority.sqlite3"),
    )
    # Offline Seals are on by default; keep each test's Seal key and batch
    # queue out of the real home directory.
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / ".seal-home"))
    # The frame's host secret: a per-test value, so a test never creates or
    # reads the real ~/.config/openadapt/frame.key.
    monkeypatch.setenv("OPENADAPT_FRAME_SECRET", "11" * 32)
    # The uncertain-write ledger: per test, never ~/.openadapt/ledger.
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / ".write-ledger"))
    monkeypatch.delenv("OPENADAPT_FRAME_SECRET_FILE", raising=False)
    # Encryption at rest is the default (openadapt_flow.at_rest). The suite
    # keeps the plaintext behaviour its fixtures assert, under the recorded
    # opt-out, and keeps the local key and the ledger out of the real home.
    # tests/test_at_rest_default.py covers the default and strict modes.
    monkeypatch.setenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", "test suite: synthetic data")
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / ".at-rest-home"))
    monkeypatch.delenv("OPENADAPT_FLOW_AT_REST", raising=False)


@pytest.fixture(autouse=True)
def _clear_leaked_playwright_loop():
    """Keep a timed-out Playwright test from failing every later browser test.

    See :mod:`tests._playwright_loop_guard`.
    """

    yield
    from tests._playwright_loop_guard import clear_leaked_running_loop

    clear_leaked_running_loop()
