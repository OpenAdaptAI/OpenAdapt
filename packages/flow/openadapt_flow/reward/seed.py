"""Synthetic MockMed reward fixtures for ``serve-reward --seed-mockmed``.

Two bundles and two stores:

* ``contracts/mockmed`` reads ``mockmed/records.db`` through the ``sqlite``
  recipe (channel ``db``, tier 2) and carries a self-signed certificate
  issued at policy update 0. The database starts with two rows for the
  duplicate patient and nothing for anybody else, so an episode has to write
  something before it can be verified.
* ``contracts/mockmed-tier0`` reads ``mockmed/screen.json`` through the
  ``screen_dump`` recipe (channel ``ocr``, tier 0). Its receipts are
  ``development_only`` and never certified, whatever the dump says.

Read the two together and the fixture makes its point: write the banner into
the screen dump and nothing into the database, and the tier-0 worker says
``verified`` while the tier-2 worker says ``wrong_effect`` about the same
episode.

The tier-2 store is a SQLite database rather than a JSON document because the
tier has to rest on something the worker can check. Both JSON recipe kinds
build the same reader over the same kind of bytes, so neither can claim a
record channel; ``openadapt_flow.reward.oracles.assert_sqlite_database``
refuses a ``sqlite`` recipe whose file is not a real database.

Nothing here is a production recipe. Both bundles are synthetic.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.process_capability import _digest_payload, canonical_json_bytes
from openadapt_types.reward import RewardCertificateV1, RewardContractV1

from openadapt_flow.reward.calibration import (
    CalibrationResult,
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
#: Trials the seed runs before it signs the synthetic certificate. 300 trials
#: with zero false accepts bound the rate at 0.0099 (95%).
CALIBRATION_TRIALS = 300
CALIBRATION_SEED = 20260901
CALIBRATION_CONFIDENCE = 0.95

#: The tier-2 store's one table and the read-only SELECT the oracle runs.
MOCKMED_TABLE = "encounters"
MOCKMED_QUERY = f"SELECT id, patient_id, type, status FROM {MOCKMED_TABLE}"

#: What the database holds before any episode runs. The honest patient and
#: the banner-lie patient have no row: a required effect now asserts a change,
#: so a row that was already there earns nothing.
_ROWS: list[dict[str, Any]] = [
    {"id": 2, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
    {"id": 3, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
]

#: What the screen shows before any episode runs.
_SCREEN: list[dict[str, Any]] = [
    {"id": 2, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
]

_REQUIRED_EFFECTS: list[dict[str, Any]] = [
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
    """Write both bundles and both stores. Returns the bundle paths."""

    data_dir = Path(data_dir)
    store = data_dir / "mockmed"
    store.mkdir(parents=True, exist_ok=True)
    database = store / "records.db"
    write_mockmed_database(database, _ROWS)
    _write(store / "screen.json", {"records": _SCREEN})

    tier2 = data_dir / "contracts" / "mockmed"
    write_bundle(
        tier2,
        contract_id=MOCKMED_CONTRACT_ID,
        oracle={
            "kind": "sqlite",
            "path": str(database),
            "query": MOCKMED_QUERY,
        },
        channel="db",
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


def write_mockmed_database(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Create the synthetic tier-2 store as a real SQLite database."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"CREATE TABLE {MOCKMED_TABLE} ("
            "id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL, "
            "type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'saved')"
        )
        connection.executemany(
            f"INSERT INTO {MOCKMED_TABLE} (id, patient_id, type, status) "
            "VALUES (:id, :patient_id, :type, :status)",
            [{"status": "saved", **row} for row in rows],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def write_mockmed_encounter(
    database: Path, patient_id: str, *, type_: str = "Triage"
) -> None:
    """Add one encounter row, standing in for what an episode would write."""

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            f"INSERT INTO {MOCKMED_TABLE} (patient_id, type, status) "
            "VALUES (?, ?, 'saved')",
            (patient_id, type_),
        )
        connection.commit()
    finally:
        connection.close()


def write_mockmed_banner(screen: Path, patient_id: str) -> None:
    """Add one row to the screen dump, standing in for a save banner."""

    body = json.loads(screen.read_text(encoding="utf-8"))
    records = list(body.get("records") or [])
    records.append(
        {
            "id": 100 + len(records),
            "patient_id": patient_id,
            "type": "Triage",
            "status": "saved",
        }
    )
    _write(screen, {"records": records})


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
    identity_keys: list[str] | None = None,
) -> None:
    """Write one synthetic bundle. Tests pass their own effects and keys."""

    directory.mkdir(parents=True, exist_ok=True)
    required = _canonical(
        _REQUIRED_EFFECTS if required_effects is None else required_effects
    )
    forbidden = _canonical(
        _FORBIDDEN_EFFECTS if forbidden_effects is None else forbidden_effects
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
            delta=1.0 - calibration.confidence,
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
