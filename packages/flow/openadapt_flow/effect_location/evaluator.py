"""Judge effect locations from readings: every intended value, or ``not_checked``.

The obligations are the run's intended values (the plan), fixed before any
reading is judged. Each one ends ``held``, ``refuted`` or ``not_checked``, the
three statuses of the verdict kernel (``openadapt_flow.verdict``):

- ``not_checked`` when no location shows the value, the location was not
  read, the read failed or came from another run, the start condition was not
  met, the locator or codec kind is unknown, the value was found a different
  number of times than expected, the text could not be read, the read was
  partial and showed no conflict, or the value equals the baseline (the read
  can't tell the change from what was there before).
- ``refuted`` when a complete or partial read shows a different value.
- ``held`` only when every location for the value was read in full and showed
  exactly the expected value the expected number of times.

The run passes only when every planned value held and no reading was left
over. The check level comes from the start condition the adapter achieved
(``openadapt_types.check_level``); the receipt word is
``check_level.outcome_word`` when that is available, so this module never
decides what ``VERIFIED`` means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union, get_args

from openadapt_types.check_level import CheckLevel

from openadapt_flow.effect_location import _compat
from openadapt_flow.effect_location._compat import verdict_site
from openadapt_flow.effect_location.codecs import CodecError, canonical, decode
from openadapt_flow.effect_location.record import (
    LOCATOR_KINDS,
    SCALAR_KINDS,
    EffectLocation,
    Reading,
    Start,
)

__all__ = [
    "CODES",
    "HELD",
    "NOT_CHECKED",
    "REFUTED",
    "KeyResult",
    "KeyStatus",
    "Report",
    "evaluate",
    "evaluate_all",
]

#: The verdict kernel's three statuses (``openadapt_flow.verdict.Status``).
KeyStatus = Literal["held", "refuted", "not_checked"]
HELD, REFUTED, NOT_CHECKED = get_args(KeyStatus)

#: Every ``KeyResult.code``. Only ``shown`` holds; only ``mismatch`` refutes.
CODES = frozenset(
    {
        "shown",
        "mismatch",
        "bad_record",
        "other_run",
        "read_failed",
        "bad_reading",
        "start_not_met",
        "not_found",
        "count_mismatch",
        "no_expected",
        "bad_expected",
        "bad_baseline",
        "unreadable",
        "partial",
        "same_as_baseline",
        "no_location",
        "not_read",
        "duplicate_location",
        "stale_record",
        "plan_conflict",
    }
)

#: The check level a start condition reaches.
LEVEL_OF_START: dict[Start, CheckLevel] = {
    Start.SAME_SCREEN: CheckLevel.SAME_SCREEN,
    Start.FRESH_NAVIGATION: CheckLevel.REOPENED,
    Start.NEW_SESSION: CheckLevel.SEPARATE_SESSION,
    Start.OTHER_CHANNEL: CheckLevel.SYSTEM_OF_RECORD,
}


@dataclass(frozen=True)
class KeyResult:
    """One intended value's verdict.

    ``shown`` holds the displayed texts the reading matched for this value,
    for the caller's message to a person. It may hold typed values, so it
    never goes into a receipt.
    """

    key: str
    status: str
    reason: str
    level: CheckLevel = CheckLevel.UNCHECKED
    basis: Optional[str] = None
    word: Optional[str] = None
    locations: tuple[str, ...] = ()
    shown: tuple[str, ...] = ()
    #: Why, for code: one of :data:`CODES`. Callers branch on this, never
    #: on ``reason``, which is for people.
    code: str = ""


@dataclass(frozen=True)
class Report:
    """Every planned value's verdict and the run's."""

    results: tuple[KeyResult, ...]
    status: str
    reason: str
    level: CheckLevel
    basis: Optional[str]
    word: Optional[str]
    #: Readings that no planned location asked for.
    unexpected: tuple[str, ...]

    @property
    def passes(self) -> bool:
        return self.status == HELD

    def by_key(self) -> dict[str, KeyResult]:
        return {r.key: r for r in self.results}


def _start(value: Any) -> Optional[Start]:
    try:
        return Start(value)
    except ValueError:
        return None


def _same(a: Any, b: Any) -> bool:
    """Equal and of the same type: ``True`` is not 1, ``"1"`` is not 1."""

    return type(a) is type(b) and a == b


def _codec_for(loc: EffectLocation, key: str) -> Any:
    if loc.codec.kind == "template":
        for part in loc.codec.parts:
            if part.key == key:
                return part.codec
        return None
    return loc.codec


def _shape_problem(loc: EffectLocation) -> Optional[str]:
    """Why the record itself can't be judged, or None."""

    if loc.locator.kind not in LOCATOR_KINDS:
        return "the locator kind is not one this engine reads"
    kind = loc.codec.kind
    if kind == "template":
        if not loc.codec.pattern or set(loc.keys) != set(loc.codec.keys()):
            return "the template does not name each key once"
        if len(loc.codec.keys()) != len(set(loc.codec.keys())):
            return "the template names a key twice"
    elif kind == "split_date" or kind in SCALAR_KINDS:
        if len(loc.keys) != 1:
            return "this codec shows one key"
    else:
        return "the codec kind is not one this engine reads"
    if len(set(loc.keys)) != len(loc.keys) or not loc.keys:
        return "the location names no key, or a key twice"
    count = loc.expected_count
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return "the expected count is not a positive whole number"
    if _start(loc.start) is None:
        return "the start condition is not one this engine knows"
    return None


