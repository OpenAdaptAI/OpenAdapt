"""Unit tests for the target-identity layer (runtime.identity).

Import-light on purpose (no cv2/OCR): everything here exercises the pure
matching logic. The four adversarial-review probes are pinned verbatim —
each was a reproduced silent wrong-verify (or false abort) against the
first identity implementation, and must never come back:

1. B1 wrong entity: shared long row text dominated char coverage, so
   "Ann Wu <same procedure>" verified at 0.89 against a "Jane Li" band.
2. B1 generic band: "Active High 3" vs "Active High 7" verified at 0.91.
3. B2 param disarm: any short param demo value in the band ("High")
   switched to param mode, which ignored the band residue entirely — a
   wrong patient verified at 1.0.
4. P1a: param mode verified ANY row containing the run's value (a
   messages row mentioning "Susan" verified for patient "Susan").

Plus the modal-overlay false abort (order-sensitive matching scored a
token permutation of the same band at ~0.66 < 0.8 on live OpenEMR).

THIRD REOPENING (2026-07-10, near-name siblings): the containment and
similarity tiers of the second fix verified sibling rows — 'Phil' inside
'Philip' (containment 1.0), 'John' vs 'Joan' (ratio 0.75), Jr/Sr rows,
off-by-one DOBs, swapped MRN digits. All pinned in TestSiblingProbes /
TestFieldLevelSiblings below; the matcher was rebuilt around strict
OCR-equivalence plus contradiction budgets and its operating point was
picked from a FROZEN held-out adversarial corpus
(docs/validation/IDENTITY_EVIDENCE.md) — pinned in TestOperatingPoint.

Additional held-out review classes remain in the private regression pack.
The small public examples below pin the resulting conservative boundaries:
confusion-only true-row evidence abstains, and name-like token absence at the
run cap refuses instead of guessing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.runtime.identity import (
    ABSENT_NAME_TOKEN_CAP,
    CONTRADICTED_CHARS_CAP,
    CONTRADICTION_SIM,
    COVERAGE_THRESHOLD,
    MIN_CONTEXT_CHARS,
    MIN_PARAM_CHARS,
    SUSPECT_CHARS_CAP,
    UNCOVERED_RUN_CAP,
    UNEXPLAINED_NAME_TOKENS_CAP,
    band_match,
    band_region,
    context_from_lines,
    coverage,
    embedded_params,
    lines_near_point,
    longest_run,
    ocr_canonical,
    required_run,
    squash,
    substitute_param,
    tokenize,
    verify_target_identity,
)

ROW = "Jane Li Comprehensive metabolic panel with lipid screening High"
WRONG_ROW = "Ann Wu Comprehensive metabolic panel with lipid screening High"


def line(text: str, region=(0, 100, 200, 20), confidence: float = 0.9):
    return SimpleNamespace(text=text, region=region, confidence=confidence)


# -- the reviewer probes, verbatim ------------------------------------------


class TestReviewerProbes:
    def test_b1_wrong_entity_with_shared_procedure_text_mismatches(self):
        """Shared row text must not buy a wrong name a pass: 'Jane Li' ->
        'Ann Wu' is a 6-char contiguous uncovered run, over the cap, even
        though raw coverage is ~0.89."""
        check = verify_target_identity(ROW, WRONG_ROW)
        assert check.status == "mismatch"
        match = band_match(ROW, WRONG_ROW)
        assert match.coverage >= COVERAGE_THRESHOLD  # coverage alone WOULD pass
        assert match.max_uncovered_run > UNCOVERED_RUN_CAP  # the run cap catches it
        # ... and 'Ann Wu' is a REPLACEMENT of 'Jane Li', so the 2026-07-10
        # contradiction budget catches it independently of the run cap:
        assert match.contradicted_chars > CONTRADICTED_CHARS_CAP

    def test_b1_generic_band_never_arms(self):
        """'Active High 3' (11 squashed chars) is generic — any sibling row
        shares it. Too short to discriminate: unreadable, never verified."""
        assert len(squash("Active High 3")) < MIN_CONTEXT_CHARS
        check = verify_target_identity("Active High 3", "Active High 7")
        assert check.status == "unreadable"

    def test_b2_param_value_in_band_cannot_disarm_the_check(self):
        """A param demo value ('High') embedded in the band must not switch
        the check into a mode that ignores the band: the substituted band's
        non-param residue still has to match."""
        check = verify_target_identity(
            ROW,
            "Ann Wu Totally different row content High",
            params={"priority": "High"},
            param_examples={"priority": "High"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"

    def test_p1a_any_row_containing_the_value_does_not_verify(self):
        """Param mode: a messages row mentioning 'Susan' is not Susan's
        target row — the recorded band's residue is missing."""
        check = verify_target_identity(
            "Open chart for Phil (active)",
            "Message from Susan re lab results",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"
        assert check.param == "patient"


# -- the third-reopening probes, verbatim (2026-07-10) -----------------------


class TestSiblingProbes:
    """The four confirmed near-name sibling probes: each returned
    (coverage=1.0, residue=0) — VERIFIED — under the pre-2026-07-10
    matcher (containment tier for Phil⊂Philip; similarity tier at 0.7 for
    John/Joan at 0.75). Real EMR rows are full of near-name siblings
    (family members, Jr/Sr, John/Joan) and a wrong-patient write is NOT
    caught downstream (the note really is saved — in the wrong chart).
    These are permanent mismatches."""

    def test_prefix_extension_phil_philip(self):
        check = verify_target_identity(
            "Belford, Phil 1985-03-12 M", "Belford, Philip 1985-03-12 M"
        )
        assert check.status == "mismatch"

    def test_prefix_extension_reverse_direction(self):
        check = verify_target_identity(
            "Belford, Philip 1985-03-12 M", "Belford, Phil 1985-03-12 M"
        )
        assert check.status == "mismatch"

    def test_single_letter_edit_john_joan(self):
        check = verify_target_identity(
            "Smith, John 1985-03-12 M", "Smith, Joan 1985-03-12 M"
        )
        assert check.status == "mismatch"

    def test_prefix_extension_phil_phillipa(self):
        # similarity('phil','phillipa') = 0.67 — BELOW the old 0.7 tier,
        # yet the old containment tier still verified it; the semantic-
        # extension contradiction rule catches it now.
        check = verify_target_identity(
            "Belford, Phil 1985-03-12 M", "Belford, Phillipa 1985-03-12 M"
        )
        assert check.status == "mismatch"


class TestFieldLevelSiblings:
    """Sibling classes beyond names, from the frozen adversarial corpus
    (all >=50% verified under the legacy matcher; 0% now)."""

    def test_generational_suffix_mismatches_both_directions(self):
        base = "Belford, Phil 1985-03-12 M"
        with_jr = "Belford, Phil Jr 1985-03-12 M"
        assert verify_target_identity(base, with_jr).status == "mismatch"
        assert verify_target_identity(with_jr, base).status == "mismatch"

    def test_generational_suffix_with_ocr_noise_still_mismatches(self):
        # A live 'II' commonly OCRs as 'lI'; suffix detection must be
        # confusion-canonical, not literal.
        check = verify_target_identity(
            "Ramirez, Stephanie 1985-06-02 F",
            "Ramire2, Stephanie lI 1985-06-02 F",
        )
        assert check.status == "mismatch"

    def test_dob_off_by_one_field_mismatches(self):
        check = verify_target_identity(
            "Belford, Phil 1985-03-12 M", "Belford, Phil 1985-03-13 M"
        )
        assert check.status == "mismatch"

    def test_mrn_digit_swap_mismatches(self):
        check = verify_target_identity(
            "Belford, Phil 1985-03-12 M MRN A123456",
            "Belford, Phil 1985-03-12 M MRN A123465",
        )
        assert check.status == "mismatch"

    def test_same_surname_dissimilar_short_first_name_mismatches(self):
        # 'Amy' -> 'Kim': similarity 0.0 and only a 3-char absence run —
        # under every workable run cap — but the replacement rule sees an
        # unexplained alphabetic observed token where ours is missing.
        check = verify_target_identity(
            "Smith, Amy 1985-03-12 M", "Smith, Kim 1985-03-12 M"
        )
        assert check.status == "mismatch"


# -- true-positive behavior around the probes --------------------------------


class TestTruePositives:
    def test_true_row_verifies(self):
        assert verify_target_identity(ROW, ROW).status == "verified"

    def test_digit_class_ocr_jitter_verifies(self):
        """Digit/symbol-class OCR noise ('5ample' ~ 'sample', 'Phi1' ~
        'Phil') must not abort a correct target: a human name contains
        no digits, so no collision with a DIFFERENT name is possible."""
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Jane 5ample Knee pain referral High",
        )
        assert check.status == "verified"

    def test_letter_letter_jitter_aborts_as_indistinguishable(self):
        """FLIPPED by the 2026-07-10 out-of-corpus review: 'paln'/'pain'
        and 'Hlgh'/'High' are letter-letter confusions — the exact
        mechanism by which 'Nell' passes for 'Neil' (a DIFFERENT
        patient). Content-agnostically the true-row misread and the
        collided sibling are the same band, so the honest outcome is an
        abort for both readings (availability cost, disclosed in
        docs/LIMITS.md; scored as the indistinguishable class in the
        v2 corpus)."""
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Jane Sample Knee paln referral Hlgh",
        )
        assert check.status == "mismatch"

    def test_token_permutation_verifies(self):
        """Live OpenEMR false abort: page chrome around a modal re-reads in
        a different segmentation order — same tokens, different order and
        merging. Order must not matter."""
        check = verify_target_identity(
            "PPV + Show All <Back to Patient ShowActive",
            "<Back to Patient PPV + Show All Show Active",
        )
        assert check.status == "verified"

    def test_param_mode_verifies_when_residue_is_stable(self):
        """The legitimate param re-anchor: the run's value replaced the
        demo's, everything else on the row still matches."""
        check = verify_target_identity(
            "Open chart for Phil (active)",
            "Open chart for Susan (active)",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "verified"
        assert check.mode == "param"
        assert check.param == "patient"

    def test_param_mode_mismatches_when_entity_row_text_varies_too(self):
        """DISCLOSED LIMIT (LIMITS.md): when the non-param row text also
        varies with the entity (a patient search result carries the
        surname), the substituted band cannot match and the run halts —
        safety over availability; clicking by position is what caused the
        wrong-patient writes."""
        check = verify_target_identity(
            "Belford, Phil MRN A12",
            "Underwood, Susan Ardmore",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"


class TestWrongEntities:
    def test_lookalike_row_mismatches(self):
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Taylor Duplicate Knee pain referral High",
        )
        assert check.status == "mismatch"

    def test_unrelated_row_mismatches(self):
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Pat Placeholder Orthopedics intake Low",
        )
        assert check.status == "mismatch"
        assert check.coverage == 0.0

    def test_empty_observed_is_unreadable(self):
        check = verify_target_identity(ROW, "   ")
        assert check.status == "unreadable"


