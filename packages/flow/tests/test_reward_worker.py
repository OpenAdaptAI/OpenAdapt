"""Reference reward worker: outcome mapping, certificate, boundary, client."""

from __future__ import annotations

import json
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

import openadapt_flow.reward.callables as callables  # noqa: E402
from openadapt_flow.reward.calibration import (  # noqa: E402
    CorpusRecipe,
    clopper_pearson_upper,
    corpus_from_effects,
    extradup_trials,
)
from openadapt_flow.reward.callables import (  # noqa: E402
    HttpRewardClient,
    episode_from_columns,
    scalar_of,
)
from openadapt_flow.reward.models import (  # noqa: E402
    CERTIFICATE_FILE,
    FORBIDDEN_RECEIPT_KEYS,
    RewardBundle,
    assert_no_forbidden_keys,
)
from openadapt_flow.reward.oracles import (  # noqa: E402
    observation,
)
from openadapt_flow.reward.seed import (  # noqa: E402
    CALIBRATION_FILE,
    CALIBRATION_TRIALS,
    MOCKMED_DUPLICATE_PATIENT,
    MOCKMED_HONEST_PATIENT,
    MOCKMED_LIE_PATIENT,
    MOCKMED_QUERY,
    mockmed_episode,
    seed_mockmed,
    write_bundle,
    write_mockmed_banner,
    write_mockmed_encounter,
)
from openadapt_flow.reward.serve import OPENAI_GRADER_ROUTE, create_app  # noqa: E402
from openadapt_flow.reward.worker import RewardWorker, RewardWorkerError  # noqa: E402
from openadapt_flow.runtime.effects.effect import Effect  # noqa: E402


@pytest.fixture()
def seeded(tmp_path: Path) -> dict[str, Any]:
    from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key

    data_dir = tmp_path / "reward-ref"
    key = load_or_create_private_key(data_dir)
    paths = seed_mockmed(
        data_dir, key, "self_signed:" + fingerprint_of(key.public_key())
    )
    return {
        "data_dir": data_dir,
        "database": data_dir / "mockmed" / "records.db",
        "screen": data_dir / "mockmed" / "screen.json",
        **paths,
    }


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


