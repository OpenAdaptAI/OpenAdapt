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


# -- exact_new_set: the over-write guard ------------------------------------
#
# The gap this closes, measured in a 150-trial benchmark study: a contract set
# declares one ``record_written`` per intended new record and says NOTHING
# about records nobody declared. An agent asked to download 6 records
# downloaded 37; all 6 declared rows exist, so every per-record contract
# CONFIRMS while the system of record holds 31 writes nobody asked for. That
# is a FALSE PASS -- the one error direction the design must never take.

SONG_ROWS = [
    {"id": 1, "user_id": "32", "song_id": "199"},
    {"id": 2, "user_id": "32", "song_id": "9"},
]
DECLARED_SONGS = [
    {"user_id": "32", "song_id": "199"},
    {"user_id": "32", "song_id": "9"},
]


def _exact(**kwargs):
    kwargs.setdefault("new_records", DECLARED_SONGS)
    kwargs.setdefault("expected_count", len(kwargs["new_records"]))
    return Effect(kind=EffectKind.EXACT_NEW_SET, **kwargs)


def test_exact_new_set_confirms_when_only_declared_records_were_added():
    pre = [{"id": 0, "user_id": "7", "song_id": "1"}]
    v = judge_records(_exact(), _state(pre), [*pre, *SONG_ROWS], substrate="test")
    assert v.verdict is Verdict.CONFIRMED
    assert v.observed_count == 2
    assert v.expected_count == 2


def test_exact_new_set_refutes_extra_undeclared_records():
    """THE REGRESSION: every declared record is present AND unintended rows
    were added to the same read set -- REFUTED, never CONFIRMED."""
    strays = [
        {"id": 10 + n, "user_id": "32", "song_id": str(500 + n)} for n in range(31)
    ]
    v = judge_records(_exact(), _state([]), [*SONG_ROWS, *strays], substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert v.observed_count == 33
    assert v.expected_count == 2
    assert "added 33 record(s)" in v.reason
    assert "declares exactly 2" in v.reason
    assert "31 record(s) the action added are NOT in the declared set" in v.reason
    # Every declared record IS present, so each per-record contract passes:
    # that is exactly why the per-record contracts cannot see this fault.
    for declared in DECLARED_SONGS:
        assert any(record_matches(r, declared) for r in SONG_ROWS)


def test_exact_new_set_refutes_one_unintended_record_and_names_the_surplus():
    stray = {"id": 7, "user_id": "32", "song_id": "404"}
    v = judge_records(_exact(), _state([]), [*SONG_ROWS, stray], substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert "added 3 record(s)" in v.reason
    assert "1 record(s) the action added are NOT in the declared set" in v.reason
    assert "song_id=404" in v.reason


def test_exact_new_set_ignores_records_that_predate_the_action():
    """A pre-existing row inside the scope that no member names is NOT an
    extra -- telling it apart from a new row is the identity_field's job."""
    pre = [{"id": 99, "user_id": "32", "song_id": "77"}]
    v = judge_records(_exact(), _state(pre), [*pre, *SONG_ROWS], substrate="test")
    assert v.verdict is Verdict.CONFIRMED
    assert v.observed_count == 2


def test_exact_new_set_refutes_a_missing_declared_record():
    v = judge_records(_exact(), _state([]), SONG_ROWS[:1], substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert "was added 0 time(s), expected 1" in v.reason


def test_exact_new_set_scope_limits_what_the_guard_speaks_for():
    other = {"id": 50, "user_id": "41", "song_id": "3"}
    v = judge_records(
        _exact(match={"user_id": "32"}),
        _state([]),
        [*SONG_ROWS, other],
        substrate="test",
    )
    assert v.verdict is Verdict.CONFIRMED


def test_exact_new_set_refutes_collateral_loss_inside_the_scope():
    pre = [{"id": 99, "user_id": "32", "song_id": "77"}]
    v = judge_records(_exact(), _state(pre), SONG_ROWS, substrate="test")
    assert v.verdict is Verdict.REFUTED
    assert "collateral loss" in v.reason


def test_exact_new_set_without_a_baseline_is_indeterminate():
    """No baseline, no delta: refuse loudly rather than call every record new
    (which would REFUTE for the wrong reason) or guess."""
    unreachable = EffectState(substrate="test", reachable=False)
    v = judge_records(_exact(), unreachable, SONG_ROWS, substrate="test")
    assert v.verdict is Verdict.INDETERMINATE
    assert "readable pre-state baseline" in v.reason
    assert v.should_halt


def test_exact_new_set_without_an_identity_is_indeterminate():
    """A record with no identity_field: the added set cannot be enumerated,
    so the judge issues a structured refusal instead of a verdict."""
    rows = [{"user_id": "7", "song_id": "1"}]
    v = judge_records(_exact(), _state(rows), rows, substrate="test")
    assert v.verdict is Verdict.INDETERMINATE
    assert "cannot enumerate the added set" in v.reason
    assert "identity_field" in v.reason
    assert v.should_halt


def test_exact_new_set_repeated_member_declares_that_many_additions():
    twice = [{"user_id": "32", "song_id": "199"}] * 2
    rows = [
        {"id": 1, "user_id": "32", "song_id": "199"},
        {"id": 2, "user_id": "32", "song_id": "199"},
    ]
    assert (
        judge_records(
            _exact(new_records=twice), _state([]), rows, substrate="test"
        ).verdict
        is Verdict.CONFIRMED
    )
    assert (
        judge_records(
            _exact(new_records=twice), _state([]), rows[:1], substrate="test"
        ).verdict
        is Verdict.REFUTED
    )


def test_exact_new_set_zero_members_asserts_nothing_was_added():
    empty = Effect(kind=EffectKind.EXACT_NEW_SET, new_records=[], expected_count=0)
    assert (
        judge_records(empty, _state([{"id": 1}]), [{"id": 1}], substrate="test").verdict
        is Verdict.CONFIRMED
    )
    assert (
        judge_records(
            empty, _state([{"id": 1}]), [{"id": 1}, {"id": 2}], substrate="test"
        ).verdict
        is Verdict.REFUTED
    )


# -- exact_new_set: contract shape (operator-authored, Path A) ---------------


def test_exact_new_set_loads_from_a_bundle_style_mapping():
    """Flow contracts are operator-authored in the bundle; the new kind loads
    through the same ``Effect`` model with no extra plumbing."""
    effect = Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {"user_id": "32"},
            "new_records": [
                {"user_id": "32", "song_id": "199"},
                {"user_id": "32", "song_id": "9"},
            ],
            "expected_count": 2,
            "identity_field": "id",
        }
    )
    assert effect.kind is EffectKind.EXACT_NEW_SET
    assert effect.identity_field == "id"
    assert effect.requires_baseline


def test_exact_new_set_cardinality_must_match_the_declared_set():
    with pytest.raises(ValueError, match=r"expected_count == len\(new_records\)"):
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            new_records=DECLARED_SONGS,
            expected_count=1,
        )


