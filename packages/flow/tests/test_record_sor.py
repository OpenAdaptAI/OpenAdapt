"""record --verify: read the system of record around a recording.

MockMed's fault server is the system of record and test code stands in for the
person demonstrating. No browser, no model calls, nothing beyond localhost.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest
import requests
import yaml

import openadapt_flow.__main__ as cli
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.record_sor import (
    PROPOSED_CONTRACT_NAME,
    SNAPSHOTS_NAME,
    RecordingObservation,
    RecordSorError,
    diff_snapshots,
    load_observer,
    propose_effects,
)
from openadapt_flow.runtime.effects import EffectKind, ValueExpr
from openadapt_flow.verify_run import (
    VerifyRunError,
    capture_baseline,
    parse_verify_config,
    verify_run,
)
from tests._rest_read_contract_fixtures import mockmed_effects_config

NOTE = "Follow-up in two weeks"


@pytest.fixture
def sor():
    url, _db, stop = fault_serve()
    try:
        yield url.rstrip("/")
    finally:
        stop()


def _verify_file(tmp_path: Path, verifier: dict) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(yaml.safe_dump({"verifier": verifier}))
    return path


def _rest(base: str) -> dict:
    return mockmed_effects_config(base)


def _write(base: str, note: str = NOTE, patient: str = "p1") -> None:
    requests.post(
        f"{base}/api/encounter",
        json={"patient_id": patient, "type": "Triage", "note": note},
        timeout=5,
    )


def _recording(tmp_path: Path, params: dict) -> Path:
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps({"params": params}))
    return rec


def _confirm(text: str) -> dict:
    """What a reviewer does: delete each needs_operator_confirmation line."""

    kept = [
        line for line in text.splitlines() if "needs_operator_confirmation" not in line
    ]
    return yaml.safe_load("\n".join(kept))


# -- diff ----------------------------------------------------------------------


def test_diff_finds_added_changed_and_removed_records():
    before = [
        {"id": 1, "status": "open", "note": "a"},
        {"id": 2, "status": "open"},
    ]
    after = [
        {"id": 1, "status": "closed", "note": "a"},
        {"id": 3, "status": "open"},
    ]
    diff = diff_snapshots(before, after)
    assert diff.identity_field == "id"
    assert diff.created == [{"id": 3, "status": "open"}]
    assert diff.removed == [{"id": 2, "status": "open"}]
    [change] = diff.changed
    assert change.identity == 1
    assert [(c.field, c.before, c.after) for c in change.changes] == [
        ("status", "open", "closed")
    ]


@pytest.mark.parametrize(
    "before",
    [
        [{"name": "a", "status": "open"}],  # no id at all
        [{"id": 1, "status": "open"}, {"id": 1, "status": "x"}],  # duplicate id
    ],
)
def test_records_without_a_unique_id_are_compared_whole(before):
    after = [dict(before[0], status="closed")]
    diff = diff_snapshots(before, after)
    assert diff.identity_field is None
    assert diff.changed == []
    assert diff.created == after
    assert len(diff.removed) == len(before)


# -- proposal ------------------------------------------------------------------


def test_new_record_becomes_one_unconfirmed_record_written():
    diff = diff_snapshots(
        [],
        [{"id": 7, "patient_id": "p1", "note": NOTE, "key": None, "extra": {"a": 1}}],
    )
    proposal = propose_effects(diff, {"note": NOTE})
    [effect] = proposal.effects
    assert effect.kind is EffectKind.RECORD_WRITTEN
    assert effect.needs_operator_confirmation is True
    assert effect.count_new_only is True
    # The surrogate id and empty fields never select; the typed value is bound
    # to its parameter, not frozen as the demonstrated literal.
    assert effect.match == {
        "note": ValueExpr(param="note"),
        "patient_id": ValueExpr(literal="p1"),
    }
    assert any("nested field(s) extra" in n for n in proposal.not_proposed)
    # Issue #1864: no single-field key -> propose composite from param-bound
    # match fields (patient_id stays out until named as record identity).
    assert effect.idempotency_key is None
    assert effect.composite_key == {"note": ValueExpr(param="note")}
    # Issue #2174: warn when proposed composite key is a single free-text parameter
    # with no identity-bearing field, as it would refute legitimate repeats.
    assert any(
        "single free-text parameter 'note' with no identity-bearing field" in n
        for n in proposal.not_proposed
    )


def test_composite_key_includes_named_record_identity_fields():
    # Issue #1864: when the read set has no key field, propose composite_key
    # from parameter-bound match fields plus the person's record_fields.
    diff = diff_snapshots(
        [],
        [{"id": 7, "patient_id": "p1", "note": NOTE, "key": None}],
    )
    proposal = propose_effects(diff, {"note": NOTE}, record_fields={"patient_id": "p1"})
    [effect] = proposal.effects
    assert effect.idempotency_key is None
    assert effect.composite_key == {
        "note": ValueExpr(param="note"),
        "patient_id": ValueExpr(literal="p1"),
    }
    assert effect.needs_operator_confirmation is True
    assert any("proposed composite_key" in n for n in proposal.not_proposed)
    # When an identity-bearing field is present, no warning is emitted.
    assert not any("no identity-bearing field" in n for n in proposal.not_proposed)


def test_composite_key_warns_on_free_text_parameters_without_identity():
    # Issue #2174: single free-text parameter (comment, description, memo)
    # refutes legitimate records that repeat the same text.
    diff = diff_snapshots(
        [],
        [{"id": 1, "comment": "standard follow up"}],
    )
    proposal = propose_effects(diff, {"comment": "standard follow up"})
    [effect] = proposal.effects
    assert effect.composite_key == {"comment": ValueExpr(param="comment")}
    assert any(
        "single free-text parameter 'comment' with no identity-bearing field" in n
        for n in proposal.not_proposed
    )


def test_composite_key_refuses_when_refuse_non_identity_set():
    # Issue #2174: refuse proposing composite_key when refuse_non_identity=True
    # and no identity-bearing field is present.
    diff = diff_snapshots(
        [],
        [{"id": 7, "patient_id": "p1", "note": NOTE, "key": None}],
    )
    proposal = propose_effects(diff, {"note": NOTE}, refuse_non_identity=True)
    [effect] = proposal.effects
    assert effect.composite_key == {}
    assert any("refusing to propose composite_key" in n for n in proposal.not_proposed)
    assert any("key has no identity-bearing field" in n for n in proposal.not_proposed)


def test_param_bound_single_field_key_becomes_idempotency_key_not_composite():
    diff = diff_snapshots(
        [],
        [{"id": 7, "patient_id": "p1", "note": NOTE, "key": "run-abc"}],
    )
    proposal = propose_effects(
        diff, {"note": NOTE, "key": "run-abc"}, record_fields={"patient_id": "p1"}
    )
    [effect] = proposal.effects
    assert effect.idempotency_key == ValueExpr(param="key")
    assert effect.composite_key == {}
    assert not any("composite_key" in n for n in proposal.not_proposed)


def test_literal_single_field_key_not_proposed_as_idempotency_key():
    diff = diff_snapshots(
        [],
        [{"id": 7, "patient_id": "p1", "note": NOTE, "key": "run-abc"}],
    )
    proposal = propose_effects(diff, {"note": NOTE}, record_fields={"patient_id": "p1"})
    [effect] = proposal.effects
    # Issue #2173: literal key from demonstration is not proposed as
    # idempotency_key to avoid freezing the key across runs (P0-3); composite_key
    # is proposed from parameter-bound fields + record identity fields.
    assert effect.idempotency_key is None
    assert effect.composite_key == {
        "note": ValueExpr(param="note"),
        "patient_id": ValueExpr(literal="p1"),
    }
    assert any("avoid freezing key" in n for n in proposal.not_proposed)


def test_no_key_and_no_param_bound_fields_notes_missing_composite():
    diff = diff_snapshots([], [{"id": 3, "status": "open"}])
    proposal = propose_effects(diff, {"note": NOTE})
    [effect] = proposal.effects
    assert effect.idempotency_key is None
    assert effect.composite_key == {}
    assert any("clinical-write policy will refuse" in n for n in proposal.not_proposed)


def test_changed_fields_become_field_equals_on_that_record():
    diff = diff_snapshots(
        [{"id": "r9", "status": "open", "owner": "x", "meta": {"v": 1}}],
        [{"id": "r9", "status": "closed", "owner": None, "meta": {"v": 2}}],
    )
    proposal = propose_effects(diff, {"new_status": "closed"})
    [effect] = proposal.effects
    assert effect.kind is EffectKind.FIELD_EQUALS
    assert effect.match == {"id": ValueExpr(literal="r9")}
    assert effect.field == "status"
    assert effect.value == ValueExpr(param="new_status")
    assert effect.needs_operator_confirmation is True
    notes = " ".join(proposal.not_proposed)
    assert "'owner' was cleared" in notes
    assert "nested field 'meta'" in notes
    # Notes never carry record values.
    assert "r9" not in notes


def test_removed_records_and_shared_parameter_values_are_reported_not_proposed():
    diff = diff_snapshots([{"id": 1, "v": "x"}], [{"id": 2, "v": "x"}])
    proposal = propose_effects(diff, {"a": "x", "b": "x"})
    [effect] = proposal.effects
    assert effect.match == {"v": ValueExpr(literal="x")}
    notes = " ".join(proposal.not_proposed)
    assert "1 record(s) disappeared" in notes
    assert "parameters a, b share one value" in notes


def test_short_values_and_bare_numerics_not_bound_by_value_alone():
    # Issue #1474: With quantity="1" typed into refills, refills must not become the quantity parameter.
    diff = diff_snapshots(
        [],
        [{"id": 1, "refills": "1", "quantity": "1", "code": "ab", "other": "ab"}],
    )
    proposal = propose_effects(diff, {"quantity": "1", "code": "ab"})
    [effect] = proposal.effects
    assert effect.match == {
        "quantity": ValueExpr(param="quantity"),
        "refills": ValueExpr(literal="1"),
        "code": ValueExpr(param="code"),
        "other": ValueExpr(literal="ab"),
    }


def test_a_value_two_fields_hold_binds_only_by_name():
    diff = diff_snapshots(
        [],
        [{"id": 9, "start": "2026-01-05", "end": "2026-01-05", "note": "Ann Lee"}],
    )
    proposal = propose_effects(diff, {"visit_date": "2026-01-05", "who": "Ann Lee"})
    [effect] = proposal.effects
    assert effect.match == {
        "start": ValueExpr(literal="2026-01-05"),
        "end": ValueExpr(literal="2026-01-05"),
        "note": ValueExpr(param="who"),
    }
    notes = " ".join(proposal.not_proposed)
    assert "field 'start' equals parameter 'visit_date'" in notes
    assert "held by more than one field" in notes


def test_a_numeric_identity_never_binds_to_an_unrelated_parameter():
    before = [{"id": 7, "status": "open"}]
    after = [{"id": 7, "status": "closed"}]
    diff = diff_snapshots(before, after)
    proposal = propose_effects(diff, {"quantity": "7", "state": "closed"})
    [effect] = proposal.effects
    assert effect.match == {"id": ValueExpr(literal="7")}
    assert effect.value == ValueExpr(param="state")
    assert "it is short, a number or a flag" in " ".join(proposal.not_proposed)


def test_decimals_and_flags_never_bind_by_value_alone():
    diff = diff_snapshots(
        [], [{"id": 1, "quantity": "30", "refills": "1.0", "active": True}]
    )
    proposal = propose_effects(diff, {"qty": "1.0", "consent": "True"})
    [effect] = proposal.effects
    assert effect.match["refills"] == ValueExpr(literal="1.0")
    assert effect.match["active"] == ValueExpr(literal="True")


# -- observer ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["onscreen", "ui_readback", "none"])
def test_observer_refuses_a_verifier_that_reads_through_the_ui(tmp_path, kind):
    with pytest.raises(RecordSorError, match="outside the UI"):
        load_observer(_verify_file(tmp_path, {"kind": kind}))


def test_observer_refuses_a_file_without_a_verifier_section(tmp_path):
    path = tmp_path / "verify.yaml"
    path.write_text("effects: []\n")
    with pytest.raises(RecordSorError, match="no verifier"):
        load_observer(path)
    with pytest.raises(RecordSorError, match="not a file"):
        load_observer(tmp_path / "missing.yaml")


def test_observer_returns_none_when_the_store_is_unreachable(tmp_path):
    observer, _ = load_observer(
        _verify_file(tmp_path, {**_rest("http://127.0.0.1:9"), "timeout_s": 0.2})
    )
    assert observer.system_of_record is None
    with pytest.raises(RecordSorError, match="could not read"):
        RecordingObservation.start(
            _verify_file(tmp_path, {**_rest("http://127.0.0.1:9"), "timeout_s": 0.2})
        )


# -- end to end ----------------------------------------------------------------


def test_recorded_change_yields_a_contract_that_verify_run_grades_independent(
    sor, tmp_path, monkeypatch
):
    monkeypatch.setenv("SOR_BASE", sor)
    # The verifier section is copied as written: the reference stays a
    # reference in the proposal.
    verify = _verify_file(tmp_path, _rest("${SOR_BASE}"))
    observation = RecordingObservation.start(verify)
    _write(sor)  # the person saves during the demonstration
    rec = _recording(tmp_path, {"note": NOTE})
    contract, proposal, diff = observation.finish(rec)

    assert diff is not None and len(diff.created) == 1
    assert contract == rec / PROPOSED_CONTRACT_NAME
    for name in (PROPOSED_CONTRACT_NAME, SNAPSHOTS_NAME):
        assert stat.S_IMODE((rec / name).stat().st_mode) == 0o600
    snapshots = json.loads((rec / SNAPSHOTS_NAME).read_text())
    assert snapshots["before"]["records"] == []
    assert len(snapshots["after"]["records"]) == 1

    text = contract.read_text()
    assert "${SOR_BASE}" in text and sor not in text
    with pytest.raises(VerifyRunError, match="unconfirmed placeholder"):
        parse_verify_config(yaml.safe_load(text))

    cfg = parse_verify_config(_confirm(text))
    [effect] = cfg.effects
    assert effect.match["note"] == ValueExpr(param="note")

    # A later run with a different note: the induced check follows the run's
    # parameter and reads the store, not the screen.
    def run(note_written: str) -> dict:
        params = {"note": "Recheck BP in 3 days"}
        baseline = tmp_path / f"baseline-{note_written[:5]}.json"
        capture_baseline(cfg, params, baseline)
        _write(sor, note=note_written)
        return verify_run(
            cfg,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": "test-agent-01"},
            authorization_ref="ticket-0000-test",
            baseline_path=baseline,
        )["receipt"]

    good = run("Recheck BP in 3 days")
    assert good["effect_verdict"] == "CONFIRMED"
    assert good["outcome"] == "verified"
    assert good["verification_tier"] == "independent_readback"
    bad = run("Wrong note")
    assert bad["effect_verdict"] != "CONFIRMED"
    assert bad["outcome"] != "verified"


def test_no_change_writes_snapshots_but_no_contract(sor, tmp_path):
    observation = RecordingObservation.start(_verify_file(tmp_path, _rest(sor)))
    rec = _recording(tmp_path, {})
    contract, proposal, diff = observation.finish(rec)
    assert contract is None and proposal.effects == []
    assert diff is not None and diff.empty
    assert (rec / SNAPSHOTS_NAME).is_file()
    assert not (rec / PROPOSED_CONTRACT_NAME).exists()


# -- CLI -----------------------------------------------------------------------


def _args(out: Path, verify: Path | None) -> argparse.Namespace:
    return argparse.Namespace(out=str(out), verify=str(verify) if verify else None)


def test_cli_record_verify_proposes_after_the_capture(
    sor, tmp_path, monkeypatch, capsys
):
    rec = tmp_path / "rec"

    def fake_capture(args):
        _write(sor)
        rec.mkdir()
        (rec / "meta.json").write_text(json.dumps({"params": {"note": NOTE}}))
        return 0

    monkeypatch.setattr(cli, "_cmd_record_capture", fake_capture)
    code = cli._cmd_record(_args(rec, _verify_file(tmp_path, _rest(sor))))
    out = capsys.readouterr().out
    assert code == 0
    assert "1 record(s) added, 0 changed, 0 removed" in out
    assert (rec / PROPOSED_CONTRACT_NAME).is_file()
    # The summary names counts and fields only, never the typed value.
    assert NOTE not in out


def test_cli_record_verify_refuses_before_recording(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "_cmd_record_capture", lambda a: called.append(a) or 0)
    code = cli._cmd_record(
        _args(tmp_path / "rec", _verify_file(tmp_path, {"kind": "onscreen"}))
    )
    assert code == 2 and called == []
    assert "Nothing was recorded" in capsys.readouterr().err


def test_cli_record_without_verify_is_unchanged(tmp_path, monkeypatch):
    rec = tmp_path / "rec"

    def fake_capture(args):
        rec.mkdir()
        return 0

    monkeypatch.setattr(cli, "_cmd_record_capture", fake_capture)
    assert cli._cmd_record(_args(rec, None)) == 0
    assert sorted(p.name for p in rec.iterdir()) == []


def test_record_parser_accepts_verify():
    args = cli.build_parser().parse_args(["record", "--out", "r", "--verify", "v.yaml"])
    assert args.verify == "v.yaml"


@pytest.mark.parametrize(
    "verifier",
    [
        # Fails validation: pydantic's error text echoes the expanded input.
        {"kind": "${SOR_TOKEN}"},
        # Validates, then fails when the verifier is built.
        {"kind": "rest", "token": "${SOR_TOKEN}", "not_a_field": "${SOR_TOKEN}"},
    ],
)
def test_an_invalid_verifier_section_never_prints_its_secrets(
    tmp_path, monkeypatch, verifier
):
    """`${ENV}` values are expanded before validation, and the raw error text
    was printed, token included."""
    monkeypatch.setenv("SOR_TOKEN", "s3cret-token-value")
    with pytest.raises(RecordSorError) as refused:
        load_observer(_verify_file(tmp_path, verifier))
    assert "s3cret-token-value" not in str(refused.value)
