"""A summary row that shows several values, judged as one effect location.

A summary often shows two intake values in one row: "Ada Byron (01/31/1990)"
holds a traveller's name and date of birth. Without a way to read the row as
two values, both keys ended ``not_checked``; a pack could only check a derived
string built from both, which says nothing about either key on its own.

A :class:`~openadapt_flow.form_tx.types.SummaryRowReadbackSpec` is turned into
an ``openadapt.effect-location/v1`` record with a ``template`` codec (start
``same_screen``) and judged by the shared evaluator. Each key gets its own
verdict: a row whose date is wrong refutes the date and still checks the name.

Codec mapping (the shared codecs read the displayed part; form_tx's codec
reads the intake value, so the intake keeps its own formats):

- ``whitespace_collapse`` reads as ``text``, ``casefold`` as ``fold``;
- ``iso_date`` reads as ``date`` in the part's ``date_order``.

A part with any other codec leaves every key of the row ``not_checked``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openadapt_flow.effect_location import (
    Codec,
    EffectLocation,
    Locator,
    Reading,
    evaluate_all,
)
from openadapt_flow.effect_location.record import Part
from openadapt_flow.form_tx.codecs import CodecValueError, normalize_with_codec
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
    match_text_pair_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    FieldCodecSpec,
    FormCodecKind,
    FormSnapshot,
    SummaryRowReadbackSpec,
)

__all__ = ["row_readback_results"]

#: The verdicts this module returns, by key: (status, reason).
RowResult = tuple[str, str]


def _codec(spec: FieldCodecSpec) -> Codec:
    if spec.kind == FormCodecKind.WHITESPACE_COLLAPSE:
        return Codec("text")
    if spec.kind == FormCodecKind.CASEFOLD:
        return Codec("fold")
    if spec.kind == FormCodecKind.ISO_DATE:
        return Codec("date", date_order=spec.date_order)
    # Any other codec: the shared evaluator judges the record not_checked.
    return Codec(f"form_tx:{spec.kind.value}")


def _location(row: SummaryRowReadbackSpec, index: int) -> EffectLocation:
    return EffectLocation(
        id=f"summary_row:{index}",
        keys=tuple(p.dossier_key for p in row.parts),
        path=(),
        start="same_screen",
        locator=Locator(
            kind="label",
            value="|".join(row.anchor.label_patterns),
            section="|".join(row.anchor.section_patterns),
        ),
        codec=Codec(
            "template",
            pattern=row.pattern,
            parts=tuple(
                Part(p.group, p.dossier_key, _codec(p.codec)) for p in row.parts
            ),
        ),
        expected_count=1,
        source="form_tx.summary_check.row_readbacks",
    )


def _shown(row: SummaryRowReadbackSpec, snapshot: FormSnapshot) -> Reading:
    """What the summary shows for the row, as a reading (not yet bound)."""

    try:
        ctrl = match_anchor_in_snapshot(row.anchor, snapshot, include_readonly=True)
        if ctrl is not None:
            texts: tuple[str, ...] = (ctrl.current_value,)
        else:
            pair = match_text_pair_in_snapshot(row.anchor, snapshot)
            texts = (pair.value,) if pair is not None else ()
        error = None
    except AmbiguousAnchorError:
        # Shown more than once: never read as proof.
        texts, error = (), "the row is shown more than once"
    return Reading(
        location_id="",
        run_id="",
        start="same_screen",
        matches=tuple({"text": t} for t in texts),
        complete=True,
        error=error,
    )


def row_readback_results(
    rows: tuple[SummaryRowReadbackSpec, ...],
    snapshot: FormSnapshot,
    expected: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, list[RowResult]]:
    """Each key's verdicts from the summary rows: ``held``, ``refuted`` or
    ``not_checked``, with a reason.

    ``expected`` maps an intake key to its value (or to an object with a
    ``value``). A key with no intake value is not judged here. An intake
    value the part's form_tx codec can't read is ``not_checked``.
    """

    out: dict[str, list[RowResult]] = {}
    for index, row in enumerate(rows):
        location = _location(row, index)
        plan: dict[str, Any] = {}
        for part in row.parts:
            if part.dossier_key not in expected:
                continue
            raw = getattr(
                expected[part.dossier_key], "value", expected[part.dossier_key]
            )
            try:
                plan[part.dossier_key] = normalize_with_codec(raw, part.codec)
            except CodecValueError:
                out.setdefault(part.dossier_key, []).append(
                    ("not_checked", "The intake value can't be read by this codec")
                )
        if not plan:
            continue
        shown = _shown(row, snapshot)
        reading = Reading(
            location_id=location.id,
            run_id=run_id,
            start=shown.start,
            matches=shown.matches,
            complete=shown.complete,
            error=shown.error,
        )
        report = evaluate_all(plan, [location], [reading], run_id=run_id)
        for result in report.results:
            out.setdefault(result.key, []).append((result.status, result.reason))
    return out
