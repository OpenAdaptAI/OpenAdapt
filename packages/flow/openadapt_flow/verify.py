"""Deprecated: use :mod:`openadapt_flow.verifier`.

This module keeps the signatures #1589 introduced, over the one verifier
API, so callers keep working while they move:

- ``WriteContract`` is :class:`~openadapt_flow.verifier.Contract`;
- ``begin_write(contract, params, workdir)`` names the executor later, in
  ``verify_write(pending, executor={...})``;
- ``VerifiedWrite`` and ``WriteRefused`` are both
  :class:`~openadapt_flow.verifier.WriteResult`: test ``result.verified``,
  not ``isinstance``. ``to_dict()`` keeps the old ``verdict`` key next to
  the new ``outcome``;
- ``BaselineUnreadable`` is :class:`~openadapt_flow.verifier.WriteBlocked`,
  so an ``except BaselineUnreadable`` also refuses a write the ledger holds.

Writes go through the uncertain-write ledger, as with the new API.
"""

from __future__ import annotations

import json
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    PendingWrite,
    WriteBlock,
    WriteResult,
)
from openadapt_flow.verifier import WriteBlocked as BaselineUnreadable
from openadapt_flow.verifier import begin_write as _begin
from openadapt_flow.verifier import verify_write as _verify

warnings.warn(
    "openadapt_flow.verify is deprecated; use openadapt_flow.verifier",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "AlreadyWritten",
    "BaselineUnreadable",
    "PendingWrite",
    "VerifiedWrite",
    "WriteContract",
    "WriteRefused",
    "WriteResult",
    "begin_write",
    "verified_write",
    "verify_write",
]

WriteContract = Contract
VerifiedWrite = WriteResult
WriteRefused = WriteResult
_UNNAMED = Actor("agent", "unidentified")


def begin_write(
    contract: Contract,
    params: Mapping[str, Any],
    workdir: Union[str, Path],
    *,
    authorization_ref: str = "",
) -> PendingWrite:
    return _begin(
        contract,
        params,
        actor=_UNNAMED,
        workdir=workdir,
        authorization_ref=authorization_ref,
    )


def verify_write(
    pending: PendingWrite,
    *,
    executor: Optional[Mapping[str, str]] = None,
    write_attempted: bool = True,
    declared_by: str = "agent",
    seal: bool = True,
) -> WriteResult:
    executor = dict(executor or {"kind": "agent", "id": "unidentified"})
    build = executor.get("build")
    pending.actor = Actor(
        executor.get("kind", "agent"),
        executor.get("id", "unidentified"),
        {"build": str(build)} if build else None,
    )
    pending.declared_by = declared_by
    return _verify(pending, write_attempted=write_attempted, seal=seal)


@contextmanager
def verified_write(
    contract: Contract,
    params: Mapping[str, Any],
    workdir: Union[str, Path],
    *,
    executor: Optional[Mapping[str, str]] = None,
    authorization_ref: str = "",
) -> Iterator[WriteBlock]:
    block = WriteBlock(
        begin_write(contract, params, workdir, authorization_ref=authorization_ref)
    )
    try:
        yield block
    finally:
        block.result = verify_write(
            block.pending, executor=executor, write_attempted=block.write_attempted
        )


def result_json(result: WriteResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
