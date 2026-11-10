"""Digit-token punctuation must survive OCR identity and saved comparisons.

All rows are synthetic. The properties construct distinct identifiers without
using the matcher's own identifier-shape predicate to select examples.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.ir import IdentityTemplate
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    _band_match_template,
    build_identity_template,
    recorded_identifier_matches,
    token_in_template,
    verify_structured_template,
    verify_template_identity,
)


def _row(identifier: str) -> str:
    return f"Ashford Jane 02/23/1975 {identifier} Active"


def _template(band: str, *, structured_identity: str | None = None) -> IdentityTemplate:
    template = build_identity_template(
        band, param_examples={}, structured_identity=structured_identity
    )
    assert template is not None
    return template


@st.composite
def _identifier_pairs(draw: st.DrawFn) -> tuple[str, str]:
    # No 0/1 glyph-collapse ambiguity: the unchanged control must verify.
    core = "RC" + draw(st.text(alphabet="23456789", min_size=2, max_size=9))
    position = draw(st.integers(min_value=0, max_value=len(core)))
    punctuation = draw(st.sampled_from([".", ",", "..", ",."]))
    changed = core[:position] + punctuation + core[position:]
    return core, changed


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@settings(max_examples=100, deadline=None)
@given(pair=_identifier_pairs())
def test_digit_identifier_punctuation_never_verifies_a_different_row(
    api: str, reverse: bool, pair: tuple[str, str]
) -> None:
    original, changed = pair
    recorded, observed = (changed, original) if reverse else (original, changed)
    if api == "plaintext":
        same = I.verify_target_identity(_row(recorded), _row(recorded))
        other = I.verify_target_identity(_row(recorded), _row(observed))
    else:
        template = _template(_row(recorded))
        same = verify_template_identity(template, _row(recorded))
        other = verify_template_identity(template, _row(observed))
    assert same.status == "verified"
    assert other.status in {"mismatch", "abstain"}


@pytest.mark.parametrize("version", [None, 1, 0, 3])
def test_legacy_or_unknown_template_cannot_recover_a_lost_identifier_separator(
    version: int | None,
) -> None:
    # The old writer hashed RC.79284 as RC79284. Construct those exact token
    # hashes, then read legacy serialized metadata rather than retagging it v2.
    serialized = _template(_row("RC79284")).model_dump()
    if version is None:
        serialized.pop("schema_version")
    else:
        serialized["schema_version"] = version
    legacy = IdentityTemplate.model_validate(serialized)
    wrong = _row("RC79284")
    assert verify_template_identity(legacy, wrong).status == "abstain"
    assert _band_match_template(legacy, wrong).coverage == 0.0
    assert not recorded_identifier_matches(legacy, "rc79284")
    assert not token_in_template(legacy, "RC79284")


def test_new_template_keeps_parameter_reanchoring_and_identifier_membership() -> None:
    template = build_identity_template(
        _row("RC79284"), param_examples={"patient": "Ashford Jane"}
    )
    assert template is not None
    assert template.schema_version == 2
    result = verify_template_identity(
        template,
        "Barnes Mike 02/23/1975 RC79284 Active",
        params={"patient": "Barnes Mike"},
        param_examples={"patient": "Ashford Jane"},
    )
    assert result.status == "verified"
    assert recorded_identifier_matches(template, "rc79284")
    assert token_in_template(template, "RC79284")


def test_legacy_band_refusal_preserves_independent_exact_structured_identity() -> None:
    template = _template(_row("RC.79284"), structured_identity="Reference RC.79284")
    serialized = template.model_dump()
    serialized.pop("schema_version")
    legacy = IdentityTemplate.model_validate(serialized)
    assert verify_template_identity(legacy, _row("RC79284")).status == "abstain"
    same = verify_structured_template(legacy, "Reference RC.79284")
    changed = verify_structured_template(legacy, "Reference RC79284")
    assert same is not None and changed is not None
    assert same.status == "verified"
    assert changed.status != "verified"