#: The basis an adapter names for a fresh navigation it proved fetched from
#: the server (cache bypassed, the request kept as evidence).
FRESH_RELOAD = "fresh_reload"


def _level_and_basis(
    start: Start, claimed: Optional[str]
) -> tuple[CheckLevel, Optional[str]]:
    """The level and basis a held check reaches from ``start``.

    A fresh navigation is level 2 (``reopened``, SHOWN) unless the adapter
    names the ``fresh_reload`` basis and the ladder lists it at level 3;
    then it is level 3, which the ladder words VERIFIED. Another channel is
    level 4 with the basis the adapter names, if the ladder lists that basis
    at level 4.
    """

    level = LEVEL_OF_START[start]
    if start is Start.NEW_SESSION:
        return level, "separate_session"
    if (
        start is Start.FRESH_NAVIGATION
        and claimed == FRESH_RELOAD
        and _compat.reaches(claimed, CheckLevel.SEPARATE_SESSION)
    ):
        return CheckLevel.SEPARATE_SESSION, claimed
    if start is Start.OTHER_CHANNEL and _compat.reaches(
        claimed, CheckLevel.SYSTEM_OF_RECORD
    ):
        return level, claimed
    return level, None


def _judge(loc: EffectLocation, reading: Reading, run_id: str) -> dict[str, KeyResult]:
    """Each of ``loc``'s keys judged from one reading."""

    def nc(code: str, reason: str, shown: tuple[str, ...] = ()) -> dict[str, KeyResult]:
        return {
            k: KeyResult(
                k, NOT_CHECKED, reason, locations=(loc.id,), shown=shown, code=code
            )
            for k in loc.keys
        }

    problem = _shape_problem(loc)
    if problem:
        return nc("bad_record", problem)
    if not run_id or reading.run_id != run_id:
        return nc("other_run", "the reading belongs to another run")
    if reading.error is not None:
        return nc("read_failed", "the read failed")
    if not isinstance(reading.complete, bool):
        return nc("bad_reading", "the reading does not say whether it was complete")
    wanted = _start(loc.start)
    got = _start(reading.start)
    if got is None or wanted is None or LEVEL_OF_START[got] < LEVEL_OF_START[wanted]:
        return nc(
            "start_not_met", "the reading did not start where the location requires"
        )
    found = len(reading.matches)
    if found == 0:
        return nc("not_found", "the location was not found")
    shown_all = tuple(str(m.get("text", "")) for m in reading.matches)
    if found != loc.expected_count:
        return nc(
            "count_mismatch",
            f"found {found} times, expected {loc.expected_count}",
            shown_all,
        )
    decoded = [decode(loc.codec, m, loc.keys) for m in reading.matches]
    out: dict[str, KeyResult] = {}
    level, basis = _level_and_basis(got, reading.basis)
    for key in loc.keys:
        codec = _codec_for(loc, key)

        def result(status: str, code: str, reason: str) -> KeyResult:
            return KeyResult(
                key,
                status,
                reason,
                code=code,
                level=level if status == HELD else CheckLevel.UNCHECKED,
                basis=basis if status == HELD else None,
                locations=(loc.id,),
                shown=shown_all,
            )

        if key not in loc.expected:
            out[key] = result(
                NOT_CHECKED, "no_expected", "the location has no expected value"
            )
            continue
        try:
            want = canonical(codec, loc.expected[key])
        except CodecError:
            out[key] = result(
                NOT_CHECKED,
                "bad_expected",
                "the expected value is not in the codec's form",
            )
            continue
        values = [d.get(key) for d in decoded]
        wrong = [
            v for v in values if not isinstance(v, CodecError) and not _same(v, want)
        ]
        if wrong:
            # A partial read can refute: it showed a different value.
            out[key] = result(
                REFUTED, "mismatch", "the location shows a different value"
            )
            continue
        if any(
            isinstance(v, CodecError) or key not in d for v, d in zip(values, decoded)
        ):
            out[key] = result(
                NOT_CHECKED, "unreadable", "the displayed text could not be read"
            )
            continue
        if reading.complete is not True:
            out[key] = result(NOT_CHECKED, "partial", "the read was partial")
            continue
        if loc.baseline is not None and key in loc.baseline:
            try:
                before = canonical(codec, loc.baseline[key])
            except CodecError:
                out[key] = result(
                    NOT_CHECKED,
                    "bad_baseline",
                    "the baseline is not in the codec's form",
                )
                continue
            if _same(before, want):
                out[key] = result(
                    NOT_CHECKED,
                    "same_as_baseline",
                    "the value was already shown before, so the read cannot show the change",
                )
                continue
        out[key] = result(HELD, "shown", "the location shows the expected value")
    return out


