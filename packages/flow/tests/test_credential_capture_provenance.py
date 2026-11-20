"""Capture privacy follows verified provenance, not writable cache metadata.

These tests use synthetic local signing keys and normal lifecycle helpers.
Canary execution is stubbed: they qualify signatures and reuse refusals,
not a browser run or a real saved effect.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.jit import AdmissionError, lifecycle
from tests.test_jit_lifecycle import (  # noqa: F401 - autouse policy fixture
    REVIEWER,
    _jit,
    _policy_passes,
    _report,
    _review,
    _stub_runs,
)
from tests.test_jit_repair_admission import _copy, _reanchor

MARKER = "credential_withholding"
MISMATCH = "CREDENTIAL_CAPTURE_PRIVACY_MISMATCH"
_ABSENT = object()


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _signed_draft_marker(jit: Any, marker: Any = _ABSENT) -> dict[str, Any]:
    """Model the stored bytes a real old or current recorder signed.

    In particular, an old block OMITTED the new field. Signing a new block
    containing null would not exercise old-signature compatibility.
    """
    path = jit.program_dir / lifecycle.CANDIDATE_FILE
    candidate = json.loads(path.read_text(encoding="utf-8"))
    block = dict(candidate["recorder"])
    if marker is _ABSENT:
        candidate.pop(MARKER, None)
        block.pop(MARKER, None)
    else:
        candidate[MARKER] = marker
        block[MARKER] = marker
    block["signature"] = lifecycle._sign_record(block, jit.key_dir)
    candidate["recorder"] = block
    _write(path, candidate)
    return candidate


def _draft(tmp_path: Path, marker: Any = _ABSENT) -> Any:
    jit = _jit(tmp_path)
    _signed_draft_marker(jit, marker)
    return jit


def _admit(jit: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Use normal review/canary/admission gates with a stubbed canary."""
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    monkeypatch.setattr(lifecycle, "_has_terminal", lambda: True)
    return jit.admit(principal_id=REVIEWER, confirm="ok")


def _signed_admission_marker(jit: Any, marker: Any = _ABSENT) -> dict[str, Any]:
    """Sign a synthetic person manifest with an absent or malformed field."""
    path = jit.bundle_dir / lifecycle.ADMISSION_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["admission_mode"] == "person"
    if marker is _ABSENT:
        manifest.pop(MARKER, None)
    else:
        manifest[MARKER] = marker
    manifest["signature"] = lifecycle._sign_record(manifest, jit.key_dir)
    _write(path, manifest)
    return manifest


def test_absent_legacy_recorder_field_keeps_its_real_signature(tmp_path: Path) -> None:
    jit = _draft(tmp_path)
    stored = json.loads(
        (jit.program_dir / lifecycle.CANDIDATE_FILE).read_text(encoding="utf-8")
    )
    assert MARKER not in stored and MARKER not in stored["recorder"]
    assert lifecycle._record_signed(stored["recorder"], jit.key_dir)
    verified = lifecycle._recorder(jit.program_dir, jit.key_dir)
    assert MARKER not in verified
    assert jit.withhold_credentials is False
    with pytest.raises(AdmissionError, match=MISMATCH):
        lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


def test_absent_legacy_admission_still_runs_for_default_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _draft(tmp_path)
    _admit(jit, monkeypatch)
    _signed_admission_marker(jit)
    manifest = lifecycle.verify_admission(jit.bundle_dir, key_dir=jit.key_dir)
    assert MARKER not in manifest
    assert jit.withhold_credentials is False
    assert jit.run().status == "VERIFIED"
    with pytest.raises(AdmissionError, match=MISMATCH):
        lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


@pytest.mark.parametrize(
    "marker", [False, None, 1, "true"], ids=["false", "null", "int", "str"]
)
@pytest.mark.parametrize("artifact", ["draft", "admission"])
def test_protected_mode_requires_the_verified_boolean_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: Any,
    artifact: str,
) -> None:
    jit = _draft(tmp_path, marker)
    if artifact == "admission":
        # A real signature does not turn a malformed policy field into True.
        _admit(jit, monkeypatch)
        _signed_admission_marker(jit, marker)
        verified = lifecycle.verify_admission(jit.bundle_dir, key_dir=jit.key_dir)
    else:
        verified = lifecycle._recorder(jit.program_dir, jit.key_dir)
    assert verified[MARKER] == marker
    assert type(verified[MARKER]) is type(marker)
    with pytest.raises(AdmissionError, match=MISMATCH):
        lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


