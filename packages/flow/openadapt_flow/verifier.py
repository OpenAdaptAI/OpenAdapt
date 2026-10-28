"""Check one write against its contract: the stable API.

    from openadapt_flow.verifier import Contract, capture, verify_write

    contract = Contract.load("verify.yaml")
    before = capture(contract, params)       # read the system of record first
    ...                                      # the agent, skill or program writes
    result = verify_write(contract, before, params=params, actor="openadapt_actor")
    if not result.verified:
        print(result.checked, result.reasons)

MCP skills, the RL reward and the registry all call this module; it wraps
``verify_run`` and checks exactly what verify-run checks:

- **delta:** the declared records were written with the declared values;
- **frame:** nothing else changed, except what the audit trail attributes to
  another user and the contract's reviewed bookkeeping tables;
- **source** (when a :class:`Source` is given): the saved values match the
  document they came from.

Fail-closed: an unreadable read, a missing baseline or an unbound parameter
gives ``verified=False`` with that part ``not_checked``. A failed check never
raises; :class:`VerifierError` is raised only for a contract that can't be
loaded or read at all. No model calls.

The public names are ``Contract``, ``Snapshot``, ``Source``, ``capture``,
``verify_write``, ``WriteResult`` and ``VerifierError``
(``tests/test_verifier_api.py`` pins their signatures).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.verify_run import (
    VerifyConfig,
    VerifyRunError,
    _write_private,
    capture_baseline,
    load_verify_config,
    parse_verify_config,
    verify_run,
)

__all__ = [
    "Contract",
    "Snapshot",
    "Source",
    "VerifierError",
    "WriteResult",
    "capture",
    "verify_write",
]


class VerifierError(ValueError):
    """The contract can't be loaded, or the system of record can't be read
    before the write (there is nothing to verify against)."""


@dataclass(frozen=True)
class Contract:
    """A parsed ``verify.yaml`` (``openadapt.verify/v1``)."""

    config: VerifyConfig

    @classmethod
    def load(cls, path: str | Path) -> "Contract":
        try:
            return cls(load_verify_config(path))
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Contract":
        try:
            return cls(parse_verify_config(dict(doc)))
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc


@dataclass
class Snapshot:
    """The system of record before the write. Private and single-use.

    It holds raw records and the run salt, so it stays on this machine:
    :meth:`save` writes it with mode 0600. One snapshot verifies at most one
    write, as verify-run's baseline does.
    """

    payload: dict[str, Any]
    used: bool = False

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        _write_private(out, self.payload)
        return out

    @classmethod
    def load(cls, path: str | Path) -> "Snapshot":
        return cls(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class Source:
    """Where the written values came from: provenance and the documents."""

    provenance: Any
    documents: Mapping[str, Any]


@dataclass(frozen=True)
class WriteResult:
    #: True only when every part checked held.
    verified: bool
    #: ``verified``, ``reconciliation_required``, ``halted_before_effect``,
    #: or another verify-run outcome: never ``verified`` on a failed check.
    outcome: str
    #: ``{"delta": ..., "frame": ..., "source": ...}``: ``held``,
    #: ``refuted`` or ``not_checked`` (``source`` is ``instruction`` when no
    #: source was given).
    checked: dict[str, str]
    #: Plain sentences, without record values.
    reasons: list[str] = field(default_factory=list)
    #: The effect receipt and its evidence, as verify-run issues them.
    receipt: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


def capture(contract: Contract, params: Mapping[str, object]) -> Snapshot:
    """Read the system of record before the write."""

    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        try:
            payload = capture_baseline(
                contract.config, params, Path(tmp) / "baseline.json"
            )
        except VerifyRunError as exc:
            raise VerifierError(str(exc)) from exc
    return Snapshot(dict(payload))


def _reasons(evidence: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for row in (evidence.get("readback") or {}).get("effects") or []:
        if not isinstance(row, Mapping) or str(row.get("verdict")) == "CONFIRMED":
            continue
        out.append(
            f"{row.get('kind')} was {str(row.get('verdict')).lower()}: observed "
            f"{row.get('observed_effect')}, {row.get('observed_count')} of "
            f"{row.get('expected_count')} expected record(s)"
        )
    frame = evidence.get("frame") or {}
    if frame.get("status") == "refuted":
        kinds = ", ".join(frame.get("collateral_kinds") or [])
        out.append(
            f"{frame.get('collateral_count', 0)} record(s) outside the declared "
            f"write moved ({kinds}); "
            f"{frame.get('unattributed_count', 0)} of them no actor explains"
        )
    elif frame.get("status") == "not_checked":
        out.append("what else changed could not be read, so the frame is not checked")
    source = evidence.get("source") or {}
    if isinstance(source, Mapping) and source.get("status") == "refuted":
        fields = ", ".join(source.get("mismatched_fields") or [])
        out.append(f"the saved record differs from its source in {fields}")
    return out


def verify_write(
    contract: Contract,
    before: Snapshot,
    *,
    params: Mapping[str, object],
    actor: str,
    source: Optional[Source] = None,
    authorization_ref: str = "verify_write",
) -> WriteResult:
    """Read the system of record now and judge the write against ``before``.

    Args:
        contract: What the write must do (and, with a frame, must not do).
        before: :func:`capture` taken before the write. Single use.
        params: The run's inputs, which the contract's values bind.
        actor: Who wrote (the bot's user or session id). Its own changes are
            never excused as another user's.
        source: The written values' provenance and documents, to compare the
            saved record with its source.
        authorization_ref: An opaque reference to what authorized the write;
            only its salted digest enters the receipt.
    """

    if before.used:
        raise VerifierError("this snapshot already verified a write; capture again")
    before.used = True
    with tempfile.TemporaryDirectory(prefix="openadapt-verify-") as tmp:
        path = before.save(Path(tmp) / "baseline.json")
        result = verify_run(
            contract.config,
            params=params,
            run_record={"executor_kind": "agent", "executor_id": actor},
            authorization_ref=authorization_ref,
            baseline_path=path,
            provenance=None if source is None else source.provenance,
            documents=None if source is None else source.documents,
        )
    receipt = dict(result["receipt"])
    evidence = dict(result["evidence"])
    outcome = str(receipt.get("terminal_outcome") or receipt.get("outcome") or "")
    return WriteResult(
        verified=outcome == "verified",
        outcome=outcome,
        checked=dict(receipt.get("checked") or {}),
        reasons=_reasons(evidence),
        receipt=receipt,
        evidence=evidence,
    )