def _run(
    worker: RewardWorker,
    seeded: dict[str, Any],
    patient_id: str,
    episode_id: str,
    *,
    encounters: int = 1,
    banners: int = 0,
    discharge: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Register the episode, simulate what it wrote, then score it.

    Registration happens first because the required effect is a claim about
    change: the judge needs the pre-episode baseline, and the subject is
    fixed before the rollout produces anything.
    """

    worker.begin_episode(episode_id, {"patient_id": patient_id})
    for _ in range(encounters):
        write_mockmed_encounter(seeded["database"], patient_id)
    if discharge:
        write_mockmed_encounter(seeded["database"], patient_id, type_="Discharge")
    for _ in range(banners):
        write_mockmed_banner(seeded["screen"], patient_id)
    return worker.score_episode(_episode(worker, patient_id, episode_id, **kwargs))


def _receipt(envelope: dict[str, Any]) -> RewardEvidenceReceiptV1:
    return RewardEvidenceReceiptV1.model_validate(envelope["receipt"])


class _UnreachableOracle:
    """A tier-2 channel that cannot be read. Never a guessed empty list."""

    channel = OracleChannel.DB

    def read(self, identity: Any) -> Any:
        return observation(self.channel, identity, None)


def _unreachable(tmp_path: Path) -> _UnreachableOracle:
    return _UnreachableOracle()


# -- outcome mapping ----------------------------------------------------------


def test_verified_tier2_is_certified(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = _run(worker, seeded, MOCKMED_HONEST_PATIENT, "episode_honest_01")
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
    envelope = _run(
        worker,
        seeded,
        MOCKMED_HONEST_PATIENT,
        "episode_honest_expired",
        policy_update=expired_update,
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    assert receipt.certified is False
    assert receipt.certificate_state.value == "expired"
    assert receipt.scalar_reward == 1.0


def test_tier0_is_development_only_never_certified(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded, "tier0")
    assert worker.certificate is None
    # The banner appeared and nothing was written to the database. The screen
    # dump agrees with the banner, so the OCR channel may say VERIFIED, and it
    # still cannot be certified.
    envelope = _run(
        worker,
        seeded,
        MOCKMED_LIE_PATIENT,
        "episode_tier0_lie",
        encounters=0,
        banners=1,
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.VERIFIED
    assert receipt.oracle_tier == 0
    assert receipt.development_only is True
    assert receipt.certified is False
    assert receipt.certificate_state.value == "absent"
    assert receipt.calibration_scope is None


def test_banner_lie_yields_zero(seeded: dict[str, Any]) -> None:
    """The same episode the OCR channel calls verified. The store holds nothing."""

    worker = _worker(seeded)
    envelope = _run(
        worker,
        seeded,
        MOCKMED_LIE_PATIENT,
        "episode_banner_lie_01",
        encounters=0,
        banners=1,
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.WRONG_EFFECT
    assert receipt.scalar_reward == 0.0
    assert receipt.certified is True
    assert envelope["unscored"] is False


def test_duplicate_create_is_wrong_effect(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = _run(
        worker,
        seeded,
        MOCKMED_DUPLICATE_PATIENT,
        "episode_duplicate_01",
        encounters=2,
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
    _run(worker, seeded, MOCKMED_HONEST_PATIENT, "episode_once_only_1")
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(
            _episode(worker, MOCKMED_HONEST_PATIENT, "episode_once_only_1")
        )
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
    worker = _worker(seeded)
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
    write_mockmed_encounter(seeded["database"], MOCKMED_LIE_PATIENT)
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
    envelope = _run(
        worker,
        seeded,
        MOCKMED_LIE_PATIENT,
        "episode_halted_01",
        encounters=0,
        runtime_signal="halted_before_effect",
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.HALTED_BEFORE_EFFECT
    assert receipt.scalar_reward == 0.0


def test_halt_signal_with_effect_present_is_reconciliation(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    envelope = _run(
        worker,
        seeded,
        MOCKMED_HONEST_PATIENT,
        "episode_halted_lie_01",
        runtime_signal="halted_before_effect",
    )
    receipt = _receipt(envelope)
    assert receipt.reward_outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED
    assert receipt.scalar_reward is None
    assert envelope["unscored"] is True


def test_forbidden_effect_is_wrong_effect(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    envelope = _run(
        worker,
        seeded,
        MOCKMED_HONEST_PATIENT,
        "episode_forbidden_01",
        discharge=True,
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
    envelope = _run(worker, seeded, MOCKMED_HONEST_PATIENT, "episode_no_bytes_01")
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


def _identity_bundle(
    seeded: dict[str, Any],
    name: str,
    required_effects: list[dict[str, Any]],
    identity_keys: list[str] | None = None,
) -> Path:
    """Write an uncertified bundle whose required effects the caller chooses."""

    from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key

    data_dir: Path = seeded["data_dir"]
    key = load_or_create_private_key(data_dir)
    directory = data_dir / "contracts" / name
    write_bundle(
        directory,
        contract_id=f"reward_contract_{name.replace('-', '_')}_000000",
        oracle={
            "kind": "sqlite",
            "path": str(data_dir / "mockmed" / "records.db"),
            "query": MOCKMED_QUERY,
        },
        channel="db",
        key=key,
        issuer_key_id="self_signed:" + fingerprint_of(key.public_key()),
        certify=False,
        required_effects=required_effects,
        identity_keys=identity_keys,
    )
    return directory


def test_bundle_refuses_content_only_required_effect(seeded: dict[str, Any]) -> None:
    """A declared identity key no required effect selects on is a false receipt.

    The oracle reads the whole collection, so a content-only selector accepts a
    write that landed on another subject while the receipt names this one.
    """

    directory = _identity_bundle(
        seeded,
        "content-only",
        [{"kind": "record_written", "match": {"type": "Triage"}, "expected_count": 1}],
    )
    with pytest.raises(ValueError, match="patient_id"):
        RewardBundle.load(directory)


def test_bundle_refusal_names_the_unselected_key(seeded: dict[str, Any]) -> None:
    """Two declared keys, one selected: the message names only the missing one."""

    directory = _identity_bundle(
        seeded,
        "half-bound",
        [
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "type": "Triage",
                },
                "expected_count": 1,
            }
        ],
        identity_keys=["patient_id", "encounter_id"],
    )
    with pytest.raises(ValueError) as excinfo:
        RewardBundle.load(directory)
    message = str(excinfo.value)
    assert "encounter_id" in message
    assert "patient_id" not in message.split(".")[0]


def test_bundle_accepts_an_identity_bound_required_effect(
    seeded: dict[str, Any],
) -> None:
    """The shipped MockMed bundle selects its subject, so it still loads."""

    bundle = RewardBundle.load(seeded["tier2"])
    assert bundle.identity_keys == ("patient_id",)
    assert RewardBundle.load(seeded["tier0"]).identity_keys == ("patient_id",)


def test_idempotency_key_binds_the_identity(seeded: dict[str, Any]) -> None:
    """An idempotency key filters the read set, so it counts as a selector."""

    directory = _identity_bundle(
        seeded,
        "by-key",
        [
            {
                "kind": "record_written",
                "match": {"type": "Triage"},
                "idempotency_key": {"param": "patient_id"},
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
    )
    assert RewardBundle.load(directory).identity_keys == ("patient_id",)


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


def _mockmed_corpus() -> CorpusRecipe:
    """The corpus the shipped MockMed contract's own effects describe."""

    return corpus_from_effects(
        [
            Effect.model_validate(
                {
                    "kind": "record_written",
                    "match": {
                        "patient_id": {"param": "patient_id"},
                        "type": "Triage",
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            )
        ],
        [],
        ["patient_id"],
    )


def test_clopper_pearson_upper_matches_known_values() -> None:
    # 0 of 15 is the bound the openadapt-evals proof run reports.
    assert clopper_pearson_upper(0, 15) == pytest.approx(0.181036, abs=1e-6)
    assert clopper_pearson_upper(0, 20) == pytest.approx(0.1391, abs=1e-3)
    assert clopper_pearson_upper(1, 20) == pytest.approx(0.2161, abs=1e-3)
    assert clopper_pearson_upper(20, 20) == 1.0
    # A checker that accepts everything false-accepts every trial, and the
    # bound it earns is 1.0, which the certificate model refuses.
    result = extradup_trials(
        lambda before, current, identity: RewardOutcomeV1.VERIFIED,
        _mockmed_corpus(),
        trials=10,
        generator_seed=1,
        corpus_digest="sha256:" + "0" * 64,
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


def _http_begin(
    client: TestClient,
    seeded: dict[str, Any],
    patient_id: str,
    episode_id: str,
    *,
    encounters: int = 1,
) -> None:
    """Register over HTTP, the way the environment does, then write."""

    response = client.post(
        "/v1/episodes",
        json={"episode_id": episode_id, "oracle_identity": {"patient_id": patient_id}},
    )
    assert response.status_code == 200, response.text
    for _ in range(encounters):
        write_mockmed_encounter(seeded["database"], patient_id)


def test_http_reward_roundtrip(seeded: dict[str, Any]) -> None:
    client, worker = _client(seeded)
    bare = TestClient(client.app)
    health = bare.get("/health").json()
    assert health["issuer"] == "self_signed"
    assert health["execute_seal"] is False
    assert health["oracle_tier"] == 2
    assert bare.post("/v1/rewards", json={}).status_code == 401
    assert bare.post("/v1/episodes", json={}).status_code == 401
    _http_begin(client, seeded, MOCKMED_HONEST_PATIENT, "episode_http_01")
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
    _http_begin(client, seeded, MOCKMED_HONEST_PATIENT, "episode_evals_client_1")
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
    _http_begin(client, seeded, MOCKMED_HONEST_PATIENT, "episode_grader_ok_1")
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
    _http_begin(
        client, seeded, MOCKMED_LIE_PATIENT, "episode_grader_lie_1", encounters=0
    )
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


# -- trainer client, and no trainer adapter ------------------------------------


def test_callables_offer_no_trainer_adapter() -> None:
    # TRL's GRPOTrainer turns a ``None`` reward into NaN, combines the
    # per-function rewards with ``nansum``, and takes the group mean over the
    # result, so with one reward function a ``None`` row trains as 0.0. The
    # reward contract forbids 0.0 for an unscored episode. verl's per-sample
    # ``compute_score`` hook has no sentinel at all. The canonical trainer
    # adapters are ``openadapt_evals.reward.trl.CertifiedRewardFunction`` and
    # ``openadapt_evals.reward.verl.CertifiedRewardManager``; they drop an
    # unscored episode by filling it with the mean of its scored group-mates.
    # flow keeps the worker and the HTTP client only.
    for name in ("trl_reward_function", "verl_compute_score", "compute_score"):
        assert not hasattr(callables, name), name
    for name in ("UNSCORED_REWARD", "is_unscored", "drop_unscored", "scored_groups"):
        assert not hasattr(callables, name), name
    assert "openadapt_evals.reward" in (callables.__doc__ or "")
    assert "0.0" in (callables.__doc__ or "")


def test_episode_from_columns_matches_the_evals_descriptor_shape(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    worker.begin_episode("episode_client_0001", {"patient_id": MOCKMED_HONEST_PATIENT})
    write_mockmed_encounter(seeded["database"], MOCKMED_HONEST_PATIENT)
    payload = episode_from_columns(
        episode_id="episode_client_0001",
        policy_checkpoint_id="policy_checkpoint_client",
        policy_update=3,
        reward_contract_digest=worker.contract.digest,
        oracle_identity={"patient_id": MOCKMED_HONEST_PATIENT},
    )
    # The keys ``openadapt_evals.reward.receipts.EpisodeDescriptor.as_payload``
    # sends, minus the optional ``task_id`` and ``environment_id``, plus the
    # descriptor model's own ``schema_version``. The worker accepts both.
    assert set(payload) == {
        "schema_version",
        "episode_id",
        "policy_checkpoint_id",
        "policy_update",
        "reward_contract_digest",
        "metadata",
    }
    assert payload["metadata"] == {
        "runtime_signal": "completed",
        "oracle_identity": {"patient_id": MOCKMED_HONEST_PATIENT},
    }
    envelope = worker.score_episode(payload)
    assert scalar_of(envelope) == 1.0


def test_http_reward_client_roundtrip(seeded: dict[str, Any], tmp_path: Path) -> None:
    import httpx

    app_client, worker = _client(seeded)

    def handler(request: httpx.Request) -> httpx.Response:
        response = app_client.post(
            request.url.path,
            content=request.content,
            headers={
                "Authorization": request.headers["Authorization"],
                "content-type": "application/json",
            },
        )
        return httpx.Response(response.status_code, content=response.content)

    client = HttpRewardClient(
        "http://reward-worker.test/",
        "test-token",
        transport=httpx.MockTransport(handler),
    )
    assert client.url == "http://reward-worker.test/v1/rewards"

    def payload(episode_id: str) -> dict[str, Any]:
        return episode_from_columns(
            episode_id=episode_id,
            policy_checkpoint_id="policy_checkpoint_client",
            policy_update=0,
            reward_contract_digest=worker.contract.digest,
            oracle_identity={"patient_id": MOCKMED_HONEST_PATIENT},
        )

    _http_begin(app_client, seeded, MOCKMED_HONEST_PATIENT, "episode_client_http_1")
    envelope = client.score_episode(payload("episode_client_http_1"))
    assert envelope["unscored"] is False
    assert scalar_of(envelope) == 1.0
    with pytest.raises(RuntimeError, match="already scored"):
        client.score_episode(payload("episode_client_http_1"))

    wrong_token = HttpRewardClient(
        "http://reward-worker.test", "wrong", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.HTTPStatusError):
        wrong_token.score_episode(payload("episode_client_http_2"))

    # An unscored episode comes back as a receipt with no scalar. The client
    # reports ``None``; it never invents a reward for it.
    unreachable_client, unreachable = _client(seeded, oracle=_unreachable(tmp_path))

    def handler2(request: httpx.Request) -> httpx.Response:
        response = unreachable_client.post(
            request.url.path,
            content=request.content,
            headers={
                "Authorization": request.headers["Authorization"],
                "content-type": "application/json",
            },
        )
        return httpx.Response(response.status_code, content=response.content)

    client2 = HttpRewardClient(
        "http://reward-worker.test",
        "test-token",
        transport=httpx.MockTransport(handler2),
    )
    envelope2 = client2.score_episode(
        episode_from_columns(
            episode_id="episode_client_http_3",
            policy_checkpoint_id="policy_checkpoint_client",
            policy_update=0,
            reward_contract_digest=unreachable.contract.digest,
            oracle_identity={"patient_id": MOCKMED_HONEST_PATIENT},
        )
    )
    assert envelope2["unscored"] is True
    assert scalar_of(envelope2) is None


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
