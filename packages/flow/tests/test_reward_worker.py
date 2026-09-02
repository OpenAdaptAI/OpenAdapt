"""Reference reward worker: outcome mapping, certificate, boundary, adapters."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("openadapt_types.reward")

from fastapi.testclient import TestClient  # noqa: E402
from openadapt_types.oracle import OracleChannel  # noqa: E402
from openadapt_types.reward import (  # noqa: E402
    RewardCalibrationScopeV1,
    RewardCertificateV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
)

from openadapt_flow.reward.calibration import (  # noqa: E402
    clopper_pearson_upper,
    extradup_trials,
)
from openadapt_flow.reward.callables import (  # noqa: E402
    UNSCORED_REWARD,
    drop_unscored,
    is_unscored,
    trl_reward_function,
    verl_compute_score,
)
from openadapt_flow.reward.models import (  # noqa: E402
    CERTIFICATE_FILE,
    FORBIDDEN_RECEIPT_KEYS,
    RewardBundle,
    assert_no_forbidden_keys,
)
from openadapt_flow.reward.oracles import JsonDocumentOracle  # noqa: E402
from openadapt_flow.reward.seed import (  # noqa: E402
    CALIBRATION_FILE,
    CALIBRATION_TRIALS,
    MOCKMED_DUPLICATE_PATIENT,
    MOCKMED_HONEST_PATIENT,
    MOCKMED_LIE_PATIENT,
    mockmed_episode,
    seed_mockmed,
)
from openadapt_flow.reward.serve import OPENAI_GRADER_ROUTE, create_app  # noqa: E402
from openadapt_flow.reward.worker import RewardWorker, RewardWorkerError  # noqa: E402


@pytest.fixture()
def seeded(tmp_path: Path) -> dict[str, Any]:
    from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key

    data_dir = tmp_path / "reward-ref"
    key = load_or_create_private_key(data_dir)
    paths = seed_mockmed(
        data_dir, key, "self_signed:" + fingerprint_of(key.public_key())
    )
    return {"data_dir": data_dir, **paths}


def _worker(
    seeded: dict[str, Any], which: str = "tier2", **kwargs: Any
) -> RewardWorker:
    return RewardWorker(seeded[which], seeded["data_dir"], token="test-token", **kwargs)


def _episode(
    worker: RewardWorker, patient_id: str, episode_id: str, **kwargs: Any
) -> dict[str, Any]:
    return mockmed_episode(
        patient_id,
        episode_id=episode_id,
        contract_digest=worker.contract.digest,
        **kwargs,
    )


def _receipt(envelope: dict[str, Any]) -> RewardEvidenceReceiptV1:
    return RewardEvidenceReceiptV1.model_validate(envelope["receipt"])


def _unreachable(tmp_path: Path) -> JsonDocumentOracle:
    return JsonDocumentOracle(tmp_path / "absent.json", channel=OracleChannel.FILE)


# -- outcome mapping ----------------------------------------------------------


def test_verified_tier2_is_certified(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_HONEST_PATIENT, "episode_honest_01")
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    assert receipt.oracle_tier == 2
    assert receipt.certified is True
    assert receipt.development_only is False
    assert receipt.scalar_reward == 1.0
    assert receipt.reward_components == {"terminal_effect": 1.0}
    assert receipt.certificate_state.value == "current"
    assert receipt.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC
    assert receipt.production_certified is False
    assert receipt.reward_contract_digest == worker.contract.digest
    assert envelope["unscored"] is False
    assert envelope["execute_seal"] is False
    assert envelope["production_seal"] is False
    assert envelope["flow_governed_policy"] is False
    assert worker.verify_receipt(receipt)


def test_verified_tier2_expired_certificate_is_not_certified(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    assert worker.certificate is not None
    expired_update = worker.certificate.expires_at_policy_update
    envelope = worker.score_episode(
        _episode(
            worker,
            MOCKMED_HONEST_PATIENT,
            "episode_honest_expired",
            policy_update=expired_update,
        )
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    assert receipt.certified is False
    assert receipt.certificate_state.value == "expired"
    assert receipt.scalar_reward == 1.0


def test_tier0_is_development_only_never_certified(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded, "tier0")
    assert worker.certificate is None
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_LIE_PATIENT, "episode_tier0_lie")
    )
    receipt = _receipt(envelope)
    # The screen dump says the lie episode saved. The channel is OCR, so the
    # verdict may be VERIFIED, and it still cannot be certified.
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    assert receipt.oracle_tier == 0
    assert receipt.development_only is True
    assert receipt.certified is False
    assert receipt.certificate_state.value == "absent"
    assert receipt.calibration_scope is None


def test_banner_lie_yields_zero(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_LIE_PATIENT, "episode_banner_lie_01")
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.WRONG_EFFECT
    assert receipt.scalar_reward == 0.0
    assert receipt.certified is True
    assert envelope["unscored"] is False


def test_duplicate_create_is_wrong_effect(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_DUPLICATE_PATIENT, "episode_duplicate_01")
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.WRONG_EFFECT
    assert receipt.scalar_reward == 0.0
    evidence = json.loads(
        (
            seeded["data_dir"] / "rewards" / receipt.receipt_id / "evidence.json"
        ).read_text()
    )
    assert evidence["required_verdicts"][0]["observed_count"] == 2


def test_same_episode_twice_is_rejected(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_once_only_1")
    worker.score_episode(payload)
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.status_code == 409
    assert excinfo.value.error == "duplicate_episode"


def test_indeterminate_is_unscored_not_zero(
    seeded: dict[str, Any], tmp_path: Path
) -> None:
    worker = _worker(seeded, oracle=_unreachable(tmp_path))
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_HONEST_PATIENT, "episode_unreachable_1")
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.FAILED_PLATFORM
    assert receipt.uncertainty.value == "oracle_unavailable"
    assert receipt.scalar_reward is None
    assert receipt.reward_components == {}
    assert envelope["unscored"] is True


def test_count_new_only_needs_a_baseline(seeded: dict[str, Any]) -> None:
    from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key
    from openadapt_flow.reward.seed import write_bundle

    key = load_or_create_private_key(seeded["data_dir"])
    directory = seeded["data_dir"] / "contracts" / "mockmed-new-only"
    write_bundle(
        directory,
        contract_id="reward_contract_mockmed_new_only",
        oracle={
            "kind": "json_file",
            "path": str(seeded["data_dir"] / "mockmed" / "records.json"),
            "records_key": "records",
        },
        channel="file",
        key=key,
        issuer_key_id="self_signed:" + fingerprint_of(key.public_key()),
        certify=False,
        required_effects=[
            {
                "kind": "record_written",
                "match": {"patient_id": {"param": "patient_id"}, "type": "Triage"},
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
    )
    worker = RewardWorker(directory, seeded["data_dir"], token="test-token")
    # No baseline: the delta is unknowable, so the judge is INDETERMINATE and
    # the episode is unscored, never 0.
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_HONEST_PATIENT, "episode_new_only_01")
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.uncertainty.value == "effect_uncertain"
    assert receipt.scalar_reward is None
    # With a baseline registered before the episode, the same store judges,
    # and the descriptor no longer needs to carry the identity.
    worker.begin_episode("episode_new_only_02", {"patient_id": MOCKMED_LIE_PATIENT})
    store = seeded["data_dir"] / "mockmed" / "records.json"
    records = json.loads(store.read_text())
    records["records"].append(
        {"id": 7, "patient_id": MOCKMED_LIE_PATIENT, "type": "Triage"}
    )
    store.write_text(json.dumps(records))
    envelope = worker.score_episode(
        {
            "episode_id": "episode_new_only_02",
            "policy_checkpoint_id": "policy_checkpoint_mockmed_0",
            "policy_update": 0,
            "reward_contract_digest": worker.contract.digest,
        }
    )
    assert _receipt(envelope).reward_outcome is RewardOutcomeV1.VERIFIED


def test_halt_signal_with_no_effect_is_halted_before_effect(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(
            worker,
            MOCKMED_LIE_PATIENT,
            "episode_halted_01",
            runtime_signal="halted_before_effect",
        )
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.HALTED_BEFORE_EFFECT
    assert receipt.scalar_reward == 0.0


def test_halt_signal_with_effect_present_is_reconciliation(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(
            worker,
            MOCKMED_HONEST_PATIENT,
            "episode_halted_lie_01",
            runtime_signal="halted_before_effect",
        )
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.scalar_reward is None
    assert envelope["unscored"] is True


def test_forbidden_effect_is_wrong_effect(seeded: dict[str, Any]) -> None:
    store = seeded["data_dir"] / "mockmed" / "records.json"
    records = json.loads(store.read_text())
    records["records"].append(
        {"id": 9, "patient_id": MOCKMED_HONEST_PATIENT, "type": "Discharge"}
    )
    store.write_text(json.dumps(records))
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_HONEST_PATIENT, "episode_forbidden_01")
    )
    assert _receipt(envelope).reward_outcome is RewardOutcomeV1.WRONG_EFFECT


# -- refusals -------------------------------------------------------------------


def test_extra_identity_key_is_refused(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_extra_key_01")
    payload["metadata"]["oracle_identity"]["encounter_id"] = "enc-1"
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.status_code == 422
    assert excinfo.value.error == "identity_mismatch"


def test_missing_identity_is_refused(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_no_identity_1")
    del payload["metadata"]["oracle_identity"]
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.error == "identity_missing"


def test_wrong_contract_digest_is_refused(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_wrong_contract")
    payload["reward_contract_digest"] = "sha256:" + "0" * 64
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.error == "contract_mismatch"


def test_extra_episode_field_is_refused(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_extra_field_1")
    payload["screenshot"] = "iVBORw0KGgo="
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.error == "invalid_episode"
    payload = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_extra_field_2")
    payload["metadata"]["screenshots"] = ["iVBORw0KGgo="]
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.error == "invalid_episode"


def test_receipt_carries_no_screenshot_or_rollout_bytes(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    envelope = worker.score_episode(
        _episode(worker, MOCKMED_HONEST_PATIENT, "episode_no_bytes_01")
    )
    receipt = envelope["receipt"]
    assert FORBIDDEN_RECEIPT_KEYS.isdisjoint(receipt)
    assert FORBIDDEN_RECEIPT_KEYS.isdisjoint(envelope)
    flat = json.dumps(envelope)
    assert MOCKMED_HONEST_PATIENT not in flat
    assert "iVBOR" not in flat
    with pytest.raises(ValueError, match="forbids"):
        assert_no_forbidden_keys({**receipt, "screenshots": []})
    for name in ("execution_id", "workflow_digest", "qualification_id", "contracts"):
        assert name not in receipt


def test_bundle_refuses_tampered_effects(seeded: dict[str, Any]) -> None:
    bundle_dir = seeded["tier2"]
    path = bundle_dir / "required_effects.json"
    effects = json.loads(path.read_text())
    effects[0]["expected_count"] = 2
    path.write_text(json.dumps(effects))
    with pytest.raises(ValueError, match="digest"):
        RewardBundle.load(bundle_dir)


def test_certificate_bound_is_recomputable(seeded: dict[str, Any]) -> None:
    bundle_dir = seeded["tier2"]
    certificate = RewardCertificateV1.model_validate(
        json.loads((bundle_dir / CERTIFICATE_FILE).read_text())
    )
    assert certificate.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC
    assert certificate.issuer.value == "self_signed"
    calibration = json.loads((bundle_dir / CALIBRATION_FILE).read_text())
    assert calibration["calibration_trials"] == CALIBRATION_TRIALS
    assert calibration["calibration_false_accepts"] == 0
    recomputed = clopper_pearson_upper(
        calibration["calibration_false_accepts"],
        calibration["calibration_trials"],
        confidence=calibration["calibration_confidence"],
    )
    assert certificate.epsilon == recomputed
    assert certificate.epsilon == pytest.approx(
        1.0 - 0.05 ** (1.0 / CALIBRATION_TRIALS)
    )


def test_clopper_pearson_upper_matches_known_values() -> None:
    # 0 of 15 is the bound the openadapt-evals proof run reports.
    assert clopper_pearson_upper(0, 15) == pytest.approx(0.181036, abs=1e-6)
    assert clopper_pearson_upper(0, 20) == pytest.approx(0.1391, abs=1e-3)
    assert clopper_pearson_upper(1, 20) == pytest.approx(0.2161, abs=1e-3)
    assert clopper_pearson_upper(20, 20) == 1.0
    result = extradup_trials(
        lambda records, identity: RewardOutcomeV1.VERIFIED,
        trials=10,
        generator_seed=1,
    )
    assert result.false_accepts == 10
    assert result.epsilon == 1.0


# -- HTTP surface ---------------------------------------------------------------


def _client(
    seeded: dict[str, Any], which: str = "tier2", **kwargs: Any
) -> tuple[TestClient, RewardWorker]:
    worker = _worker(seeded, which, **kwargs)
    client = TestClient(create_app(worker))
    client.headers["Authorization"] = "Bearer test-token"
    return client, worker


def test_http_reward_roundtrip(seeded: dict[str, Any]) -> None:
    client, worker = _client(seeded)
    bare = TestClient(client.app)
    health = bare.get("/health").json()
    assert health["issuer"] == "self_signed"
    assert health["execute_seal"] is False
    assert health["oracle_tier"] == 2
    assert bare.post("/v1/rewards", json={}).status_code == 401
    created = client.post(
        "/v1/rewards", json=_episode(worker, MOCKMED_HONEST_PATIENT, "episode_http_01")
    )
    assert created.status_code == 200
    assert created.headers["X-OpenAdapt-Execute-Seal"] == "false"
    receipt_id = created.json()["receipt"]["receipt_id"]
    fetched = client.get(f"/v1/rewards/{receipt_id}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert client.get("/v1/rewards/reward_receipt_missing").status_code == 404
    again = client.post(
        "/v1/rewards", json=_episode(worker, MOCKMED_HONEST_PATIENT, "episode_http_01")
    )
    assert again.status_code == 409


def test_http_matches_the_evals_client_wire_shape(seeded: dict[str, Any]) -> None:
    """Round-trip the exact JSON ``openadapt_evals.reward.receipts`` sends.

    ``EpisodeDescriptor.as_payload()`` drops ``None`` fields and always sends
    ``metadata``; ``HttpRewardEndpoint`` requires HTTP 200 and parses either
    a bare receipt or ``{"receipt": ...}`` when the body has no top-level
    ``schema_version``.
    """

    client, worker = _client(seeded)
    payload = {
        "episode_id": "episode_evals_client_1",
        "policy_checkpoint_id": "policy_checkpoint_evals",
        "policy_update": 4,
        "reward_contract_digest": worker.contract.digest,
        "metadata": {"oracle_identity": {"patient_id": MOCKMED_HONEST_PATIENT}},
    }
    response = client.post("/v1/rewards", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "receipt" in body and "schema_version" not in body
    receipt = RewardEvidenceReceiptV1.model_validate(body["receipt"])
    assert receipt.episode_id == "episode_evals_client_1"
    assert receipt.policy_checkpoint_id == "policy_checkpoint_evals"
    assert receipt.policy_update == 4
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    minimal = {
        "episode_id": "episode_evals_client_2",
        "policy_checkpoint_id": "policy_checkpoint_evals",
        "policy_update": 4,
        "reward_contract_digest": worker.contract.digest,
        "metadata": {},
    }
    refused = client.post("/v1/rewards", json=minimal)
    assert refused.status_code == 422
    assert refused.json()["error"] == "identity_missing"


def test_openai_grader_route_contract(seeded: dict[str, Any]) -> None:
    client, worker = _client(seeded)
    item = _episode(worker, MOCKMED_HONEST_PATIENT, "episode_grader_ok_1")
    scored = client.post(
        OPENAI_GRADER_ROUTE,
        json={"sample": {"output_text": "saved"}, "item": item},
    )
    assert scored.status_code == 200
    body = scored.json()
    assert body["score"] == 1.0
    assert 0.0 <= body["score"] <= 1.0
    assert body["certified"] is True
    lie = client.post(
        OPENAI_GRADER_ROUTE,
        json={
            "sample": {"output_text": "saved"},
            "item": _episode(worker, MOCKMED_LIE_PATIENT, "episode_grader_lie_1"),
        },
    )
    assert lie.json()["score"] == 0.0
    assert client.post(OPENAI_GRADER_ROUTE, json={"item": item}).status_code == 400


def test_openai_grader_route_refuses_to_grade_unscored(
    seeded: dict[str, Any], tmp_path: Path
) -> None:
    client, worker = _client(seeded, oracle=_unreachable(tmp_path))
    response = client.post(
        OPENAI_GRADER_ROUTE,
        json={
            "sample": {"output_text": "saved"},
            "item": _episode(worker, MOCKMED_HONEST_PATIENT, "episode_grader_x_1"),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "unscored"
    assert "score" not in response.json()


# -- trainer callables ----------------------------------------------------------


class _State:
    global_step = 3


def test_trl_reward_function_contract(seeded: dict[str, Any], tmp_path: Path) -> None:
    worker = _worker(seeded)
    reward = trl_reward_function(
        worker,
        policy_checkpoint_id="policy_checkpoint_trl_1",
        reward_contract_digest=worker.contract.digest,
    )
    assert reward.__name__ == "openadapt_verified_effect_reward"
    rewards = reward(
        prompts=["p1", "p2"],
        completions=["c1", "c2"],
        completion_ids=[[1], [2]],
        trainer_state=_State(),
        episode_id=["episode_trl_0001", "episode_trl_0002"],
        oracle_identity=[
            {"patient_id": MOCKMED_HONEST_PATIENT},
            {"patient_id": MOCKMED_LIE_PATIENT},
        ],
    )
    assert rewards == [1.0, 0.0]
    unreachable = _worker(seeded, oracle=_unreachable(tmp_path))
    reward2 = trl_reward_function(
        unreachable,
        policy_checkpoint_id="policy_checkpoint_trl_2",
        reward_contract_digest=unreachable.contract.digest,
    )
    rewards2 = reward2(
        prompts=["p"],
        completions=["c"],
        trainer_state=_State(),
        episode_id=["episode_trl_0003"],
        oracle_identity=[{"patient_id": MOCKMED_HONEST_PATIENT}],
    )
    assert rewards2 == [None]
    kept, (kept_completions,) = drop_unscored([1.0, None, 0.0], ["a", "b", "c"])
    assert kept == [1.0, 0.0]
    assert kept_completions == ["a", "c"]


def test_verl_compute_score_contract(seeded: dict[str, Any], tmp_path: Path) -> None:
    worker = _worker(seeded)
    compute_score = verl_compute_score(
        worker,
        policy_checkpoint_id="policy_checkpoint_verl",
        reward_contract_digest=worker.contract.digest,
    )
    result = compute_score(
        data_source="mockmed",
        solution_str="saved",
        ground_truth=None,
        extra_info={
            "openadapt_episode": {
                "episode_id": "episode_verl_0001",
                "oracle_identity": {"patient_id": MOCKMED_HONEST_PATIENT},
                "policy_update": 0,
            }
        },
    )
    assert result["score"] == 1.0
    assert result["openadapt_unscored"] is False
    unreachable = _worker(seeded, oracle=_unreachable(tmp_path))
    compute2 = verl_compute_score(
        unreachable,
        policy_checkpoint_id="policy_checkpoint_verl",
        reward_contract_digest=unreachable.contract.digest,
    )
    result2 = compute2(
        "mockmed",
        "saved",
        None,
        {
            "openadapt_episode": {
                "episode_id": "episode_verl_0002",
                "oracle_identity": {"patient_id": MOCKMED_HONEST_PATIENT},
            }
        },
    )
    assert math.isnan(result2["score"])
    assert result2["openadapt_unscored"] is True
    assert is_unscored(UNSCORED_REWARD)
    assert not is_unscored(0.0)
    kept, _ = drop_unscored([result["score"], result2["score"]])
    assert kept == [1.0]


# -- CLI and boundary -----------------------------------------------------------


def test_cli_wires_serve_reward() -> None:
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(
        ["serve-reward", "--contract", "/tmp/c", "--port", "8788", "--seed-mockmed"]
    )
    assert args.command == "serve-reward"
    assert args.contract == "/tmp/c"
    assert args.port == 8788
    assert args.seed_mockmed is True
    assert args.func.__name__ == "_cmd_serve_reward"


def test_source_boundary_has_no_cloud_modules() -> None:
    root = Path(__file__).resolve().parents[1] / "openadapt_flow" / "reward"
    names = {path.name for path in root.glob("*.py")}
    assert names.isdisjoint(
        {"tenant.py", "billing.py", "stripe.py", "control_plane.py"}
    )
    joined = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "openadapt_cloud" not in joined
    assert "openadapt-cloud" not in joined
    assert "app.openadapt.ai" not in joined
    assert "ExecuteEvidenceReceiptV1" not in joined
