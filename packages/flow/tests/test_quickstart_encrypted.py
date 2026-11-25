"""``openadapt quickstart`` on a fresh machine, with encryption at rest on.

The launcher's quickstart tests run against a fake engine, and the Flow suite
opts out of encryption unless ``OPENADAPT_TEST_DEFAULT_AT_REST=1``. So nothing
in CI ran the real quickstart under the default (encrypted) mode, and on
Flow 4.0.0 it exited 1 after "Saved and checked": the tutorial's run summary
was written in plaintext into the encrypted run folder, and sealing the run
refused it.

This test drives the real quickstart in a hidden browser, with encryption on
whatever lane it runs in and an empty ``HOME`` (no key, no seal home), then
opens every sealed folder it wrote and checks that each file on disk is in
that folder's index.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from openadapt_flow import at_rest, crypto, sealed_set

pytest.importorskip("openadapt.quickstart", reason="needs the in-tree launcher")
pytest.importorskip("playwright.sync_api", reason="needs Playwright")


def _browsers_path() -> str:
    """Playwright's browser cache under the real home, before HOME changes."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit:
        return explicit
    home = Path.home()
    if sys.platform == "darwin":
        return str(home / "Library" / "Caches" / "ms-playwright")
    if sys.platform.startswith("win"):
        return str(Path(os.environ.get("LOCALAPPDATA", home)) / "ms-playwright")
    return str(home / ".cache" / "ms-playwright")


@pytest.fixture
def fresh_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    browsers = _browsers_path()
    home = tmp_path / "home"
    home.mkdir()
    for name in (
        at_rest.OPT_OUT_ENV,
        at_rest.MODE_ENV,
        at_rest.ENCRYPT_ENV,
        at_rest.HOME_ENV,
        crypto.ENV_KEY,
        sealed_set.RECORDING_ENV_KEY,
        "OPENADAPT_BUNDLE_KEY",
        "OPENADAPT_POLICY",
        "OPENADAPT_SEAL_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", browsers)
    # The device key goes to a file under the empty home, never a keychain.
    monkeypatch.setenv("OPENADAPT_FLOW_KEY_STORE", "file")
    if sys.platform == "darwin":
        # Playwright's headless shell, never an installed Chrome on a Mac.
        monkeypatch.setenv("OPENADAPT_FLOW_BROWSER", "bundled")
    monkeypatch.chdir(tmp_path)
    at_rest._reset_for_tests()
    yield home
    at_rest._reset_for_tests()


def _unindexed(root: Path) -> list[str]:
    opened = sealed_set.open_set(root)
    opened.verify_all()
    indexed = {
        sealed_set._physical(rel, entry["codec"])
        for rel, entry in opened._files.items()
    }
    return sorted(set(sealed_set._disk_files(root)) - indexed)


def test_quickstart_on_a_fresh_machine_seals_every_run(
    fresh_machine: Path, tmp_path: Path
) -> None:
    from openadapt.quickstart import next_steps, run_quickstart
    from openadapt.quickstart.engine import FlowEngine

    root = tmp_path / "openadapt-quickstart"
    stream = io.StringIO()
    outcome = run_quickstart(
        root,
        visible=False,
        break_it=True,
        verbose=False,
        steps=next_steps(lambda command: command, str, True),
        stream=stream,
        interactive=False,
    )
    output = stream.getvalue()
    runs = len(FlowEngine.inputs)
    assert outcome.checked == runs, output
    assert outcome.broken is not None and outcome.broken.stopped, output
    # Encryption really was on: a key was made under the empty home.
    assert "Created a local encryption key" in output
    assert (fresh_machine / ".openadapt" / "keys").is_dir()

    sealed = sorted(p.parent for p in root.rglob(sealed_set.HEADER))
    names = {p.name for p in sealed}
    assert {"recording", "bundle", "run-1", "run-broken"} <= names, names
    for folder in sealed:
        assert _unindexed(folder) == [], folder

    for index in range(1, runs + 1):
        run_dir = root / f"run-{index}"
        report = sealed_set.read_bytes(run_dir / "report.json")
        assert b'"execution_outcome":"VERIFIED"' in report.replace(b" ", b"")
        # The run sealed: this is the step that failed on Flow 4.0.0.
        assert (run_dir / "seal.json").is_file()
        assert (run_dir / "receipt.json").is_file()
        assert sealed_set.open_set(run_dir).is_file("run-summary.json")
