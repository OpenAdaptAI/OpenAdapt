"""Shared test fixtures for OpenAdapt Desktop engine tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from openadapt_desktop.config import EngineConfig


@pytest.fixture(scope="module")
def source_flow_package() -> Path | None:
    """Bind the source contract to this checkout's installed Flow package."""
    mode = os.environ.get("OPENADAPT_DESKTOP_FLOW_CONTRACT", "bundled")
    assert mode in {"bundled", "source"}, "unknown Desktop Flow contract mode"
    if mode == "bundled":
        return None
    assert os.environ.get("OPENADAPT_DESKTOP_REQUIRE_BUNDLED_FLOW") != "1", (
        "a bundled Flow qualification cannot run in source mode"
    )

    import importlib
    import tomllib
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("openadapt-flow")
    except PackageNotFoundError:
        pytest.fail("the checkout's Flow distribution is required in source mode")
    source = Path(__file__).resolve().parents[2] / "flow"
    package = (source / "openadapt_flow").resolve()
    module = importlib.import_module("openadapt_flow")
    assert Path(module.__file__).resolve() == package / "__init__.py"
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    assert installed == project["project"]["version"]
    return package


class FakeRecorder:
    """Inert recorder used by unit tests; live capture is contract-tested separately."""

    def __init__(self, capture_dir: str, task_description: str = "") -> None:
        self.capture_dir = capture_dir
        self.task_description = task_description
        self.event_count = 0
        self.is_recording = False

    def __enter__(self):
        self.is_recording = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.is_recording = False

    def wait_for_ready(self, timeout: float = 60) -> bool:
        return self.is_recording

    def stop(self) -> None:
        self.is_recording = False


@pytest.fixture(autouse=True)
def fake_native_recorder(monkeypatch):
    """Prevent unit tests from injecting real mouse/keyboard/screen capture."""

    monkeypatch.setattr("openadapt_desktop.controller._load_capture_recorder", lambda: FakeRecorder)


@pytest.fixture(autouse=True)
def no_real_secure_enclave(monkeypatch):
    """Keep tests off this Mac's Secure Enclave and the in-app approver key.

    The engine's default approver keychain would probe the Secure Enclave and
    read the login Keychain. Tests get a computer without one (the terminal
    path) unless they build an ``ApproverKeychain`` with a fake enclave. The
    real-hardware test (tests/test_approver_keychain_real.py) makes its own.
    """

    from openadapt_desktop import approver_keychain

    monkeypatch.setattr(approver_keychain, "default_enclave", approver_keychain.NoEnclave)
    monkeypatch.setattr(approver_keychain, "_DEFAULT", [])