# -- band_match internals ------------------------------------------------------


class TestBandMatch:
    def test_exact_match(self):
        assert band_match(ROW, ROW) == (1.0, 0, 0, 0, 0, 0, 0)

    def test_adjacent_unmatched_tokens_merge_into_one_run(self):
        match = band_match(ROW, WRONG_ROW)
        assert match.max_uncovered_run == len("janeli")

    def test_separated_unmatched_tokens_do_not_merge(self):
        match = band_match("alpha beta gamma delta", "alpha WRONG gamma NOPE")
        assert match.max_uncovered_run == max(len("beta"), len("delta"))

    def test_short_tokens_match_only_verbatim(self):
        # 'li' must not match inside 'lipid'.
        assert band_match("li", "lipid screening").coverage == 0.0
        assert band_match("li", "jane li").coverage == 1.0

    def test_split_and_join_tolerated(self):
        # Recorded 'ShowActive' vs observed 'Show Active' (and vice versa):
        # splits/joins are the ONLY sub-token acceptance left — full
        # consumption, no partial containment ('Phil' in 'Philip' is a
        # sibling, not a join; see TestSiblingProbes).
        assert band_match("ShowActive", "Show Active") == (1.0, 0, 0, 0, 0, 0, 0)
        assert band_match("Show Active", "ShowActive") == (1.0, 0, 0, 0, 0, 0, 0)
        # ... and with digit-class OCR noise inside the split form:
        assert band_match("ShowActive", "Sh0w Active") == (1.0, 0, 0, 0, 0, 0, 0)

    def test_letter_letter_confusion_is_suspect_not_clean(self):
        # 'Cornprehensive' matches 'Comprehensive' canonically (rn/m) but
        # the pair is digit-free on both sides — the same shape as the
        # Marnie/Mamie sibling collision, so it is charged to the
        # suspect budget instead of being a clean match.
        match = band_match("Comprehensive panel", "Cornprehensive panel")
        assert match.coverage == 1.0
        assert match.suspect_chars == len("comprehensive")

    def test_empty_inputs(self):
        assert band_match("", "anything") == (0.0, 0, 0, 0, 0, 0, 0)
        match = band_match("abcdef", "")
        assert match.coverage == 0.0 and match.max_uncovered_run == 6
        # ... and the fully absent alphabetic token registers as an
        # absent name-like token.
        assert match.max_absent_alpha_token == 6

    def test_tokenize(self):
        assert tokenize("  Jane   Li \n panel ") == ["jane", "li", "panel"]
        assert tokenize("") == []


