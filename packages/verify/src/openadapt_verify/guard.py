"""One-line checks for plain Python writes.

Both helpers are thin compositions of Flow's canonical verifier; neither adds
a check, a retry or a verdict of its own.

``@verified(contract)`` wraps a write function with Flow's ``WriteGuard``,
the guard the Claude, OpenAI, browser-use and Stagehand adapters use. The
function's arguments are the contract's values. It returns the guard's reply
(``outcome``, ``verified``, ``seal_path``, and the function's own return as
``tool_output``), never raises for a failed write, and refuses to send a write
it can't check::

    @verified("verify.yaml", pinned={"account": "account-17"})
    def save_item(code, quantity, account):
        api.post("/items", json={"code": code, "quantity": quantity})

    reply = save_item(code="item-17", quantity="3", account="account-17")
    reply["outcome"]  # CONFIRMED, REFUTED or UNCERTAIN

``checked_write(contract, values)`` is the canonical ``begin_write`` /
``verify_write`` pair as a context manager, with a portable signed receipt::

    with checked_write("verify.yaml", values) as write:
        api.post("/items", json=values)
    write.result.outcome, write.result.write_receipt_path

The block's exception, if any, propagates after the read-back. Set
``write.write_attempted = False`` only when you know nothing was sent.
"""

from __future__ import annotations

import contextlib
import inspect
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, TypeVar, Union

F = TypeVar("F", bound=Callable[..., Any])


def verified(contract: Any, **options: Any) -> Callable[[F], F]:
    """Decorate a write function; each call is read back from the store.

    ``contract`` is a path to an ``openadapt.verify/v1`` contract, the same
    document as a dict, or a loaded contract. ``options`` go to Flow's
    ``WriteGuard`` (``agent_id``, ``pinned``, ``workdir``). Works on sync and
    async functions. The returned function has a ``guard`` attribute.
    """

    from openadapt_flow.integrations.agents import WriteGuard

    guard = WriteGuard(contract, **options)

    def decorate(write: F) -> F:
        signature = inspect.signature(write)

        def arguments(args: tuple, kwargs: dict) -> dict[str, Any]:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)

        if inspect.iscoroutinefunction(write):

            @wraps(write)
            async def checked_async(*args: Any, **kwargs: Any) -> dict:
                return await guard.arun(
                    arguments(args, kwargs), lambda: write(*args, **kwargs)
                )

            checked_async.guard = guard  # type: ignore[attr-defined]
            return checked_async  # type: ignore[return-value]

        @wraps(write)
        def checked(*args: Any, **kwargs: Any) -> dict:
            return guard.run(arguments(args, kwargs), lambda: write(*args, **kwargs))

        checked.guard = guard  # type: ignore[attr-defined]
        return checked  # type: ignore[return-value]

    return decorate


@contextlib.contextmanager
def checked_write(
    contract: Any,
    values: Mapping[str, Any],
    *,
    agent_id: str = "agent",
    workdir: Optional[Union[str, Path]] = None,
    write_receipt: bool = True,
    **options: Any,
) -> Iterator[Any]:
    """Read the store, run the block's write, read it back.

    Yields Flow's ``WriteBlock``; after the block, ``.result`` is the
    canonical ``WriteResult``. ``options`` go to ``begin_write``. If the
    store can't be read first, ``begin_write`` raises and the block never
    runs, so nothing is sent.
    """

    from openadapt_flow.verifier import (
        Actor,
        Contract,
        WriteBlock,
        begin_write,
        verify_write,
    )

    if isinstance(contract, (str, Path)):
        contract = Contract.load(contract)
    elif isinstance(contract, Mapping):
        contract = Contract.from_dict(contract)
    block = WriteBlock(
        begin_write(
            contract,
            values,
            actor=Actor("agent", agent_id),
            workdir=None if workdir is None else Path(workdir).expanduser(),
            **options,
        )
    )
    try:
        yield block
    finally:
        block.result = verify_write(
            block.pending,
            write_attempted=block.write_attempted,
            write_receipt=write_receipt,
        )
