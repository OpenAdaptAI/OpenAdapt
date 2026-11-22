"""The review page's text check, ported onto the effect-location record.

The port must not change a single decision. ``_old_text_pair_matches`` is
form_tx's comparison as it was before the port (vendored below from
``openadapt_flow/form_tx/judge.py`` at 1ddd69712, lines 129 to 184). Every
intake value and every shown text in the corpus, under every ported binding,
gets the same answer from the old comparison and from the effect location.
Bindings that are not ported return None and keep the old comparison.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from openadapt_flow.form_tx.codecs import collapse_whitespace, values_equivalent
from openadapt_flow.form_tx.review_location import (
    PORTED_CODECS,
    review_location,
    review_text_matches,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    ObservedTextPair,
)


# vendored: 1ddd69712:packages/flow/openadapt_flow/form_tx/judge.py 129-134
def _old_is_placeholder_or_empty(value: str, placeholders: Any) -> bool:
    norm = collapse_whitespace(value).casefold()
    if not norm:
        return True
    placeholder_set = {collapse_whitespace(p).casefold() for p in placeholders}
    return norm in placeholder_set


# vendored: 1ddd69712:packages/flow/openadapt_flow/form_tx/judge.py 167-184
def _old_text_pair_matches(expected_val: Any, pair: Any, binding: Any) -> bool:
    if _old_is_placeholder_or_empty(pair.value, binding.placeholder_values):
        return False
    if values_equivalent(expected_val, pair.value, binding.codec):
        return True
    if binding.control_kind in (ControlKind.CHECKBOX, ControlKind.RADIO_GROUP):
        return values_equivalent(
            expected_val,
            pair.value,
            FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO),
        )
    return False


TEXTS = [
    "Ada Byron",
    "Ada  Byron",
    " ada\tbyron\n",
    "ADA BYRON",
    "Ada",
    "Ada Byron 0",
    "Ada Byron (old)",
    "",
    "   ",
    "-- Select --",
    "select one",
    "Straße",
    "STRASSE",
    "strasse",
    "Ünïcode Ño",
    "ünïcode ño",
    "5",
    "05",
    "Yes",
    "yes",
    "No",
    "true",
    "Ada Byron",
]
INTAKE: list[Any] = [*TEXTS, 5, 0, True, False, None, 1.5]
CONTROL_KINDS = [
    ControlKind.TEXT,
    ControlKind.SELECT,
    ControlKind.DATE,
    ControlKind.READONLY_OUTPUT,
]


def _binding(kind: FormCodecKind, control: ControlKind) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id="b",
        dossier_key="k",
        anchor=FieldAnchorSpec(label_patterns=("^Name$",)),
        control_kind=control,
        codec=FieldCodecSpec(kind=kind),
    )


@pytest.mark.parametrize("kind", sorted(PORTED_CODECS, key=lambda k: k.value))
@pytest.mark.parametrize("control", CONTROL_KINDS, ids=lambda c: c.value)
def test_ported_decisions_are_unchanged(
    kind: FormCodecKind, control: ControlKind
) -> None:
    binding = _binding(kind, control)
    ported = 0
    for intake, shown in itertools.product(INTAKE, TEXTS):
        pair = ObservedTextPair(label="Name", value=shown)
        old = _old_text_pair_matches(intake, pair, binding)
        new = review_text_matches(binding, intake, pair, run_id="r")
        if new is None:
            # Not ported: an intake value the codec can't read. The old
            # comparison then says no match too, and it is what runs.
            assert old is False, (intake, shown)
            continue
        ported += 1
        assert new is old, (kind, control, intake, shown, old, new)
    assert ported > len(TEXTS) * 10


@pytest.mark.parametrize(
    "kind", [k for k in FormCodecKind if k not in PORTED_CODECS], ids=lambda k: k.value
)
def test_other_codecs_stay_on_the_old_comparison(kind: FormCodecKind) -> None:
    pair = ObservedTextPair(label="Name", value="Ada Byron")
    assert (
        review_text_matches(
            _binding(kind, ControlKind.TEXT), "Ada Byron", pair, run_id="r"
        )
        is None
    )


@pytest.mark.parametrize("control", [ControlKind.CHECKBOX, ControlKind.RADIO_GROUP])
def test_yes_no_bindings_stay_on_the_old_comparison(control: ControlKind) -> None:
    binding = _binding(FormCodecKind.WHITESPACE_COLLAPSE, control)
    pair = ObservedTextPair(label="Name", value="Yes")
    assert review_text_matches(binding, "true", pair, run_id="r") is None


def test_the_record_says_where_and_how() -> None:
    binding = _binding(FormCodecKind.CASEFOLD, ControlKind.TEXT)
    location = review_location(binding, " Ada  Byron ")
    assert location is not None
    assert location.start == "same_screen"
    assert location.keys == ("k",)
    assert location.expected == {"k": "ada byron"}
    assert location.codec.kind == "fold"
    assert location.locator.value == "^Name$"
    assert location.expected_count == 1
