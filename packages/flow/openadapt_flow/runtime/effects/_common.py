"""Substrate-independent judging of a typed :class:`Effect` against a list of
system-of-record records.

Every substrate (REST, FHIR, filesystem) normalizes its system of record into
a list of plain dicts and calls :func:`judge_records`, so the *decision* logic
-- at-most-once counting, idempotency-key de-duplication, field read-back,
collateral-loss detection, and the ``exact_new_set`` over-write guard -- lives
in exactly ONE place (the same single-source-of-truth discipline the
fault-model study uses for ``classify``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
    record_matches,
    stable_id,
)


def drop_record_fields(
    records: Optional[list[dict[str, Any]]], fields: Iterable[str]
) -> Optional[list[dict[str, Any]]]:
    """``records`` without ``fields`` (``None`` stays ``None``: unreadable)."""

    names = set(fields)
    if records is None or not names:
        return records
    return [{k: v for k, v in r.items() if k not in names} for r in records]


def new_records_only(
    matched: list[dict[str, Any]], before: EffectState
) -> list[dict[str, Any]]:
    """The subset of ``matched`` that did NOT exist in the pre-state.

    The cross-cutting duplicate-write guard (``Effect.count_new_only``):
    identity is :func:`stable_id`, the same accounting collateral-loss uses,
    so the delta works on every substrate that normalizes to dict records.
    """
    before_ids = {stable_id(r) for r in before.records}
    return [r for r in matched if stable_id(r) not in before_ids]


def _indeterminate(
    effect: Effect, substrate: str, reason: str, *, unavailable: bool = False
) -> EffectVerdict:
    return EffectVerdict(
        verdict=Verdict.INDETERMINATE,
        kind=effect.kind,
        substrate=substrate,
        reason=reason,
        unavailable=unavailable,
    )


def judge_records(
    effect: Effect,
    before: EffectState,
    current: Optional[list[dict[str, Any]]],
    *,
    substrate: str,
) -> EffectVerdict:
    """Judge ``effect`` against the current system-of-record ``current``.

    Args:
        effect: The typed effect contract to verify.
        before: The pre-action snapshot (baseline + collateral-loss basis).
        current: The system-of-record records read AFTER the action, in the
            substrate's native dict shape -- or ``None`` when the system of
            record could not be read (unreachable / unusable response), which
            forces an INDETERMINATE (HALT) verdict.
        substrate: Substrate name for the verdict record.

    Returns:
        The :class:`EffectVerdict` (CONFIRMED / REFUTED / INDETERMINATE).
    """
    if current is None:
        return _indeterminate(
            effect,
            substrate,
            "system of record unreachable at verify time -- cannot certify "
            "the declared outcome/effect; HALT (never assume success)",
            unavailable=True,
        )

    if effect.kind is EffectKind.EXACT_NEW_SET:
        return _judge_exact_new_set(effect, before, current, substrate)

    matched = [r for r in current if record_matches(r, effect.match)]
    if effect.idempotency_key is not None:
        matched = [
            r
            for r in matched
            if str(r.get(effect.key_field, None)) == str(effect.idempotency_key)
        ]
    key_holders: list[dict[str, Any]] = []
    if effect.composite_key:
        # Several fields together are the key (OpenEMR: lname + fname + DOB).
        # The save's own record is counted by exact text, so a correct save
        # is never held. Records that carry the key are then counted across
        # the WHOLE read set, pre-existing ones included and before any
        # newness filter below, in the loose form a person would call the
        # same (case, spacing, a date read back as a datetime). Loose
        # matching only ever adds holders.
        key = _key_text(effect.composite_key)
        matched = [r for r in matched if record_matches(r, key)]
        key_holders = [r for r in current if carries_composite_key(r, effect)]

    # Duplicate-write guard (count_new_only): judge only the records that
    # APPEARED since the pre-state -- "exactly expected_count NEW record(s)".
    # Without a readable baseline the delta is unknowable -> INDETERMINATE
    # (never guess which records are new).
    if effect.count_new_only and effect.kind is EffectKind.RECORD_WRITTEN:
        if not before.reachable:
            return _indeterminate(
                effect,
                substrate,
                "count_new_only requires a readable pre-state baseline, but "
                "the system of record was unreachable before the action -- "
                "cannot attribute records to this action; HALT",
            )
        matched = new_records_only(matched, before)

    if effect.kind is EffectKind.FIELD_EQUALS:
        return _judge_field_equals(effect, matched, substrate)
    verdict = _judge_record_written(effect, before, current, matched, substrate)
    if len(key_holders) > 1:
        return _refute_duplicate_key(effect, verdict, key_holders, substrate)
    return verdict


def _key_text(selector: dict[str, Any]) -> dict[str, Any]:
    """A key selector for :func:`record_matches`.

    Values compare as text, the rule every selector here uses. A value that
    declares a value kind (``kind: date`` for a birth date, see the value-kinds
    change) stays an expression so the record is matched in that kind's
    canonical form; on a ValueExpr without kinds this is plain ``str()``.
    """

    return {k: (v if getattr(v, "kind", None) else str(v)) for k, v in selector.items()}


_WHITESPACE = re.compile(r"\s+")
_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00(:00(\.0+)?)?$")


def loose_key_form(value: Any) -> Optional[str]:
    """``value`` in the form two key values are called the same in.

    Text is trimmed, its inner whitespace collapsed to one space and case
    folded (``"SMITH "`` and ``"Smith"`` agree, as under a case-insensitive
    database collation). A date or datetime is its ISO date, and so is a
    date-shaped string at midnight (``1984-03-07 00:00:00``), because a
    driver may read a DATE column back as a datetime. ``None`` stays
    ``None`` and agrees with nothing.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _WHITESPACE.sub(" ", str(value)).strip()
    midnight = _MIDNIGHT.match(text)
    if midnight:
        return midnight.group(1)
    return text.casefold()


