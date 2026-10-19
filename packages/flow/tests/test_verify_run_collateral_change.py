"""A record outside the target that CHANGED is collateral, not only one that vanished.

Regression for the OpenEMR ``collateral_edit`` case (scoreboard claims run
``raw/openemr_referral/claims/2026-10-19-a``, items 931 to 935): the agent saved
its own chart correctly while another, pre-existing chart's home phone was
changed. ``verify-run`` said ``verified``, because the collateral rule compared
records by id only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import EffectState
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

# OpenEMR patient-API shaped records (made up).
EXISTING = {
    "id": 1,
    "fname": "Wendell",
    "lname": "Priorchart",
    "phone_home": "555-0188",
    "email": "w.priorchart@example.invalid",
}
TARGET = {
    "id": 2,
    "fname": "Barnaby",
    "lname": "ScoreboardV931",
    "phone_home": "555-0131",
    "email": "barnaby.scoreboard.931@example.invalid",
}


def _effect(**extra: object) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"email": TARGET["email"], "lname": TARGET["lname"]},
        expected_count=1,
        count_new_only=True,
        **extra,
    )


def _judge(effect: Effect, after: list[dict]) -> object:
    before = EffectState(substrate="rest", reachable=True, records=[dict(EXISTING)])
    return judge_records(effect, before, after, substrate="rest")


def test_openemr_collateral_edit_is_refuted() -> None:
    edited = {**EXISTING, "phone_home": "555-0199"}
    verdict = _judge(_effect(forbid_collateral_change=True), [edited, TARGET])
    assert verdict.verdict is Verdict.REFUTED
    assert "collateral change" in verdict.reason
    assert "phone_home" in verdict.reason


def test_without_the_flag_the_old_rule_still_confirms() -> None:
    # Unchanged default: the run engine's contracts behave as before.
    edited = {**EXISTING, "phone_home": "555-0199"}
    verdict = _judge(_effect(), [edited, TARGET])
    assert verdict.verdict is Verdict.CONFIRMED


def test_untouched_neighbours_confirm() -> None:
    verdict = _judge(_effect(forbid_collateral_change=True), [dict(EXISTING), TARGET])
    assert verdict.verdict is Verdict.CONFIRMED


def test_nested_key_order_is_not_a_change() -> None:
    # A baseline file stores records as canonical JSON (sorted keys); OpenEMR
    # returns its nested addresses in another key order.
    before = EffectState(
        substrate="rest",
        reachable=True,
        records=[{**EXISTING, "addresses": [{"zip": "02139", "city": "EXAMPLETON"}]}],
    )
    after = [
        {**EXISTING, "addresses": [{"city": "EXAMPLETON", "zip": "02139"}]},
        TARGET,
    ]
    verdict = judge_records(
        _effect(forbid_collateral_change=True), before, after, substrate="rest"
    )
    assert verdict.verdict is Verdict.CONFIRMED


def test_a_vanished_neighbour_is_still_collateral_loss() -> None:
    verdict = _judge(_effect(forbid_collateral_change=True), [TARGET])
    assert verdict.verdict is Verdict.REFUTED
    assert "vanished" in verdict.reason


def test_the_target_itself_may_change() -> None:
    # A pre-existing record inside the target selector is the record the
    # action is allowed to write.
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"email": EXISTING["email"]},
        expected_count=1,
        forbid_collateral_change=True,
    )
    edited = {**EXISTING, "phone_home": "555-0199"}
    assert _judge(effect, [edited]).verdict is Verdict.CONFIRMED


def test_flag_only_on_record_written() -> None:
    with pytest.raises(ValueError, match="record_written"):
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"email": TARGET["email"]},
            field="phone_home",
            value="555-0131",
            forbid_collateral_change=True,
        )


def test_existing_contract_digests_are_unchanged() -> None:
    assert "forbid_collateral_change" not in _effect()._contract_payload()
    assert (
        _effect().contract_hash()
        == _effect(forbid_collateral_change=False).contract_hash()
    )
    assert (
        _effect(forbid_collateral_change=True).contract_hash()
        != _effect().contract_hash()
    )


def _yaml(base: str, effects: list[dict] | None = None, record: bool = False) -> dict:
    doc: dict = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "rest", "base_url": base, "records_path": "/api/db"},
    }
    if record:
        doc["record"] = {
            "name": "encounter",
            "match": ["patient_id", "note"],
            "fields": {"patient_id": "patient_id", "note": "note", "type": "type"},
        }
    else:
        doc["effects"] = effects or [
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "type": "Triage",
                    "note": {"param": "note"},
                },
                "expected_count": 1,
                "count_new_only": True,
                "timeout_s": 0.5,
            }
        ]
    return doc


def test_verify_run_turns_the_check_on_when_it_judges() -> None:
    from openadapt_flow.verify_run import _audit_effect

    params = {"patient_id": "p1", "note": "n", "type": "Triage"}
    for doc in (_yaml("http://127.0.0.1:1"), _yaml("http://127.0.0.1:1", record=True)):
        cfg = parse_verify_config(doc)
        for effect in cfg.effects:
            # The contract stays as the file states it (qualify's digest)...
            assert effect.forbid_collateral_change is False
            judged = _audit_effect(effect, effect.resolve(params))
            # ...and verify-run judges record_written with the change check.
            assert judged.forbid_collateral_change is (
                effect.kind is EffectKind.RECORD_WRITTEN
            )


def test_an_effect_can_opt_out() -> None:
    from openadapt_flow.verify_run import _audit_effect

    doc = _yaml("http://127.0.0.1:1")
    doc["effects"][0]["forbid_collateral_change"] = False
    effect = parse_verify_config(doc).effects[0]
    params = {"patient_id": "p1", "note": "n", "type": "Triage"}
    assert (
        _audit_effect(effect, effect.resolve(params)).forbid_collateral_change is False
    )


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def test_verify_run_refuses_an_in_place_edit_of_another_record(sor, tmp_path: Path):
    base, db = sor
    params = {"patient_id": "p1", "note": "Follow-up in two weeks"}
    # A concurrent clinician's earlier encounter on another patient.
    db.add("p2", "Consult", "Allergy: penicillin")
    cfg = parse_verify_config(_yaml(base))
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, params, baseline)
    requests.post(
        f"{base}/api/encounter",
        json={"patient_id": "p1", "type": "Triage", "note": params["note"]},
        timeout=5,
    )
    # The same save also rewrote the other record in place.
    with db._lock:
        for r in db._records:
            if r["patient_id"] == "p2":
                r["note"] = "Allergy: none"
    result = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "test-agent"},
        authorization_ref="ticket-0000-collateral",
        baseline_path=baseline,
        private_dir=tmp_path / "out",
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == "REFUTED"
    assert receipt["outcome"] != "verified"
    assert result["evidence"]["readback"]["effects"][0]["collateral_change_checked"]
