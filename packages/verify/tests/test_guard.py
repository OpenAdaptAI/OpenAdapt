"""The one-line plain-Python helpers keep the canonical verdicts."""

from __future__ import annotations

import asyncio

import pytest

from openadapt_verify import Outcome, WriteBlocked, checked_write, verified

VALUES = {"code": "item-17", "quantity": "3"}


def test_decorator_confirms_only_what_the_store_shows(store, tmp_path):
    @verified(store.contract(), workdir=tmp_path / "w")
    def save(code, quantity):
        return store.save({"code": code, "quantity": quantity})

    reply = save(**VALUES)
    assert reply["outcome"] == "CONFIRMED" and reply["seal"] == "sealed"
    assert save.guard.contract is not None and store.posts == 1


@pytest.mark.parametrize("mode", ["wrong", "skip", "collateral"])
def test_decorator_refuses_bad_store_and_holds_the_retry(store, tmp_path, mode):
    store.mode = mode

    @verified(store.contract(), workdir=tmp_path / "w")
    def save(code, quantity):
        return store.save({"code": code, "quantity": quantity})

    reply = save(**VALUES)
    assert reply["outcome"] in ("REFUTED", "UNCERTAIN") and not reply["verified"]
    assert save(**VALUES)["verified"] is False and store.posts == 1


def test_decorator_reads_back_after_the_write_raises(store, tmp_path):
    @verified(store.contract(), workdir=tmp_path / "w")
    def save(code, quantity):
        store.save({"code": code, "quantity": quantity})
        raise TimeoutError("lost response")

    assert save(**VALUES)["outcome"] == "CONFIRMED"


def test_async_decorator_and_operator_pins(store, tmp_path):
    @verified(store.contract(), pinned={"code": "item-17"}, workdir=tmp_path / "w")
    async def save(code, quantity):
        return await asyncio.to_thread(store.save, {"code": code, "quantity": quantity})

    assert asyncio.run(save(code="other", quantity="3"))["written"] is False
    assert store.posts == 0
    assert asyncio.run(save(**VALUES))["outcome"] == "CONFIRMED"


def test_context_manager_signs_portable_receipts_for_each_outcome(store, tmp_path):
    with checked_write(store.contract(), VALUES, workdir=tmp_path / "a") as ok:
        store.save(VALUES)
    assert ok.result.outcome is Outcome.CONFIRMED
    assert ok.result.write_receipt == "issued"
    store.mode = "skip"
    other = {"code": "item-18", "quantity": "4"}
    with checked_write(store.contract(), other, workdir=tmp_path / "b") as missing:
        store.save(other)
    assert missing.result.outcome is Outcome.UNCERTAIN
    assert missing.result.write_receipt == "issued"


def test_context_manager_refuses_before_the_block_when_unreadable(store, tmp_path):
    store.unreadable = True
    ran = []
    with pytest.raises(WriteBlocked):
        with checked_write(store.contract("rest"), VALUES, workdir=tmp_path):
            ran.append(True)
    assert ran == [] and store.posts == 0


def test_context_manager_reads_back_then_propagates(store, tmp_path):
    with pytest.raises(RuntimeError):
        with checked_write(store.contract(), VALUES, workdir=tmp_path) as write:
            store.save(VALUES)
            raise RuntimeError("after the write")
    assert write.result.outcome is Outcome.CONFIRMED