def test_exact_new_set_refuses_an_empty_member_selector():
    with pytest.raises(ValueError, match="EMPTY selector"):
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            new_records=[{}],
            expected_count=1,
        )


def test_new_records_on_another_kind_is_refused():
    with pytest.raises(ValueError, match="new_records applies only to exact_new_set"):
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=TARGET,
            new_records=DECLARED_SONGS,
        )


def test_exact_new_set_binds_its_declared_set_in_the_contract_hash():
    """A receipt must not be able to claim a set the judge did not judge."""
    original = _exact().contract_hash()
    altered = _exact(
        new_records=[
            {"user_id": "32", "song_id": "199"},
            {"user_id": "32", "song_id": "0"},
        ]
    ).contract_hash()
    assert original != altered
    assert (
        _exact(identity_field="row_id").contract_hash()
        != _exact(identity_field="id").contract_hash()
    )


def test_exact_new_set_resolves_param_references_in_its_declared_set():
    effect = Effect(
        kind=EffectKind.EXACT_NEW_SET,
        new_records=[{"song_id": {"param": "first"}}, {"song_id": {"param": "second"}}],
        expected_count=2,
    )
    assert effect.referenced_params() == {"first", "second"}
    resolved = effect.resolve({"first": "199", "second": "9"})
    assert [dict(s) for s in resolved.new_records] == [
        {"song_id": "199"},
        {"song_id": "9"},
    ]


# -- backward compatibility: pre-existing kinds are judged as before ---------


def test_pre_existing_effect_contract_hashes_are_unchanged():
    """The new fields enter ``contract_hash`` ONLY on the new kind, so every
    contract written before this option keeps its exact digest (pinned from
    the parent commit)."""
    assert Effect(
        kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1
    ).contract_hash() == (
        "sha256:ee92689f85689bbd45ea87b6c554857d0f2f80dea54711581bf79ca6953a698a"
    )
    assert Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        count_new_only=True,
    ).contract_hash() == (
        "sha256:f0341dde936ba727389d9b55e6adebaf66e2a9a4d1de5777b6b46b23bca951a0"
    )
    assert Effect(
        kind=EffectKind.FIELD_EQUALS, match=TARGET, field="note", value=NOTE
    ).contract_hash() == (
        "sha256:d4453d6a40b045d781cd1d1f6a4c294017aad850e8b74e06a618ada3b9903f1d"
    )
    assert Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        idempotency_key="abc",
    ).contract_hash() == (
        "sha256:86fa2aaab213d36378032804e3243ce9f449e7f7a927a0dfb7c6b02f58a78a9f"
    )


def test_pre_existing_kinds_do_not_require_a_baseline():
    """``requires_baseline`` is additive: it is True exactly where
    ``count_new_only`` already was, plus the new kind."""
    assert not Effect(kind=EffectKind.RECORD_WRITTEN, match=TARGET).requires_baseline
    assert not Effect(
        kind=EffectKind.FIELD_EQUALS, match=TARGET, field="note", value=NOTE
    ).requires_baseline
    assert Effect(
        kind=EffectKind.RECORD_WRITTEN, match=TARGET, count_new_only=True
    ).requires_baseline
