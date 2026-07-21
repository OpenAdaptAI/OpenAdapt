"""Unit + live-local tests for the EffectVerifier subsystem.

The REST verifier runs against the REAL MockMed transactional back end
(``mockmed.fault_server``, an in-process HTTP system of record); the document
verifier runs against a real temp directory. No network beyond localhost, no
model calls -- these run in CI.
"""

from __future__ import annotations


import pytest
import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    DocumentHashVerifier,
    Effect,
    EffectKind,
    EffectState,
    EffectVerifier,
    RestCompensator,
    RestRecordVerifier,
    Verdict,
    reconcile_or_escalate,
    record_matches,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.compensation import CompensationOutcome

TARGET = {"patient_id": "p1", "type": "Triage"}
NOTE = "Follow-up in two weeks"


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def sor():
    """A running MockMed fault-server system of record."""
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _post_encounter(base, *, note=NOTE, fault="", key=None):
    payload = {"patient_id": "p1", "type": "Triage", "note": note}
    if key is not None:
        payload["key"] = key
    url = f"{base}/api/encounter"
    if fault:
        url += f"?fault={fault}"
    return requests.post(url, json=payload, timeout=5)


# -- protocol conformance ---------------------------------------------------


def test_all_verifiers_satisfy_protocol(tmp_path):
    assert isinstance(RestRecordVerifier("http://x"), EffectVerifier)
    assert isinstance(DocumentHashVerifier(tmp_path), EffectVerifier)
    from openadapt_flow.runtime.effects import FhirEffectVerifier

    assert isinstance(FhirEffectVerifier("http://x"), EffectVerifier)


# -- judge_records unit logic (no network) ----------------------------------


def _state(records):
    return EffectState(substrate="test", reachable=True, records=records)


def test_record_written_confirmed_exactly_one():
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1)
    recs = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": NOTE}]
    v = judge_records(eff, _state([]), recs, substrate="test")
    assert v.verdict is Verdict.CONFIRMED
    assert v.observed_count == 1


def test_record_written_refutes_duplicate():
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1)
    recs = [
        {"id": 1, "patient_id": "p1", "type": "Triage", "note": NOTE},
        {"id": 2, "patient_id": "p1", "type": "Triage", "note": NOTE},
    ]
    v = judge_records(eff, _state([]), recs, substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert v.observed_count == 2
    assert "duplicate" in v.reason


def test_record_written_refutes_missing():
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1)
    v = judge_records(eff, _state([]), [], substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert v.observed_count == 0


def test_indeterminate_on_unreadable_sor():
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET)
    v = judge_records(eff, _state([]), None, substrate="test")
    assert v.verdict is Verdict.INDETERMINATE
    assert v.should_halt


def test_collateral_loss_refuted():
    # A concurrent actor's row existed before; after our write it is gone.
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1)
    before = _state(
        [{"id": 1, "patient_id": "p1", "type": "Consult", "note": "URGENT"}]
    )
    after = [{"id": 2, "patient_id": "p1", "type": "Triage", "note": NOTE}]
    v = judge_records(eff, before, after, substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert "collateral" in v.reason


def test_field_equals_refutes_partial():
    eff = Effect(kind=EffectKind.FIELD_EQUALS, match=TARGET, field="note", value=NOTE)
    recs = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": ""}]
    v = judge_records(eff, _state([]), recs, substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert v.observed_value == ""


def test_idempotency_key_counts_only_keyed_records():
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        idempotency_key="abc",
    )
    recs = [{"id": 1, "patient_id": "p1", "type": "Triage", "note": NOTE, "key": "abc"}]
    v = judge_records(eff, _state([]), recs, substrate="test")
    assert v.verdict is Verdict.CONFIRMED


def test_record_matches_string_coercion():
    assert record_matches({"id": 1}, {"id": "1"})
    assert not record_matches({"id": 1}, {"id": "2"})


# -- RestRecordVerifier against the live system of record -------------------


def test_rest_verifier_confirms_real_write(sor):
    base, _db = sor
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    assert before.reachable and before.records == []
    _post_encounter(base)
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        timeout_s=2.0,
    )
    v = verifier.verify(eff, before)
    assert v.verdict is Verdict.CONFIRMED


