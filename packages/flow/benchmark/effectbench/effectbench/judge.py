"""Substrate-independent judging of an :class:`Effect` against record lists.

Every oracle substrate normalizes its system of record into a list of plain
dicts and calls :func:`judge_records`, so the decision logic -- at-most-once
counting, idempotency-key de-duplication, field read-back, and collateral-loss
detection -- lives in exactly one place. Vendored verbatim (mechanism only) from
the OpenAdapt engine.
"""

from __future__ import annotations

from typing import Any, Optional

from effectbench.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
    record_matches,
    stable_id,
)


def new_records_only(
    matched: list[dict[str, Any]], before: EffectState
) -> list[dict[str, Any]]:
    """The subset of ``matched`` that did NOT exist in the pre-state."""
    before_ids = {stable_id(r) for r in before.records}
    return [r for r in matched if stable_id(r) not in before_ids]


def _indeterminate(effect: Effect, substrate: str, reason: str) -> EffectVerdict:
    return EffectVerdict(
        verdict=Verdict.INDETERMINATE,
        kind=effect.kind,
        substrate=substrate,
        reason=reason,
    )


def judge_records(
    effect: Effect,
    before: EffectState,
    current: Optional[list[dict[str, Any]]],
    *,
    substrate: str,
) -> EffectVerdict:
    """Judge ``effect`` against the current system-of-record ``current``.

    ``current is None`` (unreachable) forces INDETERMINATE (HALT).
    """
    if current is None:
        return _indeterminate(
            effect,
            substrate,
            "system of record unreachable at verify time -- cannot certify "
            "the write landed; HALT (never assume success)",
        )

    matched = [r for r in current if record_matches(r, effect.match)]
    if effect.idempotency_key is not None:
        matched = [
            r
            for r in matched
            if str(r.get(effect.key_field, None)) == str(effect.idempotency_key)
        ]

    if effect.count_new_only and effect.kind is EffectKind.RECORD_WRITTEN:
        if not before.reachable:
            return _indeterminate(
                effect,
                substrate,
                "count_new_only requires a readable pre-state baseline, but "
                "the system of record was unreachable before the action; HALT",
            )
        matched = new_records_only(matched, before)

    if effect.kind is EffectKind.FIELD_EQUALS:
        return _judge_field_equals(effect, matched, substrate)
    return _judge_record_written(effect, before, current, matched, substrate)


def _judge_record_written(
    effect: Effect,
    before: EffectState,
    current: list[dict[str, Any]],
    matched: list[dict[str, Any]],
    substrate: str,
) -> EffectVerdict:
    observed = len(matched)
    count_ok = observed == effect.expected_count

    collateral_lost: list[dict[str, Any]] = []
    if effect.forbid_collateral_loss and before.reachable:
        current_ids = {stable_id(r) for r in current}
        for r in before.records:
            if record_matches(r, effect.match):
                continue
            if stable_id(r) not in current_ids:
                collateral_lost.append(r)

    if count_ok and not collateral_lost:
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=substrate,
            reason=f"exactly {observed} record(s) match the target selector",
            observed_count=observed,
            expected_count=effect.expected_count,
            matched_records=matched,
        )

    reasons: list[str] = []
    if not count_ok:
        if observed > effect.expected_count:
            reasons.append(
                f"{observed} records match the target selector but exactly "
                f"{effect.expected_count} was expected (duplicate / "
                f"double-delivered write -- not at-most-once)"
            )
        else:
            reasons.append(
                f"{observed} records match the target selector, expected "
                f"{effect.expected_count} (missing / phantom / rejected "
                f"write -- the screen may show success but nothing landed)"
            )
    if collateral_lost:
        reasons.append(
            f"{len(collateral_lost)} pre-existing record(s) vanished -- "
            f"collateral loss (stale / concurrent last-write-wins overwrote "
            f"another actor's row)"
        )
    return EffectVerdict(
        verdict=Verdict.REFUTED,
        kind=effect.kind,
        substrate=substrate,
        reason="; ".join(reasons),
        observed_count=observed,
        expected_count=effect.expected_count,
        matched_records=matched,
    )


def _judge_field_equals(
    effect: Effect,
    matched: list[dict[str, Any]],
    substrate: str,
) -> EffectVerdict:
    want = "" if effect.value is None else str(effect.value)
    if not matched:
        return EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=effect.kind,
            substrate=substrate,
            reason=(
                "no record matches the target selector, so field "
                f"'{effect.field}' cannot equal the expected value "
                "(phantom / rejected write)"
            ),
            observed_count=0,
            expected_value=want,
        )
    if len(matched) > 1:
        return EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=effect.kind,
            substrate=substrate,
            reason=(
                f"{len(matched)} records match the target selector -- "
                "ambiguous field read-back (duplicate write)"
            ),
            observed_count=len(matched),
            expected_value=want,
            matched_records=matched,
        )
    record = matched[0]
    observed = (
        "" if record.get(effect.field) is None else str(record.get(effect.field))
    )
    if observed == want:
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=substrate,
            reason=f"field '{effect.field}' equals the expected value",
            observed_count=1,
            observed_value=observed,
            expected_value=want,
            matched_records=matched,
        )
    return EffectVerdict(
        verdict=Verdict.REFUTED,
        kind=effect.kind,
        substrate=substrate,
        reason=(
            f"field '{effect.field}' is {observed!r}, expected {want!r} "
            "(partial save -- the row persisted but this field was dropped "
            "or differs)"
        ),
        observed_count=1,
        observed_value=observed,
        expected_value=want,
        matched_records=matched,
    )