def carries_composite_key(record: dict[str, Any], effect: Effect) -> bool:
    """Whether ``record`` carries ``effect``'s composite key, loosely.

    Used to count the records that share the key, never to count the
    save's own record (that stays exact). A value with a declared value kind
    compares in that kind's canonical form instead.
    """

    if not effect.composite_key:
        return False
    for name, want in effect.composite_key.items():
        observed = record.get(name, None)
        if getattr(want, "kind", None):
            if not want.matches(observed):  # type: ignore[attr-defined]
                return False
            continue
        expected = loose_key_form(str(want))
        if expected is None or loose_key_form(observed) != expected:
            return False
    return True


def _refute_duplicate_key(
    effect: Effect,
    verdict: EffectVerdict,
    holders: list[dict[str, Any]],
    substrate: str,
) -> EffectVerdict:
    """REFUTE: more than one record carries the composite duplicate key."""

    fields = ", ".join(sorted(effect.composite_key))
    reason = (
        f"{len(holders)} records share the duplicate key ({fields}) after the "
        "action, counting records that existed before it -- a duplicate "
        "record (not at-most-once)"
    )
    if verdict.verdict is Verdict.REFUTED and verdict.reason:
        reason = f"{verdict.reason}; {reason}"
    return EffectVerdict(
        verdict=Verdict.REFUTED,
        kind=effect.kind,
        substrate=substrate,
        reason=reason,
        observed_count=verdict.observed_count,
        expected_count=effect.expected_count,
        matched_records=holders,
    )


#: How many offending records one refutation reason names before it stops
#: listing them. The COUNT is always exact; the sample keeps an audit line
#: readable when an agent added dozens of unintended rows.
_SAMPLE_LIMIT = 5


def _selector_text(selector: dict[str, Any]) -> str:
    return "{" + ", ".join(f"{k}={v}" for k, v in sorted(selector.items())) + "}"