def test_rest_verifier_indeterminate_when_unreachable():
    verifier = RestRecordVerifier("http://127.0.0.1:1")  # nothing listening
    before = verifier.capture_pre_state()
    assert not before.reachable
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET, timeout_s=0.1)
    v = verifier.verify(eff, before)
    assert v.verdict is Verdict.INDETERMINATE
    assert v.should_halt


def test_rest_idempotent_write_is_at_most_once(sor):
    base, _db = sor
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    # Two submissions carrying the SAME idempotency key -> server dedupes.
    _post_encounter(base, fault="idempotent", key="run-42")
    _post_encounter(base, fault="idempotent", key="run-42")
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        idempotency_key="run-42",
        timeout_s=2.0,
    )
    v = verifier.verify(eff, before)
    assert v.verdict is Verdict.CONFIRMED, v.reason


# -- DocumentHashVerifier (filesystem substrate) ----------------------------


def test_document_hash_confirms_and_reads_back(tmp_path):
    store = tmp_path / "exports"
    store.mkdir()
    verifier = DocumentHashVerifier(store, glob="*.txt")
    before = verifier.capture_pre_state()
    assert before.reachable and before.records == []
    doc = store / "report.txt"
    doc.write_text("signed export body")
    import hashlib

    digest = hashlib.sha256(b"signed export body").hexdigest()
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"name": "report.txt"},
        expected_count=1,
    )
    assert verifier.verify(written, before).verdict is Verdict.CONFIRMED
    field = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"name": "report.txt"},
        field="sha256",
        value=digest,
    )
    assert verifier.verify(field, before).verdict is Verdict.CONFIRMED


def test_document_hash_refutes_duplicate_export(tmp_path):
    store = tmp_path / "exports"
    store.mkdir()
    verifier = DocumentHashVerifier(store, glob="report*.txt")
    before = verifier.capture_pre_state()
    (store / "report.txt").write_text("x")
    (store / "report (1).txt").write_text("x")  # duplicate export
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match={}, expected_count=1)
    v = verifier.verify(eff, before)
    assert v.verdict is Verdict.REFUTED
    assert v.observed_count == 2


def test_document_hash_indeterminate_when_store_absent(tmp_path):
    verifier = DocumentHashVerifier(tmp_path / "does-not-exist")
    before = verifier.capture_pre_state()
    assert not before.reachable
    eff = Effect(kind=EffectKind.RECORD_WRITTEN, match={})
    assert verifier.verify(eff, before).verdict is Verdict.INDETERMINATE


# -- Compensation: reconcile-or-escalate ------------------------------------


def test_compensation_reconciles_detected_duplicate(sor):
    base, db = sor
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _post_encounter(base)  # two non-idempotent submissions -> two rows
    _post_encounter(base)
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        risk="irreversible",
        timeout_s=2.0,
    )
    verdict = verifier.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED and verdict.observed_count == 2

    compensator = RestCompensator(base)
    result = reconcile_or_escalate(
        eff, verdict, verifier=verifier, before=before, compensator=compensator
    )
    assert result.outcome is CompensationOutcome.RECONCILED
    assert result.proceed and result.actions_taken == 1
    # The system of record now holds exactly one row.
    assert len(db.snapshot()["records"]) == 1


def test_compensation_escalates_partial_save(sor):
    base, _db = sor
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _post_encounter(base, fault="partial")  # row persists, note dropped
    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=TARGET,
        field="note",
        value=NOTE,
        risk="irreversible",
        timeout_s=2.0,
    )
    verdict = verifier.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED
    result = reconcile_or_escalate(
        eff,
        verdict,
        verifier=verifier,
        before=before,
        compensator=RestCompensator(base),
    )
    assert result.outcome is CompensationOutcome.ESCALATED
    assert not result.proceed
    assert result.escalation


def test_compensation_escalates_when_indeterminate():
    verifier = RestRecordVerifier("http://127.0.0.1:1")
    before = EffectState(substrate="rest", reachable=False)
    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        risk="irreversible",
        timeout_s=0.1,
    )
    verdict = verifier.verify(eff, before)
    assert verdict.verdict is Verdict.INDETERMINATE
    result = reconcile_or_escalate(
        eff,
        verdict,
        verifier=verifier,
        before=before,
        compensator=RestCompensator("http://127.0.0.1:1"),
    )
    assert result.outcome is CompensationOutcome.ESCALATED
    assert not result.proceed
