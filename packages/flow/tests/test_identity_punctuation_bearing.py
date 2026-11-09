"""Cross strict punctuation and bearing-only coverage, without weakening either."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.ir import IdentityTemplate
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    _band_match_template,
    build_identity_template,
    verify_template_identity,
)

APIS = ("plaintext", "fresh_template", "round_trip_template")
LABELS = "Hint: enter values in multiple boxes. Last Name First Name"


def _template(recorded, api, param_examples=None):
    result = build_identity_template(recorded, param_examples=param_examples or {})
    assert result is not None
    if api == "round_trip_template":
        result = IdentityTemplate.model_validate(result.model_dump(mode="json"))
    return result


def _check(api, recorded, observed):
    if api == "plaintext":
        return I.verify_target_identity(
            recorded, observed, params={}, param_examples={}
        )
    return verify_template_identity(
        _template(recorded, api), observed, params={}, param_examples={}
    )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("punctuation", [".", ",", "..", ",."])
def test_label_padding_never_licenses_a_punctuation_changed_identifier(
    api, punctuation
):
    recorded = f"Ashford Jane 02/23/1975 RC{punctuation}79284 {LABELS}"
    observed = f"Ashford Jane 02/23/1975 RC79284 {LABELS}"
    assert _check(api, recorded, recorded).status == "verified"
    assert _check(api, recorded, observed).status != "verified"
    # Same strict punctuation must not let generic labels pad missing
    # identity-bearing evidence either. Both rules apply to one model.
    expected = f"Smith John 15 47 {LABELS}"
    missing = f"Smith John {LABELS}"
    assert _check(api, expected, missing).status != "verified"
    match = (
        I.band_match(expected, missing)
        if api == "plaintext"
        else _band_match_template(_template(expected, api), missing)
    )
    assert match[0] == pytest.approx(9 / 13)
    assert match[7]


@pytest.mark.parametrize("api", APIS)
@settings(max_examples=80, deadline=None)
@given(
    digits=st.text(alphabet="23456789", min_size=2, max_size=9),
    punctuation=st.sampled_from([".", ",", "..", ",."]),
    repeats=st.integers(min_value=1, max_value=6),
    reverse=st.booleans(),
)
def test_crossed_identity_model_survives_label_multiplicity_and_round_trip(
    api, digits, punctuation, repeats, reverse
):
    original = "RC" + digits
    marked = "RC" + punctuation + digits
    recorded_id, observed_id = (marked, original) if reverse else (original, marked)
    suffix = " ".join([LABELS] * repeats)
    recorded = f"Ashford Jane 02/23/1975 {recorded_id} {suffix}"
    observed = f"Ashford Jane 02/23/1975 {observed_id} {suffix}"
    assert _check(api, recorded, recorded).status == "verified"
    assert _check(api, recorded, observed).status != "verified"
    expected = f"Smith John 15 47 {suffix}"
    missing = f"Smith John {suffix}"
    assert _check(api, expected, missing).status != "verified"
    match = (
        I.band_match(expected, missing)
        if api == "plaintext"
        else _band_match_template(_template(expected, api), missing)
    )
    assert match[0] == pytest.approx(9 / 13)


@pytest.mark.parametrize("api", ("fresh_template", "round_trip_template"))
def test_template_bearing_flags_use_raw_case_and_label_shape_with_strict_tokens(api):
    text = "Last: smith First: John MRN: RC.79284 Age: 47"
    template = _template(text, api)
    raw = [token for token in text.split() if I.comparison_squash(token)]
    assert len(template.tokens) == len(raw)
    assert [token.bearing for token in template.tokens] == [
        I.identity_bearing(token, I.comparison_squash(token)) for token in raw
    ]
    assert [word for word, token in zip(raw, template.tokens) if token.bearing] == [
        "John",
        "RC.79284",
        "47",
    ]
    assert (
        verify_template_identity(template, text, params={}, param_examples={}).status
        == "verified"
    )


@pytest.mark.parametrize("api", APIS)
def test_no_bearing_evidence_still_abstains_under_strict_punctuation(api):
    text = "Last Name: First Name: Phone: Address: search all records"
    assert _check(api, text, text).status == "abstain"


@pytest.mark.parametrize("api", ("fresh_template", "round_trip_template"))
def test_parameter_reanchoring_retains_both_raw_bearing_and_strict_identifier(api):
    recorded = "Last: smith First: John ID: RC.79284 Age: 47"
    params, examples = {"last": "jones"}, {"last": "smith"}
    template = _template(recorded, api, examples)
    same = "Last: jones First: John ID: RC.79284 Age: 47"
    other = "Last: jones First: John ID: RC79284 Age: 47"
    plain_same = I.verify_target_identity(
        recorded, same, params=params, param_examples=examples
    )
    hashed_same = verify_template_identity(
        template, same, params=params, param_examples=examples
    )
    assert plain_same.status == hashed_same.status
    assert plain_same.status == "verified"
    assert (
        I.verify_target_identity(
            recorded, other, params=params, param_examples=examples
        ).status
        != "verified"
    )
    assert (
        verify_template_identity(
            template, other, params=params, param_examples=examples
        ).status
        != "verified"
    )


@pytest.mark.parametrize("api", APIS)
@settings(max_examples=40, deadline=None)
@given(
    labels=st.sampled_from([("Date", "Time"), ("Phone", "Fax"), ("City", "State")]),
    separator=st.sampled_from(["/", "-", ":", ";"]),
)
def test_generic_label_separator_has_exact_role_owned_witness(api, labels, separator):
    prefix = "Ashford Jane 02/23/1975 RC79284"
    left, right = labels
    row = f"{prefix} {left} {separator} {right}"
    assert _check(api, row, row).status == "verified"
    for changed in ("", separator + " " + separator, "+"):
        assert (
            _check(api, row, f"{prefix} {left} {changed} {right}").status != "verified"
        )
    # A generic label elsewhere cannot lend ownership to a numeric span.
    ambiguous = f"{row} RC79 {separator} {separator} 284"
    assert _check(api, ambiguous, ambiguous).status == "abstain"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "example,value",
    [
        ("Ashford Jane", "Barnes Mike"),
        ("Ashford Jane", "Bennett"),
        ("Ashford", "Barnes Mike"),
    ],
)
@settings(max_examples=30, deadline=None)
@given(repeats=st.integers(min_value=1, max_value=3))
def test_exact_parameter_projection_preserves_order_roles_and_repeated_occurrences(
    api, example, value, repeats
):
    recorded = "Name: " + " Review ".join([example] * repeats) + " ID: RC.79284 Age: 47"
    observed = recorded.replace(example, value)
    examples, params = {"person": example}, {"person": value}
    if api == "plaintext":
        result = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
        wrong = I.verify_target_identity(
            recorded,
            observed.replace("RC.79284", "RC79284"),
            params=params,
            param_examples=examples,
        )
    else:
        template = _template(recorded, api, examples)
        result = verify_template_identity(
            template, observed, params=params, param_examples=examples
        )
        wrong = verify_template_identity(
            template,
            observed.replace("RC.79284", "RC79284"),
            params=params,
            param_examples=examples,
        )
    assert result.status == "verified"
    assert wrong.status != "verified"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("reverse", [False, True])
def test_two_disjoint_parameter_fields_share_one_projection(api, reverse):
    recorded = "First: Ashford Last: Jane ID: RC.79284 Age: 47"
    pairs = [("first", "Ashford"), ("last", "Jane")]
    if reverse:
        pairs.reverse()
    examples = dict(pairs)
    params = {"first": "Barnes Mike", "last": "Bennett"}
    observed = "First: Barnes Mike Last: Bennett ID: RC.79284 Age: 47"
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    assert check.status == "verified"


@pytest.mark.parametrize("api", APIS)
def test_overlapping_parameter_ownership_never_gains_authority(api):
    recorded = "Name: Ashford Jane ID: RC.79284 Age: 47"
    examples = {"person": "Ashford Jane", "first": "Ashford"}
    params = {"person": "Barnes Mike", "first": "Bennett"}
    observed = "Name: Barnes Mike ID: RC.79284 Age: 47"
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    assert check.status != "verified"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("contextual_punctuation", [False, True])
def test_ocr_fallback_only_keeps_position_independent_ownership(
    api, contextual_punctuation
):
    recorded = "Open chart for Phi1lip now"
    observed = "Open chart for Susan now"
    if contextual_punctuation:
        recorded = "Patient: Phi1lip Last: Ashford Age: 47"
        observed = "Patient: Susan Last: Ashford Age: 47"
    examples, params = {"person": "Phillip"}, {"person": "Susan"}
    assert I.embedded_params(recorded, examples) == ["person"]
    assert I.parameter_token_ownership(recorded, examples) == {
        "person": [1 if contextual_punctuation else 3]
    }
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    if contextual_punctuation:
        assert check.status == "abstain"
    else:
        assert check.status == "verified"


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
@pytest.mark.parametrize(
    "damage",
    ["absent", "unknown", "numeric", "identity", "wrong_label", "legacy_policy"],
)
def test_role_metadata_has_no_inferred_or_corrupt_positive_path(api, damage):
    text = "Ashford Jane 02/23/1975 RC79284 Date / Time"
    template = _template(text, api)
    raw = text.split()
    if damage == "legacy_policy":
        template = template.model_copy(
            update={"punctuation_policy": "strict-lexeme-punctuation-v1"}
        )
    else:
        index = raw.index("Date")
        value = None if damage == "absent" else "unknown"
        if damage == "numeric":
            index, value = raw.index("RC79284"), True
        elif damage == "identity":
            index, value = raw.index("Ashford"), True
        elif damage == "wrong_label":
            value = False
        tokens = list(template.tokens)
        tokens[index] = tokens[index].model_copy(update={"label": value})
        template = template.model_copy(update={"tokens": tokens})
    assert verify_template_identity(template, text).status == "abstain"
    assert _band_match_template(template, text).coverage == 0


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
def test_missing_serialized_role_metadata_is_not_favorably_reconstructed(api):
    text = "Ashford Jane 02/23/1975 RC79284 Date / Time"
    payload = _template(text, api).model_dump(mode="json")
    for token in payload["tokens"]:
        token.pop("label", None)
    restored = IdentityTemplate.model_validate(payload)
    assert verify_template_identity(restored, text).status == "abstain"
    assert _band_match_template(restored, text).coverage == 0


@pytest.mark.parametrize(
    "owned", [{"p": [0, 2]}, {"p": [0, 1], "q": [1]}, {"p": [True]}, {"p": [1, 0]}]
)
def test_projection_rejects_noncontiguous_overlapping_or_malformed_ownership(owned):
    assert (
        I.parameter_projection(
            ["alpha", "other", "beta"],
            owned,
            {"p": ["alpha", "beta"], "q": ["other"]},
            [False, False, False],
            {"p": False, "q": False},
        )
        is None
    )


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
def test_role_and_bearing_flags_share_the_exact_raw_comparison_pair(api):
    text = "Last: smith First: John ID: RC.79284 Date / Time Age: 47"
    template = _template(text, api)
    raw = text.split()
    words = I.tokenize(text)
    assert len(raw) == len(words) == len(template.tokens)
    assert [token.bearing for token in template.tokens] == [
        I.identity_bearing(original, word) for original, word in zip(raw, words)
    ]
    assert [token.label for token in template.tokens] == [
        I.generic_label_role(original, word) for original, word in zip(raw, words)
    ]
    assert verify_template_identity(template, text).status == "verified"


@pytest.mark.parametrize("invalid", ["true", 1, "unknown"])
def test_serialized_label_role_rejects_non_boolean_values(invalid):
    text = "Ashford Jane 02/23/1975 RC79284 Date / Time"
    payload = _template(text, "fresh_template").model_dump(mode="json")
    payload["tokens"][4]["label"] = invalid
    with pytest.raises(ValueError):
        IdentityTemplate.model_validate(payload)


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
def test_parameter_metadata_cannot_omit_a_duplicate_exact_occurrence(api):
    text = "Name: Ashford Jane Review Ashford Jane ID: RC.79284 Age: 47"
    examples = {"person": "Ashford Jane"}
    template = _template(text, api, examples)
    ownership = {"person": template.param_token_indices["person"][:2]}
    damaged = template.model_copy(update={"param_token_indices": ownership})
    observed = "Name: Barnes Mike Review Ashford Jane ID: RC.79284 Age: 47"
    assert (
        verify_template_identity(
            damaged, observed, params={"person": "Barnes Mike"}, param_examples=examples
        ).status
        == "abstain"
    )


@pytest.mark.parametrize("api", APIS)
def test_partial_token_overlap_cannot_hide_an_ambiguous_parameter(api):
    recorded = "Name: JoAnne ID: RC.79284 Age: 47"
    examples = {"person": "JoAnne", "suffix": "Anne"}
    params = {"person": "Bennett", "suffix": "Susan"}
    observed = "Name: Bennett ID: RC.79284 Age: 47"
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    assert check.status == "abstain"


@pytest.mark.parametrize("api", APIS)
def test_multiple_ocr_fallback_spans_are_explicitly_unsupported(api):
    recorded = "Open chart Phi1lip beside Wil1iam for Ashford Jane"
    examples = {"first": "Phillip", "second": "William"}
    params = {"first": "Susan", "second": "Bennett"}
    observed = "Open chart Susan beside Bennett for Ashford Jane"
    assert I.embedded_params(recorded, examples) == ["first", "second"]
    assert I.parameter_token_ownership(recorded, examples) == {
        "first": [2],
        "second": [4],
    }
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    assert check.status == "abstain"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("punctuation", ["", ","])
def test_mixed_exact_and_fallback_projection_never_invents_punctuation_ownership(
    api, punctuation
):
    recorded = "Open chart Phi1lip then Ashford with Jane 02/23/1975 RC79284"
    examples = {"first": "Phillip", "second": "Ashford"}
    params = {"first": "Susan", "second": "Bennett" + punctuation}
    observed = (
        f"Open chart Susan then Bennett{punctuation} with Jane 02/23/1975 RC79284"
    )
    assert I.embedded_params(recorded, examples) == ["first", "second"]
    assert I.parameter_token_ownership(recorded, examples) == {
        "first": [2],
        "second": [4],
    }
    if api == "plaintext":
        check = I.verify_target_identity(
            recorded, observed, params=params, param_examples=examples
        )
    else:
        check = verify_template_identity(
            _template(recorded, api, examples),
            observed,
            params=params,
            param_examples=examples,
        )
    assert check.status == ("abstain" if punctuation else "verified")


def _optional_bearing_weights(template, api, mode):
    tokens = []
    for index, token in enumerate(template.tokens):
        missing = (
            mode == "all"
            or (mode == "mixed" and index % 2 == 0)
            or (mode == "labels" and token.label is True)
        )
        tokens.append(token.model_copy(update={"bearing": None}) if missing else token)
    result = template.model_copy(update={"tokens": tokens})
    assert any(token.bearing is None for token in result.tokens)
    if api == "round_trip_template":
        result = IdentityTemplate.model_validate(result.model_dump(mode="json"))
    return result


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
@pytest.mark.parametrize("mode", ["all", "mixed", "labels"])
@pytest.mark.parametrize(
    "change", ["same", "missing_number", "changed_name", "changed_identifier"]
)
@settings(max_examples=12, deadline=None)
@given(padding=st.integers(min_value=1, max_value=5))
def test_optional_bearing_weights_never_let_labels_pad_identity(
    api, mode, change, padding
):
    text = "Smith John ID: RC.79284 Age: 47 " + " ".join([LABELS] * padding)
    observed = text
    if change == "missing_number":
        observed = text.replace("47", "")
    elif change == "changed_name":
        observed = text.replace("John", "James")
    elif change == "changed_identifier":
        observed = text.replace("RC.79284", "RC79284")
    template = _optional_bearing_weights(_template(text, "fresh_template"), api, mode)
    check = verify_template_identity(template, observed)
    if change == "same":
        assert check.status == "verified"
    else:
        assert check.status in ("mismatch", "abstain")


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
@pytest.mark.parametrize("mode", ["all", "mixed", "labels"])
def test_optional_bearing_weights_cannot_make_label_only_identity(api, mode):
    text = "Last Name First Name Phone Address City State"
    template = _template(text, "fresh_template")
    assert all(token.label is True for token in template.tokens)
    template = _optional_bearing_weights(template, api, mode)
    match = _band_match_template(template, text)
    assert match.coverage == 1
    assert match.identity_evidence_short
    assert verify_template_identity(template, text).status == "abstain"


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
@pytest.mark.parametrize("mode", ["all", "mixed", "labels"])
def test_optional_bearing_weights_keep_parameter_projection_and_strict_identifiers(
    api, mode
):
    text = "Last: smith First: John ID: RC.79284 Age: 47"
    examples, params = {"last": "smith"}, {"last": "jones"}
    template = _optional_bearing_weights(
        _template(text, "fresh_template", examples), api, mode
    )
    observed = text.replace("smith", "jones")
    assert (
        verify_template_identity(
            template, observed, params=params, param_examples=examples
        ).status
        == "verified"
    )
    assert verify_template_identity(
        template,
        observed.replace("RC.79284", "RC79284"),
        params=params,
        param_examples=examples,
    ).status in ("mismatch", "abstain")


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_label",
        "unknown_label",
        "wrong_label",
        "numeric_label",
        "legacy_policy",
        "unknown_policy",
    ],
)
def test_optional_bearing_weights_never_supply_missing_label_authority(api, damage):
    text = "Smith John 15 47 Last Name"
    template = _optional_bearing_weights(_template(text, "fresh_template"), api, "all")
    if damage in ("legacy_policy", "unknown_policy"):
        policy = (
            "strict-lexeme-punctuation-v1" if damage == "legacy_policy" else "unknown"
        )
        template = template.model_copy(update={"punctuation_policy": policy})
    else:
        index = 2 if damage == "numeric_label" else 4
        value = {
            "missing_label": None,
            "unknown_label": "unknown",
            "wrong_label": False,
            "numeric_label": True,
        }[damage]
        tokens = list(template.tokens)
        tokens[index] = tokens[index].model_copy(update={"label": value})
        template = template.model_copy(update={"tokens": tokens})
    assert _band_match_template(template, text).coverage == 0
    assert verify_template_identity(template, text).status == "abstain"


@pytest.mark.parametrize("invalid", [0, 1, "false", "true", "unknown"])
def test_optional_bearing_weights_reject_corrupt_values_before_authority(invalid):
    text = "Smith John 15 47 Last Name"
    template = _template(text, "fresh_template")
    payload = template.model_dump(mode="json")
    payload["tokens"][0]["bearing"] = invalid
    with pytest.raises(ValueError):
        IdentityTemplate.model_validate(payload)
    tokens = list(template.tokens)
    tokens[0] = tokens[0].model_copy(update={"bearing": invalid})
    malformed = template.model_copy(update={"tokens": tokens})
    assert _band_match_template(malformed, text).coverage == 0
    assert verify_template_identity(malformed, text).status == "abstain"


@pytest.mark.parametrize("api", ["fresh_template", "round_trip_template"])
def test_optional_bearing_weights_do_not_waive_known_label_contradictions(api):
    text = "Smith John 15 47 Last Name"
    template = _optional_bearing_weights(_template(text, "fresh_template"), api, "all")
    tokens = list(template.tokens)
    tokens[4] = tokens[4].model_copy(update={"bearing": True})
    malformed = template.model_copy(update={"tokens": tokens})
    assert _band_match_template(malformed, text).coverage == 0
    assert verify_template_identity(malformed, text).status == "abstain"
