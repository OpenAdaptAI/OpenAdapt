"""Shared test setup: make the repo root importable without pip install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = ("tests._orders_portal",)

from tests import lanes  # noqa: E402 - needs the sys.path line above
from tests._approver_keys import approver  # noqa: E402,F401 - shared fixture
from tests.repo_tree_guard import (  # noqa: E402 - needs the sys.path line above
    capture_baseline,
    check_and_report,
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """In the fast and main lanes, leave out tests in tests/nightly_only.txt."""

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


def _isolation_env(root: Path) -> dict[str, str | None]:
    """One policy for test-time keys and ledgers at either fixture scope."""

    return {
        # Offline Seals must not create keys or a batch queue in the real home.
        "OPENADAPT_SEALED_LOCK_DIR": str(root / ".sealed-locks"),
        "OPENADAPT_SEAL_HOME": str(root / ".seal-home"),
        # Never create or read the real ~/.config/openadapt/frame.key.
        "OPENADAPT_FRAME_SECRET": "11" * 32,
        "OPENADAPT_FRAME_SECRET_FILE": None,
        # The uncertain-write ledger is owned by this fixture, not the user.
        "OPENADAPT_LEDGER_DIR": str(root / ".write-ledger"),
        "OPENADAPT_LEDGER_AUTHORITY_CONFIG": None,
        # Preserve the suite's recorded synthetic-data opt-out. The default
        # encryption and strict modes are covered by test_at_rest_default.py.
        "OPENADAPT_FLOW_PLAINTEXT_AT_REST": "test suite: synthetic data",
        "OPENADAPT_FLOW_AT_REST_HOME": str(root / ".at-rest-home"),
        "OPENADAPT_FLOW_AT_REST": None,
    }


@pytest.fixture(scope="module")
def module_durable_authority(tmp_path_factory: pytest.TempPathFactory):
    """Isolate opted-in module setup before function-scoped fixtures run."""

    root = tmp_path_factory.mktemp("module-authority")
    database = root / "authority.sqlite3"
    # A dependency's teardown also runs when the consuming fixture fails
    # before yield. Function-scoped authority isolation remains independent.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENADAPT_DURABLE_AUTHORITY_DB", str(database))
        for name, value in _isolation_env(root).items():
            if value is None:
                patch.delenv(name, raising=False)
            else:
                patch.setenv(name, value)
        yield database


@pytest.fixture(autouse=True)
def _isolate_durable_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test's monotonic run authority independent and disposable."""

    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB",
        str(tmp_path / ".durable-authority.sqlite3"),
    )
    for name, value in _isolation_env(tmp_path).items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Keep a timed-out Playwright test from failing every later browser test.

    Only after a pytest-timeout failure: a healthy scoped browser fixture
    keeps the loop running between tests. See
    :mod:`tests._playwright_loop_guard`.
    """

    yield
    if call.when != "call" or call.excinfo is None:
        return
    from tests._playwright_loop_guard import (
        clear_leaked_running_loop,
        is_pytest_timeout,
    )

    if is_pytest_timeout(call.excinfo.value):
        clear_leaked_running_loop()


def pytest_configure(config: pytest.Config) -> None:
    # A package-local pyproject.toml cuts off ancestor conftest discovery.
    if not config.pluginmanager.hasplugin("openadapt-headless"):
        from tests import headless_guard

        config.pluginmanager.register(headless_guard, "openadapt-headless")
