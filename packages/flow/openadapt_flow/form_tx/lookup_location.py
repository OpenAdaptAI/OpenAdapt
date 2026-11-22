"""The chosen row of a lookup table as an effect location: echo before commit.

A lookup popup (a class finder, a directory search) shows one row per result,
each with its own commit button. The value the popup puts back in the parent
must be the one the chosen row showed. The row is read just before the commit
click (``row_reading``): the cells that fully match the subroutine's
``result_row_pattern`` are the matches. After the popup closes, the value in
the parent is judged against that reading by the shared evaluator
(``judge_returned_value``), as an ``openadapt.effect-location/v1`` record with
a ``row_key`` locator and start ``same_screen``.

- ``held``: the row showed exactly that value once.
- ``refuted``: the row showed another value.
- ``not_checked``: the row showed no such cell or more than one, the value
  can't be read, or the return codec is one the shared codecs don't read the
  same way. The caller stops on anything but ``held``.
"""

from __future__ import annotations

import re
from typing import Any

from openadapt_flow.effect_location import (
    Codec,
    EffectLocation,
    KeyResult,
    Locator,
    Reading,
    evaluate,
)
from openadapt_flow.form_tx.codecs import CodecValueError, normalize_with_codec
from openadapt_flow.form_tx.types import (
    FieldCodecSpec,
    FormCodecKind,
    ObservedControl,
    PopupSubroutineSpec,
)

__all__ = ["judge_returned_value", "row_reading"]


def _codec(spec: FieldCodecSpec) -> Codec:
    if spec.kind == FormCodecKind.WHITESPACE_COLLAPSE:
        return Codec("text")
    if spec.kind == FormCodecKind.CASEFOLD:
        return Codec("fold")
    if spec.kind == FormCodecKind.ISO_DATE:
        return Codec("date", date_order=spec.date_order)
    return Codec(f"form_tx:{spec.kind.value}")


def _location_id(subroutine: PopupSubroutineSpec) -> str:
    return f"lookup_row:{subroutine.subroutine_id}"


def row_reading(
    subroutine: PopupSubroutineSpec, commit_control: ObservedControl, *, run_id: str
) -> Reading:
    """The chosen row's matching cells, read before the commit click."""

    pattern = subroutine.result_row_pattern or ""
    cells = tuple(" ".join(c.split()) for c in commit_control.row_cells)
    try:
        hits = tuple(c for c in cells if c and re.fullmatch(pattern, c))
        error = None if cells else "the commit control is not in a table row"
    except re.error:
        hits, error = (), "the result row pattern does not compile"
    return Reading(
        location_id=_location_id(subroutine),
        run_id=run_id,
        start="same_screen",
        matches=tuple({"text": h} for h in hits),
        complete=True,
        error=error,
    )


def judge_returned_value(
    subroutine: PopupSubroutineSpec,
    reading: Reading,
    returned: Any,
    *,
    run_id: str,
) -> KeyResult:
    """Whether the value put back in the parent is the one the row showed."""

    key = subroutine.return_output_key
    try:
        expected = normalize_with_codec(returned, subroutine.return_codec)
    except CodecValueError:
        return KeyResult(
            key, "not_checked", "the value put back can't be read", code="bad_expected"
        )
    location = EffectLocation(
        id=_location_id(subroutine),
        keys=(key,),
        path=(),
        start="same_screen",
        locator=Locator(kind="row_key", value=subroutine.result_row_pattern or ""),
        codec=_codec(subroutine.return_codec),
        expected={key: expected},
        expected_count=1,
        source="form_tx.popup_subroutines.result_row_pattern",
    )
    return evaluate(location, reading, run_id=run_id).results[0]
