"""A repaired copy of an admitted agent-compiled program keeps its admission.

Silent-saves 1.1.0 (#1380) found that self-repair could never probe or resume
a program compiled from an agent run: every patch changes the bundle's
digest, and the admission pins it, so the run gate refused the patched copy
("DraftNotAdmitted"). A repair keeps the record check, so the admission now
carries over: for the probe only (a run that stops before the save), then,
after a person approves the fix, for every run. No browser and no model.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.jit import AdmissionError, lifecycle
from openadapt_flow.jit.lifecycle import DraftNotAdmitted, admit_repair, ensure_runnable
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    _admitted,
    _policy_passes,
)


def _copy(jit, tmp_path: Path, name: str, edit) -> Path:
    out = tmp_path / name
    shutil.copytree(jit.bundle_dir, out)
    (out / lifecycle.ADMISSION_FILE).unlink()
    workflow = Workflow.load(out)
    edit(workflow)
    workflow.save(out)
    return out


def _reanchor(workflow: Workflow) -> None:
    # A repair re-targets steps; the record check (the save's contract) stays.
    workflow.steps[0].text = "y"


def _recontract(workflow: Workflow) -> None:
    workflow.steps[1].effects[0].field = "summary"


def test_the_patched_copy_is_refused_without_a_carried_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    shutil.copytree(jit.bundle_dir, tmp_path / "copy")
    workflow = Workflow.load(tmp_path / "copy")
    _reanchor(workflow)
    workflow.save(tmp_path / "copy")
    with pytest.raises(DraftNotAdmitted, match="not admitted"):
        ensure_runnable(tmp_path / "copy", key_dir=jit.key_dir)


def test_a_probe_admission_runs_only_behind_a_gate_that_stops_before_the_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _reanchor)
    manifest = admit_repair(
        jit.bundle_dir,
        repaired,
        candidate_id="cand-1",
        probe_only=True,
        key_dir=jit.key_dir,
    )
    assert manifest["admission_mode"] == "repair_probe"
    assert (
        manifest["repair_of"]["prior_digest"]
        == json.loads((jit.bundle_dir / lifecycle.ADMISSION_FILE).read_text())["digest"]
    )
    with pytest.raises(DraftNotAdmitted, match="has not approved"):
        ensure_runnable(repaired, key_dir=jit.key_dir)
    ensure_runnable(repaired, key_dir=jit.key_dir, stops_before_save=True)


def test_a_person_approved_repair_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _reanchor)
    manifest = admit_repair(
        jit.bundle_dir,
        repaired,
        candidate_id="cand-1",
        approved_by="decision_portal:person",
        route="decision_portal",
        key_dir=jit.key_dir,
    )
    assert manifest["admission_mode"] == "repair"
    assert (
        manifest["record_check_digest"]
        == manifest["repair_of"]["prior_manifest"]["record_check_digest"]
    )
    ensure_runnable(repaired, key_dir=jit.key_dir)
    assert (
        lifecycle.verify_admission(repaired, key_dir=jit.key_dir)["digest"]
        != (manifest["repair_of"]["prior_digest"])
    )


def test_a_repair_of_a_repair_keeps_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    first = _copy(jit, tmp_path, "first", _reanchor)
    admit_repair(
        jit.bundle_dir,
        first,
        candidate_id="cand-1",
        approved_by="Dana Reviewer",
        key_dir=jit.key_dir,
    )
    second = tmp_path / "second"
    shutil.copytree(first, second)
    workflow = Workflow.load(second)
    workflow.steps[0].text = "z"
    workflow.save(second)
    admit_repair(
        first,
        second,
        candidate_id="cand-2",
        approved_by="standard-change:policy-1",
        route="standard_change",
        key_dir=jit.key_dir,
    )
    ensure_runnable(second, key_dir=jit.key_dir)


def test_a_repair_that_changes_the_record_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _recontract)
    with pytest.raises(AdmissionError, match="record check"):
        admit_repair(
            jit.bundle_dir,
            repaired,
            candidate_id="cand-1",
            approved_by="Dana Reviewer",
            key_dir=jit.key_dir,
        )


@pytest.mark.parametrize("who", ["", "agent:repair-bot", "model:claude"])
def test_only_a_person_or_a_standard_change_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _reanchor)
    with pytest.raises(AdmissionError):
        admit_repair(
            jit.bundle_dir,
            repaired,
            candidate_id="cand-1",
            approved_by=who,
            key_dir=jit.key_dir,
        )


def test_an_edited_repair_admission_does_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _reanchor)
    admit_repair(
        jit.bundle_dir,
        repaired,
        candidate_id="cand-1",
        approved_by="Dana Reviewer",
        key_dir=jit.key_dir,
    )
    path = repaired / lifecycle.ADMISSION_FILE
    manifest = json.loads(path.read_text())
    manifest["repair_of"]["prior_manifest"]["params"] = ["anything"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(DraftNotAdmitted):
        ensure_runnable(repaired, key_dir=jit.key_dir)


def test_a_probe_admission_cannot_be_carried_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    probe = _copy(jit, tmp_path, "probe", _reanchor)
    admit_repair(
        jit.bundle_dir,
        probe,
        candidate_id="cand-1",
        probe_only=True,
        key_dir=jit.key_dir,
    )
    again = tmp_path / "again"
    shutil.copytree(probe, again)
    workflow = Workflow.load(again)
    workflow.steps[0].text = "z"
    workflow.save(again)
    with pytest.raises(AdmissionError, match="probe"):
        admit_repair(
            probe,
            again,
            candidate_id="cand-2",
            approved_by="Dana Reviewer",
            key_dir=jit.key_dir,
        )
