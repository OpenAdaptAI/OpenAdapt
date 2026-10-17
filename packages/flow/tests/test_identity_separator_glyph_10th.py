"""10th wrong-patient reopening: SEPARATOR-formatted collapsible MRNs.

An adversarial review found the glyph-abstain gate exempted any identifier
carrying a separator: ``_is_identifier_shaped`` ended with ``token.isalnum()``,
which is False for ``MG-4408``. So a same-NAME / same-DOB homonym whose only
discriminator is a DASH-formatted MRN differing by one O/0 glyph
(``MG-4408`` vs ``MG-44O8`` -> OCR collapses to the byte-identical band
``MG-4408``) VERIFIED instead of abstaining -- the exact wrong-patient
false-accept the 8th/9th fixes closed for bare MRNs, reopened by one separator.

The gate now strips intra-identifier separators before judging, excluding only
date-shaped tokens (so a DOB does not become a gated identifier and over-halt
every band). These tests pin: separator collapsible MRNs ABSTAIN; clean
separator MRNs still VERIFY; dates never gate.
"""

from __future__ import annotations

from openadapt_flow.runtime import identity as I

# Same name + same DOB on both sides: the ONLY discriminator is the MRN.
_NAME_DOB = "Smith, John 01/15/1980 "


def _verify(mrn: str) -> str:
    band = _NAME_DOB + mrn
    return I.verify_target_identity(band, band).status


# ---------------------------------------------------------------------------
# The reopening: dash/slash-formatted collapsible MRNs must ABSTAIN.
# ---------------------------------------------------------------------------


def test_dashed_o0_mrn_abstains():
    # MG-4408 (recorded) vs MG-44O8 (a DIFFERENT patient) OCR-collapse to the
    # same band. OCR cannot rule out the homonym -> ABSTAIN (never verify).
    assert _verify("MG-4408") == "abstain"


def test_multi_segment_and_numeric_separator_mrns_abstain():
    for mrn in ("AC-50-061", "1OO-512", "AC/50/061", "123-45-0O1"):
        assert _verify(mrn) == "abstain", mrn


def test_lowercase_l1_dashed_mrn_abstains():
    assert _verify("ab-l408") == "abstain"


# ---------------------------------------------------------------------------
# No over-halt: a CLEAN separator MRN (no O/0/l/1/I glyph) still VERIFIES.
# ---------------------------------------------------------------------------


def test_clean_dashed_mrn_still_verifies():
    for mrn in ("RC79284", "AC-79-284", "MG-4478"):
        assert _verify(mrn) == "verified", mrn


# ---------------------------------------------------------------------------
# Dates never become gated identifiers (else every DOB would over-halt).
# ---------------------------------------------------------------------------


def test_dob_does_not_gate_the_band():
    # The band carries a DOB (01/15/1980) AND a clean MRN -> verifies.
    assert _verify("RC79284") == "verified"


def test_is_date_like_classification():
    for t in ("01/15/1980", "1980-01-15", "15.01.1980", "0l/l5/l980"):
        assert I._is_date_like(t) is True, t
    for t in ("MG-4408", "123-45-6789", "RC79284", "Active"):
        assert I._is_date_like(t) is False, t


def test_is_identifier_shaped_includes_separator_mrn_excludes_dates():
    assert I._is_identifier_shaped("MG-4408") is True
    assert I._is_identifier_shaped("123-45-6789") is True
    assert I._is_identifier_shaped("01/15/1980") is False  # a date
    assert I._is_identifier_shaped("Active") is False  # a name/word