def _judge_exact_new_set(
    effect: Effect,
    before: EffectState,
    current: list[dict[str, Any]],
    substrate: str,
) -> EffectVerdict:
    """Judge the ``exact_new_set`` over-write guard.

    The claim: the records ADDED to the scoped read set by this action are
    EXACTLY ``effect.new_records`` -- each declared member added exactly as
    many times as it is declared, and NO record added that no member names.
    ``effect.match`` is the SCOPE (empty = the whole read set), not a target
    selector.

    Refuses (INDETERMINATE) rather than guesses when the added set cannot be
    enumerated: an unreachable baseline, or any record on either side missing
    ``effect.identity_field``.
    """
    if not before.reachable:
        return _indeterminate(
            effect,
            substrate,
            "exact_new_set requires a readable pre-state baseline, but the "
            "system of record was unreachable before the action -- the set of "
            "records this action ADDED cannot be enumerated; HALT",
        )

    identity = effect.identity_field
    for label, records in (("pre-state", before.records), ("current", current)):
        missing = sum(1 for r in records if r.get(identity, None) is None)
        if missing:
            return _indeterminate(
                effect,
                substrate,
                f"exact_new_set cannot enumerate the added set: {missing} "
                f"{label} record(s) carry no {identity!r} value. Supply an "
                "identity_field that every record of the read set carries "
                "(widen the query to return a stable identity column), or "
                "remove this guard -- newness is never guessed",
            )

    before_identities = {str(r[identity]) for r in before.records}
    in_scope = [r for r in current if record_matches(r, effect.match)]
    added = [r for r in in_scope if str(r[identity]) not in before_identities]

    reasons: list[str] = []

    # (1) The headline: how many records the action added versus how many it
    # was allowed to add.
    if len(added) != effect.expected_count:
        reasons.append(
            f"the action added {len(added)} record(s) to the scoped read set "
            f"but the contract declares exactly {effect.expected_count}"
        )

    # (2) Per declared member: present exactly as many times as declared.
    declared: dict[str, tuple[dict[str, Any], int]] = {}
    for selector in effect.new_records:
        plain = {k: str(v) for k, v in selector.items()}
        key = _selector_text(plain)
        found, multiplicity = declared.get(key, (plain, 0))
        declared[key] = (found, multiplicity + 1)
    for key, (selector, multiplicity) in sorted(declared.items()):
        observed = sum(1 for r in added if record_matches(r, selector))
        if observed != multiplicity:
            reasons.append(
                f"declared new record {key} was added {observed} time(s), "
                f"expected {multiplicity}"
            )

    # (3) THE GUARD: a record the action added that no declared member names.
    extra = [
        r
        for r in added
        if not any(
            record_matches(r, {k: str(v) for k, v in selector.items()})
            for selector in effect.new_records
        )
    ]
    if extra:
        sample = ", ".join(
            _selector_text({k: str(v) for k, v in r.items() if k != identity})
            for r in extra[:_SAMPLE_LIMIT]
        )
        hidden = len(extra) - _SAMPLE_LIMIT
        more = "" if hidden <= 0 else f", and {hidden} more"
        reasons.append(
            f"{len(extra)} record(s) the action added are NOT in the declared "
            f"set -- unintended write(s) to the system of record: {sample}{more}"
        )

    # (4) Removals inside the scope this effect speaks for.
    if effect.forbid_collateral_loss:
        current_identities = {str(r[identity]) for r in current}
        lost = [
            r
            for r in before.records
            if record_matches(r, effect.match)
            and str(r[identity]) not in current_identities
        ]
        if lost:
            reasons.append(
                f"{len(lost)} pre-existing record(s) inside the declared scope "
                "vanished -- collateral loss"
            )

    if reasons:
        return EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=effect.kind,
            substrate=substrate,
            reason="; ".join(reasons),
            observed_count=len(added),
            expected_count=effect.expected_count,
            matched_records=added,
        )
    return EffectVerdict(
        verdict=Verdict.CONFIRMED,
        kind=effect.kind,
        substrate=substrate,
        reason=(
            f"the action added exactly the {effect.expected_count} declared "
            "record(s) to the scoped read set, and nothing else"
        ),
        observed_count=len(added),
        expected_count=effect.expected_count,
        matched_records=added,
    )


_NUMBER = (int, float, Decimal)
_WHEN = (datetime, date, time)


