"""Write, calibrate and self-sign a synthetic reward bundle.

A reward bundle is the directory ``openadapt_flow.reward.models.RewardBundle``
loads: the contract, its required and forbidden effects, the oracle recipe,
and (when certified) a self-signed certificate with the calibration counts
beside it. :func:`write_bundle` writes one from the effects and oracle a
caller passes; tests, the benchmark harnesses and the ``serve-reward`` demo
seed (:mod:`openadapt_flow.reward.seed`) all build their bundles with it.

Nothing here is a production recipe. A production-scope certificate is
calibrated on a held-out corpus by the OpenAdapt control service.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.process_capability import (
    _digest_payload,
    canonical_json_bytes,
)
from openadapt_types.reward import RewardCertificateV1, RewardContractV1

from openadapt_flow.reward.calibration import (
    CalibrationResult,
    confidence_delta,
    corpus_digest_for,
    corpus_from_effects,
    extradup_trials,
)
from openadapt_flow.reward.models import (
    CERTIFICATE_FILE,
    CONTRACT_FILE,
    FORBIDDEN_EFFECTS_FILE,
    ORACLE_FILE,
    REQUIRED_EFFECTS_FILE,
    RewardBundle,
)
from openadapt_flow.runtime.effects.effect import Effect

#: The task and environment a synthetic bundle's contract names. The values
#: are the ones the ``serve-reward`` demo seed has always used, so a bundle's
#: digests do not change with the module it was written from.
SYNTHETIC_TASK_ID = "task_mockmed_encounter_note"
SYNTHETIC_ENVIRONMENT_ID = "environment_mockmed_synthetic"
CERTIFICATE_EXPIRY_UPDATES = 1000
CALIBRATION_FILE = "calibration.json"
#: Trials run before the synthetic certificate is signed. 300 trials with
#: zero false accepts bound the rate at 0.0099 (95%).
CALIBRATION_TRIALS = 300
CALIBRATION_SEED = 20260901
CALIBRATION_CONFIDENCE = 0.95

#: Effects a bundle requires and forbids when the caller passes none.
DEFAULT_REQUIRED_EFFECTS: list[dict[str, Any]] = [
    {
        "kind": "record_written",
        "match": {"patient_id": {"param": "patient_id"}, "type": "Triage"},
        "expected_count": 1,
        # The claim is about what this episode added, not about what the
        # store happens to hold. Without it a rollout that did nothing
        # scores the full reward whenever the subject already had a row.
        "count_new_only": True,
    }
]

DEFAULT_FORBIDDEN_EFFECTS: list[dict[str, Any]] = [
    {
        "kind": "record_written",
        "match": {"patient_id": {"param": "patient_id"}, "type": "Discharge"},
        "expected_count": 1,
    }
]


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
    identity_keys: list[str] | None = None,
) -> None:
    """Write one synthetic bundle. Tests pass their own effects and keys."""

    directory.mkdir(parents=True, exist_ok=True)
    required = _canonical(
        DEFAULT_REQUIRED_EFFECTS if required_effects is None else required_effects
    )
    forbidden = _canonical(
        DEFAULT_FORBIDDEN_EFFECTS if forbidden_effects is None else forbidden_effects
    )
    oracle_doc = _canonical(oracle)
    keys = ["patient_id"] if identity_keys is None else list(identity_keys)
    # The corpus a certificate is calibrated on comes from the contract's own
    # effects, so its digest is computed here and not chosen.
    corpus_digest = corpus_digest_for(
        [Effect.model_validate(item) for item in required],
        [Effect.model_validate(item) for item in forbidden],
        keys,
    )
    contract = RewardContractV1.model_validate(
        {
            "contract_id": contract_id,
            "contract_version": "version_0001",
            "task_id": SYNTHETIC_TASK_ID,
            "task_digest": _digest_payload({"task": SYNTHETIC_TASK_ID}),
            "environment_id": SYNTHETIC_ENVIRONMENT_ID,
            "environment_digest": _digest_payload(
                {"environment": SYNTHETIC_ENVIRONMENT_ID}
            ),
            "required_effect_contract_digest": _digest_payload(required),
            "forbidden_effect_contract_digest": _digest_payload(forbidden),
            "oracle": {
                "channel": channel,
                "identity_keys": keys,
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
            delta=confidence_delta(calibration.confidence),
        )
        _write(directory / CERTIFICATE_FILE, certificate.model_dump(mode="json"))
        _write(directory / CALIBRATION_FILE, calibration.as_metadata())


def calibrate_bundle(directory: Path) -> CalibrationResult:
    """Run the seeded trials through this bundle's judge.

    The corpus is read off the bundle's own required and forbidden effects,
    so the faults it plants are faults in the records this contract talks
    about. The certificate's ``epsilon`` is the exact one-sided
    Clopper-Pearson bound from these counts, and ``calibration.json`` beside
    the certificate records the counts, the corpus digest, and which fault
    classes applied, so a reader can recompute the bound and see what it
    covers.
    """

    from openadapt_types.oracle import OracleChannel

    from openadapt_flow.reward.oracles import observation
    from openadapt_flow.reward.worker import judge_episode
    from openadapt_flow.runtime.effects.effect import EffectState

    bundle = RewardBundle.load(directory)
    channel = OracleChannel(bundle.contract.oracle.channel)
    corpus = corpus_from_effects(
        bundle.required_effects,
        bundle.forbidden_effects,
        bundle.identity_keys,
    )

    def checker(before: Any, current: Any, identity: dict[str, str]) -> Any:
        pre = EffectState(substrate=channel.value, reachable=True, records=list(before))
        observed = observation(channel, identity, list(current))
        return judge_episode(bundle, identity, "completed", pre, observed).outcome

    return extradup_trials(
        checker,
        corpus,
        trials=CALIBRATION_TRIALS,
        generator_seed=CALIBRATION_SEED,
        confidence=CALIBRATION_CONFIDENCE,
        corpus_digest=bundle.contract.certificate_policy.calibration_corpus_digest,
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
