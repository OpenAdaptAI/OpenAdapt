"""The qualification attempt ledger on real Windows directories.

These run only on Windows: a junction, a rename while the ledger is held, and
a ledger outside the user's profile are real file-system objects, not mocks.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows file system")

from tests.test_run_gate import (  # noqa: E402
    _KEY,
    _representative_qualification_bundle,
)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def test_a_held_ledger_cannot_be_renamed(tmp_path):
    from openadapt_flow._windows_directory import hold_private_directory

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    held = hold_private_directory(ledger)
    try:
        with pytest.raises(PermissionError):
            os.rename(ledger, tmp_path / "moved")
    finally:
        held.close()
    os.rename(ledger, tmp_path / "moved")


def test_a_junction_is_refused(tmp_path):
    from openadapt_flow._windows_directory import hold_private_directory

    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "ledger"
    _junction(link, target)
    with pytest.raises(ValueError, match="not a private directory"):
        hold_private_directory(link)


def test_a_ledger_outside_the_profile_is_refused():
    from openadapt_flow._windows_directory import hold_private_directory

    outside = Path(os.environ["SystemDrive"] + "\\") / f"oa-ledger-{uuid.uuid4().hex}"
    try:
        outside.mkdir()
    except OSError:
        pytest.skip("cannot create a folder outside the profile here")
    try:
        with pytest.raises(ValueError, match="outside this user's profile"):
            hold_private_directory(outside)
    finally:
        outside.rmdir()


def _run_case(main, bundle, inputs_path, run_dir):
    return main.main(
        [
            "qualify",
            "run-case",
            str(bundle),
            "--case-id",
            "representative-1",
            "--inputs",
            str(inputs_path),
            "--campaign-id",
            "campaign-1",
            "--run-id",
            "run-1",
            "--run-dir",
            str(run_dir),
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor.local",
            "--backend",
            "web",
        ]
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    import openadapt_flow.__main__ as main

    _workflow, bundle, inputs = _representative_qualification_bundle(tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    inputs_path = tmp_path / "case-inputs.json"
    inputs_path.write_bytes(inputs)
    called = []
    monkeypatch.setattr(main, "_cmd_replay", lambda args: called.append(args) or 0)
    return main, bundle, inputs_path, called


def test_one_attempt_is_claimed_once(case, tmp_path, monkeypatch, capsys):
    main, bundle, inputs_path, called = case
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "state"))
    assert _run_case(main, bundle, inputs_path, tmp_path / "run") == 0
    assert len(called) == 1
    assert list((tmp_path / "state" / "qualification-attempts").glob("*.json"))
    assert _run_case(main, bundle, inputs_path, tmp_path / "run") != 0
    assert "already started" in capsys.readouterr().out
    assert len(called) == 1


def test_a_junctioned_ledger_refuses_before_any_actuator(
    case, tmp_path, monkeypatch, capsys
):
    main, bundle, inputs_path, called = case
    state = tmp_path / "state"
    state.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _junction(state / "qualification-attempts", elsewhere)
    monkeypatch.setenv("OPENADAPT_HOME", str(state))
    assert _run_case(main, bundle, inputs_path, tmp_path / "run") != 0
    assert "not available safely" in capsys.readouterr().out
    assert called == []
    assert list(elsewhere.iterdir()) == []