def _combine(key: str, results: Sequence[KeyResult]) -> KeyResult:
    """One value's verdict from every location and reading that judged it.

    Any refutation refutes. Otherwise it holds only when every result held,
    at the weakest level among them (``check_level``: the weakest wins).
    """

    locs = tuple(dict.fromkeys(loc for r in results for loc in r.locations))
    shown = tuple(s for r in results for s in r.shown)
    if not results:
        return KeyResult(
            key, NOT_CHECKED, "no effect location shows this value", code="no_location"
        )
    refuted = [r for r in results if r.status == REFUTED]
    if refuted:
        return KeyResult(
            key,
            REFUTED,
            refuted[0].reason,
            locations=locs,
            shown=shown,
            code=refuted[0].code,
        )
    open_ = [r for r in results if r.status != HELD]
    if open_:
        return KeyResult(
            key,
            NOT_CHECKED,
            open_[0].reason,
            locations=locs,
            shown=shown,
            code=open_[0].code,
        )
    weakest = min(results, key=lambda r: r.level)
    bases = {r.basis for r in results if r.level == weakest.level}
    basis = bases.pop() if len(bases) == 1 else None
    return KeyResult(
        key,
        HELD,
        weakest.reason,
        level=weakest.level,
        basis=basis,
        word=_compat.word(weakest.level, basis, confirmed=True),
        code=weakest.code,
        locations=locs,
        shown=shown,
    )


Plan = Union[Mapping[str, Any], Iterable[Sequence[Any]]]


def _plan_items(plan: Plan) -> list[tuple[str, Any]]:
    items = plan.items() if isinstance(plan, Mapping) else plan
    out = []
    for item in items:
        pair: Sequence[Any] = item
        key, value = pair
        out.append((key, value))
    return out