@pytest.fixture(autouse=True)
def restore_umask():
    """The engine entry points set a private umask; keep it from leaking between tests."""

    previous = os.umask(0o022)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory for tests."""
    data_dir = tmp_path / ".openadapt"
    data_dir.mkdir()
    (data_dir / "captures").mkdir()
    (data_dir / "archive").mkdir()
    (data_dir / "tombstones").mkdir()
    return data_dir


@pytest.fixture
def config(tmp_data_dir: Path) -> EngineConfig:
    """Create a test configuration pointing to the temporary data directory."""
    return EngineConfig(
        data_dir=tmp_data_dir,
        storage_mode="air-gapped",
        max_storage_gb=1.0,
        log_level="DEBUG",
    )


@pytest.fixture
def sample_capture_dir(tmp_data_dir: Path) -> Path:
    """Create a sample capture directory with minimal structure."""
    capture_dir = tmp_data_dir / "captures" / "2026-03-02_14-30-00_test123"
    capture_dir.mkdir(parents=True)
    (capture_dir / "video").mkdir()
    (capture_dir / "screenshots").mkdir()

    # Write minimal meta.json
    import json

    meta = {
        "capture_id": "test123",
        "started_at": "2026-03-02T14:30:00Z",
        "platform": "test",
        "screen_size": [1920, 1080],
    }
    (capture_dir / "meta.json").write_text(json.dumps(meta))

    return capture_dir


@pytest.fixture(autouse=True)
def _isolated_flow_at_rest(tmp_path_factory, monkeypatch):
    """Keep Flow's at-rest key and plaintext ledger out of the real home.

    Flow encrypts at rest by default (openadapt_flow.at_rest); these tests
    use its recorded opt-out, as the Flow suite does.
    """
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path_factory.mktemp("at-rest-home")))
    monkeypatch.setenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", "test suite: synthetic data")
    monkeypatch.delenv("OPENADAPT_FLOW_AT_REST", raising=False)
    # Never the OS keychain from tests: no dialog, no login-keychain write.
    monkeypatch.setenv("OPENADAPT_FLOW_KEY_STORE", "file")
    # The stop queue also lists Flow's exception registry under
    # OPENADAPT_HOME; keep a developer's real cards out of every test.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path_factory.mktemp("openadapt-home")))


@pytest.fixture(autouse=True)
def no_usage_stats_or_update_check(monkeypatch):
    """Keep the suite from sending usage stats or asking GitHub for releases.

    A developer machine may have said Share; tests that exercise either path
    clear these and point ``OPENADAPT_CONFIG_TOML`` at a temporary file.
    """

    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("OPENADAPT_UPDATE_CHECK", "false")


# On the hosted macOS runners a forked copy of the pytest process sometimes never
# reached exec, and pytest then waited inside Popen until the job was cancelled.
# The whole session therefore starts subprocesses without forking.
#
# Python starts a child with posix_spawn instead of fork when the program path
# is absolute, no working directory or new session is asked for, and
# ``close_fds`` is false. Most calls here and in the engine fail only the last
# condition, because ``close_fds`` defaults to true, so the session changes that
# default. Python opens its own descriptors close-on-exec, so a child still
# inherits only its standard streams. A call that would fork anyway fails with
# the reason, unless the test asks for the ``forking_allowed`` fixture.
# Windows has no fork and is left alone. Tests of how the engine itself starts a
# process ask for ``python_subprocess_defaults`` and get none of this.
_FORKS: list[str] = []
_REAL: dict[str, object] = {}
_FORKING_ALLOWED = False


def _spawn_is_available() -> bool:
    return bool(getattr(subprocess, "_USE_POSIX_SPAWN", False))


@pytest.fixture(scope="session", autouse=True)
def _subprocesses_do_not_fork():
    if not _spawn_is_available():
        yield
        return

    patch = pytest.MonkeyPatch()
    real_init = subprocess.Popen.__init__
    real_fork_exec = subprocess._fork_exec

    def init(self, *args, **kwargs):
        # close_fds is the eighth positional parameter of Popen.
        if len(args) < 8 and "close_fds" not in kwargs and not kwargs.get("pass_fds"):
            kwargs["close_fds"] = False
        real_init(self, *args, **kwargs)

    def fork_exec(args, *rest, **named):
        command = " ".join(os.fsdecode(part) for part in args)
        if not _FORKING_ALLOWED:
            raise AssertionError(
                f"{command!r} would fork pytest. Give the program's absolute path, "
                "do not pass cwd, start_new_session, preexec_fn or pass_fds, "
                "and run git through native_release.run_git."
            )
        _FORKS.append(command)
        return real_fork_exec(args, *rest, **named)

    _REAL.update(init=real_init, fork_exec=real_fork_exec)
    patch.setattr(subprocess.Popen, "__init__", init)
    patch.setattr(subprocess, "_fork_exec", fork_exec)
    yield
    patch.undo()
    _REAL.clear()


@pytest.fixture
def python_subprocess_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give this test Python's own subprocess behaviour, as the installed engine has it.

    A test that checks how production code starts a process must not get the
    session's ``close_fds`` default, or it would pass whatever the code did.
    """

    if _REAL:
        monkeypatch.setattr(subprocess.Popen, "__init__", _REAL["init"])
        monkeypatch.setattr(subprocess, "_fork_exec", _REAL["fork_exec"])


@pytest.fixture
def forking_allowed():
    """Let this test start subprocesses that Python can only start with fork."""

    global _FORKING_ALLOWED
    _FORKING_ALLOWED = True
    yield
    _FORKING_ALLOWED = False


def pytest_terminal_summary(terminalreporter) -> None:
    if _spawn_is_available():
        terminalreporter.write_line(f"subprocesses that forked pytest: {len(_FORKS)}")
        for command in _FORKS:
            terminalreporter.write_line(f"  forked: {command[:200]}")
