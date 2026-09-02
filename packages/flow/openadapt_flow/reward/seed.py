"""Synthetic MockMed reward fixtures for ``serve-reward --seed-mockmed``.

Two bundles and one records file:

* ``contracts/mockmed`` reads ``mockmed/records.json`` through the
  ``json_file`` recipe (channel ``file``, tier 2) and carries a self-signed
  certificate issued at policy update 0. Episode ``honest`` finds its
  encounter and scores ``verified``. Episode ``banner-lie`` finds nothing:
  the screen said saved, the store holds no record, ``wrong_effect`` at
  the contract's declared penalty of 0.
* ``contracts/mockmed-tier0`` reads ``mockmed/screen.json`` through the
  ``screen_dump`` recipe (channel ``ocr``, tier 0). The screen dump shows
  the banner-lie encounter as saved. The receipt is ``development_only``
  and never certified, whatever the dump says.

Nothing here is a production recipe. Both bundles are synthetic.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.process_capability import _digest_payload, canonical_json_bytes
from openadapt_types.reward import RewardCertificateV1, RewardContractV1

from openadapt_flow.reward.calibration import CalibrationResult, extradup_trials
from openadapt_flow.reward.models import (
    CERTIFICATE_FILE,
    CONTRACT_FILE,
    FORBIDDEN_EFFECTS_FILE,
    ORACLE_FILE,
    REQUIRED_EFFECTS_FILE,
    RewardBundle,
)

MOCKMED_TASK_ID = "task_mockmed_encounter_note"
MOCKMED_ENVIRONMENT_ID = "environment_mockmed_synthetic"
MOCKMED_CONTRACT_ID = "reward_contract_mockmed"
MOCKMED_TIER0_CONTRACT_ID = "reward_contract_mockmed_tier0"
MOCKMED_HONEST_PATIENT = "patient-honest-0001"
MOCKMED_LIE_PATIENT = "patient-lie-0002"
MOCKMED_DUPLICATE_PATIENT = "patient-dup-0003"
MOCKMED_CHECKPOINT = "policy_checkpoint_mockmed_0"
CERTIFICATE_EXPIRY_UPDATES = 1000
CALIBRATION_FILE = "calibration.json"
#: ExtraDup trials the seed runs before it signs the synthetic certificate.
#: 300 trials with zero false accepts bound the rate at 0.0099 (95%).
CALIBRATION_TRIALS = 300
CALIBRATION_SEED = 20260901
CALIBRATION_CONFIDENCE = 0.95

_RECORDS: list[dict[str, Any]] = [
    {
        "id": 1,
        "patient_id": MOCKMED_HONEST_PATIENT,
        "type": "Triage",
        "status": "saved",
    },
    {
        "id": 2,
        "patient_id": MOCKMED_DUPLICATE_PATIENT,
        "type": "Triage",
        "status": "saved",
    },
    {
        "id": 3,
        "patient_id": MOCKMED_DUPLICATE_PATIENT,
        "type": "Triage",
        "status": "saved",
    },
]

_SCREEN: list[dict[str, Any]] = [
    {"patient_id": MOCKMED_HONEST_PATIENT, "type": "Triage", "status": "saved"},
    {"patient_id": MOCKMED_LIE_PATIENT, "type": "Triage", "status": "saved"},
]

_REQUIRED_EFFECTS: list[dict[str, Any]] = [
    {
        "kind": "record_written",
        "match": {"patient_id": {"param": "patient_id"}, "type": "Triage"},
        "expected_count": 1,
    }
]

_FORBIDDEN_EFFECTS: list[dict[str, Any]] = [
    {
        "kind": "record_written",
        "match": {"patient_id": {"param": "patient_id"}, "type": "Discharge"},
        "expected_count": 1,
    }
]


def seed_mockmed(
    data_dir: Path, key: Ed25519PrivateKey, issuer_key_id: str
) -> dict[str, Path]:
    """Write both bundles and the records files. Returns the bundle paths."""

    data_dir = Path(data_dir)
    store = data_dir / "mockmed"
    store.mkdir(parents=True, exist_ok=True)
    _write(store / "records.json", {"records": _RECORDS})
    _write(store / "screen.json", {"records": _SCREEN})

    tier2 = data_dir / "contracts" / "mockmed"
    write_bundle(
        tier2,
        contract_id=MOCKMED_CONTRACT_ID,
        oracle={
            "kind": "json_file",
            "path": str(store / "records.json"),
            "records_key": "records",
        },
        channel="file",
        key=key,
        issuer_key_id=issuer_key_id,
        certify=True,
    )
    tier0 = data_dir / "contracts" / "mockmed-tier0"
    write_bundle(
        tier0,
        contract_id=MOCKMED_TIER0_CONTRACT_ID,
        oracle={
            "kind": "screen_dump",
            "path": str(store / "screen.json"),
            "records_key": "records",
        },
        channel="ocr",
        key=key,
        issuer_key_id=issuer_key_id,
        certify=False,
    )
    return {"tier2": tier2, "tier0": tier0}


def mockmed_episode(
    patient_id: str,
    *,
    episode_id: str,
    contract_digest: str,
    policy_update: int = 0,
    runtime_signal: str = "completed",
) -> dict[str, Any]:
    """The wire payload for one seeded episode, in the trainer client's shape."""

    return {
        "episode_id": episode_id,
        "policy_checkpoint_id": MOCKMED_CHECKPOINT,
        "policy_update": policy_update,
        "reward_contract_digest": contract_digest,
        "task_id": MOCKMED_TASK_ID,
        "environment_id": MOCKMED_ENVIRONMENT_ID,
        "metadata": {
            "oracle_identity": {"patient_id": patient_id},
            "runtime_signal": runtime_signal,
        },
    }


