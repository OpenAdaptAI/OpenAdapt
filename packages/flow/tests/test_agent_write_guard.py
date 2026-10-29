"""WriteGuard: the core every agent-framework adapter shares.

Each case writes through the example portal's real New Order form and reads
its order store back, including the portal's own faults.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from openadapt_flow.integrations.agents import WriteGuard
from tests._orders_portal import Portal, portal  # noqa: F401 - fixture

ORDER = {"customer": "Acme", "product": "Desk lamp", "units": "3"}


def _guard(portal: Portal, tmp_path: Path, **options) -> WriteGuard:
    return WriteGuard(portal.contract(), workdir=tmp_path, agent_id="bot-1", **options)


def _write(portal: Portal, args: dict):
    return lambda: portal.save_order(**args)


def test_a_right_write_returns_a_receipt(portal: Portal, tmp_path: Path) -> None:
    reply = _guard(portal, tmp_path).run(ORDER, _write(portal, ORDER))
    assert reply["verified"] is True and reply["verdict"] == "VERIFIED"
    assert len(reply["receipt_sha256"]) == 64
    assert reply["tool_output"].startswith("saved: /orders/WO-")


def test_an_order_the_store_does_not_keep_is_refused_and_held(
    portal: Portal, tmp_path: Path
) -> None:
    guard = _guard(portal, tmp_path)
    portal.fault("no_persist")
    reply = guard.run(ORDER, _write(portal, ORDER))
    assert reply["verified"] is False
    assert reply["verdict"] == "RECONCILIATION_REQUIRED"
    assert "Don't retry" in reply["next"]
    portal.fault("")
    again = guard.run(ORDER, _write(portal, ORDER))
    assert again["verdict"] == "NOT_WRITTEN" and "held" in again["reason"]
    assert portal.posts == 1


def test_an_order_saved_twice_is_refuted(portal: Portal, tmp_path: Path) -> None:
    portal.fault("twice")
    reply = _guard(portal, tmp_path).run(ORDER, _write(portal, ORDER))
    assert reply["verified"] is False and reply["verdict"] == "REFUTED"


def test_a_pinned_value_cannot_be_overridden(portal: Portal, tmp_path: Path) -> None:
    guard = _guard(portal, tmp_path, pinned={"customer": "Acme"})
    other = {**ORDER, "customer": "Globex"}
    reply = guard.run(other, _write(portal, other))
    assert (
        reply["verdict"] == "NOT_WRITTEN" and "fixed by the operator" in reply["reason"]
    )
    assert portal.posts == 0


def test_the_pinned_value_is_what_gets_checked(portal: Portal, tmp_path: Path) -> None:
    guard = _guard(portal, tmp_path, pinned={"customer": "Acme"})
    args = {"product": "Desk lamp", "units": "3"}
    # The tool writes another customer than the pinned one: refused.
    reply = guard.run(args, _write(portal, {**args, "customer": "Globex"}))
    assert reply["verified"] is False


def test_a_call_without_a_contract_value_is_not_written(
    portal: Portal, tmp_path: Path
) -> None:
    args = {"customer": "Acme", "product": "Desk lamp"}
    reply = _guard(portal, tmp_path).run(
        args, lambda: portal.save_order(units="3", **args)
    )
    assert reply["verdict"] == "NOT_WRITTEN" and "units" in reply["reason"]
    assert portal.posts == 0


def test_an_unreadable_store_stops_the_write(portal: Portal, tmp_path: Path) -> None:
    contract = portal.contract()
    contract["verifier"]["base_url"] = "http://127.0.0.1:9"  # nothing listens
    guard = WriteGuard(contract, workdir=tmp_path)
    reply = guard.run(ORDER, _write(portal, ORDER))
    assert reply["verdict"] == "NOT_WRITTEN" and reply["written"] is False
    assert portal.posts == 0


def test_a_tool_that_raises_after_saving_is_still_read_back(
    portal: Portal, tmp_path: Path
) -> None:
    def write():
        portal.save_order(**ORDER)
        raise TimeoutError("the reply was lost")

    reply = _guard(portal, tmp_path).run(ORDER, write)
    assert reply["verified"] is True
    assert reply["tool_output"] == "the tool raised TimeoutError"


def test_async_writes_are_checked_too(portal: Portal, tmp_path: Path) -> None:
    async def write():
        return await asyncio.to_thread(portal.save_order, **ORDER)

    reply = asyncio.run(_guard(portal, tmp_path).arun(ORDER, write))
    assert reply["verified"] is True


def test_a_cancelled_write_is_read_back_and_not_sent_again(
    portal: Portal, tmp_path: Path
) -> None:
    guard = _guard(portal, tmp_path)

    async def slow_write():
        out = await asyncio.to_thread(portal.save_order, **ORDER)
        await asyncio.sleep(5)  # the reply is slow after the POST landed
        return out

    async def timed_out():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await guard.arun(ORDER, slow_write)

    asyncio.run(timed_out())
    retry = guard.run(ORDER, _write(portal, ORDER))
    assert retry["verified"] is True and retry["not_sent_again"] is True
    assert portal.posts == 1 and len(portal.orders()) == 1


def test_an_interrupted_sync_write_is_read_back(portal: Portal, tmp_path: Path) -> None:
    guard = _guard(portal, tmp_path)

    def write():
        portal.save_order(**ORDER)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        guard.run(ORDER, write)
    retry = guard.run(ORDER, _write(portal, ORDER))
    assert retry["not_sent_again"] is True and portal.posts == 1


def test_a_waiter_cancelled_on_the_lock_does_not_keep_it(
    portal: Portal, tmp_path: Path
) -> None:
    guard = _guard(portal, tmp_path)
    guard._lock.acquire()  # another write in progress

    async def cancelled_waiter():
        task = asyncio.ensure_future(guard.arun(ORDER, lambda: asyncio.sleep(0)))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled_waiter())
    guard._lock.release()
    assert guard._lock.acquire(timeout=1)
    guard._lock.release()
    assert guard.run(ORDER, _write(portal, ORDER))["verified"] is True


def test_the_reply_and_receipt_say_who_declared_each_value(
    portal: Portal, tmp_path: Path
) -> None:
    pinned = _guard(portal, tmp_path, pinned={"customer": "Acme"})
    reply = pinned.run(ORDER, _write(portal, ORDER))
    assert reply["values_declared_by"] == {
        "customer": "operator",
        "product": "agent",
        "units": "agent",
    }
    plain = _guard(portal, tmp_path / "plain")
    other = {**ORDER, "product": "Office chair"}
    second = plain.run(other, _write(portal, other))
    assert set(second["values_declared_by"].values()) == {"agent"}
    # The split is bound into the evidence the receipt covers, as the
    # digest of the executor build.
    first = json.loads(Path(reply["receipt_path"]).read_text())
    other_doc = json.loads(Path(second["receipt_path"]).read_text())
    first_build = first["evidence"]["executor"]["build_sha256"]
    assert (
        first_build and first_build != other_doc["evidence"]["executor"]["build_sha256"]
    )


def _saturate_pool() -> threading.Event:
    """Fill the guard's read-back pool so the next job waits in its queue."""

    from openadapt_flow.integrations.agents import _POOL

    release = threading.Event()
    for _ in range(_POOL._max_workers):
        _POOL.submit(release.wait, 30)
    return release


def test_a_read_back_queued_behind_a_full_pool_survives_a_timeout(
    portal: Portal, tmp_path: Path
) -> None:
    guard = _guard(portal, tmp_path)
    blockers: list[threading.Event] = []

    async def write():
        out = await asyncio.to_thread(portal.save_order, **ORDER)
        blockers.append(_saturate_pool())  # the read-back will queue
        return out

    async def timed_out():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await guard.arun(ORDER, write)

    try:
        asyncio.run(timed_out())
    finally:
        for release in blockers:
            release.set()
    retry = guard.run(ORDER, _write(portal, ORDER))
    assert retry["verified"] is True and retry["not_sent_again"] is True
    assert portal.posts == 1 and len(portal.orders()) == 1


def test_a_first_read_queued_behind_a_full_pool_writes_nothing(
    portal: Portal, tmp_path: Path
) -> None:
    guard = _guard(portal, tmp_path)
    release = _saturate_pool()

    async def timed_out():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await guard.arun(
                    ORDER, lambda: asyncio.to_thread(portal.save_order, **ORDER)
                )

    try:
        asyncio.run(timed_out())
    finally:
        release.set()
    assert portal.posts == 0
    assert guard.run(ORDER, _write(portal, ORDER))["verified"] is True
