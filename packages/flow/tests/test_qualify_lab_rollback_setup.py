"""Rollback lab setup refuses missing helpers before contacting a guest."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "scripts" / "desktop"
sys.path.insert(0, str(DESKTOP))
spec = importlib.util.spec_from_file_location(
    "qualify_lab_rollback_setup", DESKTOP / "qualify_lab_rollback.py"
)
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


def test_missing_helpers_refuse_before_lab_construction(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    tools = tmp_path / "tools"
    scripts.mkdir()
    tools.mkdir()
    (scripts / "pn_db.py").write_text("# synthetic helper\n")
    monkeypatch.setattr(rollback, "HERE", scripts)
    monkeypatch.setattr(rollback.winrm_lab, "__file__", str(tools / "winrm_lab.py"))

    def must_not_contact_guest():
        pytest.fail("lab was constructed before all helpers were checked")

    monkeypatch.setattr(rollback.winrm_lab, "WinRMLab", must_not_contact_guest)
    with pytest.raises(FileNotFoundError) as err:
        rollback.run_trial("native", tmp_path / "out", {})
    assert "patient_notes.ps1" in str(err.value)
    assert "foreign_window.ps1" in str(err.value)


def test_helper_search_preserves_precedence_and_requires_files(tmp_path):
    scripts = tmp_path / "scripts"
    tools = tmp_path / "tools"
    scripts.mkdir()
    tools.mkdir()
    for name in ("pn_db.py", "patient_notes.ps1", "foreign_window.ps1"):
        (tools / name).write_text("synthetic\n")
    (scripts / "pn_db.py").write_text("preferred\n")
    (scripts / "foreign_window.ps1").mkdir()
    helpers = rollback._required_helpers((scripts, tools))
    assert helpers == {
        "pn_db.py": scripts / "pn_db.py",
        "patient_notes.ps1": tools / "patient_notes.ps1",
        "foreign_window.ps1": tools / "foreign_window.ps1",
    }


def test_checked_in_helpers_are_complete():
    assert set(rollback._required_helpers((DESKTOP,))) == {
        "pn_db.py",
        "patient_notes.ps1",
        "foreign_window.ps1",
    }


def test_main_does_not_load_a_shared_environment_file(tmp_path, monkeypatch):
    out = tmp_path / "trial"
    evidence = tmp_path / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify",
            "--transport",
            "native",
            "--out",
            str(out),
            "--evidence",
            str(evidence),
        ],
    )

    def forbidden_read(path):
        pytest.fail("the harness tried to read a shared credential file")

    monkeypatch.setattr(rollback.winrm_lab, "load_env_file", forbidden_read)
    monkeypatch.setattr(
        rollback, "run_trial", lambda transport, out, row: row.update(accepted=True)
    )
    assert rollback.main() == 0
    assert json.loads(evidence.read_text())[-1]["accepted"] is True
