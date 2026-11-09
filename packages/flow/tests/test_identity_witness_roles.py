"""Diagnostic/authority separation and finite punctuation ownership witnesses.

Synthetic software controls, not real-app qualification or arbitrary ID parsing.
"""

from __future__ import annotations

import copy
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    _band_match_template,
    build_identity_template,
    verify_template_identity,
)


def _check(api: str, expected: str, observed: str):
    if api == "plaintext":
        return I.verify_target_identity(expected, observed)
    template = build_identity_template(expected)
    assert template is not None
    return verify_template_identity(template, observed)


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("identifier", ["", " 222"])
@settings(max_examples=40, deadline=None)
@given(name=st.sampled_from(["Aaaa", "Ashford", "Bennett", "Hawthorne"]))
def test_unchanged_repeated_name_has_complete_ordered_witness(api, identifier, name):
    row = f"{name}, {name} 01/01/1900{identifier}"
    assert _check(api, row, row).status == "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@settings(max_examples=40, deadline=None)
@given(name=st.sampled_from(["Aaaa", "Ashford", "Bennett", "Hawthorne"]))
def test_repeated_name_cannot_reassign_its_punctuation(api, reverse, name):
    original = f"{name}, {name} 01/01/1900 222"
    moved = f"{name} {name}, 01/01/1900 222"
    expected, observed = (moved, original) if reverse else (original, moved)
    assert _check(api, expected, observed).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("punctuation", [".", ",", "..", ",."])
def test_short_repeated_prefix_has_no_ordered_name_exception(api, reverse, punctuation):
    original = f"Ashford Jane R R{punctuation} R 222 02/23/1975"
    moved = f"Ashford Jane R{punctuation} R R 222 02/23/1975"
    expected, observed = (moved, original) if reverse else (original, moved)
    assert _check(api, expected, observed).status != "verified"
    assert _check(api, original, original).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
def test_text_only_permutation_preserves_exact_punctuation_multiset(api, reverse):
    original = "PPV + Show All <Back to Patient ShowActive"
    permuted = "<Back to Patient PPV + Show All Show Active"
    expected, observed = (permuted, original) if reverse else (original, permuted)
    assert _check(api, expected, observed).status == "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("replacement", ["", "+ +", "-", "."])
def test_text_only_permutation_never_erases_or_reuses_punctuation(
    api, reverse, replacement
):
    original = "PPV + Show All <Back to Patient ShowActive"
    changed = f"<Back to Patient PPV {replacement} Show All Show Active"
    expected, observed = (changed, original) if reverse else (original, changed)
    assert _check(api, expected, observed).status != "verified"


@pytest.mark.parametrize("api", ["plaintext", "template"])
@pytest.mark.parametrize("reverse", [False, True])
def test_numeric_band_cannot_borrow_text_only_standalone_permission(api, reverse):
    original = "Ashford Jane RC79 . . 284 02/23/1975"
    changed = "Ashford Jane RC79 . 284 02/23/1975"
    expected, observed = (changed, original) if reverse else (original, changed)
    assert _check(api, expected, observed).status != "verified"
    assert _check(api, original, original).status != "verified"


def test_legacy_diagnostics_never_become_verdict_or_budget_authority():
    original = "100512 Smith. John 01/15/1980 Active"
    changed = "100512 Smith, John 01/15/1980 Active"
    match = I.band_match(original, changed)
    assert match.coverage == 1.0
    assert match.contradicted_chars == 0
    assert match.unexplained_name_tokens == 0
    assert I.band_verdict(match) == "mismatch"
    assert not I._band_ok_sans_glyph(match)
    assert not I._band_ok(match)
    assert I.band_abstain_reason(match) is None
    assert len(match) == 8
    assert I.band_match("ShowActive", "Show Active") == (1.0, 0, 0, 0, 0, 0, 0, False)


@pytest.mark.parametrize(
    "conversion",
    ["tuple", "list", "json", "shallow", "deep", "attribute_lost", "make", "replace"],
)
def test_diagnostic_copy_or_attribute_loss_preserves_strict_tuple_evidence(conversion):
    match = I.band_match(
        "100512 Smith. John 01/15/1980 Active",
        "100512 Smith, John 01/15/1980 Active",
    )
    assert match.coverage == 1.0
    if conversion == "tuple":
        restored = I.BandMatch(*tuple(match))
    elif conversion == "list":
        restored = I.BandMatch(*list(match))
    elif conversion == "json":
        restored = I.BandMatch(*json.loads(json.dumps(match)))
    elif conversion == "make":
        restored = type(match)._make(match)
        assert not hasattr(restored, "diagnostic_match")
    elif conversion == "replace":
        restored = match._replace()
        assert not hasattr(restored, "diagnostic_match")
    elif conversion == "shallow":
        restored = copy.copy(match)
    elif conversion == "deep":
        restored = copy.deepcopy(match)
    else:
        restored = copy.copy(match)
        del restored.diagnostic_match
    assert I.band_verdict(restored) == "mismatch"
    assert not I._band_ok(restored)
    assert not I._band_ok_sans_glyph(restored)


def test_unknown_diagnostic_carrier_cannot_supply_positive_evidence():
    class UnknownCarrier(I.BandMatch):
        pass

    unknown = UnknownCarrier(1.0, 0, 0)
    assert I.band_verdict(unknown) != "verified"
    assert not I._band_ok(unknown)


@settings(max_examples=60, deadline=None)
@given(
    operations=st.lists(
        st.sampled_from(["tuple", "json", "copy"]), min_size=1, max_size=12
    )
)
def test_repeated_transport_transitions_cannot_promote_diagnostics(operations):
    match = I.band_match(
        "100512 Smith. John 01/15/1980 Active",
        "100512 Smith, John 01/15/1980 Active",
    )
    for operation in operations:
        if operation == "tuple":
            match = I.BandMatch(*tuple(match))
        elif operation == "json":
            match = I.BandMatch(*json.loads(json.dumps(match)))
        else:
            match = copy.deepcopy(match)
        assert I.band_verdict(match) == "mismatch"
        assert not I._band_ok(match)


def test_hashed_repair_matcher_contains_strict_metrics_only():
    template = build_identity_template("100512 Smith. John 01/15/1980 Active")
    assert template is not None
    match = _band_match_template(template, "100512 Smith, John 01/15/1980 Active")
    assert type(match) is I.BandMatch
    assert I.band_verdict(match) == "mismatch"
    assert match.coverage < 1.0 or match.contradicted_chars > 0
