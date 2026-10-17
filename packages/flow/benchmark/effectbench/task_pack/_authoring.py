"""Shared authoring helpers for the EffectBench first task pack.

These build the two things every consequential-write task needs:

- the DECLARED expected effect (a typed
  :class:`~openadapt_flow.benchmark.effectbench.Effect`), parameterized by run
  params via :class:`~openadapt_flow.benchmark.effectbench.ValueExpr` so the
  record CHECKED is the record this trial WROTE (trial-unique payload); and
- the :class:`~openadapt_flow.benchmark.effectbench.OracleSpec` that declares
  the independent read channel and the non-gameability attestations the
  adversarial audit signs off on.

Keeping the effect/oracle construction here (rather than repeated inline in
every task module) means the whole pack verifies the SAME contract shape a real
"save" needs — an at-most-once ``record_written`` PLUS a ``field_equals``
read-back of the consequential field — so a partial save, a duplicate, a
phantom, a lost update, and a wrong-record are all catchable by construction.
"""

from __future__ import annotations

from typing import Any, Optional

from openadapt_flow.benchmark.effectbench import (
    Effect,
    EffectKind,
    OracleSpec,
    ValueExpr,
)
from openadapt_flow.benchmark.effectbench.schema import OracleChannel

# Canonical run-param names a trial binds a trial-unique value to. The driver
# derives each from the trial seed so the oracle checks THIS run's exact write.
PARAM_RECORD_KEY = "record_key"  # trial-unique idempotency / target key
PARAM_NOTE = "note"  # trial-unique consequential free-text field
PARAM_TARGET = "target_id"  # trial-unique target-record identifier (MRN / loan id)


def record_written_effect(
    match: dict[str, Any],
    *,
    expected_count: int = 1,
    count_new_only: bool = True,
    forbid_collateral_loss: bool = True,
    idempotency_key: Optional[ValueExpr] = None,
    key_field: str = "key",
    risk: str = "reversible",
    probe: str = "",
    timeout_s: float = 2.0,
) -> Effect:
    """A ``record_written`` at-most-once contract for a consequential write.

    ``count_new_only`` defaults True so a selector that legitimately matches a
    pre-existing decoy row still catches a double-submit (2 NEW rows) and a
    phantom (0 NEW rows); ``forbid_collateral_loss`` catches a stale
    last-write-wins that destroyed a concurrent actor's row.
    """
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: _expr(v) for k, v in match.items()},
        expected_count=expected_count,
        count_new_only=count_new_only,
        forbid_collateral_loss=forbid_collateral_loss,
        idempotency_key=idempotency_key,
        key_field=key_field,
        risk=risk,
        probe=probe or None,
        timeout_s=timeout_s,
    )


def field_equals_effect(
    match: dict[str, Any],
    *,
    field: str,
    value: Any,
    risk: str = "reversible",
    probe: str = "",
    timeout_s: float = 2.0,
) -> Effect:
    """A ``field_equals`` read-back contract for the consequential field.

    This is what catches a partial save: the row persisted, but the field that
    carries the intent (the clinical note, the loan amount) was dropped or
    differs from what this trial wrote.
    """
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: _expr(v) for k, v in match.items()},
        field=field,
        value=_expr(value),
        risk=risk,
        probe=probe or None,
        timeout_s=timeout_s,
    )


def _expr(v: Any) -> ValueExpr:
    """Coerce a literal / ``{"param": ...}`` marker / existing expr to ValueExpr."""
    if isinstance(v, ValueExpr):
        return v
    if isinstance(v, dict) and "param" in v:
        return ValueExpr(param=str(v["param"]))
    return ValueExpr(literal=str(v))


def param(name: str) -> ValueExpr:
    """A run-param reference (trial-unique value bound by the driver)."""
    return ValueExpr(param=name)


def oracle_spec(
    channel: OracleChannel,
    *,
    description: str,
    config: Optional[dict[str, Any]] = None,
    isolated_from_agent: bool = True,
    trial_unique_payload: bool = True,
    refusal_controls: bool = False,
    adversarially_audited: bool = False,
) -> OracleSpec:
    """An :class:`OracleSpec` with the non-gameability attestations set
    EXPLICITLY and truthfully.

    ``adversarially_audited`` defaults False: a task is not release-eligible
    until a red-team pass has actually tried and failed to satisfy the oracle
    without the true effect. The MockMed pack flips it True only for tasks the
    live adversarial audit (:mod:`.audit`) actually exercised; the
    container-gated OpenEMR/Frappe tasks keep it False until a container run
    audits them.
    """
    return OracleSpec(
        channel=channel,
        description=description,
        config=dict(config or {}),
        isolated_from_agent=isolated_from_agent,
        trial_unique_payload=trial_unique_payload,
        refusal_controls=refusal_controls,
        adversarially_audited=adversarially_audited,
    )
