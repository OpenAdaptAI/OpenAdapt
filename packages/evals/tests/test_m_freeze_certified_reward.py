"""The Phase-1/pilot M-freeze is complete and still names the live sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openadapt_evals.extradup.mutations import MUTANTS

REPO = Path(__file__).resolve().parents[1]
FREEZE_PATH = (
    REPO
    / "docs"
    / "preregistrations"
    / "M_FREEZE_CERTIFIED_REWARD_RL_PILOT_2026_09_02.json"
)
PROOF_JSON = REPO / "docs" / "reward" / "proof_2026-09-01.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _load() -> dict:
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


def test_freeze_file_exists_and_parses() -> None:
    freeze = _load()
    assert freeze["schema"] == "openadapt.certified-reward-rl.m-freeze/v1"
    assert freeze["status"] == "frozen"
    assert freeze["reports_a_result"] is False


def test_required_pins_are_present() -> None:
    freeze = _load()
    assert freeze["base_checkpoint"]["model_id"] == "Qwen/Qwen2.5-VL-3B-Instruct"
    assert freeze["base_checkpoint"]["revision"]
    assert freeze["hyperparameters"]["learning_rate"] == "1.0e-6"
    assert freeze["hyperparameters"]["group_size"] == 4
    assert freeze["hyperparameters"]["num_generations"] == 4
    assert freeze["seed_schedule"]["K"] == 3
    assert freeze["epsilon_levels"]["certified_sor"]["certificate_digest"].startswith(
        "sha256:"
    )
    assert set(freeze["arms"]) == {
        "visual_only",
        "certified_sor",
        "shuffled",
        "no-train",
    }
    assert freeze["outcomes"]["primary"]["name"] == "holdout_silent_incorrect_success"
    secondary = {item["name"] for item in freeze["outcomes"]["secondary"]}
    assert secondary == {
        "honest_write_success",
        "over_halt",
        "group_unscored_rate",
    }


def test_seed_schedule_is_the_proof_schedule() -> None:
    freeze = _load()
    proof_payload = json.loads(PROOF_JSON.read_text(encoding="utf-8"))
    assert freeze["seed_schedule"]["seeds"] == proof_payload["seed_schedule"]
    assert freeze["seed_schedule"]["K"] == proof_payload["trials_per_condition"]
    assert freeze["seed_schedule"]["K"] == 3
    assert freeze["statistical_analysis_plan"]["best_seed_selection"] is False


def test_extradup_mutants_match_the_kit_and_stay_out_of_training() -> None:
    freeze = _load()
    assert tuple(freeze["extradup_mutants"]["operators"]) == MUTANTS
    assert freeze["extradup_mutants"]["in_training_dataset"] is False
    assert freeze["extradup_mutants"]["in_training_reward"] is False
    assert freeze["task_split"]["train_operators"] == ["control"]
    for mutant in MUTANTS:
        assert mutant not in freeze["task_split"]["train_operators"]
        assert mutant in freeze["task_split"]["holdout_operators"]


def test_train_and_holdout_patient_ids_are_disjoint() -> None:
    freeze = _load()
    train = freeze["task_split"]["train_patient_ids"]
    holdout = freeze["task_split"]["holdout_patient_ids"]
    assert train == [row["patient_id"] for row in freeze["task_split"]["train_gold"]]
    assert holdout == [row["patient_id"] for row in freeze["task_split"]["holdout_gold"]]
    assert len(train) == 8
    assert len(holdout) == 8
    assert set(train).isdisjoint(set(holdout))
    assert freeze["task_split"]["holdout_mutant_host"] in holdout
    assert freeze["task_split"]["holdout_mutant_host"] not in train


def test_certificate_digest_matches_the_committed_proof() -> None:
    freeze = _load()
    proof_payload = json.loads(PROOF_JSON.read_text(encoding="utf-8"))
    certified = freeze["epsilon_levels"]["certified_sor"]
    assert certified["certificate_digest"] == proof_payload["certificate"]["digest"]
    assert certified["contract_digest"] == proof_payload["contract"]["digest"]
    assert certified["epsilon"] == proof_payload["certificate"]["epsilon"]
    assert certified["delta"] == proof_payload["certificate"]["delta"]
    assert certified["expiry_policy_updates"] == proof_payload["certificate"][
        "expiry_policy_updates"
    ]


def test_execute_seal_stays_false() -> None:
    freeze = _load()
    assert freeze["scope"]["execute_seal"] is False
    assert freeze["scope"]["production_acceptance"] is False
    assert freeze["scope"]["calibration_scope"] == "synthetic"


def test_pinned_source_hashes_still_match() -> None:
    freeze = _load()
    assert freeze["pinned_sources"]
    for item in freeze["pinned_sources"]:
        path = REPO / item["path"]
        assert path.is_file(), item["path"]
        assert _sha256(path) == item["sha256"], item["path"]
