"""Admission of an agent-taught JIT draft needs a person's signed teach authorization.

Real pieces throughout: a real approver roster in the key directory, real
approver signatures, the real verifier (the approver verifier's own), the
real recorder signature and the real admission code. Only the canary's run
is a stub report, as in tests/test_jit_lifecycle.py. All data is synthetic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openadapt_flow import teach_authorization as ta
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from openadapt_flow.jit import AdmissionError, lifecycle
from openadapt_flow.jit.lifecycle import admit_repair
from tests import _teach_authorization_support as support
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    REVIEWER,
    _ci,
    _jit,
    _policy_passes,
    _report,
    _review,
    _stub_runs,
)


def _ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw):
    jit = _jit(tmp_path, **kw)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    return jit


def _refusal(jit, monkeypatch) -> str:
    with pytest.raises(AdmissionError) as err:
        jit.admit_with_key(_ci(jit, monkeypatch))
    return str(err.value)


def test_an_authorized_draft_admits_and_keeps_the_statement(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    decision = jit.admit_with_key(_ci(jit, monkeypatch))
    assert decision["outcome"] == "admitted"
    manifest = decision["manifest"]
    assert manifest["teach_authorization"]["authorizer"] == support.AUTHORIZER
    kept = jit.bundle_dir / lifecycle.TEACH_AUTHORIZATION_DIR / ta.STATEMENT_FILE
    assert kept.is_file()
    assert lifecycle.admission_status(jit.program_dir, key_dir=jit.key_dir)


def test_a_draft_without_an_authorization_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch, authorized=False)
    assert "teach_auth_not_recorded" in _refusal(jit, monkeypatch)
    status = lifecycle.admission_status(jit.program_dir, key_dir=jit.key_dir)
    assert status["requirements"]["teach_authorization"]["met"] is False


def test_an_authorizer_removed_after_teaching_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    root = support.roster_root(jit.key_dir)
    root.remove(support.authorizer(jit.key_dir))
    assert "teach_auth_unknown_signer" in _refusal(jit, monkeypatch)


def test_an_unpinned_approver_root_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    monkeypatch.delenv(ROOT_ENV)
    assert "teach_auth_identity_unverified" in _refusal(jit, monkeypatch)


def test_another_roster_root_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer

    monkeypatch.setenv(ROOT_ENV, LocalAuthorizer(tmp_path / "elsewhere").root)
    assert "teach_auth_identity_unverified" in _refusal(jit, monkeypatch)


def test_a_replaced_statement_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    kept = jit.program_dir / lifecycle.TEACH_AUTHORIZATION_DIR
    other = support.issue(jit.key_dir, tmp_path / "other", "t.add_note")
    for name in (ta.STATEMENT_FILE, ta.SIGNATURE_FILE):
        shutil.copy(other.with_name(name), kept / name)
    assert "teach_auth_digest_mismatch" in _refusal(jit, monkeypatch)


def test_an_edited_statement_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    path = jit.program_dir / lifecycle.TEACH_AUTHORIZATION_DIR / ta.STATEMENT_FILE
    data = json.loads(path.read_text())
    data["origins"] = ["http://127.0.0.1:9", "http://127.0.0.1:10"]
    path.write_text(json.dumps(data))
    assert "teach_auth_bad_signature" in _refusal(jit, monkeypatch)


def test_stripping_the_authorization_from_the_candidate_is_refused(
    tmp_path, monkeypatch
):
    jit = _ready(tmp_path, monkeypatch)
    path = jit.program_dir / "candidate.json"
    candidate = json.loads(path.read_text())
    candidate.pop("teach_authorization")
    candidate["demonstrated_by"] = "person"
    path.write_text(json.dumps(candidate))
    # The recorder block is signed; the gate reads it, not the editable fields.
    assert "changed after it was recorded" in _refusal(jit, monkeypatch)


def test_an_authorization_for_another_task_is_refused(tmp_path, monkeypatch):
    jit = _jit(tmp_path, authorized=False)
    program = jit.program_dir
    candidate = json.loads((program / "candidate.json").read_text())
    candidate.pop("recorder")
    candidate.update(support.draft_fields(program, jit.key_dir, "t.other_task"))
    candidate = lifecycle.sign_recorder(program, candidate, key_dir=jit.key_dir)
    (program / "candidate.json").write_text(json.dumps(candidate))
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    assert "teach_auth_task_mismatch" in _refusal(jit, monkeypatch)


def test_a_capture_not_bound_to_the_authorization_is_refused(tmp_path, monkeypatch):
    jit = _jit(tmp_path, authorized=False)
    program = jit.program_dir
    candidate = json.loads((program / "candidate.json").read_text())
    candidate.pop("recorder")
    fields = support.draft_fields(program, jit.key_dir, "t.add_note")
    fields["teach_scope"]["scope"]["authorization"] = None  # an unsigned scope
    candidate.update(fields)
    candidate = lifecycle.sign_recorder(program, candidate, key_dir=jit.key_dir)
    (program / "candidate.json").write_text(json.dumps(candidate))
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    assert "teach_auth_not_bound" in _refusal(jit, monkeypatch)


# -- a repaired copy (self-repair's carried admission) ---------------------------


def _repair_copy(jit, tmp_path: Path) -> Path:
    copy = tmp_path / "repaired"
    shutil.copytree(jit.bundle_dir, copy)
    (copy / lifecycle.ADMISSION_FILE).unlink()
    shutil.rmtree(copy / lifecycle.TEACH_AUTHORIZATION_DIR)
    return copy


def test_a_repair_carries_an_authorization_that_covers_repair(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    jit.admit_with_key(_ci(jit, monkeypatch))
    copy = _repair_copy(jit, tmp_path)
    manifest = admit_repair(
        jit.bundle_dir,
        copy,
        candidate_id="c1",
        approved_by=REVIEWER,
        key_dir=jit.key_dir,
    )
    assert manifest["teach_authorization"]["sha256"]
    assert (copy / lifecycle.TEACH_AUTHORIZATION_DIR / ta.STATEMENT_FILE).is_file()


def test_a_repair_needs_the_repair_purpose(tmp_path, monkeypatch):
    jit = _jit(tmp_path, authorized=False)
    program = jit.program_dir
    candidate = json.loads((program / "candidate.json").read_text())
    candidate.pop("recorder")
    candidate.update(
        support.draft_fields(program, jit.key_dir, "t.add_note", purposes=("teach",))
    )
    candidate = lifecycle.sign_recorder(program, candidate, key_dir=jit.key_dir)
    (program / "candidate.json").write_text(json.dumps(candidate))
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    assert jit.admit_with_key(_ci(jit, monkeypatch))["outcome"] == "admitted"
    copy = _repair_copy(jit, tmp_path)
    with pytest.raises(AdmissionError, match="teach_auth_purpose"):
        admit_repair(
            jit.bundle_dir,
            copy,
            candidate_id="c1",
            probe_only=True,
            key_dir=jit.key_dir,
        )


def test_a_repair_after_the_authorizer_was_removed_is_refused(tmp_path, monkeypatch):
    jit = _ready(tmp_path, monkeypatch)
    jit.admit_with_key(_ci(jit, monkeypatch))
    support.roster_root(jit.key_dir).remove(support.authorizer(jit.key_dir))
    with pytest.raises(AdmissionError, match="teach_auth_unknown_signer"):
        admit_repair(
            jit.bundle_dir,
            _repair_copy(jit, tmp_path),
            candidate_id="c1",
            approved_by=REVIEWER,
            key_dir=jit.key_dir,
        )
