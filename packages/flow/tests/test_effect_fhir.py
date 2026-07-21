"""FHIR R4 EffectVerifier tests.

CI runs against a faithful in-repo fake (``tests/_fhir_fake.py``) that emits
byte-real FHIR R4 ``Bundle`` search-sets and 401s on a bad token -- the REAL
FHIR wire contract, never MockMed's screen. A separate live-gated test hits a
real OpenEMR when ``OPENEMR_FHIR_BASE_URL`` (+ optional
``OPENEMR_FHIR_TOKEN``) is set, and is skipped otherwise.
"""

from __future__ import annotations

import os

import pytest

from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    FhirEffectVerifier,
    Verdict,
)
from tests import _fhir_fake

PATIENT = "9"
REF = f"Patient/{PATIENT}"
NOTE = "Renal panel ordered ahead of the next quarterly visit."


@pytest.fixture
def fhir():
    base, store, stop = _fhir_fake.serve()
    try:
        yield base, store
    finally:
        stop()


def _verifier(base, *, token=None):
    return FhirEffectVerifier(
        base,
        resource_type="Observation",
        search_params={"patient": PATIENT},
        access_token=token,
        timeout_s=2.0,
    )


def test_fhir_confirms_real_write(fhir):
    base, store = fhir
    v = _verifier(base)
    before = v.capture_pre_state()
    assert before.reachable and before.records == []
    store.add_observation(patient=PATIENT, note=NOTE)
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": REF},
        expected_count=1,
        timeout_s=1.0,
    )
    assert v.verify(eff, before).verdict is Verdict.CONFIRMED


def test_fhir_field_equals_reads_back_note(fhir):
    base, store = fhir
    v = _verifier(base)
    before = v.capture_pre_state()
    store.add_observation(patient=PATIENT, note=NOTE)
    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient": REF},
        field="note",
        value=NOTE,
        timeout_s=1.0,
    )
    assert v.verify(eff, before).verdict is Verdict.CONFIRMED


def test_fhir_refutes_partial_save(fhir):
    # OpenEMR persisted the Observation but dropped valueString (partial save).
    base, store = fhir
    v = _verifier(base)
    before = v.capture_pre_state()
    store.add_observation(patient=PATIENT, note=None)
    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient": REF},
        field="note",
        value=NOTE,
        timeout_s=1.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED


def test_fhir_refutes_duplicate(fhir):
    base, store = fhir
    v = _verifier(base)
    before = v.capture_pre_state()
    store.add_observation(patient=PATIENT, note=NOTE)
    store.add_observation(patient=PATIENT, note=NOTE)
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": REF},
        expected_count=1,
        timeout_s=1.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED
    assert verdict.observed_count == 2


def test_fhir_refutes_phantom_write(fhir):
    # Optimistic-UI success: nothing landed in the system of record.
    base, store = fhir
    v = _verifier(base)
    before = v.capture_pre_state()
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": REF},
        expected_count=1,
        timeout_s=0.5,
    )
    assert v.verify(eff, before).verdict is Verdict.REFUTED


def test_fhir_refutes_collateral_loss(fhir):
    # A concurrent clinician's Observation existed; a stale overwrite deleted
    # it while writing ours. Our row lands (looks fine) but the concurrent
    # record vanished -- caught against the baseline.
    base, store = fhir
    store.add_observation(patient=PATIENT, note="URGENT: allergy", source="other")
    v = _verifier(base)
    before = v.capture_pre_state()
    assert len(before.records) == 1
    store.delete_where(patient=PATIENT, source="other")
    store.add_observation(patient=PATIENT, note=NOTE, source="replay")
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": REF, "note": NOTE},
        expected_count=1,
        timeout_s=1.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED
    assert "collateral" in verdict.reason


def test_fhir_expired_token_is_indeterminate_not_absent(fhir):
    # A 401 must NEVER read as "record absent" -- it HALTS.
    base, store = fhir
    _base, store2, stop = _fhir_fake.serve(token="good-token")
    try:
        v = FhirEffectVerifier(
            _base,
            resource_type="Observation",
            search_params={"patient": PATIENT},
            access_token="WRONG",
            timeout_s=0.5,
        )
        before = v.capture_pre_state()
        assert not before.reachable  # 401 -> unreadable
        eff = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient": REF},
            timeout_s=0.3,
        )
        assert v.verify(eff, before).verdict is Verdict.INDETERMINATE
    finally:
        stop()


def test_fhir_unreachable_is_indeterminate():
    v = FhirEffectVerifier(
        "http://127.0.0.1:1",
        resource_type="Observation",
        search_params={"patient": PATIENT},
        timeout_s=0.2,
    )
    before = v.capture_pre_state()
    assert not before.reachable
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match={"patient": REF}, timeout_s=0.1)
    assert v.verify(eff, before).verdict is Verdict.INDETERMINATE


@pytest.mark.skipif(
    not os.environ.get("OPENEMR_FHIR_BASE_URL"),
    reason="live OpenEMR FHIR: set OPENEMR_FHIR_BASE_URL (+ OPENEMR_FHIR_TOKEN)",
)
def test_live_openemr_fhir_reachable():
    """Live smoke test against a real OpenEMR FHIR R4 endpoint.

    Confirms the verifier can READ the system of record (capture_pre_state
    reachable). A full write+verify against a live instance belongs in the
    ``scripts/openemr_demo.py`` harness (it performs a real GUI write first);
    this only asserts the FHIR contract is reachable with the given creds.
    """
    base = os.environ["OPENEMR_FHIR_BASE_URL"]
    token = os.environ.get("OPENEMR_FHIR_TOKEN")
    patient = os.environ.get("OPENEMR_FHIR_PATIENT", "1")
    v = FhirEffectVerifier(
        base,
        resource_type="Observation",
        search_params={"patient": patient},
        access_token=token,
        timeout_s=15.0,
    )
    before = v.capture_pre_state()
    assert before.reachable, (
        "live OpenEMR FHIR unreachable/unauthorized -- check base URL + token"
    )
