"""Verified write tools for agent frameworks: one line per tool.

Each adapter takes a write tool an agent already has and returns the same
tool, with the same name and arguments, that goes through the verifier:

1. The operator's contract says what must be true in the system of record
   after this kind of write. The agent never supplies or changes it, and
   values the operator pins can't be overridden by the agent's arguments.
2. Before the write, the system of record is read. If it can't be read, the
   tool doesn't write and says so.
3. The tool writes, the way it always did.
4. The system of record is read again, also when the call is cancelled or
   times out after the write may have been sent. The tool returns a verified
   receipt or a refusal.

``verified: true`` means the system of record shows exactly the write the
call declared. It does not mean the agent picked the right record: values
the agent supplies are its own (``values_declared_by`` says which, and the
receipt binds it), so pin any value that names the record or the tenant.

Within one process, a refused write is held (the same values are refused
until a person checks), and a write whose reply was lost to a cancellation
is not sent again: the retry gets the first result. This is a convenience
against retries, not a guarantee across processes.

The adapters are :mod:`.claude_sdk`, :mod:`.openai_agents` and
:mod:`.langgraph` (each behind its own extra) and :mod:`.crewai`. They share
:class:`WriteGuard`, which works with any framework or none. See
``docs/AGENT_FRAMEWORKS.md``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

from openadapt_flow.agent.verify_tools import contract_params
from openadapt_flow.verify import (
    BaselineUnreadable,
    WriteContract,
    begin_write,
    verify_write,
)

__all__ = ["ContractLike", "WriteGuard", "reply_text"]

ContractLike = Union[WriteContract, str, Path, Mapping[str, Any]]
DEFAULT_WORKDIR = "~/.openadapt/verify"
#: The reads run here, so a cancelled caller can still wait for them.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openadapt-verify")

_DO_NOT_RETRY = (
    "Don't retry this write. A person must check the record; tell them the "
    "verdict and reason."
)


def _contract(value: ContractLike) -> WriteContract:
    if isinstance(value, WriteContract):
        return value
    if isinstance(value, (str, Path)):
        return WriteContract.load(value)
    return WriteContract.from_dict(value)


class WriteGuard:
    """Run one kind of write under an operator's contract.

    ``contract`` is a :class:`~openadapt_flow.verify.WriteContract`, the path
    of its ``verify.yaml``, or the same document as a dict. ``pinned`` holds
    values the operator fixes (a clinic, a tenant); a call that names a
    different value is refused before anything is written. ``agent_id``
    names the agent on the receipt.

    :meth:`run` and :meth:`arun` return a dict the adapters hand back to the
    model: ``verified: true`` with ``receipt_sha256``, or ``verified: false``
    with a ``verdict`` and ``reason``. Writes under one guard run one at a
    time, so each read-back sees only its own write.
    """

    def __init__(
        self,
        contract: ContractLike,
        *,
        agent_id: str = "agent",
        pinned: Optional[Mapping[str, Any]] = None,
        workdir: Union[str, Path] = DEFAULT_WORKDIR,
    ) -> None:
        self.contract = _contract(contract)
        self.params = contract_params(self.contract)
        self.pinned = dict(pinned or {})
        unknown = sorted(set(self.pinned) - set(self.params))
        if unknown:
            raise ValueError(f"pinned values the contract doesn't use: {unknown}")
        self.agent_id = agent_id
        self.workdir = Path(workdir).expanduser()
        self._held: set[str] = set()
        self._lost_replies: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- before the write ------------------------------------------------------

    def values(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The contract's values for a call: the agent's, then the pins.

        Raises ``ValueError`` when the call leaves a value out, or names a
        pinned one differently.
        """

        values: dict[str, Any] = {}
        for name in self.params:
            if name in self.pinned:
                if name in arguments and arguments[name] != self.pinned[name]:
                    raise ValueError(f"{name} is fixed by the operator")
                values[name] = self.pinned[name]
            elif name in arguments:
                values[name] = arguments[name]
            else:
                raise ValueError(f"the call has no value for {name}")
        return values

    # -- the write -------------------------------------------------------------

    def run(self, arguments: Mapping[str, Any], write: Callable[[], Any]) -> dict:
        """Check, call ``write()``, read back. ``arguments`` are the tool call's."""

        with self._lock:
            pending, refusal = self._begin(arguments)
            if refusal is not None:
                return refusal
            try:
                output = write()
            except Exception as exc:  # the write may still have been sent
                output = _failed(exc)
            except BaseException:
                # Interrupted (KeyboardInterrupt, SystemExit): read back first,
                # and keep the result for a retry, then let it through.
                self._lost(pending, self._finish(pending, "interrupted", True))
                raise
            if inspect.isawaitable(output):
                raise TypeError("write() returned an awaitable; use arun")
            return self._finish(pending, output, True)

    async def arun(
        self, arguments: Mapping[str, Any], write: Callable[[], Awaitable[Any]]
    ) -> dict:
        """:meth:`run` for an async ``write``; the reads run off the loop.

        A cancelled call (a framework's tool timeout, ``asyncio.timeout``)
        still reads the write back before the cancellation goes on, and an
        identical retry gets that result instead of a second write.
        """

        # Poll rather than block in a thread: a waiter cancelled here must
        # not take the lock later and never release it.
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            try:
                pending, refusal = await _off_loop(self._begin, arguments)
            except _Cancelled as stop:
                pending, refusal = stop.value
                if refusal is None:
                    # The store was read, nothing was written.
                    pending[0].baseline_path.unlink(missing_ok=True)
                raise asyncio.CancelledError from None
            if refusal is not None:
                return refusal
            try:
                output = await write()
            except asyncio.CancelledError:
                self._lost(pending, self._finish(pending, "cancelled", True))
                raise
            except Exception as exc:  # the write may still have been sent
                output = _failed(exc)
            try:
                return await _off_loop(self._finish, pending, output, True)
            except _Cancelled as stop:
                self._lost(pending, stop.value)
                raise asyncio.CancelledError from None
        finally:
            self._lock.release()

    def _begin(self, arguments: Mapping[str, Any]) -> tuple[Any, Optional[dict]]:
        try:
            values = self.values(arguments)
        except ValueError as exc:
            return None, _not_written(str(exc))
        key = _digest(values)
        if key in self._lost_replies:
            # The same write was made and read back, but its reply never
            # reached the caller (cancelled, timed out). Don't send it again.
            reply = dict(self._lost_replies[key])
            reply["not_sent_again"] = True
            reply["next"] = (
                "This write was already made once, and its result is above; "
                "it was not sent again."
            )
            return None, reply
        if key in self._held:
            return None, _not_written(
                "the same write was refused earlier in this process; it is "
                "held until a person checks the record",
                held=True,
            )
        try:
            pending = begin_write(self.contract, values, self.workdir)
        except BaselineUnreadable as exc:
            return None, _not_written(str(exc))
        return (pending, key), None

    def _finish(self, pending: Any, output: Any, attempted: bool) -> dict:
        pending, key = pending
        declared_by = {
            name: ("operator" if name in self.pinned else "agent")
            for name in self.params
        }
        result = verify_write(
            pending,
            executor={
                "kind": "agent",
                "id": self.agent_id,
                # Enters the receipt as a digest: which values the operator
                # pinned and which the agent's tool call declared.
                "build": "values-declared-by:"
                + json.dumps(declared_by, sort_keys=True),
            },
            write_attempted=attempted,
        )
        reply = result.to_dict()
        reply["values_declared_by"] = declared_by
        reply["tool_output"] = output
        if not result.verified:
            self._held.add(key)
            reply["next"] = _DO_NOT_RETRY
        return reply

    def _lost(self, pending: Any, reply: dict) -> None:
        self._lost_replies[pending[1]] = reply


