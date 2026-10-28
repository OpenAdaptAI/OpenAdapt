"""Verified write tools for agent frameworks: one line per tool.

Each adapter takes a write tool an agent already has and returns the same
tool, with the same name and arguments, that goes through the verifier:

1. The operator's contract says what must be true in the system of record
   after this kind of write. The agent never supplies or changes it, and
   values the operator pins can't be overridden by the agent's arguments.
2. Before the write, the system of record is read. If it can't be read, the
   tool doesn't write and says so.
3. The tool writes, the way it always did.
4. The system of record is read again. The tool returns a verified receipt or
   a refusal. A refused write is held: the same call is not made again until
   a person has checked the record.

The adapters are :mod:`.claude_sdk`, :mod:`.openai_agents`, :mod:`.langgraph`
and :mod:`.crewai`, each behind its own extra. They share :class:`WriteGuard`,
which works with any framework or none. See ``docs/AGENT_FRAMEWORKS.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
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
            output, attempted = _call(write)
            return self._finish(pending, output, attempted)

    async def arun(
        self, arguments: Mapping[str, Any], write: Callable[[], Awaitable[Any]]
    ) -> dict:
        """:meth:`run` for an async ``write``; the reads run off the loop."""

        await asyncio.to_thread(self._lock.acquire)
        try:
            pending, refusal = await asyncio.to_thread(self._begin, arguments)
            if refusal is not None:
                return refusal
            try:
                output, attempted = await write(), True
            except Exception as exc:  # the write may still have been sent
                output, attempted = _failed(exc), True
            return await asyncio.to_thread(self._finish, pending, output, attempted)
        finally:
            self._lock.release()

    def _begin(self, arguments: Mapping[str, Any]) -> tuple[Any, Optional[dict]]:
        try:
            values = self.values(arguments)
        except ValueError as exc:
            return None, _not_written(str(exc))
        key = _digest(values)
        if key in self._held:
            return None, _not_written(
                "this write was refused before and is held until a person "
                "checks the record",
                held=True,
            )
        try:
            pending = begin_write(self.contract, values, self.workdir)
        except BaselineUnreadable as exc:
            return None, _not_written(str(exc))
        return (pending, key), None

    def _finish(self, pending: Any, output: Any, attempted: bool) -> dict:
        pending, key = pending
        result = verify_write(
            pending,
            executor={"kind": "agent", "id": self.agent_id},
            write_attempted=attempted,
        )
        reply = result.to_dict()
        reply["tool_output"] = output
        if not result.verified:
            self._held.add(key)
            reply["next"] = _DO_NOT_RETRY
        return reply


def _call(write: Callable[[], Any]) -> tuple[Any, bool]:
    try:
        output = write()
    except Exception as exc:  # the write may still have been sent
        return _failed(exc), True
    if inspect.isawaitable(output):
        raise TypeError("write() returned an awaitable; use arun")
    return output, True


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
