"""How the commit guard reads a save back: the :class:`ReadbackProvider` seam.

After the agent's save goes through, the commit guard asks a provider what
the system of record now holds. A provider returns a :class:`ReadbackResult`:
its ``tier`` (how independent the read was), the saved value of each task
field it could read (``None``: not read), its verdict and its evidence.

:class:`VerifyRunReadback`, the default, is today's path unchanged:
:meth:`Guard.confirm <openadapt_flow.guard.Guard.confirm>` runs
``verify-run`` and its receipt decides. It also reads the record once more
through the same ``verify.yaml`` reader, so the ledger can show which fields
differ from what the task intended.

:func:`rule` makes the final verdict, and no provider can get past it:

* only an ``independent_readback`` may be ``VERIFIED``; a ``screen_readback``
  (or anything weaker) that says verified is ``RECONCILIATION_REQUIRED``;
* a field the read shows with a value other than the task's is a refutation,
  and nothing (a provider, a model) turns that back into ``VERIFIED``.

Another provider (an app's own API, a second session) is named in
``guard.yaml`` as ``readback_provider: <kind>`` and registered through
:func:`openadapt_flow.runtime.effects.register_verifier_factory` under the
kind ``readback:<kind>``. The factory is called with the ``readback_provider``
mapping and the task's parameters and must return a provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from openadapt_flow.guard import (
    Guard,
    GuardResult,
    GuardTask,
    GuardVerdict,
    _write_json,
)

__all__ = [
    "KIND_PREFIX",
    "ReadbackProvider",
    "ReadbackResult",
    "VerifyRunReadback",
    "confirm",
    "differing_fields",
    "load_provider",
    "rule",
    "same_value",
]

Tier = Literal["independent_readback", "screen_readback", "unchecked"]
TIERS = ("independent_readback", "screen_readback", "unchecked")
#: The registry kind a readback provider factory is registered under.
KIND_PREFIX = "readback:"


@dataclass(frozen=True)
class ReadbackResult:
    """What one read-back of one save found."""

    tier: str
    #: Task field name -> the value the read shows, or None when not read.
    fields: Mapping[str, Optional[str]]
    #: Ids, codes and paths only (a receipt path, a reason). No values.
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: The provider's own verdict, before :func:`rule`.
    verdict: GuardVerdict = GuardVerdict.RECONCILIATION_REQUIRED
    #: Set by :class:`VerifyRunReadback`: the guard's own result.
    guard_result: Optional[GuardResult] = None


class ReadbackProvider(Protocol):
    """Reads one committed save back from the system of record."""

    def read(self, task: GuardTask, guard: Guard) -> ReadbackResult: ...


def same_value(intended: Any, saved: str) -> bool:
    """Whether two values are the same by the provenance value comparison."""

    from openadapt_flow.runtime.effects.readback_values import infer_value_kind

    a = "" if intended is None else str(intended)
    try:
        from openadapt_flow.provenance import same_value as _provenance_same

        return _provenance_same(a, saved, infer_value_kind(a))
    except Exception:  # noqa: BLE001 - an unusual value: compare exactly
        return a.strip() == saved.strip()


def differing_fields(task: GuardTask, saved: Mapping[str, Optional[str]]) -> list[str]:
    """Task fields the read shows with another value. Unread ones don't count."""

    return sorted(
        name
        for name, value in saved.items()
        if name in task.intended_fields
        and value is not None
        and not same_value(task.intended_fields[name], value)
    )


def rule(task: GuardTask, result: ReadbackResult) -> GuardVerdict:
    """The final verdict. Fail closed; see the module docstring."""

    if differing_fields(task, result.fields):
        return GuardVerdict.REFUTED
    verdict = result.verdict
    if verdict is GuardVerdict.VERIFIED and result.tier != "independent_readback":
        return GuardVerdict.RECONCILIATION_REQUIRED
    return verdict