def test_unsigned_candidate_edit_cannot_upgrade_the_signed_capture(
    tmp_path: Path,
) -> None:
    jit = _draft(tmp_path, False)
    path = jit.program_dir / lifecycle.CANDIDATE_FILE
    candidate = json.loads(path.read_text(encoding="utf-8"))
    candidate[MARKER] = True
    _write(path, candidate)
    assert lifecycle._record_signed(candidate["recorder"], jit.key_dir)
    with pytest.raises(AdmissionError):
        lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


def test_unsigned_admission_edit_cannot_upgrade_capture_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _draft(tmp_path, False)
    _admit(jit, monkeypatch)
    path = jit.bundle_dir / lifecycle.ADMISSION_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[MARKER] = True
    _write(path, manifest)
    with pytest.raises(AdmissionError):
        lifecycle.verify_admission(jit.bundle_dir, key_dir=jit.key_dir)
    with pytest.raises(AdmissionError):
        lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


def test_a_selected_foreign_draft_cannot_borrow_the_original_recorder(
    tmp_path: Path,
) -> None:
    jit = _draft(tmp_path, True)
    lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)
    foreign = tmp_path / "foreign-draft"
    shutil.copytree(jit.bundle_dir, foreign)
    changed = Workflow.load(foreign)
    changed.steps[0].text = "different synthetic value"
    changed.save(foreign)
    with pytest.raises(AdmissionError, match=MISMATCH):
        lifecycle.require_credential_withholding(
            jit.program_dir, bundle_dir=foreign, key_dir=jit.key_dir
        )


def test_admission_carries_verified_capture_even_when_request_is_default_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _draft(tmp_path, True)
    assert jit.withhold_credentials is False
    manifest = _admit(jit, monkeypatch)
    assert manifest[MARKER] is True
    assert (
        lifecycle.verify_admission(jit.bundle_dir, key_dir=jit.key_dir)[MARKER] is True
    )
    lifecycle.require_credential_withholding(jit.program_dir, key_dir=jit.key_dir)


def test_a_protected_cached_request_refuses_legacy_capture_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = _draft(tmp_path)
    _admit(ordinary, monkeypatch)
    _signed_admission_marker(ordinary)
    protected = dataclasses.replace(ordinary, withhold_credentials=True)
    calls = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("replay")
        raise AssertionError("an unprotected cache reached governed replay")

    monkeypatch.setattr(protected, "_run_governed", forbidden)
    try:
        result = protected.run()
    except AdmissionError as exc:
        assert MISMATCH in str(exc)
    else:
        assert result.status == "refused"
        assert MISMATCH in (result.detail or "")
    assert calls == []


@pytest.mark.parametrize("probe_only", [False, True], ids=["repair", "probe"])
def test_repair_and_probe_carry_the_signed_capture_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_only: bool
) -> None:
    jit = _draft(tmp_path, True)
    _admit(jit, monkeypatch)
    repaired = _copy(jit, tmp_path, "repaired", _reanchor)
    manifest = lifecycle.admit_repair(
        jit.bundle_dir,
        repaired,
        candidate_id="synthetic-capture-repair",
        probe_only=probe_only,
        approved_by=None if probe_only else "Synthetic Reviewer",
        key_dir=jit.key_dir,
    )
    assert manifest[MARKER] is True
    assert manifest["repair_of"]["prior_manifest"][MARKER] is True
    assert lifecycle.verify_admission(repaired, key_dir=jit.key_dir)[MARKER] is True
    lifecycle.require_credential_withholding(
        jit.program_dir, bundle_dir=repaired, key_dir=jit.key_dir
    )

    # Even a newly signed repair cannot change the prior capture contract.
    manifest[MARKER] = False
    manifest["signature"] = lifecycle._sign_record(manifest, jit.key_dir)
    _write(repaired / lifecycle.ADMISSION_FILE, manifest)
    with pytest.raises(AdmissionError):
        lifecycle.verify_admission(repaired, key_dir=jit.key_dir)
    with pytest.raises(AdmissionError):
        lifecycle.require_credential_withholding(
            jit.program_dir, bundle_dir=repaired, key_dir=jit.key_dir
        )