class TestOperatingPoint:
    """Pin the ROC-chosen decision parameters and their boundaries
    (docs/validation/IDENTITY_EVIDENCE.md). Moving any of these constants
    invalidates the committed ROC — regenerate it in the same change."""

    def test_pinned_constants(self):
        assert COVERAGE_THRESHOLD == 0.8
        assert UNCOVERED_RUN_CAP == 4
        assert CONTRADICTION_SIM == 0.62
        assert CONTRADICTED_CHARS_CAP == 0
        assert SUSPECT_CHARS_CAP == 0
        assert UNEXPLAINED_NAME_TOKENS_CAP == 0
        assert ABSENT_NAME_TOKEN_CAP == 3

    def test_pure_absence_boundary_is_class_weighted(self):
        """FLIPPED by the 2026-07-10 out-of-corpus review (Major 4): a
        fully absent 4-char ALPHABETIC token used to sit exactly on
        coverage 0.8 / run 4 and VERIFY — the band's identity token was
        never read. Absence of a name-like token now refuses; absence of
        a numeric token at the same coverage/run (trailing DOB/MRN
        dropout, the $-cost OCR direction) still verifies."""
        match = band_match("abcd efgh ijkl mnop qrst", "abcd efgh ijkl mnop")
        assert match.coverage == pytest.approx(0.8)
        assert match.max_uncovered_run == 4
        assert match.contradicted_chars == 0
        assert match.max_absent_alpha_token == 4  # the flipped signal
        check = verify_target_identity(
            "abcd efgh ijkl mnop qrst", "abcd efgh ijkl mnop"
        )
        assert check.status == "mismatch"
        # The same absence with a NUMERIC token (trailing-numerics
        # dropout) keeps the old tolerance: class-weighted, not blanket.
        check = verify_target_identity(
            "abcd efgh ijkl mnop 1234", "abcd efgh ijkl mnop"
        )
        assert check.status == "verified"
        # A 5-char numeric absence still fails the generic run cap.
        check = verify_target_identity(
            "abcd efgh ijkl mnop 12345", "abcd efgh ijkl mnop"
        )
        assert check.status == "mismatch"

    def test_replacement_at_same_coverage_mismatches(self):
        """The same 4-char gap with a foreign token IN ITS PLACE is a
        replacement — contradiction budget (0) fails it even though
        coverage and run cap alone would pass."""
        match = band_match("abcd efgh ijkl mnop qrst", "abcd efgh ijkl mnop wxyz")
        assert match.coverage == pytest.approx(0.8)
        assert match.max_uncovered_run == 4
        assert match.contradicted_chars > 0
        check = verify_target_identity(
            "abcd efgh ijkl mnop qrst", "abcd efgh ijkl mnop wxyz"
        )
        assert check.status == "mismatch"

    def test_near_miss_similarity_boundary(self):
        """3-char single-edit names sit at ratio 0.67 — the 0.62
        near-miss threshold must catch them ('Ted'/'Tad')."""
        check = verify_target_identity(
            "Smith, Ted 1985-03-12 M", "Smith, Tad 1985-03-12 M"
        )
        assert check.status == "mismatch"


