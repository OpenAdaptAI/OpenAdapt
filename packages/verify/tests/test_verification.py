from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from openadapt_verify import (
    Actor,
    Contract,
    Outcome,
    PortableReceiptVerification,
    ReceiptError,
    WriteGuard,
    begin_write,
    verify_local_seal,
    verify_receipt,
    verify_write,
)

VALUES = {"code": "item-17", "quantity": "3"}


def check_local(reply):
    from openadapt_flow.offline_seal import public_key_entry
    from openadapt_types.seal import parse_key_registry

    assert reply["seal"] == "sealed"
    receipt = Path(reply["receipt_path"]).read_bytes()
    seal = json.loads(Path(reply["seal_path"]).read_text())
    assert seal["run_id"] == reply["write_id"]
    pinned = parse_key_registry([public_key_entry()])
    checked = verify_local_seal(seal, hashlib.sha256(receipt).hexdigest(), pinned)
    assert checked.ok
    assert not verify_local_seal(
        seal, hashlib.sha256(receipt + b"tampered").hexdigest(), pinned
    ).ok
    assert not verify_local_seal(seal, hashlib.sha256(receipt).hexdigest(), []).ok


@pytest.mark.parametrize("kind", ["sql", "rest"])
def test_independent_store_and_signed_local_receipt(store, tmp_path, kind):
    reply = WriteGuard(store.contract(kind), workdir=tmp_path / "writes").run(
        VALUES, lambda: store.save(VALUES)
    )
    assert reply["outcome"] == "CONFIRMED" and reply["verified"] is True
    assert store.posts == 1 and len(store.records()) == 1
    assert reply["scope"]["strength"] == "independent_system_of_record"
    check_local(reply)


@pytest.mark.parametrize("mode", ["wrong", "duplicate", "collateral", "skip"])
def test_writer_success_text_cannot_confirm_bad_store(store, tmp_path, mode):
    store.mode = mode
    guard = WriteGuard(store.contract(), workdir=tmp_path / "writes")
    result = guard.run(VALUES, lambda: store.save(VALUES))
    assert result["verified"] is False
    assert result["outcome"] in ("REFUTED", "UNCERTAIN")
    assert result["seal"] != "sealed"
    again = guard.run(VALUES, lambda: store.save(VALUES))
    assert again["verified"] is False and store.posts == 1


def test_baseline_and_operator_pins_refuse_before_dispatch(store, tmp_path):
    guard = WriteGuard(
        store.contract(), pinned={"code": "other"}, workdir=tmp_path / "pins"
    )
    assert guard.run(VALUES, lambda: store.save(VALUES))["written"] is False
    store.unreadable = True
    guard = WriteGuard(store.contract("rest"), workdir=tmp_path / "unreadable")
    assert guard.run(VALUES, lambda: store.save(VALUES))["written"] is False
    assert store.posts == 0


def test_cancellation_reads_back_and_prevents_second_write(store, tmp_path):
    guard = WriteGuard(store.contract(), workdir=tmp_path / "writes")

    async def write():
        await asyncio.to_thread(store.save, VALUES)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard.arun(VALUES, write))
    result = guard.run(VALUES, lambda: store.save(VALUES))
    assert result["verified"] is True and result["not_sent_again"] is True
    assert store.posts == 1
    check_local(result)


def test_error_after_commit_uses_store_not_tool_exception(store, tmp_path):
    def write():
        store.save(VALUES)
        raise TimeoutError("scripted lost response")

    result = WriteGuard(store.contract(), workdir=tmp_path / "writes").run(
        VALUES, write
    )
    assert result["verified"] is True and store.posts == 1
    check_local(result)


def test_portable_receipt_pins_and_tampering_are_separate_from_outcome(store, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from openadapt_flow.receipt_log import LocalLog
    from openadapt_flow.write_receipt import default_signer

    pending = begin_write(
        Contract.from_dict(store.contract()),
        VALUES,
        actor=Actor("agent", "scripted-agent"),
        workdir=tmp_path / "writes",
    )
    store.save(VALUES)
    result = verify_write(pending, write_receipt=True)
    assert result.outcome is Outcome.CONFIRMED and result.write_receipt == "issued"
    data = Path(result.write_receipt_path).read_bytes()
    evidence = Path(result.write_receipt_path + ".evidence.json").read_bytes()
    issuer, log = default_signer().public_raw(), LocalLog().public_raw()
    checked = verify_receipt(
        data, issuer_public=issuer, log_public=log, evidence=evidence
    )
    assert isinstance(checked, PortableReceiptVerification)
    assert checked.payload.verdict == "CONFIRMED" and checked.evidence_opened
    wrong = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    for receipt, key, opened in [
        (data, wrong, evidence),
        (data[:-1] + bytes([data[-1] ^ 1]), issuer, evidence),
        (data, issuer, evidence.replace(b'"schema"', b'"tampered_schema"', 1)),
    ]:
        with pytest.raises(ReceiptError):
            verify_receipt(receipt, issuer_public=key, log_public=log, evidence=opened)


def test_signer_failure_does_not_become_signature_success(store, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "OPENADAPT_SEAL_KEY", "file:" + str(tmp_path / "nonexistent-test-key")
    )
    pending = begin_write(
        Contract.from_dict(store.contract()),
        VALUES,
        actor=Actor("agent", "scripted-agent"),
        workdir=tmp_path / "writes",
    )
    store.save(VALUES)
    result = verify_write(pending, write_receipt=True)
    assert result.outcome is Outcome.CONFIRMED
    assert (
        result.write_receipt.startswith("not issued:")
        and result.write_receipt_path is None
    )
    assert result.seal.startswith("unsealed:")


@pytest.mark.parametrize("complete", [None, False], ids=["undeclared", "false"])
def test_rest_completeness_is_required_before_writer_dispatch(
    store, tmp_path, complete
):
    contract = store.contract("rest")
    if complete is None:
        del contract["verifier"]["records_complete"]
    else:
        contract["verifier"]["records_complete"] = complete
    guard = WriteGuard(contract, workdir=tmp_path / "writes")
    reply = guard.run(VALUES, lambda: store.save(VALUES))
    assert reply["verified"] is False and reply["written"] is False
    assert store.posts == 0 and store.records() == []
