"""What the reward worker refuses to take on trust from its counterparty.

Every test here drives a path that used to succeed. The reward reads a store
and signs what it read, and each of these was a way to make the signature say
something the read did not support: a tier the bundle only asserted, a subject
the trainer picked after the rollout, a reward for an episode that changed
nothing, a certificate kept current by counting backwards, and a bound
measured on records the contract never mentions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("openadapt_types.reward")

from openadapt_types.oracle import OracleChannel  # noqa: E402
from openadapt_types.reward import RewardOutcomeV1  # noqa: E402

from openadapt_flow.reward.calibration import (  # noqa: E402
    FAULT_CLASSES,
    CalibrationRefused,
    corpus_digest_for,
    corpus_from_effects,
)
from openadapt_flow.reward.models import (  # noqa: E402
    CERTIFICATE_FILE,
    CONTRACT_FILE,
    BundleError,
    OracleRecipeV1,
    RewardBundle,
)
from openadapt_flow.reward.oracles import (  # noqa: E402
    JsonDocumentOracle,
    OracleMechanismError,
    assert_sqlite_database,
    build_oracle,
)
from openadapt_flow.reward.seed import (  # noqa: E402
    MOCKMED_HONEST_PATIENT,
    MOCKMED_LIE_PATIENT,
    MOCKMED_QUERY,
    calibrate_bundle,
    mockmed_episode,
    seed_mockmed,
    write_bundle,
    write_mockmed_encounter,
)
from openadapt_flow.reward.worker import RewardWorker, RewardWorkerError  # noqa: E402
from openadapt_flow.runtime.effects.effect import Effect  # noqa: E402

_TRIAGE = {
    "kind": "record_written",
    "match": {"patient_id": {"param": "patient_id"}, "type": "Triage"},
    "expected_count": 1,
    "count_new_only": True,
}


@pytest.fixture()
def seeded(tmp_path: Path) -> dict[str, Any]:
    from openadapt_flow.execute.keys import fingerprint_of, load_or_create_private_key

    data_dir = tmp_path / "reward-ref"
    key = load_or_create_private_key(data_dir)
    issuer = "self_signed:" + fingerprint_of(key.public_key())
    paths = seed_mockmed(data_dir, key, issuer)
    return {
        "data_dir": data_dir,
        "key": key,
        "issuer": issuer,
        "database": data_dir / "mockmed" / "records.db",
        "screen": data_dir / "mockmed" / "screen.json",
        **paths,
    }


def _worker(seeded: dict[str, Any], which: str = "tier2") -> RewardWorker:
    return RewardWorker(seeded[which], seeded["data_dir"], token="test-token")


def _bundle(
    seeded: dict[str, Any],
    name: str,
    *,
    kind: str,
    channel: str,
    path: Path,
    certify: bool = False,
    required_effects: list[dict[str, Any]] | None = None,
    forbidden_effects: list[dict[str, Any]] | None = None,
) -> Path:
    directory = seeded["data_dir"] / "contracts" / name
    oracle: dict[str, Any] = {"kind": kind, "path": str(path)}
    if kind == "sqlite":
        oracle["query"] = MOCKMED_QUERY
    else:
        oracle["records_key"] = "records"
    write_bundle(
        directory,
        contract_id=f"reward_contract_{name.replace('-', '_')}_0000",
        oracle=oracle,
        channel=channel,
        key=seeded["key"],
        issuer_key_id=seeded["issuer"],
        certify=certify,
        required_effects=required_effects or [dict(_TRIAGE)],
        forbidden_effects=forbidden_effects,
    )
    return directory


# -- 1. the tier is not a string the bundle author writes ----------------------


def test_the_same_document_cannot_buy_two_tiers(seeded: dict[str, Any]) -> None:
    """One file, both JSON recipe kinds, one tier.

    Before this, ``screen_dump`` on a document gave tier 0 and
    ``development_only``, and ``json_file`` on the identical bytes gave tier 2
    and ``certified``.
    """

    document = seeded["data_dir"] / "one-document.json"
    document.write_text(json.dumps({"records": []}), encoding="utf-8")
    tiers = set()
    for kind in ("screen_dump", "json_file"):
        recipe = OracleRecipeV1.model_validate(
            {"kind": kind, "path": str(document), "records_key": "records"}
        )
        adapter = build_oracle(recipe, document.parent)
        assert isinstance(adapter, JsonDocumentOracle)
        assert adapter.channel is OracleChannel.OCR
        tiers.add(int(recipe.tier))
    assert tiers == {0}


def test_json_file_may_not_declare_a_record_channel(seeded: dict[str, Any]) -> None:
    document = seeded["data_dir"] / "claimed-as-file.json"
    document.write_text(json.dumps({"records": []}), encoding="utf-8")
    directory = _bundle(
        seeded, "json-as-file", kind="json_file", channel="file", path=document
    )
    with pytest.raises(BundleError, match="does not match the contract channel"):
        RewardBundle.load(directory)


def test_a_json_document_renamed_as_a_database_is_refused(
    seeded: dict[str, Any],
) -> None:
    """The db channel rests on opening a real database, not on the file name."""

    fake = seeded["data_dir"] / "screen.db"
    fake.write_text(json.dumps({"records": []}), encoding="utf-8")
    with pytest.raises(OracleMechanismError, match="not a SQLite database"):
        assert_sqlite_database(fake)
    directory = _bundle(seeded, "fake-db", kind="sqlite", channel="db", path=fake)
    with pytest.raises(OracleMechanismError):
        RewardWorker(directory, seeded["data_dir"], token="test-token")
    # The real store opens.
    assert assert_sqlite_database(seeded["database"]) == seeded["database"]


def test_the_adapter_decides_the_channel(
    seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recipe table that drifts from its adapter stops the worker.

    The table in ``models`` and the adapter in ``oracles`` are two places that
    have to agree about a tier. Make them disagree and the build refuses,
    rather than shipping the table's answer.
    """

    from openadapt_flow.reward import models

    document = seeded["data_dir"] / "drift.json"
    document.write_text(json.dumps({"records": []}), encoding="utf-8")
    monkeypatch.setitem(models._RECIPE_CHANNEL, "screen_dump", OracleChannel.FILE)
    recipe = OracleRecipeV1.model_validate(
        {"kind": "screen_dump", "path": str(document), "records_key": "records"}
    )
    assert recipe.channel is OracleChannel.FILE
    with pytest.raises(OracleMechanismError, match="reads through"):
        build_oracle(recipe, document.parent)


