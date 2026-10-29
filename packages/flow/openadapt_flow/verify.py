"""Verify a write against the system of record: a verified receipt or a refusal.

Any executor can use this around its own write: a compiled program, an agent
(Claude Agent SDK, OpenAI Agents, LangGraph...), a script or a person. The
contract says what to read and what must be true there afterwards; the
executor never decides that::

    from openadapt_flow.verify import WriteContract, begin_write, verify_write

    contract = WriteContract.load("verify.yaml")
    pending = begin_write(contract, {"fname": "Ana", "lname": "Ruiz"}, workdir)
    ...  # the executor makes the write
    result = verify_write(pending, executor={"kind": "agent", "id": "intake-bot"})
    if result.verified:
        print(result.receipt_sha256)
    else:
        print(result.verdict, result.reason)  # a person checks; never retry

:func:`begin_write` reads the system of record before the write, so a new
record can be told from one that was already there. :func:`verify_write` reads
it again and returns a :class:`VerifiedWrite` only when every declared effect
is confirmed by that read; anything else is a :class:`WriteRefused`. There is
no third outcome and no unverified success. An unreadable store is
``RECONCILIATION_REQUIRED``: the write may still have landed, so retrying it
could save it twice.

This module is a thin public face over :mod:`openadapt_flow.verify_run`
(``capture_baseline`` and ``verify_run``); the contract is its ``verify.yaml``
(:class:`~openadapt_flow.verify_run.VerifyConfig`). No model is called.
"""

from __future__ import annotations

import json
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional, Sequence, Union

from openadapt_flow.verify_run import (
    VerifyConfig,
    capture_baseline,
    parse_verify_config,
    receipt_sha256,
    verify_run,
    write_result,
)

__all__ = [
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

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
ExecutorKind = Literal["program", "agent", "person", "script"]
#: This module's executor kinds, as verify_run names them.
_KINDS = {
    "program": "flow_replay",
    "agent": "agent",
    "person": "human",
    "script": "script",
}


class WriteContract:
    """What to read, and what must be true there after the write.

    The ``openadapt.verify/v1`` contract (``verify.yaml``): a ``verifier``
    that reads the system of record (REST, FHIR, SQL...) and the ``effects``
    the write must produce there, with ``{param}`` placeholders the write's
    values fill in.
    """

    def __init__(
        self,
        config: VerifyConfig,
        *,
        name: str = "",
        writes: Sequence[str] = (),
    ) -> None:
        self.config = config
        self.name = name
        #: The requests a taught write under this contract may send, as
        #: ``"METHOD /path"`` (a sign-in, the save). Any other write a taught
        #: program attempts is stopped before it is sent.
        self.writes = tuple(_parse_write(w) for w in writes)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "WriteContract":
        import yaml

        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, Mapping):
            raise ValueError(f"{path.name}: a contract is a YAML mapping")
        contract = cls.from_dict(data, name=path.stem)
        contract.config._base_dir = path.resolve().parent
        return contract

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, name: str = "") -> "WriteContract":
        data = dict(data)
        writes = data.pop("writes", None) or ()
        if isinstance(writes, str) or not isinstance(writes, (list, tuple)):
            raise ValueError('writes is a list of "METHOD /path" strings')
        return cls(
            parse_verify_config(data, source=name or "contract"),
            name=name,
            writes=[str(w) for w in writes],
        )


def _parse_write(text: str) -> str:
    method, _, path = text.strip().partition(" ")
    method = method.upper()
    path = path.strip()
    if method not in ("POST", "PUT", "PATCH", "DELETE") or not path.startswith("/"):
        raise ValueError(
            f'writes entry {text!r}: use "METHOD /path" (POST, PUT, PATCH or DELETE)'
        )
    return f"{method} {path}"


@dataclass(frozen=True)
class PendingWrite:
    """A write that may now happen: the store was read before it.

    Single use: :func:`verify_write` consumes the baseline.
    """

    contract: WriteContract = field(repr=False)
    params: Mapping[str, Any] = field(repr=False)
    write_id: str
    workdir: Path
    authorization_ref: str
    #: An operator's authorization reference was given (not defaulted).
    authorized: bool = False

    @property
    def baseline_path(self) -> Path:
        return self.workdir / "baseline.json"


