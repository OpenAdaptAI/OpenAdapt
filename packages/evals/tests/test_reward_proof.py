"""The MockMed reward proof is deterministic and complete under its seed schedule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_evals.extradup.checkers import new_records
from openadapt_evals.extradup.gold import MOCKMED_GOLD
from openadapt_evals.reward import proof
from openadapt_evals.reward.devsigner import verify_signature

DOCS = Path(__file__).resolve().parents[1] / "docs" / "reward"
COMMITTED_2026_09_01 = DOCS / "proof_2026-09-01.json"
COMMITTED_2026_09_02 = DOCS / "proof_2026-09-02.json"


@pytest.fixture(scope="module")
def run() -> proof.ProofRun:
    return proof.run_proof()


@pytest.fixture(scope="module")
def run_2026_09_01() -> proof.ProofRun:
    return proof.run_proof(conditions=proof.PROOF_2026_09_01_CONDITIONS)


def _strip_versions(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "versions"}


def _fail_families(conditions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in conditions if proof.gold_for(item) == "FAIL")


def test_run_is_deterministic(run: proof.ProofRun) -> None:
    again = proof.run_proof()
    assert proof.to_json(run) == proof.to_json(again)
    assert run.certificate.signature == again.certificate.signature


def test_table_has_every_condition_and_three_trials(run: proof.ProofRun) -> None:
    cells = {(row["condition"], row["reward"]) for row in run.table}
    assert cells == {(c, r) for c in proof.CONDITIONS for r in proof.REWARDS}
    assert set(proof.OPERATORS) <= set(proof.CONDITIONS)
    assert proof.IDENTITY_SWAP in proof.CONDITIONS
    assert all(row["trials"] >= proof.MIN_TRIALS for row in run.table)
    assert len(run.seeds) >= proof.MIN_TRIALS
    assert run.conditions == proof.CONDITIONS


def test_visual_pays_every_mutant_and_certified_refuses(run: proof.ProofRun) -> None:
    by_cell = {(row["condition"], row["reward"]): row for row in run.table}
    for family in _fail_families(run.conditions):
        visual = by_cell[(family, proof.VISUAL)]
        certified = by_cell[(family, proof.CERTIFIED)]
        assert visual["silent_incorrect_success_rate"] == 1.0
        assert certified["silent_incorrect_success_rate"] == 0.0
        assert visual["certified"] is False
        assert visual["calibration_scope"] is None
    control = by_cell[("control", proof.CERTIFIED)]
    assert control["over_refusal_rate"] == 0.0
    assert control["unscored"] == 0
    assert control["paid"] == run.trials


def test_identity_swap_writes_the_gold_note_onto_the_wrong_patient(run: proof.ProofRun) -> None:
    identity = proof.named_oracle_identity()
    assert identity["patient_id"] == MOCKMED_GOLD.fields["patient_id"]
    assert identity["patient_id"] != proof.WRONG_PATIENT_ID
    rollouts = [item for item in run.rollouts if item.condition == proof.IDENTITY_SWAP]
    assert len(rollouts) == run.trials
    for item in rollouts:
        written = new_records(item.before, item.after)
        assert len(written) == MOCKMED_GOLD.expected_new
        assert written[0]["patient_id"] == proof.WRONG_PATIENT_ID
        assert written[0]["note"] == MOCKMED_GOLD.fields["note"]
        assert written[0]["type"] == MOCKMED_GOLD.fields["type"]
        assert item.screen["banner_saved"] is True
        named_before = proof.records_for_identity(item.before, identity)
        named_after = proof.records_for_identity(item.after, identity)
        assert named_before == named_after
        assert len(new_records(named_before, named_after)) == 0
        visual = run.scored[(item.condition, item.trial, proof.VISUAL)]
        certified = run.scored[(item.condition, item.trial, proof.CERTIFIED)]
        assert visual.scalar == 1.0
        assert certified.scalar == 0.0
        assert certified.unscored is False
        assert "|new(M)|=0" in ";".join(proof._named_sor_reasons(item))


def test_identity_swap_table_row(run: proof.ProofRun) -> None:
    by_cell = {(row["condition"], row["reward"]): row for row in run.table}
    visual = by_cell[(proof.IDENTITY_SWAP, proof.VISUAL)]
    certified = by_cell[(proof.IDENTITY_SWAP, proof.CERTIFIED)]
    assert visual["gold"] == "FAIL"
    assert visual["paid"] == run.trials
    assert visual["silent_incorrect_success_rate"] == 1.0
    assert visual["certified"] is False
    assert certified["paid"] == 0
    assert certified["silent_incorrect_success_rate"] == 0.0
    assert certified["unscored"] == 0
    assert certified["certified"] is True
    assert certified["calibration_scope"] == "synthetic"


def test_outage_is_unscored_never_zero(run: proof.ProofRun) -> None:
    outage = [
        run.scored[(proof.ORACLE_OUTAGE, trial, proof.CERTIFIED)] for trial in range(run.trials)
    ]
    assert all(item.unscored for item in outage)
    assert all(item.scalar is None for item in outage)
    assert all(item.outcome.value == "failed_platform" for item in outage)
    row = next(
        r
        for r in run.table
        if r["condition"] == proof.ORACLE_OUTAGE and r["reward"] == proof.CERTIFIED
    )
    assert row["unscored"] == run.trials
    assert row["paid"] == 0 and row["over_refusal_rate"] == 0.0


def test_certified_reward_is_certified_with_synthetic_scope(run: proof.ProofRun) -> None:
    for row in run.table:
        if row["reward"] == proof.CERTIFIED:
            assert row["certified"] is True
            assert row["calibration_scope"] == "synthetic"


def test_certificate_epsilon_is_the_clopper_pearson_bound(run: proof.ProofRun) -> None:
    cal = run.calibration
    n = cal["gold_fail_trials"]
    assert n == len(_fail_families(run.conditions)) * run.trials
    assert n == 6 * run.trials
    assert cal["false_accepts"][proof.VISUAL] == n
    assert cal["false_accepts"][proof.CERTIFIED] == 0
    assert cal["clopper_pearson_upper_95"][proof.VISUAL] == 1.0
    expected = round(1.0 - 0.05 ** (1.0 / n), 6)
    assert cal["clopper_pearson_upper_95"][proof.CERTIFIED] == pytest.approx(expected, abs=1e-6)
    assert run.certificate.epsilon == cal["clopper_pearson_upper_95"][proof.CERTIFIED]


def test_clopper_pearson_known_values() -> None:
    assert proof.clopper_pearson_upper(15, 15) == 1.0
    assert proof.clopper_pearson_upper(0, 15) == pytest.approx(1 - 0.05 ** (1 / 15), abs=1e-6)
    assert proof.clopper_pearson_upper(1, 20) == pytest.approx(0.216, abs=2e-3)
    assert proof.clopper_pearson_upper(0, 261) == pytest.approx(0.0114, abs=2e-4)
    with pytest.raises(ValueError):
        proof.clopper_pearson_upper(3, 2)


def test_expiry_check_drops_certification_and_logs(run: proof.ProofRun) -> None:
    check = run.expiry_check
    assert check["policy_update"] == run.certificate.expires_at_policy_update
    assert check["receipts_checked"] == run.trials
    assert check["certified_after_expiry"] == 0
    assert check["expiry_warnings_logged"] == run.trials


def test_receipts_and_certificate_verify_against_the_recorded_key(run: proof.ProofRun) -> None:
    from base64 import b64decode

    key = b64decode(run.public_key_b64)
    assert verify_signature(run.certificate, key)
    assert all(verify_signature(receipt, key) for receipt in run.receipts.values())


def test_cli_writes_json_and_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    json_path = tmp_path / "proof.json"
    md_path = tmp_path / "proof.md"
    assert (
        proof.main(["--json", str(json_path), "--markdown", str(md_path), "--date", "2026-09-02"])
        == 0
    )
    payload = json.loads(json_path.read_text())
    assert payload["proof"] == proof.PROOF_NAME
    assert proof.IDENTITY_SWAP in payload["conditions"]
    assert len(payload["rollouts"]) == len(proof.CONDITIONS) * proof.MIN_TRIALS
    swap = next(item for item in payload["rollouts"] if item["condition"] == proof.IDENTITY_SWAP)
    assert swap["written_patient_id"] == proof.WRONG_PATIENT_ID
    assert swap["named_new_count"] == 0
    assert swap["oracle_identity"] == proof.named_oracle_identity()
    text = md_path.read_text()
    assert "| condition | gold | reward |" in text
    assert "identity_swap" in text
    assert "Clopper-Pearson" in text
    assert "—" not in text
    assert "not a Production Seal" in text
    assert "| condition |" in capsys.readouterr().out


def test_committed_2026_09_01_proof_matches_frozen_conditions(
    run_2026_09_01: proof.ProofRun,
) -> None:
    """The ExtraDup-only snapshot stays byte-identical to the #332 claim."""

    assert COMMITTED_2026_09_01.exists()
    assert proof.IDENTITY_SWAP not in run_2026_09_01.conditions
    assert run_2026_09_01.conditions == proof.PROOF_2026_09_01_CONDITIONS
    committed = json.loads(COMMITTED_2026_09_01.read_text())
    assert proof.IDENTITY_SWAP not in committed["conditions"]
    assert committed["calibration"]["gold_fail_trials"] == 15
    assert _strip_versions(committed) == _strip_versions(proof.to_json(run_2026_09_01))


def test_committed_2026_09_02_proof_matches_a_fresh_run(run: proof.ProofRun) -> None:
    """The identity-swap proof in docs/reward is this code at this seed schedule."""

    assert COMMITTED_2026_09_02.exists(), (
        "run python -m openadapt_evals.reward.proof "
        "--json docs/reward/proof_2026-09-02.json "
        "--markdown docs/reward/proof_2026-09-02.md --date 2026-09-02"
    )
    committed = json.loads(COMMITTED_2026_09_02.read_text())
    assert proof.IDENTITY_SWAP in committed["conditions"]
    assert committed["calibration"]["gold_fail_trials"] == 18
    assert _strip_versions(committed) == _strip_versions(proof.to_json(run))