class TestOcrCanonical:
    """Only characteristic OCR char-class confusions are equivalences;
    semantic letter substitutions are not."""

    def test_confusion_classes_are_equivalent(self):
        assert ocr_canonical("paln") == ocr_canonical("pain")  # l/i
        assert ocr_canonical("hlgh") == ocr_canonical("high")
        assert ocr_canonical("5ample") == ocr_canonical("sample")  # 5/s
        assert ocr_canonical("c0de") == ocr_canonical("code")  # 0/o
        assert ocr_canonical("cornpre") == ocr_canonical("compre")  # rn/m
        assert ocr_canonical("clinic") == ocr_canonical("dinic")  # cl/d

    def test_semantic_edits_are_not_equivalent(self):
        assert ocr_canonical("john") != ocr_canonical("joan")  # a/o mid-token
        assert ocr_canonical("phil") != ocr_canonical("philip")  # extension
        assert ocr_canonical("mark") != ocr_canonical("marc")
        assert ocr_canonical("1985-03-12") != ocr_canonical("1985-03-13")


# -- helpers ---------------------------------------------------------------


class TestHelpers:
    def test_longest_run(self):
        assert longest_run("abcdef", "xxabcdefyy") == 6
        assert longest_run("abcdef", "abcXdef") == 3
        assert longest_run("", "abc") == 0
        assert longest_run("abc", "") == 0

    def test_required_run_scales(self):
        assert required_run(30) == 16  # capped
        assert required_run(16) == 13  # ~80%
        assert required_run(5) == 4
        assert required_run(3) == 3  # floor
        assert required_run(1) == 3

    def test_coverage_ignores_scattered_short_blocks(self):
        assert coverage("abcxyzdef", "abc123def") == pytest.approx(6 / 9)
        assert coverage("abcdef", "abcdef") == 1.0
        assert coverage("", "x") == 0.0

    def test_band_region_clamps_to_viewport(self):
        assert band_region((500, 10), 64, (1280, 800)) == (0, 0, 1280, 64)
        assert band_region((500, 795), 64, (1280, 800)) == (0, 736, 1280, 64)
        assert band_region((500, 400), 64, (1280, 800)) == (0, 368, 1280, 64)
        # Band taller than the viewport clamps to the viewport.
        assert band_region((10, 10), 2000, (100, 50)) == (0, 0, 100, 50)

    def test_embedded_params_edges(self):
        band = "Open chart for Phil (active)"
        assert embedded_params(band, {"patient": "Phil"}) == ["patient"]
        # Below MIN_PARAM_CHARS: never triggers param mode.
        short = "x" * (MIN_PARAM_CHARS - 1)
        assert embedded_params(f"row with {short}", {"p": short}) == []
        assert embedded_params(band, {"patient": ""}) == []
        assert embedded_params(band, {}) == []
        # Value not in the band: no param mode.
        assert embedded_params(band, {"patient": "Zebra"}) == []

    def test_substitute_param_verbatim(self):
        assert (
            substitute_param("Open chart for Phil now", "Phil", "Susan")
            == "Open chart for Susan now"
        )
        # Case-insensitive.
        assert (
            substitute_param("open PHIL chart", "Phil", "Susan") == "open Susan chart"
        )

    def test_substitute_param_fallback_drops_value_tokens(self):
        # The demo value never appears verbatim (OCR mangled it): tokens
        # belonging to it are dropped and the run's value appended —
        # matching is order-insensitive so position does not matter.
        out = substitute_param("Open chart for Phi1 now", "Phil", "Susan")
        assert "Phi1" not in out
        assert "Susan" in out
        assert "Open" in out and "chart" in out

    def test_substitute_param_empty_example(self):
        assert substitute_param("band text", "  ", "x") == "band text"


