"""The deprecated ``openadapt_flow.verify`` wrapper, over the one verifier API.

A verified receipt or a refusal, never an unverified success.

Each case runs against MockMed's fault server, a real HTTP app with its own
record store, read through ``GET /api/db``. An agent stand-in sends the write
over HTTP, with the server's own faults: a write that lands on another
patient, one that isn't saved, one that is saved twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.guard_demo import FakeAgent, verify_yaml
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify import (
    BaselineUnreadable,
    VerifiedWrite,
    WriteContract,
    WriteRefused,
    begin_write,
    verified_write,
    verify_write,
)
from openadapt_flow.verify_run import receipt_problem

NOTE = "Follow-up in two weeks"
AGENT = {"kind": "agent", "id": "intake-bot"}


@pytest.fixture(autouse=True)
def _seal_home(tmp_path, monkeypatch):
    # Receipts are sealed; keep the test's Seal key out of the home folder.
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _contract(base: str) -> WriteContract:
    return WriteContract.from_dict(verify_yaml(base), name="referral")


def test_a_correct_write_is_verified(sor, tmp_path: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    FakeAgent(base).click_save("p1", NOTE)
    result = verify_write(pending, executor=AGENT)
    assert result.verified and result.verdict == "VERIFIED"
    assert result.outcome.value == "CONFIRMED"
    assert isinstance(result, VerifiedWrite) and not isinstance(result, WriteRefused)
    assert receipt_problem(dict(result.receipt)) is None
    assert len(result.receipt_sha256) == 64
    assert Path(result.receipt_path).exists()


@pytest.mark.parametrize(
    "fault, verdict",
    [
        # Another patient's record moved: the store shows a different write.
        ("wrong_record", "REFUTED"),
        ("no_persist", "RECONCILIATION_REQUIRED"),
    ],
)
def test_a_write_the_store_does_not_show_is_refused(
    sor, tmp_path: Path, fault: str, verdict: str
) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    FakeAgent(base).click_save("p1", NOTE, fault=fault)
    result = verify_write(pending, executor=AGENT)
    assert not result.verified and not result.retry_safe
    assert result.verdict == verdict
    assert "observed absent" in result.reason


def test_a_write_saved_twice_is_refuted(sor, tmp_path: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    agent = FakeAgent(base)
    agent.click_save("p1", NOTE)
    agent.click_save("p1", NOTE)
    result = verify_write(pending, executor=AGENT)
    assert not result.verified and result.verdict == "REFUTED"
    assert isinstance(result, WriteRefused) and not isinstance(result, VerifiedWrite)


def test_a_baseline_is_used_once(sor, tmp_path: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    FakeAgent(base).click_save("p1", NOTE)
    assert verify_write(pending, executor=AGENT).verified
    with pytest.raises(ValueError, match="already checked"):
        verify_write(pending, executor=AGENT)


def test_an_unreadable_store_refuses_before_the_write(tmp_path: Path) -> None:
    contract = _contract("http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(BaselineUnreadable):
        begin_write(contract, {"patient_id": "p1", "note": NOTE}, tmp_path)


def test_the_context_manager_verifies_after_the_block(sor, tmp_path: Path) -> None:
    base, _db = sor
    with verified_write(
        _contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path, executor=AGENT
    ) as write:
        FakeAgent(base).click_save("p1", NOTE)
    assert write.result is not None and write.result.verified


def test_an_unknown_executor_kind_is_refused(sor, tmp_path: Path) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    with pytest.raises(ValueError, match="actor kind"):
        verify_write(pending, executor={"kind": "robot", "id": "x"})


def test_a_verified_receipt_says_whether_it_is_sealed(
    sor, tmp_path: Path, monkeypatch
) -> None:
    base, _db = sor
    pending = begin_write(_contract(base), {"patient_id": "p1", "note": NOTE}, tmp_path)
    FakeAgent(base).click_save("p1", NOTE)
    result = verify_write(pending, executor=AGENT)
    assert result.verified and result.seal == "sealed"
    assert Path(result.seal_path).exists()

    # A key that can't be used: still verified, and the result says unsealed.
    from openadapt_flow import offline_seal

    def broken(*_a, **_k):
        raise PermissionError("key unreadable")

    monkeypatch.setattr(offline_seal, "sign_file", broken)
    pending = begin_write(
        _contract(base), {"patient_id": "p2", "note": NOTE}, tmp_path / "two"
    )
    FakeAgent(base).click_save("p2", NOTE)
    result = verify_write(pending, executor=AGENT)
    assert result.verified and result.seal_path is None
    assert result.seal.startswith("unsealed:")
    assert result.to_dict()["seal"].startswith("unsealed:")


def test_the_module_says_it_is_deprecated() -> None:
    import importlib

    import openadapt_flow.verify

    with pytest.warns(DeprecationWarning, match="openadapt_flow.verifier"):
        importlib.reload(openadapt_flow.verify)
