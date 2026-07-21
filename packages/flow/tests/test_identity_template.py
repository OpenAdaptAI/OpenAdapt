"""PHI-free identity template (audit REM-2): the wrong-patient guard still
works from a salted-hash template, with NO plaintext identifier in the artifact.

Three things are proven here:

1. **Parity + safety invariant.** For a corpus spanning every adversarial class
   the plaintext matcher is tuned against (right row, replaced name, near-name
   sibling, changed DOB/MRN, middle initial, generational suffix, glyph-collapse
   homonym, param re-anchor), the template verdict either EQUALS the plaintext
   verdict or is STRICTER — it NEVER turns a plaintext ``mismatch``/``abstain``
   into ``verified`` (the never-false-accept invariant). In practice it matches
   exactly on all these cases.
2. **Right row verifies, wrong row refuses — from the hash alone.** The template
   contains no readable identifier (asserted by grepping the serialized JSON),
   yet the correct row verifies and a different patient does not.
3. **Param re-anchor stores no demonstrated identifier**, and the structured
   tier is exact-match over the hash.

All patient strings here are synthetic (MockMed-style fakes), never real PHI.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    token_in_template,
    verify_structured_template,
    verify_template_identity,
)

# (recorded_band, observed_band) pairs spanning the guard's adversarial classes.
# Non-param cases; identifiers use fake, glyph-clean values unless the case is
# specifically about a glyph-collapse.
_CORPUS = [
    # right row (clean, non-confusable identifier RC79284)
    (
        "Ashford Jane 02/23/1975 RC79284 Active",
        "Ashford Jane 02/23/1975 RC79284 Active",
    ),
    # replaced name (different patient)
    ("Ashford Jane 02/23/1975 RC79284 Active", "Barnes Mike 04/28/1962 RC33847 Active"),
    # near-name sibling, short (Ted/Tad)
    ("Ted Ashford 02/23/1975 RC79284", "Tad Ashford 02/23/1975 RC79284"),
    # near-name extension (Phil/Philip)
    ("Phil Ashford 02/23/1975 RC79284", "Philip Ashford 02/23/1975 RC79284"),
    # changed DOB (one field edit)
    ("Ashford Jane 02/23/1975 RC79284", "Ashford Jane 02/23/1976 RC79284"),
    # changed MRN body (clean digits, different account)
    ("Ashford Jane 02/23/1975 RC79284", "Ashford Jane 02/23/1975 RC79285"),
    # middle-initial replacement
    ("Frank R Ashford 02/23/1975 RC79284", "Frank K Ashford 02/23/1975 RC79284"),
    # generational suffix appears on the observed side
    ("Phil Ashford 02/23/1975 RC79284", "Phil Ashford Jr 02/23/1975 RC79284"),
    # glyph-collapse homonym: recorded MRN carries a confusable 0, observed
    # reads byte-identically (a same-name/same-DOB homonym cannot be ruled out).
    ("Ashford Jane 02/23/1975 MG40081", "Ashford Jane 02/23/1975 MG40081"),
    # unreadable live band
    ("Ashford Jane 02/23/1975 RC79284 Active", ""),
    # OCR jitter on a clean true row (l/i confusion in a name)
    ("Bail Jonathan 02/23/1975 RC79284", "Ball Jonathan 02/23/1975 RC79284"),
]

_SAFE_FALLBACK = {"mismatch", "abstain", "unreadable"}


@pytest.mark.parametrize("recorded,observed", _CORPUS)
def test_template_parity_and_safety(recorded: str, observed: str) -> None:
    tmpl = build_identity_template(recorded, param_examples={})
    assert tmpl is not None
    plain = I.verify_target_identity(recorded, observed, params={}, param_examples={})
    tpl = verify_template_identity(tmpl, observed, params={}, param_examples={})
    if plain.status == "verified":
        # A true row the plaintext matcher verifies must still verify from the
        # template (no availability regression on the happy path).
        assert tpl.status == "verified", (recorded, observed, plain.status, tpl.status)
    else:
        # A refused row must never become verified via the template
        # (never-false-accept). Template may only be as strict or stricter.
        assert tpl.status in _SAFE_FALLBACK, (recorded, observed, tpl.status)


def test_right_row_verifies_wrong_row_refuses_from_hash_only() -> None:
    band = "Kramer Susan 07/14/1983 RC73842 Active High"
    tmpl = build_identity_template(band, param_examples={})
    assert tmpl is not None
    # No plaintext identifier survives in the serialized artifact.
    blob = tmpl.model_dump_json()
    for needle in ("Kramer", "Susan", "07/14/1983", "RC73842", "73842"):
        assert needle not in blob, needle
    # Right row verifies.
    assert (
        verify_template_identity(tmpl, band, params={}, param_examples={}).status
        == "verified"
    )
    # A different patient (same layout) is refused.
    wrong = "Delgado Omar 11/02/1959 RC29655 Active High"
    assert (
        verify_template_identity(tmpl, wrong, params={}, param_examples={}).status
        == "mismatch"
    )


def test_param_mode_stores_no_demonstrated_identifier() -> None:
    # The patient name is a workflow parameter: its demonstrated value must not
    # be stored, and identity re-anchors on the RUN's value.
    band = "Ashford Jane 02/23/1975 RC79284 Active"
    tmpl = build_identity_template(band, param_examples={"patient": "Ashford Jane"})
    assert tmpl is not None
    assert "patient" in tmpl.param_token_indices
    blob = tmpl.model_dump_json()
    assert "Ashford" not in blob and "Jane" not in blob
    # Run against a DIFFERENT patient's row: identity should re-anchor on the
    # run value and refuse when the run's patient is not the row shown.
    got = verify_template_identity(
        tmpl,
        "Barnes Mike 04/28/1962 RC33847 Active",
        params={"patient": "Barnes Mike"},
        param_examples={"patient": "Ashford Jane"},
    )
    plain = I.verify_target_identity(
        band,
        "Barnes Mike 04/28/1962 RC33847 Active",
        params={"patient": "Barnes Mike"},
        param_examples={"patient": "Ashford Jane"},
    )
    assert got.status == plain.status  # residue (DOB/MRN) differs -> mismatch
    assert got.mode == "param"


def test_param_mode_ignores_shared_token_from_unembedded_longer_param() -> None:
    """Template ownership matches the plaintext whole-value embedding gate."""
    band = "Name OpenAdapt Middle Name Last Name"
    examples = {
        "fname": "OpenAdapt",
        "email": "openadapt.loan-parity@example.invalid",
    }
    tmpl = build_identity_template(band, param_examples=examples)
    assert tmpl is not None
    assert set(tmpl.param_token_indices) == {"fname"}

    params = {**examples, "fname": "ChangedName"}
    observed = "Name ChangedName Middle Name Last Name"
    plain = I.verify_target_identity(
        band, observed, params=params, param_examples=examples
    )
    got = verify_template_identity(
        tmpl, observed, params=params, param_examples=examples
    )
    assert got.status == plain.status == "verified"
    assert got.mode == plain.mode == "param"


def test_param_span_does_not_drop_separate_mrn_substring() -> None:
    """A parameter may not claim identity tokens outside its exact occurrence."""
    band = "MRN 123456 Email patient123456@example.invalid Active"
    examples = {"email": "patient123456@example.invalid"}
    tmpl = build_identity_template(band, param_examples=examples)
    assert tmpl is not None
    # Only the exact email token is parameter-owned; the independent MRN stays
    # in the hashed residue and must still reject a wrong record.
    assert tmpl.param_token_indices["email"] == [3]

    params = {"email": "changed@example.invalid"}
    observed = "MRN 654321 Email changed@example.invalid Active"
    plain = I.verify_target_identity(
        band, observed, params=params, param_examples=examples
    )
    got = verify_template_identity(
        tmpl, observed, params=params, param_examples=examples
    )
    assert plain.status == "mismatch"
    assert got.status == plain.status


def test_partial_token_parameter_is_left_hashed_not_dropped() -> None:
    """Prefix/suffix identity cannot be discarded by an inner param match."""
    band = "Account ID=234567X Status Active"
    examples = {"account": "234567"}
    tmpl = build_identity_template(band, param_examples=examples)
    assert tmpl is not None
    assert "account" not in tmpl.param_token_indices

    params = {"account": "876543"}
    observed = "Account ID=876543Y Status Active 876543"
    plain = I.verify_target_identity(
        band, observed, params=params, param_examples=examples
    )
    got = verify_template_identity(
        tmpl, observed, params=params, param_examples=examples
    )
    assert plain.status == "mismatch"
    assert got.status != "verified"


def test_param_substitution_preserves_untouched_split_concat_windows() -> None:
    """OCR-glued residue labels remain matchable after a param is replaced."""
    band = "Postal Code: 02139 Country: Add"
    examples = {"postal_code": "02139"}
    tmpl = build_identity_template(band, param_examples=examples)
    assert tmpl is not None

    observed = "PostalCode: 02139 Country: Add"
    plain = I.verify_target_identity(
        band,
        observed,
        params=examples,
        param_examples=examples,
    )
    got = verify_template_identity(
        tmpl,
        observed,
        params=examples,
        param_examples=examples,
    )

    # The identifier is glyph-confusable, so the conservative final verdict is
    # abstain. The regression is that the glued static label no longer causes
    # an earlier false mismatch after parameter substitution.
    assert plain.status == "abstain"
    assert got.status == plain.status
    assert got.coverage == 1.0


def test_structured_tier_is_exact_match_over_hash() -> None:
    struct = "MG4408 Okafor, Philip 1966-01-17 M Active"
    tmpl = build_identity_template(None, structured_identity=struct)
    assert tmpl is not None and tmpl.structured is not None
    assert struct not in tmpl.model_dump_json()
    assert verify_structured_template(tmpl, struct).status == "verified"
    # One-glyph MRN difference is a different patient in the DOM/a11y tree.
    assert (
        verify_structured_template(
            tmpl, "MG44O8 Okafor, Philip 1966-01-17 M Active"
        ).status
        == "mismatch"
    )
    # Tier unavailable when there is no live structured text.
    assert verify_structured_template(tmpl, None) is None


def test_external_salt_keeps_salt_out_of_bundle(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_IDENTITY_SALT", "shared-secret-pepper")
    band = "Ashford Jane 02/23/1975 RC79284 Active"
    tmpl = build_identity_template(band, param_examples={})
    assert tmpl is not None
    # With an external salt, nothing salt-bearing is persisted in the bundle.
    assert tmpl.salt == ""
    # Verification still works because replay reads the same env salt.
    assert (
        verify_template_identity(tmpl, band, params={}, param_examples={}).status
        == "verified"
    )
    # token_in_template also honors the env salt.
    assert token_in_template(tmpl, "ashford")


def test_too_short_band_is_unreadable_not_stored() -> None:
    # A generic, too-short band is not discriminative; build returns None (never
    # stored) and verification treats a short template as unreadable.
    tmpl = build_identity_template("High 3", param_examples={})
    assert tmpl is None