@dataclass(frozen=True)
class VerifiedWrite:
    """The system of record shows exactly the declared write.

    It proves the store holds what was declared, not that the write was
    authorized: ``declared_by`` says who declared the values, and
    ``authorization`` is ``unbound`` unless the caller passed an operator's
    authorization reference. A Seal (``seal_path``), signed with this
    computer's Seal key, lets someone else check the receipt was issued here;
    ``seal`` says ``sealed``, ``off`` (Seals turned off), or ``unsealed: ...``
    with why the key couldn't be used.
    """

    write_id: str
    receipt: Mapping[str, Any] = field(repr=False)
    receipt_sha256: str
    receipt_path: Optional[str] = None
    declared_by: str = "agent"
    authorization: str = "unbound"
    seal_path: Optional[str] = None
    seal: str = "off"
    verified: Literal[True] = True
    verdict: Literal["VERIFIED"] = "VERIFIED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": True,
            "verdict": self.verdict,
            "write_id": self.write_id,
            "receipt_sha256": self.receipt_sha256,
            "receipt_path": self.receipt_path,
            "values_declared_by": self.declared_by,
            "authorization": self.authorization,
            "seal_path": self.seal_path,
            "seal": self.seal,
        }


@dataclass(frozen=True)
class WriteRefused:
    """Not verified. ``REFUTED``: the store shows a different write (a wrong
    value, a duplicate). ``RECONCILIATION_REQUIRED``: the store doesn't show
    the write or couldn't be read, so it may still land. Either way a person
    checks the record; the write is never retried."""

    write_id: str
    verdict: Literal["REFUTED", "RECONCILIATION_REQUIRED"]
    reason: str
    receipt: Optional[Mapping[str, Any]] = field(default=None, repr=False)
    receipt_sha256: Optional[str] = None
    receipt_path: Optional[str] = None
    verified: Literal[False] = False
    retry_safe: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": False,
            "verdict": self.verdict,
            "write_id": self.write_id,
            "reason": self.reason,
            "retry_safe": False,
            "receipt_sha256": self.receipt_sha256,
            "receipt_path": self.receipt_path,
        }


WriteResult = Union[VerifiedWrite, WriteRefused]


class BaselineUnreadable(RuntimeError):
    """The store could not be read before the write, so the write could never
    be verified. Nothing may be written."""


def begin_write(
    contract: WriteContract,
    params: Mapping[str, Any],
    workdir: Union[str, Path],
    *,
    authorization_ref: str = "",
) -> PendingWrite:
    """Read the system of record before the write.

    ``params`` are the values the write will save (and any that name the
    record); they fill the contract's placeholders. ``workdir`` gets a new
    private folder for this write. Raises :class:`BaselineUnreadable` when
    the store can't be read: then the write must not happen.
    """

    write_id = f"write-{secrets.token_hex(12)}"
    folder = Path(workdir).expanduser() / write_id
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    if authorization_ref and not _OPAQUE.match(authorization_ref):
        raise ValueError(
            "authorization_ref must be an opaque id (8-128 of A-Za-z0-9._:-)"
        )
    pending = PendingWrite(
        contract=contract,
        params=dict(params),
        write_id=write_id,
        workdir=folder,
        authorization_ref=authorization_ref or write_id,
        authorized=bool(authorization_ref),
    )
    try:
        baseline = capture_baseline(
            contract.config, pending.params, pending.baseline_path
        )
        reachable = bool(baseline["state"].get("reachable"))
    except Exception as exc:  # a bad contract, or a store that can't be reached
        raise BaselineUnreadable(
            f"the system of record could not be read before the write ({type(exc).__name__})"
        ) from None
    if not reachable:
        raise BaselineUnreadable(
            "the system of record could not be read before the write"
        )
    return pending


