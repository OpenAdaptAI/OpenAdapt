"""Strict positive identity evidence; lossy punctuation may only add uncertainty.

Synthetic software-only cases. Standalone structural spans deliberately abstain
until their field ownership is established; this is not arbitrary ID parsing.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.compiler.loop_shapes import _hashed_indices
from openadapt_flow.ir import IdentityTemplate
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    _band_match_template,
    build_identity_template,
    record_key_matches,
    recorded_identifier_matches,
    token_in_template,
    verify_signal_template,
    verify_structured_template,
    verify_template_identity,
)


def _row(identifier: str, surname: str = "Ashford") -> str:
    return f"{surname} Jane 02/23/1975 {identifier} Active"


def _result(api: str, expected: str, observed: str, **kwargs):
    if api == "plaintext":
        return I.verify_target_identity(expected, observed, **kwargs)
    template = build_identity_template(
        expected, param_examples=kwargs.get("param_examples")
    )
    assert template is not None
    return verify_template_identity(template, observed, **kwargs)


@st.composite
def _separated_identifier(draw):
    core = "RC" + draw(st.text(alphabet="23456789", min_size=2, max_size=8))
    at = draw(st.integers(min_value=0, max_value=len(core)))
    punctuation = draw(st.sampled_from([".", ",", "..", ",.", "-", "/", ":", ";"]))
    # Split on either side or internally in the punctuation span. This is
    # generated from source positions, not the matcher's own shape predicates.
    left = draw(st.sampled_from(["", " "]))
    right = draw(st.sampled_from(["", " "]))
    middle = " ".join(punctuation) if draw(st.booleans()) else punctuation
    return core, core[:at] + left + middle + right + core[at:]


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@settings(max_examples=100, deadline=None)
@given(pair=_separated_identifier())
def test_structural_punctuation_never_gains_positive_authority(api, reverse, pair):
    original, separated = pair
    expected, observed = (separated, original) if reverse else (original, separated)
    assert _result(api, _row(expected), _row(observed)).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "expected,observed",
    [
        ("RC79 . . 284", "RC79 284"),
        ("RC79 . . 284", "RC79 . 284"),
        ("RC79 , , 284", "RC79 , 284"),
        ("RC79 - - 284", "RC79 - 284"),
        ("RC79 / / 284", "RC79 / 284"),
        ("RC79 : : 284", "RC79 : 284"),
        ("RC79 ; ; 284", "RC79 ; 284"),
        ("RC . 79284", "RC79284"),
        ("RC. 79284", "RC79284"),
        (".RC 79284", "RC79284"),
        ("79284 RC.", "79284RC"),
        (".R 79284", "R 79284"),
        ("R. 79284 R 2345", "R 79284 R. 2345"),
        ("R. R 79284", "R R. 79284"),
        ("R R. R R 79284", "R R R. R 79284"),
    ],
)
def test_known_separator_ownership_and_count_siblings(api, reverse, expected, observed):
    if reverse:
        expected, observed = observed, expected
    assert _result(api, _row(expected), _row(observed)).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("separator", [".", ",", "-", "/", ":", ";"])
def test_unchanged_standalone_span_is_explicitly_uncertain(api, separator):
    row = _row(f"RC79 {separator} {separator} 284")
    assert _result(api, row, row).status == "abstain"


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_digit_gluing_stays_uncertain(api):
    assert _result(api, _row("RC79 . . 284"), _row("RC79284")).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_noise_only_changes_mismatch_to_glyph_uncertainty(api):
    expected = _row("100512", "Smith,")
    observed = _row("100512", "Smith.")
    strict = I.band_match(expected, observed)
    assert I.band_verdict(strict) == "mismatch"
    result = _result(api, expected, observed)
    assert result.status == "abstain"
    assert result.abstain_reason == "glyph_confusable_identifier"


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_noise_verified_is_never_positive_authority(api):
    expected = _row("RC79284", "Smith,")
    observed = _row("RC79284", "Smith.")
    assert I.band_verdict(I._punctuation_noise_match(expected, observed)) == "verified"
    assert _result(api, expected, observed).status == "mismatch"


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_true_name_contradiction_survives_glyph_noise(api):
    assert (
        _result(api, _row("100512", "Smith,"), _row("100512", "Barnes.")).status
        == "mismatch"
    )


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_parameterized_calls_do_not_adopt_unowned_noise_space(api):
    result = _result(
        api,
        _row("100512", "Smith,"),
        _row("100512", "Smith."),
        params={"other": "Unrelated"},
        param_examples={"other": "Nobody"},
    )
    assert result.status == "mismatch"


def test_noise_template_cannot_authorize_or_supply_membership():
    expected = _row("100512", "Smith,")
    template = build_identity_template(expected, param_examples={})
    assert template is not None and template.ocr_noise is not None
    noise = template.ocr_noise
    assert noise.schema_version == 0
    assert verify_template_identity(noise, expected).status == "abstain"
    assert _band_match_template(noise, expected).coverage == 0
    assert not token_in_template(noise, "100512")
    assert not recorded_identifier_matches(noise, "100512")
    assert _hashed_indices(noise, "100512") is None
    assert not record_key_matches(noise, "100512")
    assert verify_structured_template(noise, "100512") is None
    assert (
        verify_signal_template(
            noise,
            source="captured_context",
            match="exact",
            normalizers=[],
            live=expected,
        )
        is None
    )
    serialized = json.dumps(template.model_dump())
    assert "Smith" not in serialized
    assert "100512" not in serialized
    assert "02/23/1975" not in serialized


def test_absent_noise_metadata_leaves_strict_failure():
    expected = _row("100512", "Smith,")
    observed = _row("100512", "Smith.")
    template = build_identity_template(expected, param_examples={})
    assert template is not None
    without = template.model_copy(update={"ocr_noise": None})
    assert verify_template_identity(without, observed).status == "mismatch"


def test_parameter_reanchoring_keeps_strict_punctuation_metadata():
    template = build_identity_template(
        _row("RC79284"), param_examples={"patient": "Ashford Jane"}
    )
    assert template is not None and template.ocr_noise is None
    assert (
        verify_template_identity(
            template,
            "Barnes Mike 02/23/1975 RC79284 Active",
            params={"patient": "Barnes Mike"},
            param_examples={"patient": "Ashford Jane"},
        ).status
        == "verified"
    )


def test_legacy_noise_utility_remains_compatible_without_authority():
    assert I.squash("Smith,") == I.squash("Smith.")
    assert I.squash("Smith, John") == "smithjohn"
    assert I.squash("Jr.") == "jr"
    assert I.squash("01.01.1900") == "01.01.1900"
    assert I.comparison_squash("Smith,") != I.comparison_squash("Smith.")


@pytest.mark.parametrize("strict_status", ["verified", "mismatch", "abstain"])
@pytest.mark.parametrize("noise_status", ["verified", "mismatch", "abstain", "missing"])
def test_classification_state_model_never_uses_noise_to_verify(
    strict_status, noise_status
):
    def match(status):
        if status == "verified":
            return I.BandMatch(1, 0, 0)
        if status == "mismatch":
            return I.BandMatch(1, 0, 1)
        return I.BandMatch(1, 0, 0, glyph_ambiguous_id_chars=5)

    strict = match(strict_status)
    noise = None if noise_status == "missing" else match(noise_status)
    status, _reason = I._status_with_punctuation_noise(strict, noise)
    assert (status == "verified") == (strict_status == "verified")
    expected = (
        "abstain"
        if strict_status == "mismatch" and noise_status == "abstain"
        else strict_status
    )
    assert status == expected


def test_unknown_strict_punctuation_contract_has_no_recovery_credit():
    row = _row("RC79284")
    template = build_identity_template(row, param_examples={})
    assert template is not None
    unknown = template.model_copy(update={"punctuation_policy": None})
    assert verify_template_identity(unknown, row).status == "abstain"
    assert _band_match_template(unknown, row).coverage == 0
    assert not token_in_template(unknown, "RC79284")
    assert not recorded_identifier_matches(unknown, "rc79284")
    assert _hashed_indices(unknown, "RC79284") is None


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_repeated_punctuation_erased_body_has_no_unique_owner(api):
    row = _row("R R. R R 79284")
    assert _result(api, row, row).status == "abstain"


def test_missing_punctuation_body_metadata_has_no_recovery_credit():
    row = _row("RC79284", "Smith,")
    template = build_identity_template(row, param_examples={})
    assert template is not None and template.punctuation_body_keys
    missing = template.model_copy(update={"punctuation_body_keys": {}})
    assert verify_template_identity(missing, row).status == "abstain"
    assert _band_match_template(missing, row).coverage == 0
    assert not token_in_template(missing, "RC79284")


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("identifier", ["RC.79284", "RC. 79284", "RC79284"])
def test_unchanged_unique_punctuation_owner_keeps_positive_path(api, identifier):
    row = _row(identifier)
    assert _result(api, row, row).status == "verified"


@pytest.mark.parametrize(
    "bad_keys", [{1: "0" * 16}, {0: "not-a-hash"}, {999: "0" * 16}]
)
def test_misaligned_punctuation_body_metadata_has_no_positive_credit(bad_keys):
    row = _row("RC79284", "Smith,")
    template = build_identity_template(row, param_examples={})
    assert template is not None and template.punctuation_body_keys
    # The ordinary writer owns the body-hash correspondence. This control
    # targets malformed/extra/misaligned indices, not cryptographic proof of
    # a supplied digest's preimage.
    invalid = template.model_copy(update={"punctuation_body_keys": bad_keys})
    assert verify_template_identity(invalid, row).status == "abstain"
    assert _band_match_template(invalid, row).coverage == 0
    assert _hashed_indices(invalid, "RC79284") is None


def test_normal_strict_template_still_supports_compiler_hash_indices():
    row = _row("RC79284")
    template = build_identity_template(row, param_examples={})
    assert template is not None
    assert _hashed_indices(template, "RC79284") is not None


@pytest.mark.parametrize("api", ["plaintext", "template"])
def test_unchanged_unique_name_punctuation_keeps_positive_path(api):
    row = _row("RC79284", "Smith,")
    assert _result(api, row, row).status == "verified"


def test_recursive_noise_template_roundtrip_keeps_separate_authority():
    expected = _row("100512", "Smith,")
    observed = _row("100512", "Smith.")
    template = build_identity_template(expected, param_examples={})
    assert template is not None
    restored = IdentityTemplate.model_validate_json(template.model_dump_json())
    assert restored.model_dump() == template.model_dump()
    assert restored.ocr_noise is not None and restored.ocr_noise.schema_version == 0
    assert verify_template_identity(restored, observed).status == "abstain"
    assert _hashed_indices(restored.ocr_noise, "100512") is None


def test_legacy_serialization_omits_empty_additive_noise_metadata():
    serialized = IdentityTemplate().model_dump()
    for name in (
        "ocr_noise",
        "punctuation_policy",
        "punctuation_indices",
        "punctuation_body_keys",
        "standalone_punctuation_indices",
    ):
        assert name not in serialized


@pytest.mark.parametrize(
    "updates",
    [
        {"punctuation_indices": [999]},
        {"punctuation_indices": [0, 0]},
        {"punctuation_indices": []},
        {"standalone_punctuation_indices": [999]},
        {"punctuation_body_keys": {0: "0" * 16, 1: "0" * 16}},
        {"punctuation_body_keys": {"0": "0" * 16}},
    ],
)
def test_incomplete_structural_metadata_has_zero_recovery_credit(updates):
    row = _row("RC79284", "Smith,")
    template = build_identity_template(row, param_examples={})
    assert template is not None
    invalid = template.model_copy(update=updates)
    assert verify_template_identity(invalid, row).status == "abstain"
    assert _band_match_template(invalid, row).coverage == 0
    assert not recorded_identifier_matches(invalid, "RC79284")
    assert _hashed_indices(invalid, "RC79284") is None


@pytest.mark.parametrize(
    "keys,bodies,numeric,indices",
    [
        (["a"], [], [False], []),
        (["a"], ["a"], [], []),
        (["a"], ["a"], [False], [1]),
        (["a"], ["a"], [False], [0, 0]),
    ],
)
def test_shared_witness_refuses_unbound_shape_or_index_metadata(
    keys, bodies, numeric, indices
):
    assert I._punctuation_signature(keys, bodies, numeric, indices, []) is None