class _Cancelled(Exception):
    """The caller was cancelled; ``value`` is what the call returned anyway."""

    def __init__(self, value: Any) -> None:
        super().__init__("cancelled")
        self.value = value


async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)`` on the pool; a cancelled caller still gets its result.

    The pool job is shielded, so cancelling the caller never cancels the
    job. On cancellation, a job that hasn't started is taken back and run
    here; one that has is waited for. Either way the result comes back in
    :class:`_Cancelled`, so a read-back is never skipped.
    """

    job = _POOL.submit(fn, *args)
    try:
        return await asyncio.shield(asyncio.wrap_future(job))
    except asyncio.CancelledError:
        if job.cancel():
            raise _Cancelled(fn(*args)) from None
        try:
            raise _Cancelled(job.result()) from None
        except concurrent.futures.CancelledError:
            raise _Cancelled(fn(*args)) from None


def _failed(exc: Exception) -> str:
    return f"the tool raised {type(exc).__name__}"


def _not_written(reason: str, *, held: bool = False) -> dict:
    reply = {
        "verified": False,
        "verdict": "NOT_WRITTEN",
        "written": False,
        "reason": reason,
    }
    if held:
        reply["next"] = _DO_NOT_RETRY
    return reply


def _digest(values: Mapping[str, Any]) -> str:
    body = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha256(body).hexdigest()


def reply_text(reply: Mapping[str, Any]) -> str:
    """The reply as the JSON text a model reads."""

    return json.dumps(reply, default=str)