def verify_write(
    pending: PendingWrite,
    *,
    executor: Optional[Mapping[str, str]] = None,
    write_attempted: bool = True,
    declared_by: str = "agent",
    seal: bool = True,
) -> WriteResult:
    """Read the system of record again and rule on the write. Never retries it.

    ``executor`` names who wrote: ``kind`` (program, agent, person, script),
    ``id`` and optionally ``build``; only digests of them enter the receipt.
    """

    executor = dict(executor or {"kind": "agent", "id": "unidentified"})
    kind = _KINDS.get(executor.get("kind", "agent"))
    if kind is None:
        raise ValueError("executor kind is program, agent, person or script")
    run_record: dict[str, Any] = {
        "executor_kind": kind,
        "executor_id": executor.get("id", "unidentified"),
    }
    if executor.get("build"):
        run_record["executor_build"] = {"build": str(executor["build"])}
    doc: Optional[dict[str, Any]] = None
    path: Optional[str] = None
    reason = ""
    try:
        doc = verify_run(
            pending.contract.config,
            params=pending.params,
            run_record=run_record,
            authorization_ref=pending.authorization_ref,
            baseline_path=pending.baseline_path,
            private_dir=pending.workdir,
            correlation_id=pending.write_id,
            write_attempted=write_attempted,
        )
        path = str(write_result(doc, pending.workdir))
    except Exception as exc:  # unreadable store, timeout, a used baseline
        reason = f"the system of record could not be read back ({type(exc).__name__})"
    if doc is None:
        return WriteRefused(
            write_id=pending.write_id,
            verdict="RECONCILIATION_REQUIRED",
            reason=reason,
        )
    receipt = doc["receipt"]
    digest = receipt_sha256(receipt)
    if receipt["outcome"] == "verified":
        seal_path, seal_state = (
            _seal(path, pending.write_id) if seal and path else (None, "off")
        )
        return VerifiedWrite(
            write_id=pending.write_id,
            receipt=receipt,
            receipt_sha256=digest,
            receipt_path=path,
            declared_by=declared_by,
            authorization="operator" if pending.authorized else "unbound",
            seal_path=seal_path,
            seal=seal_state,
        )
    rows = doc["evidence"]["readback"]["effects"]
    refuted = receipt.get("effect_verdict") == "REFUTED" and any(
        row.get("observed_effect") in ("present", "conflicting") for row in rows
    )
    return WriteRefused(
        write_id=pending.write_id,
        verdict="REFUTED" if refuted else "RECONCILIATION_REQUIRED",
        reason=_why(rows, refuted),
        receipt=receipt,
        receipt_sha256=digest,
        receipt_path=path,
    )


def _seal(receipt_path: str, write_id: str) -> tuple[Optional[str], str]:
    """Sign the receipt file with this computer's Seal key. Returns the Seal's
    path and ``sealed``; or no path and ``off`` (Seals turned off) or
    ``unsealed: <why>`` (the key couldn't be used). The write stays verified
    either way: the Seal attests who issued the receipt, not the write."""

    from openadapt_flow import offline_seal

    if not offline_seal.mode_enabled():
        return None, "off"
    try:
        sealed = offline_seal.sign_file(
            Path(receipt_path), subject_kind="record", run_id=write_id
        )
        out = Path(receipt_path).with_name("receipt.seal.json")
        out.write_text(json.dumps(sealed.model_dump(mode="json"), indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001 - say why, never fail the write
        return (
            None,
            f"unsealed: this computer's Seal key couldn't be used ({type(exc).__name__})",
        )
    return str(out), "sealed"


def _why(rows: Any, refuted: bool) -> str:
    observed = sorted({str(row.get("observed_effect")) for row in rows or ()})
    if refuted:
        return (
            "the system of record shows a different write than declared "
            f"(observed: {', '.join(observed)})"
        )
    return (
        "the system of record does not show the declared write "
        f"(observed: {', '.join(observed) or 'nothing readable'})"
    )


@contextmanager
def verified_write(
    contract: WriteContract,
    params: Mapping[str, Any],
    workdir: Union[str, Path],
    *,
    executor: Optional[Mapping[str, str]] = None,
    authorization_ref: str = "",
) -> Iterator["_Holder"]:
    """``with verified_write(...) as w: do_the_write()`` then read ``w.result``.

    If the block raises before the write is dispatched, set
    ``w.write_attempted = False`` first; the read-back still runs, because a
    half-sent write may have landed.
    """

    holder = _Holder(
        begin_write(contract, params, workdir, authorization_ref=authorization_ref)
    )
    try:
        yield holder
    finally:
        holder.result = verify_write(
            holder.pending, executor=executor, write_attempted=holder.write_attempted
        )


@dataclass
class _Holder:
    pending: PendingWrite
    write_attempted: bool = True
    result: Optional[WriteResult] = None


def result_json(result: WriteResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