def _saved_fields(
    task: GuardTask,
    verify: Any,
    before: Optional[Sequence[Mapping[str, Any]]],
) -> dict[str, Optional[str]]:
    """The saved value of each task field, read again from the record reader.

    The saved record is the one record of the task's record that is new or
    changed since ``before``. Anything else (no read, no change, two
    candidates) leaves every field unread.
    """

    from openadapt_flow.agent_guard.approval_fields import (
        bound_fields,
        matching_records,
        read_records,
    )

    unread: dict[str, Optional[str]] = {n: None for n in task.intended_fields}
    bound = bound_fields(verify)
    after = matching_records(
        read_records(verify, task.params), task.expected_record, bound
    )
    prior = matching_records(before, task.expected_record, bound)
    if after is None or prior is None:
        return unread
    import json

    def key(row: Mapping[str, Any]) -> str:
        return json.dumps(row, sort_keys=True, default=str)

    left = [key(r) for r in prior]
    changed = []
    for row in after:
        k = key(row)
        if k in left:
            left.remove(k)
        else:
            changed.append(row)
    if len(changed) != 1:
        return unread
    row = changed[0]
    return {
        name: (
            None
            if bound.get(name) not in row or row.get(bound[name]) is None
            else str(row.get(bound[name]))
        )
        for name in task.intended_fields
    }


class VerifyRunReadback:
    """The default provider: ``Guard.confirm`` (verify-run), unchanged."""

    def __init__(self) -> None:
        self._before: dict[str, Optional[list[dict]]] = {}

    def before_send(self, task: GuardTask, guard: Guard) -> None:
        """Read the task's record just before the save goes on."""

        from openadapt_flow.agent_guard.approval_fields import read_records

        self._before[guard.guard_id] = read_records(guard.owner.verify, task.params)

    def read(self, task: GuardTask, guard: Guard) -> ReadbackResult:
        result = guard.confirm()
        receipt = (result.receipt or {}).get("receipt") or {}
        tier = str(receipt.get("verification_tier") or "unchecked")
        saved = (
            _saved_fields(task, guard.owner.verify, self._before.pop(guard.guard_id))
            if guard.guard_id in self._before
            else {n: None for n in task.intended_fields}
        )
        return ReadbackResult(
            tier=tier if tier in TIERS else "unchecked",
            fields=saved,
            evidence={
                "receipt_path": result.receipt_path,
                "outcome": result.outcome,
                "reason": result.reason,
            },
            verdict=result.verdict,
            guard_result=result,
        )


#: ``GuardResult.outcome`` (and ``guard.json``'s ``outcome``) when the guard's
#: verdict overrides the verify-run receipt's.
OVERRIDDEN = "overridden"
#: Written next to a receipt the guard overrode: ``<receipt>.override.json``.
OVERRIDE_SCHEMA = "openadapt.guard-receipt-override/v1"


def _mark_overridden(
    receipt: Path, receipt_outcome: str, verdict: GuardVerdict, reason: Optional[str]
) -> None:
    import hashlib

    try:
        digest = "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()
    except OSError:
        digest = None
    _write_json(
        receipt.with_name(receipt.stem + ".override.json"),
        {
            "schema": OVERRIDE_SCHEMA,
            "receipt": receipt.name,
            "receipt_sha256": digest,
            "receipt_outcome": receipt_outcome,
            "guard_verdict": verdict.value,
            "reason": reason,
        },
    )


