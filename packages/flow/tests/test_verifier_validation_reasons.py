"""Closed diagnostics preserve refusal and keep raw errors out of public results."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import openadapt_flow.verify_run as producer
from openadapt_flow import verifier
from openadapt_flow.verify_run import VerifyRunError, receipt_sha256, sha256_tagged
from tests.test_verifier_api import BOT, PARAMS, SAVE, _contract, _store, _write

# The public diagnostic contract, independent of the implementation's allowlist.
CODES = (
    "schema_version",
    "unknown_field",
    "missing_field",
    "execute_v1_rules",
    "issued_at",
    "executor_kind",
    "executor_ref_sha256",
    "effect_verdict",
    "verification_tier",
    "remediates",
    "correlation_id",
    "recheck_due_at",
    "checked_how",
    "checked",
    "verified_without_every_part",
    "verified_without_independent_readback",
    "delivery_uncertain",
    "evidence_mismatch",
    "unsalted",
    "baseline_window",
    "self_approved",
    "issued_before_read",
)
CANARY = "synthetic-private-diagnostic-DO-NOT-EMIT"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def checked(tmp_path: Path) -> verifier.WriteResult:
    db = _store(tmp_path)
    pending = verifier.begin_write(
        _contract(db), PARAMS, actor=BOT, ledger=None, workdir=tmp_path / "run"
    )
    _write(db, SAVE)
    return verifier.verify_write(pending, seal=False)


def refused(
    result: verifier.WriteResult, code: str, kind: str = "VerifyRunError"
) -> None:
    assert result.outcome is verifier.Outcome.UNCERTAIN
    assert not result.verified and not result.retry_safe and not result.provisional
    assert result.reasons == [
        "the system of record could not be read back "
        f"({kind}); the write may still land",
        f"verification diagnostic: {code}",
    ]
    assert result.receipt == {} and result.evidence == {}
    assert result.receipt_path is None and result.receipt_sha256 == ""


@pytest.mark.parametrize("code", CODES)
def test_producer_reason_reaches_uncertain_public_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    original = producer.evidence_problem
    calls: list[str] = []

    def reject(evidence: Any, receipt: Any) -> str:
        assert original(evidence, receipt) is None
        calls.append(code)
        return code

    monkeypatch.setattr(producer, "evidence_problem", reject)
    result = checked(tmp_path)
    assert calls == [code]
    refused(result, f"receipt_validation.{code}")


class HostileString(str):
    def __hash__(self) -> int:
        raise AssertionError("diagnostics must not hash caller-defined strings")


class HostileObject:
    def __str__(self) -> str:
        raise AssertionError("diagnostics must not stringify caller objects")

    def __repr__(self) -> str:
        raise AssertionError("diagnostics must not render caller objects")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(CANARY, id="unknown-string"),
        pytest.param([CANARY], id="list"),
        pytest.param({"value": CANARY}, id="mapping"),
        pytest.param(HostileString("issued_before_read"), id="string-subclass"),
        pytest.param(HostileObject(), id="hostile-object"),
        pytest.param(None, id="missing"),
        pytest.param(1, id="integer"),
    ],
)
@pytest.mark.parametrize(
    "forged", [False, True], ids=["constructor", "forged-attribute"]
)
def test_unknown_diagnostics_and_raw_messages_never_reach_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object, forged: bool
) -> None:
    error = VerifyRunError(CANARY, validation_reason=value)
    if forged:
        error.__dict__["_validation_reason"] = value

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(verifier, "verify_run", fail)
    result = checked(tmp_path)
    refused(result, "unclassified")
    assert CANARY not in repr(result) + json.dumps(result.to_dict())


def test_unknown_producer_code_is_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(producer, "evidence_problem", lambda *args: CANARY)
    result = checked(tmp_path)
    refused(result, "unclassified")
    assert CANARY not in repr(result) + json.dumps(result.to_dict())


def test_exception_subclass_cannot_supply_a_diagnostic_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForeignError(VerifyRunError):
        @property
        def validation_reason(self) -> str:
            raise AssertionError("a foreign diagnostic getter must not run")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ForeignError(CANARY)

    monkeypatch.setattr(verifier, "verify_run", fail)
    result = checked(tmp_path)
    refused(result, "unclassified", "ForeignError")
    assert CANARY not in repr(result) + json.dumps(result.to_dict())


def test_other_exception_keeps_its_original_generic_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(CANARY)

    monkeypatch.setattr(verifier, "verify_run", fail)
    result = checked(tmp_path)
    assert result.outcome is verifier.Outcome.UNCERTAIN and not result.retry_safe
    assert result.reasons == [
        "the system of record could not be read back (ValueError); the write may still land"
    ]
    assert CANARY not in repr(result) + json.dumps(result.to_dict())


@pytest.mark.parametrize("args", [(), ("legacy",), ("legacy", 2)])
def test_existing_exception_arguments_remain_compatible(
    args: tuple[object, ...],
) -> None:
    error = VerifyRunError(*args)
    assert error.args == args and str(error) == str(ValueError(*args))
    assert error.validation_reason == "unclassified"


def test_success_receipt_and_evidence_bytes_are_unchanged_by_result_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = verifier.verify_run
    produced: dict[str, bytes] = {}

    def observe(*args: Any, **kwargs: Any) -> Any:
        document = original(*args, **kwargs)
        for name in ("receipt", "evidence"):
            produced[name] = producer._canonical(document[name])
        return document

    monkeypatch.setattr(verifier, "verify_run", observe)
    result = checked(tmp_path)
    assert result.verified and result.reasons == []
    assert producer._canonical(result.receipt) == produced["receipt"]
    assert producer._canonical(result.evidence) == produced["evidence"]
    assert result.receipt_sha256 == receipt_sha256(result.receipt)
    assert result.receipt["evidence_digest"] == sha256_tagged(result.evidence)
    assert "validation_reason" not in json.dumps(result.to_dict())


def test_existing_fixed_time_receipt_guard_still_refuses(tmp_path: Path) -> None:
    result = checked(tmp_path)
    receipt, evidence = copy.deepcopy(result.receipt), copy.deepcopy(result.evidence)
    evidence["readback"]["baseline_read_at"] = "2030-01-01T00:00:00Z"
    evidence["readback"]["read_at"] = "2030-01-01T00:00:02Z"
    receipt["issued_at"] = "2030-01-01T00:00:01Z"
    receipt["evidence_digest"] = sha256_tagged(evidence)
    assert producer.receipt_problem(receipt) is None
    assert producer.evidence_problem(evidence, receipt) == "issued_before_read"