def write_bundle(
    directory: Path,
    *,
    contract_id: str,
    oracle: dict[str, Any],
    channel: str,
    key: Ed25519PrivateKey,
    issuer_key_id: str,
    certify: bool,
    required_effects: list[dict[str, Any]] | None = None,
    forbidden_effects: list[dict[str, Any]] | None = None,
) -> None:
    """Write one synthetic bundle. Tests pass their own effects."""

    directory.mkdir(parents=True, exist_ok=True)
    required = _canonical(
        _REQUIRED_EFFECTS if required_effects is None else required_effects
    )
    forbidden = _canonical(
        _FORBIDDEN_EFFECTS if forbidden_effects is None else forbidden_effects
    )
    oracle_doc = _canonical(oracle)
    corpus_digest = _digest_payload({"corpus": "synthetic-mockmed", "size": 0})
    contract = RewardContractV1.model_validate(
        {
            "contract_id": contract_id,
            "contract_version": "version_0001",
            "task_id": MOCKMED_TASK_ID,
            "task_digest": _digest_payload({"task": MOCKMED_TASK_ID}),
            "environment_id": MOCKMED_ENVIRONMENT_ID,
            "environment_digest": _digest_payload(
                {"environment": MOCKMED_ENVIRONMENT_ID}
            ),
            "required_effect_contract_digest": _digest_payload(required),
            "forbidden_effect_contract_digest": _digest_payload(forbidden),
            "oracle": {
                "channel": channel,
                "identity_keys": ["patient_id"],
                "oracle_contract_digest": _digest_payload(oracle_doc),
            },
            "components": [{"name": "terminal_effect", "weight": 1.0}],
            # The synthetic banner lie yields 0, not the default -1 penalty:
            # this fixture demonstrates "no reward", not a tuned penalty.
            "scoring": {"wrong_effect_reward": 0.0},
            "certificate_policy": {
                "epsilon": 0.05,
                "delta": 0.05,
                "threshold": 0.5,
                "calibration_corpus_digest": corpus_digest,
                "expiry_policy_updates": CERTIFICATE_EXPIRY_UPDATES,
            },
        }
    )
    _write(directory / CONTRACT_FILE, contract.model_dump(mode="json"))
    _write(directory / REQUIRED_EFFECTS_FILE, required)
    _write(directory / FORBIDDEN_EFFECTS_FILE, forbidden)
    _write(directory / ORACLE_FILE, oracle_doc)
    if certify:
        calibration = calibrate_bundle(directory)
        certificate = self_signed_certificate(
            contract,
            key=key,
            issuer_key_id=issuer_key_id,
            issued_at_policy_update=0,
            expiry_policy_updates=CERTIFICATE_EXPIRY_UPDATES,
            epsilon=calibration.epsilon,
            delta=1.0 - calibration.confidence,
        )
        _write(directory / CERTIFICATE_FILE, certificate.model_dump(mode="json"))
        _write(directory / CALIBRATION_FILE, calibration.as_metadata())


def calibrate_bundle(directory: Path) -> CalibrationResult:
    """Run the seeded ExtraDup trials through this bundle's judge.

    The certificate's ``epsilon`` is the exact one-sided Clopper-Pearson
    bound from these counts. The counts are written beside the certificate
    so a reader can recompute the bound.
    """

    from openadapt_types.oracle import OracleChannel

    from openadapt_flow.reward.oracles import observation
    from openadapt_flow.reward.worker import judge_episode
    from openadapt_flow.runtime.effects.effect import EffectState

    bundle = RewardBundle.load(directory)
    channel = OracleChannel(bundle.contract.oracle.channel)

    def checker(records: Any, identity: dict[str, str]) -> Any:
        before = EffectState(substrate=channel.value, reachable=False)
        observed = observation(channel, identity, list(records))
        return judge_episode(bundle, identity, "completed", before, observed).outcome

    return extradup_trials(
        checker,
        trials=CALIBRATION_TRIALS,
        generator_seed=CALIBRATION_SEED,
        confidence=CALIBRATION_CONFIDENCE,
    )


def self_signed_certificate(
    contract: RewardContractV1,
    *,
    key: Ed25519PrivateKey,
    issuer_key_id: str,
    issued_at_policy_update: int,
    expiry_policy_updates: int,
    epsilon: float,
    delta: float,
    certificate_id: str = "reward_certificate_mockmed",
) -> RewardCertificateV1:
    """A certificate signed by the local key, synthetic scope.

    ``epsilon`` comes from :func:`calibrate_bundle`, never from a constant.
    A production-scope certificate is calibrated on a held-out corpus by
    the OpenAdapt control service and is not published. This one exists so
    the seeded run can show a certified receipt next to an uncertified one.
    """

    from openadapt_flow.reward.worker import _now

    unsigned = {
        "schema_version": "openadapt.reward-certificate/v1",
        "certificate_id": certificate_id,
        "reward_contract_digest": contract.digest,
        "checker_configuration_digest": _digest_payload({"checker": "synthetic"}),
        "epsilon": epsilon,
        "delta": delta,
        "threshold": contract.certificate_policy.threshold,
        "calibration_corpus_digest": contract.certificate_policy.calibration_corpus_digest,
        "calibration_scope": "synthetic",
        "issued_at_policy_update": issued_at_policy_update,
        "expiry_policy_updates": expiry_policy_updates,
        "issued_at": _now(),
        "issuer": "self_signed",
        "issuer_key_id": issuer_key_id,
    }
    signature = base64.b64encode(key.sign(canonical_json_bytes(unsigned))).decode(
        "ascii"
    )
    return RewardCertificateV1.model_validate(
        {**unsigned, "signature_algorithm": "ed25519", "signature": signature}
    )


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
