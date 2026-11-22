"""The review page as an effect location: echo before commit.

Before submit, form_tx reads the review screen and stops when it shows a
value other than the intake's. This module states that check as an
``openadapt.effect-location/v1`` record (start ``same_screen``) and judges it
with the shared evaluator, :mod:`openadapt_flow.effect_location`, for the
value a review page shows as page text.

Only the codecs whose reading the shared codecs reproduce exactly are ported:
``whitespace_collapse`` and ``casefold``. For any other codec, a checkbox or
radio binding (whose text falls back to yes or no), and a value held in a form
control (which may match by its option label), :func:`review_text_matches`
returns None and the caller keeps form_tx's own comparison.
``tests/test_form_tx_review_location.py`` compares the two over a corpus.

Gate direction: a value that is found but does not hold stops the run, as it
did before. Only "not shown" and "shown more than once" are skips, and those
are decided before this module is called.
"""

from __future__ import annotations

from typing import Any, Optional

from openadapt_flow.effect_location import (
    Codec,
    EffectLocation,
    Locator,
    Reading,
    evaluate,
)
from openadapt_flow.form_tx.codecs import (
    CodecValueError,
    collapse_whitespace,
    normalize_with_codec,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldBindingSpec,
    FormCodecKind,
    ObservedTextPair,
)

__all__ = ["PORTED_CODECS", "review_location", "review_text_matches"]

#: form_tx codec kind to the shared codec kind that reads text the same way.
PORTED_CODECS: dict[FormCodecKind, str] = {
    FormCodecKind.WHITESPACE_COLLAPSE: "text",
    FormCodecKind.CASEFOLD: "fold",
}

#: Bindings whose page text is also read as yes or no: kept on form_tx's path.
_YES_NO_BINDINGS = frozenset({ControlKind.CHECKBOX, ControlKind.RADIO_GROUP})


def review_location(
    binding: FieldBindingSpec, intake_value: Any
) -> Optional[EffectLocation]:
    """The review-page effect location for ``binding``, or None when this
    binding stays on form_tx's comparison (see the module)."""

    kind = PORTED_CODECS.get(binding.codec.kind)
    if kind is None or binding.control_kind in _YES_NO_BINDINGS:
        return None
    try:
        expected = normalize_with_codec(intake_value, binding.codec)
    except CodecValueError:
        return None
    if not isinstance(expected, str):
        return None
    return EffectLocation(
        id=f"review:{binding.dossier_key}",
        keys=(binding.dossier_key,),
        path=(),
        start="same_screen",
        locator=Locator(
            kind="label",
            value="|".join(binding.anchor.label_patterns),
            section="|".join(binding.anchor.section_patterns),
        ),
        codec=Codec(kind),
        expected={binding.dossier_key: expected},
        expected_count=1,
        source="form_tx.summary_check.review_readbacks",
    )


def _placeholder_or_empty(value: str, placeholders: tuple[str, ...]) -> bool:
    norm = collapse_whitespace(value).casefold()
    return not norm or norm in {collapse_whitespace(p).casefold() for p in placeholders}


def review_text_matches(
    binding: FieldBindingSpec,
    intake_value: Any,
    pair: ObservedTextPair,
    *,
    run_id: str,
) -> Optional[bool]:
    """Whether the review page's text ``pair`` shows the intake value.

    True when the effect location holds. False when it does not: refuted,
    or found but not readable as the value, and that stops the run. None when
    the binding is not ported, so the caller compares it the old way.
    """

    location = review_location(binding, intake_value)
    if location is None:
        return None
    # A placeholder ("-- Select --") is never the value, even when the intake
    # happens to read the same. The placeholder list is the binding's data.
    if _placeholder_or_empty(pair.value, binding.placeholder_values):
        return False
    reading = Reading(
        location_id=location.id,
        run_id=run_id,
        start="same_screen",
        matches=({"text": pair.value},),
        complete=True,
        error=None,
    )
    return evaluate(location, reading, run_id=run_id).passes
