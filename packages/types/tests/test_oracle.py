"""A visual-only oracle cannot mint production VERIFIED."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Mapping

import pytest
from pydantic import ValidationError

from openadapt_types import (
    EffectStrengthV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteTerminalOutcomeV1,
    OracleAdapter,
    OracleChannel,
    OracleObservation,
    OracleTier,
    ProductionSealRefused,
    issue_production_verified,
    oracle_tier_from_effect_strength,
    production_seal_allowed,
    refuse_production_verified,
    tier_of,
)


class BannerOracle:
    """Tier-0: the acting screen said saved."""

    channel = OracleChannel.OCR

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        return OracleObservation(
            channel=self.channel,
            identity={"record_id": identity["record_id"]},
            value={"banner": "Saved"},
        )


class FileStatusOracle:
    """Tier-2: a JSON file is the system of record."""

    channel = OracleChannel.FILE

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        records = json.loads(self._path.read_text(encoding="utf-8"))
        record_id = identity["record_id"]
        return OracleObservation(
            channel=self.channel,
            identity={"record_id": record_id},
            value={"status": records[record_id]["status"]},
        )


class SecondSessionOracle:
    channel = OracleChannel.SECOND_SESSION

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        return OracleObservation(
            channel=self.channel,
            identity={"record_id": identity["record_id"]},
            value={"status": "posted"},
        )


def _contract(**updates: object) -> ExecuteEvidenceContractV1:
    fields: dict[str, object] = {
        "authorization_passed": True,
        "identity_passed": True,
        "postcondition_passed": True,
        "effect_passed": True,
        "minimum_effect_strength": EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION,
        "observed_effect_strength": EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION,
        "model_used": False,
        "external_network_used": False,
    }
    fields.update(updates)
    return ExecuteEvidenceContractV1.model_validate(fields)


def _receipt(**updates: object) -> ExecuteEvidenceReceiptV1:
    fields: dict[str, object] = {
        "receipt_id": "receipt_12345678",
        "execution_id": "execution_12345678",
        "workflow_digest": "sha256:" + "a" * 64,
        "workflow_version": "workflow_20260729",
        "qualification_id": "qualification_12345678",
        "environment_id": "environment_12345678",
        "runner_id": "runner:hosted",
        "nonce": "nonce:execution_12345678",
        "oracle_tier": 2,
        "outcome": ExecuteTerminalOutcomeV1.VERIFIED,
        "contracts": _contract(
            minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
            observed_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
        ),
        "delivery_uncertain": False,
        "evidence_digest": "sha256:" + "b" * 64,
        "issued_at": "2026-07-29T12:00:00Z",
    }
    fields.update(updates)
    return ExecuteEvidenceReceiptV1.model_validate(fields)


def test_channel_sets_the_tier_not_the_payload() -> None:
    visual = OracleObservation(
        channel=OracleChannel.VISUAL,
        identity={"record_id": "demo-1"},
        value={"status": "posted", "so_r_looking": True},
    )
    assert visual.tier is OracleTier.VISUAL
    assert tier_of(OracleChannel.FILE) is OracleTier.SYSTEM_OF_RECORD
    assert tier_of(OracleChannel.SECOND_SESSION) is OracleTier.INDEPENDENT_SESSION
    assert not production_seal_allowed(visual.tier)


def test_visual_only_oracle_cannot_mint_production_verified() -> None:
    obs = BannerOracle().read({"record_id": "demo-1"})
    assert isinstance(BannerOracle(), OracleAdapter)
    assert obs.tier is OracleTier.VISUAL
    with pytest.raises(ProductionSealRefused, match="oracle tier 2 or 3"):
        issue_production_verified(obs)
    with pytest.raises(ProductionSealRefused, match="oracle tier 2 or 3"):
        refuse_production_verified(OracleTier.VISUAL)

    with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
        _receipt(
            contracts=_contract(
                minimum_effect_strength=EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION,
                observed_effect_strength=EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION,
            ),
            oracle_tier=0,
        )


def test_ocr_and_persisted_screen_readback_are_tier_zero() -> None:
    assert oracle_tier_from_effect_strength(
        EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION
    ) is OracleTier.VISUAL
    assert oracle_tier_from_effect_strength(
        EffectStrengthV1.PERSISTED_STATE_REACQUISITION
    ) is OracleTier.VISUAL
    with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
        _receipt(
            contracts=_contract(
                minimum_effect_strength=EffectStrengthV1.PERSISTED_STATE_REACQUISITION,
                observed_effect_strength=EffectStrengthV1.PERSISTED_STATE_REACQUISITION,
            ),
            oracle_tier=0,
        )


def test_second_session_adapter_is_valid_and_cannot_seal() -> None:
    obs = SecondSessionOracle().read({"record_id": "demo-1"})
    assert obs.tier is OracleTier.INDEPENDENT_SESSION
    with pytest.raises(ProductionSealRefused, match="oracle tier 2 or 3"):
        issue_production_verified(obs)
    with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
        _receipt(
            contracts=_contract(
                minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SESSION,
                observed_effect_strength=EffectStrengthV1.INDEPENDENT_SESSION,
            ),
            oracle_tier=1,
        )


def test_file_oracle_can_issue_production_verified(tmp_path: Path) -> None:
    store = tmp_path / "claims.json"
    store.write_text(
        json.dumps({"demo-1": {"status": "posted"}}),
        encoding="utf-8",
    )
    oracle = FileStatusOracle(store)
    obs = oracle.read({"record_id": "demo-1"})
    assert isinstance(oracle, OracleAdapter)
    assert obs.tier is OracleTier.SYSTEM_OF_RECORD
    assert obs.value == {"status": "posted"}
    assert issue_production_verified(obs) is OracleTier.SYSTEM_OF_RECORD
    receipt = _receipt()
    assert receipt.outcome is ExecuteTerminalOutcomeV1.VERIFIED


def test_visual_observation_may_reconcile_but_not_verify() -> None:
    receipt = _receipt(
        outcome=ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED,
        delivery_uncertain=True,
        contracts=_contract(effect_passed=False),
        oracle_tier=0,
    )
    assert receipt.outcome is ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED


def test_shipped_file_oracle_example_is_tier_two(tmp_path: Path) -> None:
    store = tmp_path / "claims.json"
    store.write_text(
        json.dumps({"demo-1": {"status": "posted"}}),
        encoding="utf-8",
    )
    path = Path(__file__).resolve().parents[1] / "examples" / "oracle" / "file_oracle.py"
    spec = importlib.util.spec_from_file_location("file_oracle_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obs = module.FileStatusOracle(store).read({"record_id": "demo-1"})
    assert issue_production_verified(obs) is OracleTier.SYSTEM_OF_RECORD
