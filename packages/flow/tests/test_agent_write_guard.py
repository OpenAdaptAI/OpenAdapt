"""WriteGuard: the core every agent-framework adapter shares.

Each case writes through the example portal's real New Order form and reads
its order store back, including the portal's own faults.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