# -- row refinement (one-row-off) -------------------------------------------


class TestLinesNearPoint:
    def test_one_row_off_resolution_reads_the_wrong_row_only(self):
        """Dense table, ~25px rows, 64px coarse band: a point resolved one
        row off must read ITS row's text, not the adjacent true row's —
        text bleed from the true row must not verify a wrong-row click."""
        true_row = line("Jane Li Comprehensive panel", region=(0, 118, 300, 16))
        wrong_row = line("Ann Wu Basic metabolic panel", region=(0, 143, 300, 16))
        # Resolved point sits on the wrong row (y=151).
        kept = lines_near_point([true_row, wrong_row], 151)
        assert kept == [wrong_row]
        # Resolved on the true row keeps the true row.
        kept = lines_near_point([true_row, wrong_row], 126)
        assert kept == [true_row]

    def test_small_lines_get_minimum_slack(self):
        tiny = line("x", region=(0, 100, 10, 2))  # center y=101
        assert lines_near_point([tiny], 104) == [tiny]  # within min 4px
        assert lines_near_point([tiny], 110) == []


class TestContextFromLines:
    BAND = (0, 100, 1280, 64)

    def test_point_refines_to_one_row(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("Ann Wu Basic metabolic panel", region=(10, 140, 300, 16)),
        ]
        ctx = context_from_lines(
            lines,
            exclude_region=(2000, 0, 1, 1),
            band=self.BAND,
            point=(150, 118),
        )
        assert ctx == "Jane Li Comprehensive panel"

    def test_without_point_keeps_the_whole_band(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("Ann Wu Basic metabolic panel", region=(10, 140, 300, 16)),
        ]
        ctx = context_from_lines(lines, exclude_region=(2000, 0, 1, 1), band=self.BAND)
        assert "Jane Li" in ctx and "Ann Wu" in ctx

    def test_timestamp_lines_dropped(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("DOB 1980-01-01", region=(320, 110, 100, 16)),
        ]
        ctx = context_from_lines(
            lines,
            exclude_region=(2000, 0, 1, 1),
            band=self.BAND,
            point=(150, 118),
        )
        assert ctx == "Jane Li Comprehensive panel"

    def test_too_short_band_returns_none(self):
        lines = [line("Active High 3", region=(10, 110, 100, 16))]
        assert (
            context_from_lines(
                lines,
                exclude_region=(2000, 0, 1, 1),
                band=self.BAND,
                point=(50, 118),
            )
            is None
        )