def settle(guard: Guard, result: ReadbackResult, verdict: GuardVerdict) -> GuardResult:
    """Record the final verdict on the guard and its task ledger.

    The default provider already confirmed the guard; it is rewritten only
    when :func:`rule` made the verdict stricter.
    """

    prior = result.guard_result
    if prior is not None and prior.verdict is verdict:
        return prior
    evidence = dict(result.evidence)
    reason: Optional[str]
    if differing_fields(guard.task, result.fields):
        reason = "readback:fields_differ"
    elif prior is not None or verdict is not result.verdict:
        reason = f"readback:tier_{result.tier}"
    else:
        reason = evidence.get("reason")
    receipt_path = (
        prior.receipt_path if prior is not None else evidence.get("receipt_path")
    )
    outcome = prior.outcome if prior is not None else evidence.get("outcome")
    overridden = prior is not None or verdict is not result.verdict
    if overridden and receipt_path and outcome is not None:
        # The receipt on disk keeps the outcome verify-run signed; the guard's
        # stricter verdict is written beside it, and the guard's own record
        # says the receipt was overridden, so no file reads as verified alone.
        _mark_overridden(Path(receipt_path), outcome, verdict, reason)
        outcome = OVERRIDDEN
    final = GuardResult(
        verdict=verdict,
        guard_id=guard.guard_id,
        task_id=guard.task.task_id,
        outcome=outcome,
        receipt_path=receipt_path,
        receipt=prior.receipt if prior is not None else None,
        reason=reason,
    )
    guard.result = final
    _write_json(
        guard.owner._ledger(guard.task.task_id),
        {"guard_id": guard.guard_id, "state": "confirmed", "verdict": verdict.value},
    )
    guard._save("confirmed", reason)
    return final


def load_provider(spec: Any, params: Mapping[str, Any]) -> ReadbackProvider:
    """The provider ``guard.yaml`` names. Raises ValueError (fail closed)."""

    if spec is None:
        return VerifyRunReadback()
    config: Mapping[str, Any] = spec if isinstance(spec, Mapping) else {"kind": spec}
    kind = str(config.get("kind") or "").strip().lower()
    if kind in ("", "verify_run"):
        return VerifyRunReadback()
    from openadapt_flow.runtime.effects.adapter import resolve_verifier_factory

    factory = resolve_verifier_factory(KIND_PREFIX + kind)
    if factory is None:
        raise ValueError(
            f"no readback provider is registered as {KIND_PREFIX}{kind} "
            "(register_verifier_factory)"
        )
    provider = factory(dict(config), dict(params))
    if not callable(getattr(provider, "read", None)):
        raise ValueError(f"{KIND_PREFIX}{kind} did not build a readback provider")
    return provider  # type: ignore[no-any-return]


def confirm(
    provider: ReadbackProvider, task: GuardTask, guard: Guard
) -> tuple[GuardResult, Optional[ReadbackResult]]:
    """Read the save back through ``provider`` and settle the final verdict.

    A provider that fails is a read that didn't happen: the save may have
    landed, so the verdict is ``RECONCILIATION_REQUIRED``, never a retry.
    """

    try:
        result = provider.read(task, guard)
        if not isinstance(result, ReadbackResult):
            raise TypeError("a readback provider returns a ReadbackResult")
    except Exception as exc:  # noqa: BLE001 - fail closed
        unread = {n: None for n in task.intended_fields}
        prior = guard.result
        if prior is not None:
            # verify-run already ran; the provider failed after it. Its
            # verdict stands only under the same tier rule.
            receipt = (prior.receipt or {}).get("receipt") or {}
            tier = str(receipt.get("verification_tier") or "unchecked")
            kept = ReadbackResult(
                tier=tier if tier in TIERS else "unchecked",
                fields=unread,
                evidence={
                    "receipt_path": prior.receipt_path,
                    "outcome": prior.outcome,
                    "reason": prior.reason,
                },
                verdict=prior.verdict,
                guard_result=prior,
            )
            return settle(guard, kept, rule(task, kept)), kept
        failed = ReadbackResult(
            tier="unchecked",
            fields=unread,
            evidence={"reason": "readback_provider_failed:" + type(exc).__name__},
        )
        return settle(guard, failed, GuardVerdict.RECONCILIATION_REQUIRED), failed
    return settle(guard, result, rule(task, result)), result