@verdict_site("effect_location.evaluate_all", classes="C1 C2 C5")
def evaluate_all(
    plan: Plan,
    locations: Iterable[EffectLocation],
    readings: Iterable[Reading],
    *,
    run_id: str,
) -> Report:
    """Judge every intended value in ``plan`` (key to expected value, or a
    list of pairs) from the pack's ``locations`` and the adapter's
    ``readings`` for run ``run_id``.

    A key planned twice with different values, two locations with one id, a
    location expecting another value than the plan, and a reading no planned
    location asked for all keep the run from passing.
    """

    planned: dict[str, Any] = {}
    conflicted: set[str] = set()
    for key, value in _plan_items(plan):
        if key in planned and not _same(planned[key], value):
            conflicted.add(key)
        planned.setdefault(key, value)
    locs = list(locations)
    ids: dict[str, int] = {}
    for loc in locs:
        ids[loc.id] = ids.get(loc.id, 0) + 1
    by_reading: dict[str, list[Reading]] = {}
    for one in readings:
        by_reading.setdefault(one.location_id, []).append(one)

    judged: dict[str, list[KeyResult]] = {k: [] for k in planned}
    used: set[str] = set()
    for loc in locs:
        relevant = [k for k in loc.keys if k in planned]
        if not relevant:
            continue
        used.add(loc.id)
        if ids[loc.id] > 1:
            for k in relevant:
                judged[k].append(
                    KeyResult(
                        k,
                        NOT_CHECKED,
                        "two locations share this id",
                        locations=(loc.id,),
                        code="duplicate_location",
                    )
                )
            continue
        got = by_reading.get(loc.id, [])
        if not got:
            for k in relevant:
                judged[k].append(
                    KeyResult(
                        k,
                        NOT_CHECKED,
                        "the location was not read",
                        locations=(loc.id,),
                        code="not_read",
                    )
                )
            continue
        # The plan is what must show; a record that expects something else
        # was made for another value and can't confirm this one.
        planned_loc = loc.with_expected({k: planned[k] for k in relevant})
        for reading in got:
            per_key = _judge(planned_loc, reading, run_id)
            for k in relevant:
                r = per_key[k]
                stale = k in loc.expected and not _same(loc.expected[k], planned[k])
                if stale and r.status != REFUTED:
                    r = KeyResult(
                        k,
                        NOT_CHECKED,
                        "the location expects a different value than the plan",
                        locations=(loc.id,),
                        shown=r.shown,
                        code="stale_record",
                    )
                judged[k].append(r)

    results = []
    for key in planned:
        if key in conflicted:
            results.append(
                KeyResult(
                    key,
                    NOT_CHECKED,
                    "the plan has two values for this key",
                    code="plan_conflict",
                )
            )
        else:
            results.append(_combine(key, judged[key]))
    unexpected = tuple(sorted(lid for lid in by_reading if lid not in used))

    if not results:
        status, reason = NOT_CHECKED, "nothing was planned"
    elif any(r.status == REFUTED for r in results):
        status, reason = REFUTED, "a location shows a different value"
    elif all(r.status == HELD for r in results) and not unexpected:
        status, reason = HELD, "every planned value was shown where expected"
    elif unexpected and all(r.status == HELD for r in results):
        status, reason = NOT_CHECKED, "a reading matches no planned location"
    else:
        status, reason = NOT_CHECKED, "a planned value was not checked"

    level, basis, word = CheckLevel.UNCHECKED, None, None
    if status == HELD:
        level = min(r.level for r in results)
        bases = {r.basis for r in results if r.level == level}
        basis = bases.pop() if len(bases) == 1 else None
        word = _compat.word(level, basis, confirmed=True)
    else:
        word = _compat.word(CheckLevel.UNCHECKED, None, confirmed=False)
    return Report(
        results=tuple(results),
        status=status,
        reason=reason,
        level=level,
        basis=basis,
        word=word,
        unexpected=unexpected,
    )


def evaluate(
    location: EffectLocation, reading: Optional[Reading], *, run_id: str
) -> Report:
    """Judge one location, planned with its own expected values, from one
    reading (None when it was not read)."""

    return evaluate_all(
        location.expected,
        [location],
        [] if reading is None else [reading],
        run_id=run_id,
    )
