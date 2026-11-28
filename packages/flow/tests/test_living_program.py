"""Unit and multi-run lifecycle tests for living program compilation.

Covers:
1. Automatic 2-run coincidence pruning (``prune_coincidences_from_two_runs``).
2. Incremental zero-PHI PrepareCard branch splicing (``splice_prepare_branch``,
   ``match_branch``, ``GuardedBranch``, ``LivingBranchTable``).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Sequence

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.cloud_prepare_bridge import compute_resolution_sha256
from openadapt_flow.compiler.living_program import (
    GuardedBranch,
    LivingBranchTable,
    match_branch,
    prune_coincidences_from_two_runs,
    splice_prepare_branch,
)
from openadapt_flow.ir import Region
from openadapt_flow.pixel_prepare_card import OcrWord, extract_pixel_prepare_card
from openadapt_flow.runtime.constellation_grounder import (
    ConstellationGrounder,
    capture_constellation,
)


@dataclass(frozen=True)
class _FakeOcrLine:
    text: str
    region: Region
    confidence: float = 0.99


def _words_to_fake_lines(words: Sequence[OcrWord]) -> list[_FakeOcrLine]:
    return [
        _FakeOcrLine(
            text=w.text,
            region=(w.x, w.y, max(1, w.width), max(1, w.height)),
            confidence=w.confidence,
        )
        for w in words
    ]


def _transform_words(
    words: Sequence[OcrWord],
    *,
    scale: float = 1.0,
    dx: int = 0,
    dy: int = 0,
) -> list[OcrWord]:
    return [
        OcrWord(
            text=w.text,
            x=int(round(w.x * scale)) + dx,
            y=int(round(w.y * scale)) + dy,
            width=max(1, int(round(w.width * scale))),
            height=max(1, int(round(w.height * scale))),
            confidence=w.confidence,
        )
        for w in words
    ]


def _transform_region(
    region: Region,
    *,
    scale: float = 1.0,
    dx: int = 0,
    dy: int = 0,
) -> Region:
    x, y, w, h = region
    return (
        int(round(x * scale)) + dx,
        int(round(y * scale)) + dy,
        max(1, int(round(w * scale))),
        max(1, int(round(h * scale))),
    )


def _point_in_box(point: tuple[int, int], box: Region, pad: int = 6) -> bool:
    px, py = point
    x, y, w, h = box
    return (x - pad) <= px <= (x + w + pad) and (y - pad) <= py <= (y + h + pad)


# ---------------------------------------------------------------------------
# 1. Automatic 2-Run Coincidence Pruning
# ---------------------------------------------------------------------------


def _build_clinical_notes_screen(
    patient_rows: Sequence[str],
    *,
    include_decoy_save_button: bool = False,
) -> tuple[list[OcrWord], Region]:
    """Build OCR words for a clinical notes screen with static UI + dynamic rows."""
    save_note_region: Region = (160, 210, 92, 24)
    words: list[OcrWord] = [
        # Static UI header and action bar around Save Note
        OcrWord(text="Clinical Notes", x=40, y=30, width=130, height=16),
        OcrWord(text="Action", x=96, y=214, width=48, height=16),
        OcrWord(text="Save Note", x=160, y=214, width=92, height=16),
        OcrWord(text="Cancel", x=272, y=214, width=54, height=16),
    ]
    for idx, row_text in enumerate(patient_rows):
        words.append(
            OcrWord(
                text=row_text,
                x=40,
                y=68 + idx * 32,
                width=260,
                height=16,
            )
        )
    if include_decoy_save_button:
        # Distant unrelated panel with another Save Note button to verify zero wrong clicks.
        words.extend(
            [
                OcrWord(text="Billing Queue", x=560, y=30, width=120, height=16),
                OcrWord(text="Invoice", x=496, y=214, width=52, height=16),
                OcrWord(text="Save Note", x=560, y=214, width=92, height=16),
            ]
        )
    return words, save_note_region


def test_two_run_coincidence_pruning_restores_100pct_hit_rate_on_run3() -> None:
    """Run 1 dynamic rows cause Run 3 abstention; 2-run pruning achieves 100% hit, 0 wrong."""
    run1_patients = [
        "James Alvarez MRN10041",
        "Maria Alvarez MRN10042",
        "Robert Chen MRN10043",
        "Elena Gomez MRN10044",
    ]
    run2_patients = [
        "Neil Patel MRN20081",
        "Priya Nair MRN20082",
        "Samuel Ortiz MRN20083",
        "Hannah Kim MRN20084",
    ]
    run3_scenarios = [
        # Empty search results (zero patient rows)
        [],
        # Completely different patients
        [
            "Aisha Khan MRN30110",
            "Devon Brooks MRN30111",
            "Lucas Meyer MRN30112",
            "Chloe Park MRN30113",
        ],
        # Single unrelated patient row
        ["Omar Farooq MRN40999"],
    ]

    run1_words, save_region = _build_clinical_notes_screen(
        run1_patients, include_decoy_save_button=True
    )
    run1_constellation = capture_constellation(
        b"run1",
        save_region,
        label="Save Note",
        intent="click save note",
        ocr_fn=lambda _png: _words_to_fake_lines(run1_words),
    )

    # Confirm Run 1 captured both static UI and dynamic patient tokens.
    run1_norms = {t.norm_text for t in run1_constellation.tokens}
    assert {"james", "alvarez", "maria", "robert", "elena"}.issubset(run1_norms)
    assert {"clinical", "notes", "action", "cancel"}.issubset(run1_norms)
    assert any(
        t.is_immediate_label and t.norm_text == "action"
        for t in run1_constellation.tokens
    )

    # BEFORE pruning: evaluate Run 3 scenarios (unpruned constellation abstains).
    unpruned_hits = 0
    unpruned_wrong = 0
    for rows in run3_scenarios:
        run3_words, expected_box = _build_clinical_notes_screen(
            rows, include_decoy_save_button=True
        )
        grounder_before = ConstellationGrounder(
            {"click save note": run1_constellation},
            ocr_fn=lambda _png, w=run3_words: _words_to_fake_lines(w),
        )
        hit = grounder_before.locate(b"run3", "click save note", ocr_text="Save Note")
        if hit is not None:
            if _point_in_box(hit.point, expected_box):
                unpruned_hits += 1
            else:
                unpruned_wrong += 1

    assert unpruned_hits == 0
    assert unpruned_wrong == 0

    # Run 2: align against different patients and prune dynamic coincidences.
    run2_words, save_region_run2 = _build_clinical_notes_screen(
        run2_patients, include_decoy_save_button=True
    )
    report = prune_coincidences_from_two_runs(
        run1_constellation,
        run2_words,
        target_region_run2=save_region_run2,
    )

    assert report.pruning_applied is True
    assert report.skipped_reason is None
    assert report.pruned_count == 12
    assert report.kept_count == 4
    assert report.preserved_immediate_labels == ["Action"]

    kept_norms = {t.norm_text for t in report.kept_tokens}
    pruned_norms = {t.norm_text for t in report.pruned_tokens}
    assert kept_norms == {"clinical", "notes", "action", "cancel"}
    assert {"james", "alvarez", "maria", "robert", "chen", "elena", "gomez"}.issubset(
        pruned_norms
    )
    assert report.cleaned_constellation == report.constellation

    # AFTER pruning: evaluate Run 3 scenarios across scales and window shifts.
    pruned_hits = 0
    pruned_wrong = 0
    total_trials = 0
    for rows in run3_scenarios:
        base_words, base_box = _build_clinical_notes_screen(
            rows, include_decoy_save_button=True
        )
        for scale, dx, dy in ((1.0, 0, 0), (1.5, 90, 45)):
            total_trials += 1
            scaled_words = _transform_words(base_words, scale=scale, dx=dx, dy=dy)
            expected_box = _transform_region(base_box, scale=scale, dx=dx, dy=dy)
            grounder_after = ConstellationGrounder(
                {"click save note": report.constellation},
                ocr_fn=lambda _png, w=scaled_words: _words_to_fake_lines(w),
            )
            hit = grounder_after.locate(
                b"run3", "click save note", ocr_text="Save Note"
            )
            assert hit is not None
            if _point_in_box(hit.point, expected_box):
                pruned_hits += 1
            else:
                pruned_wrong += 1

    assert pruned_hits == total_trials == 6
    assert pruned_wrong == 0


def test_pruning_preserves_immediate_field_label_and_survives_run2_scale_shift() -> (
    None
):
    """Immediate field labels remain protected even when Run 2 is scaled and shifted."""
    run1_words = [
        OcrWord(text="Patient Intake", x=30, y=20, width=120, height=16),
        OcrWord(text="James Alvarez", x=30, y=48, width=120, height=16),
        OcrWord(text="Date of Birth", x=30, y=90, width=106, height=16),
        OcrWord(text="MRN10041", x=320, y=90, width=82, height=16),
    ]
    dob_box: Region = (150, 82, 150, 28)
    const1 = capture_constellation(
        b"run1",
        dob_box,
        intent="click dob",
        ocr_fn=lambda _png: _words_to_fake_lines(run1_words),
    )

    run2_base = [
        OcrWord(text="Patient Intake", x=30, y=20, width=120, height=16),
        OcrWord(text="Neil Patel", x=30, y=48, width=90, height=16),
        OcrWord(text="Date of Birth", x=30, y=90, width=106, height=16),
        OcrWord(text="MRN20081", x=320, y=90, width=82, height=16),
    ]
    run2_scaled = _transform_words(run2_base, scale=1.5, dx=120, dy=60)
    dob_box_run2 = _transform_region(dob_box, scale=1.5, dx=120, dy=60)

    report = prune_coincidences_from_two_runs(
        const1,
        run2_scaled,
        target_region_run2=dob_box_run2,
    )
    assert report.pruning_applied is True
    assert set(t.lower() for t in report.preserved_immediate_labels) == {
        "date",
        "of",
        "birth",
    }
    kept_norms = {t.norm_text for t in report.kept_tokens}
    assert {"patient", "intake", "date", "of", "birth"} == kept_norms
    pruned_norms = {t.norm_text for t in report.pruned_tokens}
    assert {"james", "alvarez", "mrn10041"} == pruned_norms


def test_pruning_safe_fallbacks_and_parameter_validation() -> None:
    """Empty or misaligned Run 2 leaves Run 1 untouched instead of wiping it out."""
    run1_words, save_region = _build_clinical_notes_screen(["James Alvarez MRN10041"])
    const1 = capture_constellation(
        b"run1",
        save_region,
        label="Save Note",
        ocr_fn=lambda _png: _words_to_fake_lines(run1_words),
    )

    # Empty Run 2 OCR
    empty_report = prune_coincidences_from_two_runs(
        const1, [], target_region_run2=save_region
    )
    assert empty_report.pruning_applied is False
    assert empty_report.skipped_reason == "empty_run2_ocr"
    assert empty_report.constellation == const1

    # Only target inner text in Run 2 (no surrounding tokens)
    only_inner = prune_coincidences_from_two_runs(
        const1,
        [OcrWord(text="Save Note", x=160, y=214, width=92, height=16)],
        target_region_run2=save_region,
    )
    assert only_inner.pruning_applied is False
    assert only_inner.skipped_reason == "no_surrounding_run2_tokens"
    assert only_inner.constellation == const1

    # Completely unrelated Run 2 screen (0 surviving tokens)
    unrelated_report = prune_coincidences_from_two_runs(
        const1,
        [OcrWord(text="System Settings Logout", x=40, y=30, width=180, height=16)],
        target_region_run2=save_region,
    )
    assert unrelated_report.pruning_applied is False
    assert unrelated_report.skipped_reason == "insufficient_surviving_tokens"
    assert unrelated_report.constellation == const1

    with pytest.raises(ValueError, match="min_surviving_tokens"):
        prune_coincidences_from_two_runs(
            const1, run1_words, target_region_run2=save_region, min_surviving_tokens=0
        )
    with pytest.raises(ValueError, match="min_token_match_score"):
        prune_coincidences_from_two_runs(
            const1,
            run1_words,
            target_region_run2=save_region,
            min_token_match_score=0.0,
        )


# ---------------------------------------------------------------------------
# 2. Incremental PrepareCard Branch Splicing (4-Run Lifecycle)
# ---------------------------------------------------------------------------


def _encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _render_citrix_before_after_modal(
    prompt_line: str,
    options: Sequence[str],
) -> tuple[bytes, bytes]:
    """Render before/after PNG frames for a synthetic Citrix modal dialog."""
    base = Image.new("RGB", (800, 600), color=(236, 239, 244))
    draw = ImageDraw.Draw(base)
    font = ImageFont.load_default()
    draw.rectangle((40, 60, 760, 540), fill=(255, 255, 255), outline=(180, 190, 205))
    draw.text((64, 92), "Dental Claim Entry", fill=(30, 41, 59), font=font)
    before_png = _encode_png(base)

    after = base.copy()
    adraw = ImageDraw.Draw(after)
    mx, my, mw, mh = 180, 180, 440, 200
    adraw.rectangle(
        (mx, my, mx + mw - 1, my + mh - 1),
        fill=(245, 247, 250),
        outline=(51, 65, 85),
        width=2,
    )
    adraw.text((mx + 20, my + 60), prompt_line, fill=(15, 23, 42), font=font)
    for idx, label in enumerate(options):
        bx = mx + 40 + idx * 180
        adraw.rectangle((bx, my + 132, bx + 140, my + 168), fill=(37, 99, 235))
        adraw.text((bx + 12, my + 144), label, fill=(255, 255, 255), font=font)
    return before_png, _encode_png(after)


def _make_signed_resolution(
    *,
    card_id: str,
    portal_origin: str,
    slot_idx: int,
    chosen_option: str,
    action: str = "approve",
    operator_id: str = "clinic_ops_lead",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "prepare_signoff_v1",
        "card_id": card_id,
        "portal_origin": portal_origin,
        "action": action,
        "resolution": {
            "card_id": card_id,
            "operator_id": operator_id,
            "slot_bindings": {str(slot_idx): chosen_option},
            "approve_all": False,
            "resolved_at_utc": "2026-11-28T19:00:00Z",
        },
        "merged_bindings": {
            "0": "$member_id_masked",
            str(slot_idx): chosen_option,
        },
        "zero_phi_verified": True,
    }
    payload["payload_sha256"] = compute_resolution_sha256(payload)
    return payload


def test_four_run_incremental_branch_splicing_lifecycle() -> None:
    """4-run lifecycle: happy path -> halt+splice -> $0 auto-resolve -> safe halt on novel modal."""
    table = LivingBranchTable()
    portal_origin = "https://citrix.clinic.org"

    # Track exact lifecycle metrics across all 4 runs.
    human_stops = {"run1": 0, "run2": 0, "run3": 0, "run4": 0}
    auto_resolutions = {"run1": 0, "run2": 0, "run3": 0, "run4": 0}
    wrong_actions = {"run1": 0, "run2": 0, "run3": 0, "run4": 0}
    llm_cost_usd = 0.0

    # --- Run 1: Happy path (no modal -> 0 stops, $0) ---
    assert len(table) == 0
    assert human_stops["run1"] == 0

    # --- Run 2: Unseen "Secondary coverage detected" modal with patient PHI ---
    run2_modal_words = [
        OcrWord(
            text="Secondary coverage detected for Jane Doe (123-45-6789)",
            x=20,
            y=60,
            width=360,
            height=18,
        ),
        OcrWord(text="Continue Anyway", x=48, y=144, width=124, height=18),
        OcrWord(text="Cancel", x=240, y=144, width=60, height=18),
    ]
    run2_options = ["Continue Anyway", "Cancel"]

    # First check branch table: no branch yet -> must halt and emit zero-PHI PrepareCard.
    assert (
        match_branch(
            table,
            modal_ocr_words=run2_modal_words,
            observed_options=run2_options,
        )
        is None
    )
    human_stops["run2"] += 1

    before_png, after_png = _render_citrix_before_after_modal(
        "Secondary coverage detected for Jane Doe (123-45-6789)",
        run2_options,
    )
    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin=portal_origin,
        field_label="Coverage Confirmation",
        card_id="prep_run2_sec_cov_001",
        ocr_fn=lambda _png: run2_modal_words,
        known_phi_values=("Jane Doe",),
        staged_bindings={0: "$member_id_masked"},
    )
    resolution = _make_signed_resolution(
        card_id=card["card_id"],
        portal_origin=portal_origin,
        slot_idx=1,
        chosen_option="Continue Anyway",
    )

    branch = splice_prepare_branch(
        table,
        card=card,
        resolution_payload=resolution,
        modal_ocr_words=run2_modal_words,
        remember_rule=True,
    )
    assert len(table) == 1
    assert branch.chosen_option == "Continue Anyway"
    assert branch.prompt_guard_tokens == ("secondary", "coverage", "detected")
    assert branch.option_set == ("cancel", "continue anyway")
    assert branch.allows_redacted_slots is True
    assert branch.remembered is True

    # Strictly zero PHI in the stored branch and serialized table.
    table_json = table.model_dump_json()
    assert "jane" not in table_json.casefold()
    assert "doe" not in table_json.casefold()
    assert "123-45-6789" not in table_json

    # --- Run 3: Same "Secondary coverage detected" modal recurs for a new patient ---
    run3_modal_words = [
        OcrWord(
            text="Secondary coverage detected for Neil Patel (987-65-4321)",
            x=20,
            y=60,
            width=360,
            height=18,
        ),
        OcrWord(text="Continue Anyway", x=48, y=144, width=124, height=18),
        OcrWord(text="Cancel", x=240, y=144, width=60, height=18),
    ]
    run3_match = match_branch(
        table,
        modal_ocr_words=run3_modal_words,
        observed_options=["Continue Anyway", "Cancel"],
    )
    assert run3_match is not None
    assert run3_match.branch_id == branch.branch_id
    assert run3_match.chosen_option == "Continue Anyway"
    auto_resolutions["run3"] += 1
    assert human_stops["run3"] == 0
    assert llm_cost_usd == 0.0

    # --- Run 4: Novel / unsafe modals with identical [Continue Anyway, Cancel] buttons ---
    unsafe_run4_modals = [
        (
            "Wrong patient record",
            ["Continue Anyway", "Cancel"],
        ),
        (
            "Coverage terminated for Neil Patel",
            ["Continue Anyway", "Cancel"],
        ),
        (
            "Secondary coverage detected: wrong patient record",
            ["Continue Anyway", "Cancel"],
        ),
        (
            "Secondary coverage NOT detected for Neil Patel",
            ["Continue Anyway", "Cancel"],
        ),
        (
            "Secondary coverage detected for Neil Patel",
            ["Continue Anyway", "Cancel", "Override Policy"],
        ),
        (
            "Secondary coverage detected for Neil Patel",
            ["Continue Anyway"],
        ),
    ]
    for prompt_text, obs_options in unsafe_run4_modals:
        modal_words = [
            OcrWord(text=prompt_text, x=20, y=60, width=360, height=18),
            *[
                OcrWord(text=opt, x=48 + i * 160, y=144, width=120, height=18)
                for i, opt in enumerate(obs_options)
            ],
        ]
        matched = match_branch(
            table,
            modal_ocr_words=modal_words,
            observed_options=obs_options,
        )
        if matched is not None:
            wrong_actions["run4"] += 1
        else:
            human_stops["run4"] += 1

    assert wrong_actions == {"run1": 0, "run2": 0, "run3": 0, "run4": 0}
    assert human_stops == {"run1": 0, "run2": 1, "run3": 0, "run4": 6}
    assert auto_resolutions["run3"] == 1


def test_static_modal_branch_requires_exact_prompt_tokens_and_exact_option_set() -> (
    None
):
    """Static modals without redacted PHI require exact structural prompt tokens."""
    table = LivingBranchTable()
    before_png, after_png = _render_citrix_before_after_modal(
        "Secondary coverage detected",
        ["Continue Anyway", "Cancel"],
    )
    modal_words = [
        OcrWord(text="Secondary coverage detected", x=20, y=60, width=220, height=18),
        OcrWord(text="Continue Anyway", x=48, y=144, width=124, height=18),
        OcrWord(text="Cancel", x=240, y=144, width=60, height=18),
    ]
    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin="https://citrix.clinic.org",
        field_label="Coverage Confirmation",
        card_id="prep_static_001",
        ocr_fn=lambda _png: modal_words,
    )
    resolution = _make_signed_resolution(
        card_id="prep_static_001",
        portal_origin="https://citrix.clinic.org",
        slot_idx=0,
        chosen_option="Continue Anyway",
    )
    branch = splice_prepare_branch(
        table,
        card=card,
        resolution_payload=resolution,
        modal_ocr_words=modal_words,
        remember_rule=True,
    )
    assert branch.allows_redacted_slots is False
    assert branch.prompt_guard_tokens == ("secondary", "coverage", "detected")

    # Matches punctuation/case variants and reordered buttons with same exact option set.
    matched = table.match(
        modal_ocr_words=[
            OcrWord(
                text="Secondary Coverage Detected.", x=20, y=60, width=220, height=18
            ),
            OcrWord(text="Cancel", x=48, y=144, width=60, height=18),
            OcrWord(text="Continue Anyway", x=180, y=144, width=124, height=18),
        ],
        observed_options=["Cancel", "Continue Anyway"],
        portal_origin="https://citrix.clinic.org",
    )
    assert matched is not None
    assert matched.chosen_option == "Continue Anyway"

    # Rejects any extra word on a static modal guard.
    assert (
        match_branch(
            table,
            modal_ocr_words=[
                OcrWord(
                    text="Secondary coverage not detected",
                    x=20,
                    y=60,
                    width=240,
                    height=18,
                ),
            ],
            observed_options=["Continue Anyway", "Cancel"],
        )
        is None
    )

    # Portal origin mismatch refuses to fire.
    assert (
        match_branch(
            table,
            modal_ocr_words=modal_words,
            observed_options=["Continue Anyway", "Cancel"],
            portal_origin="https://other-portal.example",
        )
        is None
    )

    # Roundtrip JSON persistence preserves matching behavior.
    restored = LivingBranchTable.model_validate_json(table.model_dump_json())
    assert (
        match_branch(
            restored,
            modal_ocr_words=modal_words,
            observed_options=["Continue Anyway", "Cancel"],
        )
        is not None
    )


def test_splice_security_rejects_unsigned_tampered_rejected_or_unremembered() -> None:
    """Unsigned, tampered, mismatched, or rejected resolutions never enter the table."""
    table = LivingBranchTable()
    before_png, after_png = _render_citrix_before_after_modal(
        "Secondary coverage detected",
        ["Continue Anyway", "Cancel"],
    )
    modal_words = [
        OcrWord(text="Secondary coverage detected", x=20, y=60, width=220, height=18),
        OcrWord(text="Continue Anyway", x=48, y=144, width=124, height=18),
        OcrWord(text="Cancel", x=240, y=144, width=60, height=18),
    ]
    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin="https://citrix.clinic.org",
        field_label="Coverage Confirmation",
        card_id="prep_sec_001",
        ocr_fn=lambda _png: modal_words,
    )
    signed = _make_signed_resolution(
        card_id="prep_sec_001",
        portal_origin="https://citrix.clinic.org",
        slot_idx=0,
        chosen_option="Continue Anyway",
    )

    # 1. Unsigned resolution is refused.
    unsigned = {k: v for k, v in signed.items() if k != "payload_sha256"}
    with pytest.raises(ValueError, match="unsigned"):
        splice_prepare_branch(
            table,
            card=card,
            resolution_payload=unsigned,
            modal_ocr_words=modal_words,
        )

    # 2. Tampered resolution digest is refused.
    tampered = dict(signed)
    tampered["payload_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        splice_prepare_branch(
            table,
            card=card,
            resolution_payload=tampered,
            modal_ocr_words=modal_words,
        )

    # 3. Mismatched card_id is refused.
    wrong_card_res = _make_signed_resolution(
        card_id="prep_other_card",
        portal_origin="https://citrix.clinic.org",
        slot_idx=0,
        chosen_option="Continue Anyway",
    )
    with pytest.raises(ValueError, match="does not match"):
        splice_prepare_branch(
            table,
            card=card,
            resolution_payload=wrong_card_res,
            modal_ocr_words=modal_words,
        )

    # 4. Rejected operator decision is refused.
    rejected = _make_signed_resolution(
        card_id="prep_sec_001",
        portal_origin="https://citrix.clinic.org",
        slot_idx=0,
        chosen_option="Cancel",
        action="reject",
    )
    with pytest.raises(ValueError, match="rejected"):
        splice_prepare_branch(
            table,
            card=card,
            resolution_payload=rejected,
            modal_ocr_words=modal_words,
        )

    # 5. Choice outside allowed_enum_options is refused.
    bad_choice = _make_signed_resolution(
        card_id="prep_sec_001",
        portal_origin="https://citrix.clinic.org",
        slot_idx=0,
        chosen_option="Force Delete",
    )
    with pytest.raises(ValueError, match="allowed_enum_options"):
        splice_prepare_branch(
            table,
            card=card,
            resolution_payload=bad_choice,
            modal_ocr_words=modal_words,
        )

    # 6. remember_rule=False validates and returns branch without mutating table.
    unremembered = splice_prepare_branch(
        table,
        card=card,
        resolution_payload=signed,
        modal_ocr_words=modal_words,
        remember_rule=False,
    )
    assert isinstance(unremembered, GuardedBranch)
    assert unremembered.remembered is False
    assert len(table) == 0
    assert (
        match_branch(
            table,
            modal_ocr_words=modal_words,
            observed_options=["Continue Anyway", "Cancel"],
        )
        is None
    )


def test_match_branch_refuses_ambiguous_multi_branch_or_duplicate_options() -> None:
    """Ambiguous multi-branch hits and duplicate option lists strictly return None."""
    table = LivingBranchTable()
    # Two distinct branches (one static, one redacted-slot) that both match the same prompt.
    branch_static = GuardedBranch(
        branch_id="branch_a",
        portal_origin="https://citrix.clinic.org",
        control_label="Coverage Confirmation",
        prompt_guard_tokens=("secondary", "coverage", "detected"),
        option_set=("cancel", "continue anyway"),
        allowed_options=("Continue Anyway", "Cancel"),
        chosen_option="Continue Anyway",
        allows_redacted_slots=False,
        max_variable_tokens=0,
        source_card_id="card_a",
        resolution_digest_sha256="a" * 64,
        operator_id="op_a",
    )
    branch_template = GuardedBranch(
        branch_id="branch_b",
        portal_origin="https://citrix.clinic.org",
        control_label="Coverage Confirmation",
        prompt_guard_tokens=("secondary", "coverage", "detected"),
        option_set=("cancel", "continue anyway"),
        allowed_options=("Continue Anyway", "Cancel"),
        chosen_option="Cancel",
        allows_redacted_slots=True,
        max_variable_tokens=8,
        source_card_id="card_b",
        resolution_digest_sha256="b" * 64,
        operator_id="op_b",
    )
    table.add(branch_static)
    table.add(branch_template)
    assert len(table) == 2

    words = [OcrWord(text="Secondary coverage detected")]
    assert (
        match_branch(
            table,
            modal_ocr_words=words,
            observed_options=["Continue Anyway", "Cancel"],
        )
        is None
    )

    # Duplicate normalized options in observed_options strictly return None.
    single_table = LivingBranchTable(branches=[branch_static])
    assert (
        match_branch(
            single_table,
            modal_ocr_words=words,
            observed_options=["Continue Anyway", "continue anyway"],
        )
        is None
    )
