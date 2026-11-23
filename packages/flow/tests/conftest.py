"""Shared test setup: make the repo root importable without pip install."""

from __future__ import annotations

import os
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
    """In the fast and main lanes, leave out tests in tests/nightly_only.txt.
    Under OPENADAPT_TEST_DEFAULT_AT_REST=1, skip the tests listed in
    tests/default_at_rest_skips.txt, each with its stated reason."""

    lanes.deselect(config, items)
    if os.environ.get("OPENADAPT_TEST_DEFAULT_AT_REST") == "1":
        _skip_default_at_rest(items)


_DEFAULT_AT_REST_SKIPS = Path(__file__).parent / "default_at_rest_skips.txt"


def _skip_default_at_rest(items: list[pytest.Item]) -> None:
    """Tests that read or plant a run, recording or bundle file directly as
    plaintext. Under the default (everything encrypted) the product refuses
    that file, correctly, so the test can't observe what it checks. Each line
    is ``<node id prefix> | <reason>``; a prefix may name a whole file."""
    if not _DEFAULT_AT_REST_SKIPS.is_file():
        return
    rules: dict[str, str] = {}
    for line in _DEFAULT_AT_REST_SKIPS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        prefix, _, reason = line.partition(" | ")
        rules[prefix.strip()] = reason.strip()
    for item in items:
        nodeid = item.nodeid.removeprefix("packages/flow/").split("[", 1)[0]
        parts = nodeid.split("::")
        # The test itself, its class, or its whole file.
        for depth in range(len(parts), 0, -1):
            reason = rules.get("::".join(parts[:depth]))
            if reason is not None:
                item.add_marker(pytest.mark.skip(reason=f"default at rest: {reason}"))
                break


_SESSION_ENV: dict[str, str | None] = {}
#: The at-rest home of an OPENADAPT_TEST_DEFAULT_AT_REST=1 session.
_SESSION_AT_REST_HOME: str | None = None


def _configure_at_rest() -> None:
    """Session-wide, before any fixture of any scope: module and session
    fixtures (an e2e recording compiled once per module) must not seal their
    output under a key a later per-test home can't open, and nothing may reach
    the person's OS keychain. The per-test isolation sets these again."""

    global _SESSION_AT_REST_HOME
    wanted = {"OPENADAPT_FLOW_KEY_STORE": "file"}
    if os.environ.get("OPENADAPT_TEST_DEFAULT_AT_REST") == "1":
        import tempfile

        _SESSION_AT_REST_HOME = os.path.realpath(
            tempfile.mkdtemp(prefix="openadapt-at-rest-home-")
        )
        wanted["OPENADAPT_FLOW_AT_REST_HOME"] = _SESSION_AT_REST_HOME
    if os.environ.get("OPENADAPT_TEST_DEFAULT_AT_REST") != "1":
        wanted["OPENADAPT_FLOW_PLAINTEXT_AT_REST"] = os.environ.get(
            "OPENADAPT_FLOW_PLAINTEXT_AT_REST", "test suite: synthetic data"
        )
    for name, value in wanted.items():
        _SESSION_ENV[name] = os.environ.get(name)
        os.environ[name] = value


def pytest_unconfigure(config: pytest.Config) -> None:
    for name, value in _SESSION_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


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
        # Under the default mode, outside the test's own directories too: a
        # lock file inside a directory a test seals is a stray file there.
        "OPENADAPT_SEALED_LOCK_DIR": str(
            Path(_SESSION_AT_REST_HOME) / "sealed-locks"
            if _SESSION_AT_REST_HOME
            else root / ".sealed-locks"
        ),
        "OPENADAPT_SEAL_HOME": str(root / ".seal-home"),
        # Never create or read the real ~/.config/openadapt/frame.key.
        "OPENADAPT_FRAME_SECRET": "11" * 32,
        "OPENADAPT_FRAME_SECRET_FILE": None,
        # The uncertain-write ledger is owned by this fixture, not the user.
        "OPENADAPT_LEDGER_DIR": str(root / ".write-ledger"),
        "OPENADAPT_LEDGER_AUTHORITY_CONFIG": None,
        # Preserve the suite's recorded synthetic-data opt-out. The default
        # encryption and strict modes are covered by test_at_rest_default.py.
        # OPENADAPT_TEST_DEFAULT_AT_REST=1 runs the suite under the real
        # default (everything sealed), to find readers that assume plaintext.
        "OPENADAPT_FLOW_PLAINTEXT_AT_REST": None
        if os.environ.get("OPENADAPT_TEST_DEFAULT_AT_REST") == "1"
        else "test suite: synthetic data",
        # Under the default mode, one device key per test session (per xdist
        # worker), outside every test's own directories: a module fixture
        # sealed in one test must open in the next, and the key files must
        # never land inside a recording or run directory a test seals.
        "OPENADAPT_FLOW_AT_REST_HOME": _SESSION_AT_REST_HOME
        or str(root / ".at-rest-home"),
        "OPENADAPT_FLOW_AT_REST": None,
        # The device key goes to a file in the isolated home, never to the
        # OS keychain (no dialog, no write to the person's login keychain).
        "OPENADAPT_FLOW_KEY_STORE": "file",
        "OPENADAPT_ENCRYPT_AT_REST": None,
        "OPENADAPT_POLICY": None,
        "OPENADAPT_RECOVERY_CODE": None,
        "OPENADAPT_ORG_KEY_FILE": None,
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
    _configure_at_rest()
    # A package-local pyproject.toml cuts off ancestor conftest discovery.
    if not config.pluginmanager.hasplugin("openadapt-headless"):
        from tests import headless_guard

        config.pluginmanager.register(headless_guard, "openadapt-headless")