# -- 2. the graded subject is settled before the rollout -----------------------


def test_a_descriptor_may_not_replace_the_registered_subject(
    seeded: dict[str, Any],
) -> None:
    """Register the subject the rollout ran on; score naming another one.

    Before this the descriptor won, and the receipt carried the trainer's
    subject with outcome ``verified``, ``certified``, scalar 1.0.
    """

    worker = _worker(seeded)
    worker.begin_episode("episode_swap_01", {"patient_id": MOCKMED_LIE_PATIENT})
    write_mockmed_encounter(seeded["database"], MOCKMED_HONEST_PATIENT)
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(
            mockmed_episode(
                MOCKMED_HONEST_PATIENT,
                episode_id="episode_swap_01",
                contract_digest=worker.contract.digest,
            )
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.error == "identity_conflict"
    assert MOCKMED_LIE_PATIENT in excinfo.value.detail
    assert MOCKMED_HONEST_PATIENT in excinfo.value.detail


def test_a_descriptor_may_repeat_the_registered_subject(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    worker.begin_episode("episode_agree_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    write_mockmed_encounter(seeded["database"], MOCKMED_HONEST_PATIENT)
    envelope = worker.score_episode(
        mockmed_episode(
            MOCKMED_HONEST_PATIENT,
            episode_id="episode_agree_01",
            contract_digest=worker.contract.digest,
        )
    )
    assert envelope["receipt"]["reward_outcome"] == "verified"


def test_an_episode_may_not_be_re_registered_under_another_subject(
    seeded: dict[str, Any],
) -> None:
    worker = _worker(seeded)
    worker.begin_episode("episode_rereg_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    # The same subject again is fine; it only re-reads the baseline.
    worker.begin_episode("episode_rereg_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.begin_episode("episode_rereg_01", {"patient_id": MOCKMED_LIE_PATIENT})
    assert excinfo.value.status_code == 409
    assert excinfo.value.error == "identity_conflict"


def test_a_scored_episode_may_not_be_re_registered(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    worker.begin_episode("episode_after_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    write_mockmed_encounter(seeded["database"], MOCKMED_HONEST_PATIENT)
    worker.score_episode(
        mockmed_episode(
            MOCKMED_HONEST_PATIENT,
            episode_id="episode_after_01",
            contract_digest=worker.contract.digest,
        )
    )
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.begin_episode("episode_after_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    assert excinfo.value.error == "duplicate_episode"


# -- 3. a rollout that changed nothing earns nothing ---------------------------


def test_required_effects_must_claim_a_change(seeded: dict[str, Any]) -> None:
    """A state-only required effect will not load.

    Before this the seeded contract's required effect was a plain
    ``record_written``, a statement about the store's current contents, so
    naming a subject whose row already existed scored 1.0 with no episode
    having run.
    """

    directory = _bundle(
        seeded,
        "state-only",
        kind="sqlite",
        channel="db",
        path=seeded["database"],
        required_effects=[
            {
                "kind": "record_written",
                "match": {"patient_id": {"param": "patient_id"}, "type": "Triage"},
                "expected_count": 1,
            }
        ],
    )
    with pytest.raises(BundleError, match="claim about change"):
        RewardBundle.load(directory)


def test_an_exact_new_set_also_claims_a_change(seeded: dict[str, Any]) -> None:
    """The other baseline-dependent kind satisfies the same rule."""

    directory = _bundle(
        seeded,
        "exact-new-set",
        kind="sqlite",
        channel="db",
        path=seeded["database"],
        required_effects=[
            {
                "kind": "exact_new_set",
                "match": {"patient_id": {"param": "patient_id"}},
                "new_records": [{"type": "Triage"}],
                "expected_count": 1,
            }
        ],
    )
    assert RewardBundle.load(directory).required_effects[0].requires_baseline


def test_an_episode_that_wrote_nothing_scores_zero(seeded: dict[str, Any]) -> None:
    worker = _worker(seeded)
    # The subject already has a row, and this episode adds none.
    write_mockmed_encounter(seeded["database"], MOCKMED_HONEST_PATIENT)
    worker.begin_episode("episode_idle_01", {"patient_id": MOCKMED_HONEST_PATIENT})
    envelope = worker.score_episode(
        mockmed_episode(
            MOCKMED_HONEST_PATIENT,
            episode_id="episode_idle_01",
            contract_digest=worker.contract.digest,
        )
    )
    receipt = envelope["receipt"]
    assert receipt["reward_outcome"] == RewardOutcomeV1.WRONG_EFFECT.value
    assert receipt["scalar_reward"] == 0.0


# -- 4. the policy update counter only moves forward ---------------------------


def _score_at(
    worker: RewardWorker, seeded: dict[str, Any], index: int, update: int
) -> dict[str, Any]:
    episode_id = f"episode_update_{index:02d}"
    patient = f"patient-run-{index:04d}"
    worker.begin_episode(episode_id, {"patient_id": patient})
    write_mockmed_encounter(seeded["database"], patient)
    return worker.score_episode(
        mockmed_episode(
            patient,
            episode_id=episode_id,
            contract_digest=worker.contract.digest,
            policy_update=update,
        )
    )["receipt"]


def test_the_policy_update_may_not_go_backwards(seeded: dict[str, Any]) -> None:
    """0, then 999, then 10^9, then 0 again.

    Before this the last one came back ``current`` and certified: expiry
    counted a number the counterparty reported and nothing compared it with
    anything seen before.
    """

    worker = _worker(seeded)
    assert _score_at(worker, seeded, 0, 0)["certificate_state"] == "current"
    assert _score_at(worker, seeded, 1, 999)["certificate_state"] == "current"
    expired = _score_at(worker, seeded, 2, 10**9)
    assert expired["certificate_state"] == "expired"
    assert expired["certified"] is False
    with pytest.raises(RewardWorkerError) as excinfo:
        _score_at(worker, seeded, 3, 0)
    assert excinfo.value.status_code == 422
    assert excinfo.value.error == "policy_update_regressed"


def test_the_ledger_survives_a_restart(seeded: dict[str, Any]) -> None:
    """It is persisted beside the episode index, not held in memory."""

    _score_at(_worker(seeded), seeded, 4, 500)
    with pytest.raises(RewardWorkerError, match="only moves forward"):
        _score_at(_worker(seeded), seeded, 5, 499)
    assert _score_at(_worker(seeded), seeded, 6, 500) is not None


def test_renaming_the_checkpoint_does_not_reset_expiry(
    seeded: dict[str, Any],
) -> None:
    """The mark is per contract, so a fresh checkpoint id cannot rewind it."""

    worker = _worker(seeded)
    _score_at(worker, seeded, 7, 900)
    worker.begin_episode("episode_new_ckpt", {"patient_id": "patient-run-9999"})
    write_mockmed_encounter(seeded["database"], "patient-run-9999")
    payload = mockmed_episode(
        "patient-run-9999",
        episode_id="episode_new_ckpt",
        contract_digest=worker.contract.digest,
        policy_update=0,
    )
    payload["policy_checkpoint_id"] = "policy_checkpoint_renamed_0"
    with pytest.raises(RewardWorkerError) as excinfo:
        worker.score_episode(payload)
    assert excinfo.value.error == "policy_update_regressed"


# -- 5. the corpus comes from the contract -------------------------------------


def test_the_corpus_plants_the_records_the_contract_names() -> None:
    """Before this the corpus always emitted ``type: Triage``."""

    corpus = corpus_from_effects(
        [
            Effect.model_validate(
                {
                    "kind": "record_written",
                    "match": {
                        "patient_id": {"param": "patient_id"},
                        "type": "Radiology",
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            )
        ],
        [],
        ["patient_id"],
    )
    planted = corpus.records({"patient_id": "p-1"}, first_id=1)
    assert planted == [{"id": 1, "patient_id": "p-1", "type": "Radiology"}]


def test_a_read_back_effect_does_not_double_the_record() -> None:
    """A field_equals on the same selector describes one row, not two."""

    corpus = corpus_from_effects(
        [
            Effect.model_validate(
                {
                    "kind": "record_written",
                    "match": {"patient_id": {"param": "patient_id"}},
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ),
            Effect.model_validate(
                {
                    "kind": "field_equals",
                    "match": {"patient_id": {"param": "patient_id"}},
                    "field": "status",
                    "value": "saved",
                }
            ),
        ],
        [],
        ["patient_id"],
    )
    assert corpus.records({"patient_id": "p-1"}, first_id=1) == [
        {"id": 1, "patient_id": "p-1", "status": "saved"}
    ]


def test_the_certificate_names_the_contract_s_own_corpus(
    seeded: dict[str, Any],
) -> None:
    bundle = RewardBundle.load(seeded["tier2"])
    assert bundle.certificate is not None
    want = corpus_digest_for(
        bundle.required_effects, bundle.forbidden_effects, bundle.identity_keys
    )
    assert bundle.certificate.calibration_corpus_digest == want
    assert bundle.contract.certificate_policy.calibration_corpus_digest == want


def test_a_certificate_for_another_corpus_is_refused(seeded: dict[str, Any]) -> None:
    """A bound measured on other records does not apply to these effects."""

    directory = seeded["tier2"]
    certificate = json.loads((directory / CERTIFICATE_FILE).read_text())
    certificate["calibration_corpus_digest"] = "sha256:" + "1" * 64
    (directory / CERTIFICATE_FILE).write_text(json.dumps(certificate))
    with pytest.raises(BundleError, match="calibration_corpus_digest"):
        RewardBundle.load(directory)


def test_a_corpus_that_cannot_verify_refuses_the_bound(
    seeded: dict[str, Any],
) -> None:
    """Contradictory required effects mean the trials measure nothing.

    Every trial refutes for a reason the planted fault did not cause, the
    false-accept count is zero, and the bound would be the best number the
    method can produce while bounding nothing.
    """

    directory = _bundle(
        seeded,
        "unexercisable",
        kind="sqlite",
        channel="db",
        path=seeded["database"],
        required_effects=[
            dict(_TRIAGE),
            dict(_TRIAGE, expected_count=2),
        ],
    )
    with pytest.raises(CalibrationRefused, match="does not exercise the contract"):
        calibrate_bundle(directory)


def test_the_fault_classes_include_the_wrong_subject(seeded: dict[str, Any]) -> None:
    """``WRONG_EFFECT`` covers a write that landed on somebody else.

    The bound did not sample that mode before, so it did not cover it. The
    class was added rather than the docstring narrowed: the mode is real, the
    judge already catches it, and a bound that never planted it was quiet
    about the failure the reward exists to price.
    """

    assert "wrong_subject" in FAULT_CLASSES
    calibration = json.loads((seeded["tier2"] / "calibration.json").read_text())
    assert "wrong_subject" in calibration["calibration_fault_classes"]
    assert calibration["calibration_false_accepts"] == 0


def test_the_calibration_records_which_corpus_it_ran_on(
    seeded: dict[str, Any],
) -> None:
    calibration = json.loads((seeded["tier2"] / "calibration.json").read_text())
    contract = json.loads((seeded["tier2"] / CONTRACT_FILE).read_text())
    assert (
        calibration["calibration_corpus_digest"]
        == contract["certificate_policy"]["calibration_corpus_digest"]
    )