def _as_number(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_when(value: Any) -> Optional[Any]:
    if isinstance(value, _WHEN):
        return value
    if isinstance(value, str):
        for parse in (datetime.fromisoformat, date.fromisoformat, time.fromisoformat):
            try:
                return parse(value)
            except ValueError:
                continue
    return None


def _same_value(before: Any, after: Any) -> bool:
    """Whether a record field is unchanged between the baseline and now.

    The baseline may have been written to JSON and read back (verify-run's
    baseline file), while the current read has the driver's own types. So a
    ``Decimal('1.50')`` read now equals the ``"1.50"`` or ``1.5`` it was
    stored as, a ``datetime`` equals its ISO string (either separator), and
    ``bytes`` equal their hex or UTF-8 text. Nested values compare element by
    element, so key order doesn't matter. Two strings compare exactly: a
    text field that changed from ``"02139"`` to ``"2139"`` is a change.
    """

    if isinstance(before, dict) and isinstance(after, dict):
        return set(before) == set(after) and all(
            _same_value(before[k], after[k]) for k in before
        )
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return len(before) == len(after) and all(
            _same_value(b, a) for b, a in zip(before, after)
        )
    if before is None or after is None:
        return before is None and after is None
    if isinstance(before, bool) or isinstance(after, bool):
        return before == after
    if isinstance(before, (bytes, bytearray)) or isinstance(after, (bytes, bytearray)):
        raw, other = (
            (before, after)
            if isinstance(before, (bytes, bytearray))
            else (after, before)
        )
        if isinstance(other, (bytes, bytearray)):
            return bytes(raw) == bytes(other)
        text = str(other)
        return text == bytes(raw).hex() or text == bytes(raw).decode("utf-8", "replace")
    if isinstance(before, _NUMBER) or isinstance(after, _NUMBER):
        b, a = _as_number(before), _as_number(after)
        return b is not None and a is not None and b == a
    if isinstance(before, _WHEN) or isinstance(after, _WHEN):
        b, a = _as_when(before), _as_when(after)
        return b is not None and a is not None and b == a
    return before == after


def _judge_record_written(
    effect: Effect,
    before: EffectState,
    current: list[dict[str, Any]],
    matched: list[dict[str, Any]],
    substrate: str,
) -> EffectVerdict:
    observed = len(matched)
    count_ok = observed == effect.expected_count

    # Collateral loss (the stale / lost-update fault): a record that existed
    # in the pre-state and does NOT match our target selector has vanished.
    # Our own row can land correctly (count 1) while a concurrent actor's row
    # was destroyed by a last-write-wins save -- invisible to a count of OUR
    # records and to the screen, caught only by comparing against the baseline.
    collateral_lost: list[dict[str, Any]] = []
    if effect.forbid_collateral_loss and before.reachable:
        current_ids = {stable_id(r) for r in current}
        for r in before.records:
            if record_matches(r, effect.match):
                continue  # the target record may legitimately change
            if stable_id(r) not in current_ids:
                collateral_lost.append(r)

    # Collateral change: a non-target record that is still there but whose
    # fields differ from the pre-state (an in-place write to the wrong row).
    collateral_changed: list[tuple[dict[str, Any], list[str]]] = []
    if effect.forbid_collateral_change and before.reachable:
        now = {stable_id(r): r for r in current}
        for r in before.records:
            if record_matches(r, effect.match):
                continue
            after = now.get(stable_id(r))
            if after is None:
                continue  # vanished: the collateral-loss rule's case
            fields = sorted(
                k
                for k in set(r) | set(after)
                if not _same_value(r.get(k), after.get(k))
            )
            if fields:
                collateral_changed.append((r, fields))

    if count_ok and not collateral_lost and not collateral_changed:
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
        elif observed == 0:
            reasons.append(
                f"{observed} records match the target selector, expected "
                f"{effect.expected_count} (missing / phantom / rejected "
                f"write -- the screen may show success but nothing landed)"
            )
        else:
            # Some of the expected records DID land. Saying "nothing landed"
            # here would invite a re-run that duplicates the ones that exist.
            reasons.append(
                f"partial write: {observed} of {effect.expected_count} "
                f"expected records landed (some writes are missing or were "
                f"rejected; {observed} already exist, so do not retry "
                f"without checking the record)"
            )
    if collateral_lost:
        reasons.append(
            f"{len(collateral_lost)} pre-existing record(s) vanished -- "
            f"collateral loss (stale / concurrent last-write-wins overwrote "
            f"another actor's row)"
        )
    if collateral_changed:
        names = sorted({f for _r, fields in collateral_changed for f in fields})
        reasons.append(
            f"{len(collateral_changed)} pre-existing record(s) outside the "
            f"target changed -- collateral change (field(s): "
            f"{', '.join(names[:_SAMPLE_LIMIT])}"
            f"{'' if len(names) <= _SAMPLE_LIMIT else ', ...'}); this may be "
            f"another user's concurrent edit, not this action -- a person "
            f"should reconcile it before anything is retried or undone"
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
                "(missing or unverifiable business outcome)"
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
                "ambiguous field/outcome read-back"
            ),
            observed_count=len(matched),
            expected_value=want,
            matched_records=matched,
        )
    record = matched[0]
    observed = "" if record.get(effect.field) is None else str(record.get(effect.field))
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
            "(the system of record contradicts the declared outcome)"
        ),
        observed_count=1,
        observed_value=observed,
        expected_value=want,
        matched_records=matched,
    )
