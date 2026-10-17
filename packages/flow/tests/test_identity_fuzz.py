"""Property-based (fuzz) testing of the identity gate's NEVER-FALSE-ACCEPT
invariant.

The separator-formatted collapsible-MRN P0 (the 10th wrong-patient reopening;
see :mod:`tests.test_identity_separator_glyph_10th`) slipped because the FROZEN
adversary corpora enumerate hand-picked shapes and simply did not contain a
dash/slash-formatted MRN. Enumerating cases can only ever pin the shapes someone
already thought of. This module instead SEARCHES the space of
collapsible-identifier homonyms with Hypothesis and asserts the invariant that
must hold for EVERY generated input:

    A same-NAME / same-DOB pair whose only difference is an OCR-collapsible
    identifier glyph must NEVER return ``verified`` — it must abstain (OCR
    cannot certify) or mismatch (affirmatively a different entity), but never a
    wrong-patient VERIFY.

Band format is "Lastname, First DOB MRN" (see
:mod:`tests.test_identity_separator_glyph_10th`). Both bands in a pair carry the
SAME fabricated name+DOB, so name/DOB always raw-match; the ONLY thing that
varies — and that must never license a VERIFY — is the identifier glyph.

Two collapse mechanisms, asserted separately (the prompt's precision point):

* **Byte-identical collapse (O/0 and l/1/I).** RapidOCR reads these class
  members as the SAME byte string, so a homonym differing only here is
  INDISTINGUISHABLE post-OCR: the recorded and live bands are literally
  identical. The gate cannot honestly certify SAME, so it must ABSTAIN. Modeled
  by feeding recorded == live and asserting ``status == "abstain"`` — there is
  no legitimate third outcome for an identical band, so a shape the code fails
  to flag would surface as ``verified`` (the P0).

* **Distinct-glyph / raw-different sibling (o0, l1i AND s5, z2, b8, g9).** These
  are read DISTINCTLY by OCR (recorded='...s...', live='...5...'): the strings
  differ but canonicalize equal. The matcher must catch this via the
  ``_suspicious_pair`` mechanism (canonical-equal, raw-unequal identifier ->
  MISMATCH). Asserted as ``status == "mismatch"``.

Crucially, the properties do NOT gate their inputs on the code-under-test's own
``_is_glyph_vulnerable_identifier`` / ``_is_identifier_shaped`` predicates:
collapsibility is established BY CONSTRUCTION (a genuine OCR-confusable glyph in
a realistic MRN shape). That is the whole point — the separator P0 was exactly a
shape the predicate wrongly excluded, so gating on the predicate would filter
out the very bug class. A shape the code misclassifies surfaces as a shrunk
counterexample.

Two positive (no-over-halt) properties guard the opposite failure — that the
gate is not trivially "safe" by abstaining on everything: a CLEAN identifier and
a fuzzed DOB, with matching name+DOB, must VERIFY.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from openadapt_flow.runtime import identity as I

# Heavy fuzz search; matches the repo convention of a generous timeout marker
# for slow property/integration tests.
pytestmark = pytest.mark.timeout(600)

# Thorough but CI-friendly: ~400 examples across the safety properties.
_MAX_SAFETY = 400
_MAX_POSITIVE = 300

_COMMON_SETTINGS = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# OCR-confusable glyphs that keep a token ALPHANUMERIC (so it stays an
# identifier). '|' and '!' are homoglyph shapes too, but they are not
# alphanumeric, so a token bearing them is not identifier-shaped; excluded here.
_BYTE_COLLAPSE_GLYPHS = "Oo0lI1"  # O/o/0 and l/I/1 — read byte-identically

# (letter, paired digit) across BOTH byte-collapse and the DISTINCT confusion
# classes the matcher canonicalizes. recorded uses the letter, live the digit:
# canonically equal, raw-unequal — the wrong-patient homonym the gate must
# refuse via _suspicious_pair.
_SIBLING_PAIRS = (
    ("O", "0"),
    ("o", "0"),
    ("l", "1"),
    ("I", "1"),
    ("s", "5"),
    ("z", "2"),
    ("b", "8"),
    ("g", "9"),
)

# CLEAN alphabets: exclude every homoglyph letter/digit so a "clean" identifier
# is provably NOT glyph-vulnerable and must verify (no-over-halt property).
_CLEAN_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I, O, L
_CLEAN_DIGITS = "23456789"  # no 0, 1


# --------------------------------------------------------------------------- #
# Shared name + DOB strategies (identical on both sides of every pair)
# --------------------------------------------------------------------------- #
@st.composite
def _name(draw) -> tuple[str, str]:
    """A discriminative (surname, first) — >=4 alpha chars, not a generic
    record-list column word — so name/DOB genuinely carry identity."""
    last = draw(st.text(alphabet=string.ascii_lowercase, min_size=4, max_size=8))
    first = draw(st.text(alphabet=string.ascii_lowercase, min_size=4, max_size=7))
    last, first = last.capitalize(), first.capitalize()
    assume(I._is_discriminative_name(I.squash(last)))
    assume(I._is_discriminative_name(I.squash(first)))
    return last, first


@st.composite
def _dob(draw) -> str:
    """A fuzzed DOB in a real 3-segment date format (M/D/Y, Y/M/D or D/M/Y)."""
    y = draw(st.integers(min_value=1900, max_value=2015))
    m = draw(st.integers(min_value=1, max_value=12))
    d = draw(st.integers(min_value=1, max_value=28))  # always a valid day
    sep = draw(st.sampled_from(("/", "-", ".")))
    order = draw(st.sampled_from(("mdy", "ymd", "dmy")))
    ys, ms, ds = f"{y:04d}", f"{m:02d}", f"{d:02d}"
    parts = {"mdy": (ms, ds, ys), "ymd": (ys, ms, ds), "dmy": (ds, ms, ys)}[order]
    return sep.join(parts)


@st.composite
def _formatter(draw):
    """A formatting transform (case + separators) drawn ONCE and applied to
    BOTH cores of a sibling pair, so aligned cores stay aligned."""
    case_mode = draw(st.sampled_from(("none", "lower", "upper")))
    sep = draw(st.sampled_from(("", "", "-", "/", ".")))  # bias toward bare
    cut = draw(st.integers(min_value=1, max_value=15))

    def fmt(core: str) -> str:
        s = core
        if case_mode == "lower":
            s = s.lower()
        elif case_mode == "upper":
            s = s.upper()
        if sep and len(s) >= 4:
            c = min(cut, len(s) - 1)
            s = s[:c] + sep + s[c:]
        return s

    return fmt


def _band(name: tuple[str, str], dob: str, mrn: str) -> str:
    last, first = name
    return f"{last}, {first} {dob} {mrn}"


# --------------------------------------------------------------------------- #
# Identifier strategies (realistic MRN shapes: alpha prefix + digit body)
# --------------------------------------------------------------------------- #
@st.composite
def _byte_collapse_mrn(draw) -> str:
    """A realistic MRN carrying at least one byte-collapsible glyph (O/o/0 or
    l/I/1), formatted (bare/hyphen/slash/dot, mixed case). The digit body
    guarantees an IDENTIFIER position (a name carries no digit)."""
    prefix = draw(st.text(alphabet=string.ascii_letters, min_size=0, max_size=3))
    body = draw(st.text(alphabet=string.digits, min_size=3, max_size=8))
    glyph = draw(st.sampled_from(_BYTE_COLLAPSE_GLYPHS))
    core = prefix + body
    pos = draw(st.integers(min_value=0, max_value=len(core)))
    core = (core[:pos] + glyph + core[pos:])[:12]
    fmt = draw(_formatter())
    return fmt(core)


@st.composite
def _sibling_mrns(draw) -> tuple[str, str]:
    """A (recorded, live) MRN pair identical except at ONE collapse position,
    where recorded holds a letter and live its paired digit — canonically
    equal, raw-unequal. Digit body (kept intact) guarantees the recorded token
    reads as an IDENTIFIER."""
    prefix = draw(st.text(alphabet=string.ascii_letters, min_size=0, max_size=3))
    body = draw(st.text(alphabet=string.digits, min_size=3, max_size=8))
    letter, digit = draw(st.sampled_from(_SIBLING_PAIRS))
    core = prefix + body
    pos = draw(st.integers(min_value=0, max_value=len(core)))
    rec_core = (core[:pos] + letter + core[pos:])[:12]
    live_core = (core[:pos] + digit + core[pos:])[:12]
    fmt = draw(_formatter())
    return fmt(rec_core), fmt(live_core)


@st.composite
def _clean_mrn(draw) -> str:
    """A CLEAN identifier: no homoglyph letter or digit anywhere, so it is
    provably not glyph-vulnerable and must verify (no-over-halt)."""
    prefix = draw(st.text(alphabet=_CLEAN_LETTERS, min_size=0, max_size=3))
    body = draw(st.text(alphabet=_CLEAN_DIGITS, min_size=3, max_size=8))
    fmt = draw(_formatter())
    return fmt(prefix + body)


# --------------------------------------------------------------------------- #
# Property 1 — the core safety invariant (P0-class): never a false accept.
# --------------------------------------------------------------------------- #
@settings(max_examples=_MAX_SAFETY, **_COMMON_SETTINGS)
@given(name=_name(), dob=_dob(), mrn=_byte_collapse_mrn())
def test_byte_identical_collapsible_mrn_never_verifies(name, dob, mrn):
    """O/0 and l/1/I collapse. Recorded and live bands are BYTE-IDENTICAL (OCR
    read the confusable glyph the same on a patient and a same-name/same-DOB
    homonym). The gate cannot certify SAME -> must ABSTAIN, never VERIFY."""
    assume(not I._is_date_like(mrn))  # dates are property 3's domain, not gated
    band = _band(name, dob, mrn)
    status = I.verify_target_identity(band, band).status
    assert status != "verified", (
        f"WRONG-PATIENT FALSE-ACCEPT: mrn={mrn!r} band={band!r}"
    )
    # For an identical band the only correct verdict is abstain (no affirmative
    # difference budget can fire); the mechanism check strengthens the guard.
    assert status == "abstain", f"expected abstain, got {status!r} for mrn={mrn!r}"


@settings(max_examples=_MAX_SAFETY, **_COMMON_SETTINGS)
@given(name=_name(), dob=_dob(), mrns=_sibling_mrns())
def test_raw_different_collapsible_sibling_never_verifies(name, dob, mrns):
    """Distinct-glyph classes (and the raw-different reading of O/0, l/1): the
    recorded and live identifiers differ by one confusable char but canonicalize
    equal. The matcher must treat a confusion-only identifier match as
    affirmative different-identifier evidence -> MISMATCH, never VERIFY."""
    rec_mrn, live_mrn = mrns
    assume(rec_mrn != live_mrn)
    assume(not I._is_date_like(rec_mrn) and not I._is_date_like(live_mrn))
    rec = _band(name, dob, rec_mrn)
    live = _band(name, dob, live_mrn)
    status = I.verify_target_identity(rec, live).status
    assert status != "verified", (
        f"WRONG-PATIENT FALSE-ACCEPT: recorded={rec_mrn!r} live={live_mrn!r} "
        f"rec_band={rec!r} live_band={live!r}"
    )
    assert status == "mismatch", (
        f"expected mismatch, got {status!r} for recorded={rec_mrn!r} live={live_mrn!r}"
    )


# --------------------------------------------------------------------------- #
# Property 2 — no over-halt: a CLEAN identifier + matching name/DOB VERIFIES.
# --------------------------------------------------------------------------- #
@settings(max_examples=_MAX_POSITIVE, **_COMMON_SETTINGS)
@given(name=_name(), dob=_dob(), mrn=_clean_mrn())
def test_clean_identifier_verifies(name, dob, mrn):
    """The gate must not be trivially safe by abstaining on everything: a clean
    (non-confusable) identifier with a matching name+DOB must VERIFY."""
    band = _band(name, dob, mrn)
    status = I.verify_target_identity(band, band).status
    assert status == "verified", f"spurious non-verify {status!r} for clean mrn={mrn!r}"


# --------------------------------------------------------------------------- #
# Property 3 — date exclusion: a DOB (a separator-bearing token) never gates.
# --------------------------------------------------------------------------- #
@settings(max_examples=_MAX_POSITIVE, **_COMMON_SETTINGS)
@given(name=_name(), dob=_dob(), mrn=_clean_mrn())
def test_dob_with_clean_mrn_does_not_over_halt(name, dob, mrn):
    """A band carrying a fuzzed DOB plus a clean MRN must still VERIFY — dates
    carry the same '/'/'-' separators as MRNs but must never be mistaken for a
    glyph-gated identifier (else every DOB would abstain)."""
    band = _band(name, dob, mrn)
    assert I.verify_target_identity(band, band).status == "verified"


@settings(max_examples=_MAX_POSITIVE, **_COMMON_SETTINGS)
@given(name=_name(), dob=_dob())
def test_dob_is_not_a_gated_identifier(name, dob):
    """The DOB token itself is date-shaped and therefore NOT identifier-shaped,
    so it is never glyph-gated; a name+DOB-only band verifies."""
    assert I._is_date_like(dob), dob
    assert not I._is_identifier_shaped(dob), dob
    band = f"{name[0]}, {name[1]} {dob}"
    assert I.verify_target_identity(band, band).status == "verified"
