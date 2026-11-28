"""Tests for the scale-normalized OCR-word constellation grounder (Arm B)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Sequence

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.ir import Region
from openadapt_flow.runtime.constellation_grounder import (
    ConstellationGrounder,
    capture_constellation,
    disambiguate_candidates,
)
from openadapt_flow.runtime.grounder import (
    FallbackGrounder,
    Grounder,
    GrounderMatch,
    OCRAnchorGrounder,
    build_grounder,
    component_may_egress,
)
from openadapt_flow.validation.hardening import fixtures as fx


@dataclass(frozen=True)
class _FakeOcrLine:
    text: str
    region: Region
    confidence: float = 0.99


def _transform_lines(
    lines: Sequence[_FakeOcrLine],
    *,
    scale: float = 1.0,
    dx: int = 0,
    dy: int = 0,
) -> list[_FakeOcrLine]:
    out: list[_FakeOcrLine] = []
    for line in lines:
        x, y, w, h = line.region
        out.append(
            _FakeOcrLine(
                text=line.text,
                region=(
                    int(round(x * scale)) + dx,
                    int(round(y * scale)) + dy,
                    max(1, int(round(w * scale))),
                    max(1, int(round(h * scale))),
                ),
                confidence=line.confidence,
            )
        )
    return out


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


def _point_in_box(point: tuple[int, int], box: Region, pad: int = 4) -> bool:
    px, py = point
    x, y, w, h = box
    return (x - pad) <= px <= (x + w + pad) and (y - pad) <= py <= (y + h + pad)


# ---------------------------------------------------------------------------
# 1. Duplicate "Date" fields under "Subscriber" vs "Patient"
# ---------------------------------------------------------------------------


def _subscriber_vs_patient_form() -> tuple[list[_FakeOcrLine], Region, Region]:
    """Two sections, each with a 'Date' label and an input box beside it."""
    lines = [
        _FakeOcrLine("Subscriber Information", (40, 30, 180, 16)),
        _FakeOcrLine("Policy ID", (40, 62, 72, 16)),
        _FakeOcrLine("Date", (40, 94, 40, 16)),
        _FakeOcrLine("Patient Information", (40, 210, 160, 16)),
        _FakeOcrLine("MRN", (40, 242, 42, 16)),
        _FakeOcrLine("Date", (40, 274, 40, 16)),
    ]
    sub_date: Region = (40, 94, 40, 16)
    pat_date: Region = (40, 274, 40, 16)
    return lines, sub_date, pat_date


@pytest.mark.parametrize("target_section", ["subscriber", "patient"])
@pytest.mark.parametrize(
    ("scale", "dx", "dy"),
    [
        (1.0, 0, 0),
        (0.8, 25, 18),
        (1.5, 110, 65),
        (2.0, 200, 90),
    ],
)
def test_duplicate_date_fields_subscriber_vs_patient(
    target_section: str, scale: float, dx: int, dy: int
) -> None:
    record_lines, sub_date, pat_date = _subscriber_vs_patient_form()
    true_region = sub_date if target_section == "subscriber" else pat_date

    constellation = capture_constellation(
        b"record",
        true_region,
        label="Date",
        intent=f"click {target_section} date",
        ocr_fn=lambda _png: record_lines,
    )

    live_lines = _transform_lines(record_lines, scale=scale, dx=dx, dy=dy)
    expected_box = _transform_region(true_region, scale=scale, dx=dx, dy=dy)
    sub_live = _transform_region(sub_date, scale=scale, dx=dx, dy=dy)
    pat_live = _transform_region(pat_date, scale=scale, dx=dx, dy=dy)

    # Direct candidate disambiguation
    picked = disambiguate_candidates(
        b"live",
        [sub_live, pat_live],
        constellation,
        ocr_fn=lambda _png: live_lines,
    )
    assert picked is not None
    assert _point_in_box(picked.point, expected_box)

    # Full Grounder.locate() protocol
    grounder = ConstellationGrounder(
        {f"click {target_section} date": constellation},
        ocr_fn=lambda _png: live_lines,
    )
    match = grounder.locate(b"live", f"click {target_section} date", ocr_text="Date")
    assert match is not None
    assert _point_in_box(match.point, expected_box)


# ---------------------------------------------------------------------------
# 2. Blank input field disambiguation across 0.8x, 1.0x, 1.5x, 2.0x + offsets
# ---------------------------------------------------------------------------


def _blank_fields_form() -> tuple[list[_FakeOcrLine], Region, Region]:
    """Two blank input fields surrounded by distinct form labels."""
    lines = [
        _FakeOcrLine("Appointment Details", (30, 20, 170, 18)),
        _FakeOcrLine("Visit Type", (30, 60, 82, 16)),
        _FakeOcrLine("Clinic", (260, 60, 50, 16)),
        _FakeOcrLine("Clinical Note", (30, 140, 104, 16)),
        _FakeOcrLine("Optional", (260, 140, 68, 16)),
    ]
    visit_input: Region = (120, 54, 120, 28)
    note_input: Region = (140, 134, 110, 28)
    return lines, visit_input, note_input


@pytest.mark.parametrize("scale", [0.8, 1.0, 1.5, 2.0])
@pytest.mark.parametrize(("dx", "dy"), [(0, 0), (140, 85)])
def test_blank_input_field_disambiguation_across_scales_and_offsets(
    scale: float, dx: int, dy: int
) -> None:
    record_lines, visit_input, note_input = _blank_fields_form()

    visit_const = capture_constellation(
        b"record",
        visit_input,
        intent="click visit type input",
        ocr_fn=lambda _png: record_lines,
    )
    note_const = capture_constellation(
        b"record",
        note_input,
        intent="click clinical note input",
        ocr_fn=lambda _png: record_lines,
    )

    live_lines = _transform_lines(record_lines, scale=scale, dx=dx, dy=dy)
    visit_live = _transform_region(visit_input, scale=scale, dx=dx, dy=dy)
    note_live = _transform_region(note_input, scale=scale, dx=dx, dy=dy)

    grounder = ConstellationGrounder(
        {
            "click visit type input": visit_const,
            "click clinical note input": note_const,
        },
        ocr_fn=lambda _png: live_lines,
    )

    visit_match = grounder.locate(b"live", "click visit type input", ocr_text=None)
    assert visit_match is not None
    assert _point_in_box(visit_match.point, visit_live, pad=6)

    note_match = grounder.locate(b"live", "click clinical note input", ocr_text=None)
    assert note_match is not None
    assert _point_in_box(note_match.point, note_live, pad=6)

    # Also verify disambiguate_candidates picks the right blank box among both
    picked = disambiguate_candidates(
        b"live",
        [visit_live, note_live],
        note_const,
        ocr_fn=lambda _png: live_lines,
    )
    assert picked is not None
    assert _point_in_box(picked.point, note_live, pad=6)


# ---------------------------------------------------------------------------
# 3. Strict safety: ambiguous identical context abstains (0 wrong clicks)
# ---------------------------------------------------------------------------


def test_ambiguous_identical_labeled_context_strictly_abstains() -> None:
    """Two identical 'Date' panels with identical surrounding labels must abstain."""
    lines = [
        _FakeOcrLine("Entry", (40, 40, 48, 16)),
        _FakeOcrLine("Date", (40, 72, 40, 16)),
        _FakeOcrLine("Entry", (40, 240, 48, 16)),
        _FakeOcrLine("Date", (40, 272, 40, 16)),
    ]
    first_date: Region = (40, 72, 40, 16)
    second_date: Region = (40, 272, 40, 16)

    constellation = capture_constellation(
        b"record",
        first_date,
        label="Date",
        max_radius_norm=6.0,
        ocr_fn=lambda _png: lines,
    )

    assert (
        disambiguate_candidates(
            b"live",
            [first_date, second_date],
            constellation,
            ocr_fn=lambda _png: lines,
        )
        is None
    )

    grounder = ConstellationGrounder(
        default_constellation=constellation,
        ocr_fn=lambda _png: lines,
    )
    assert grounder.locate(b"live", "click date", ocr_text="Date") is None


def test_ambiguous_identical_blank_fields_strictly_abstains() -> None:
    """Two identical blank fields with identical labels must abstain."""
    lines = [
        _FakeOcrLine("Amount", (40, 60, 64, 16)),
        _FakeOcrLine("USD", (240, 60, 36, 16)),
        _FakeOcrLine("Amount", (40, 280, 64, 16)),
        _FakeOcrLine("USD", (240, 280, 36, 16)),
    ]
    first_input: Region = (115, 54, 115, 28)

    constellation = capture_constellation(
        b"record",
        first_input,
        max_radius_norm=8.0,
        ocr_fn=lambda _png: lines,
    )
    grounder = ConstellationGrounder(
        default_constellation=constellation,
        ocr_fn=lambda _png: lines,
    )
    assert grounder.locate(b"live", "click amount", ocr_text=None) is None


def test_abstains_when_score_below_threshold_or_ocr_empty() -> None:
    record_lines, sub_date, _pat_date = _subscriber_vs_patient_form()
    constellation = capture_constellation(
        b"record",
        sub_date,
        label="Date",
        ocr_fn=lambda _png: record_lines,
    )
    # Completely different screen context
    unrelated_lines = [
        _FakeOcrLine("Settings", (40, 30, 80, 16)),
        _FakeOcrLine("Date", (40, 94, 40, 16)),
    ]
    grounder = ConstellationGrounder(
        default_constellation=constellation,
        ocr_fn=lambda _png: unrelated_lines,
    )
    assert grounder.locate(b"live", "click date", ocr_text="Date") is None

    # Empty OCR / undecodable PNG
    empty_grounder = ConstellationGrounder(default_constellation=constellation)
    assert empty_grounder.locate(b"not a png", "click date", ocr_text="Date") is None


def test_numeric_ids_do_not_fuzzy_collapse_to_sibling_row() -> None:
    """O/0 or 1/l confusable numeric IDs must not falsely match a sibling."""
    record_lines = [
        _FakeOcrLine("MRN 500612", (20, 60, 100, 16)),
        _FakeOcrLine("Delete", (240, 60, 60, 16)),
    ]
    constellation = capture_constellation(
        b"record",
        (240, 60, 60, 16),
        label="Delete",
        ocr_fn=lambda _png: record_lines,
    )
    # Live screen only has the confusable sibling MRN 5OO612, not 500612
    sibling_only = [
        _FakeOcrLine("MRN 5OO612", (20, 60, 100, 16)),
        _FakeOcrLine("Delete", (240, 60, 60, 16)),
    ]
    # Only "MRN" matches, "500612" must not match "5OO612" -> score below 0.65
    assert (
        disambiguate_candidates(
            b"live",
            [(240, 60, 60, 16)],
            constellation,
            min_score=0.65,
            ocr_fn=lambda _png: sibling_only,
        )
        is None
    )


def test_inserted_row_above_target_never_clicks_wrong_row_despite_static_header() -> (
    None
):
    """Distant static header tokens must not outvote a shifted immediate field label."""
    record_lines = [
        _FakeOcrLine("Intake Form: Ada Lovelace (MRN-482910)", (20, 18, 360, 16)),
        _FakeOcrLine("Date of Birth", (20, 66, 110, 16)),
    ]
    dob_recorded: Region = (150, 58, 160, 28)

    constellation = capture_constellation(
        b"record",
        dob_recorded,
        intent="click dob",
        ocr_fn=lambda _png: record_lines,
    )
    imm_norms = {t.norm_text for t in constellation.tokens if t.is_immediate_label}
    assert imm_norms == {"date", "of", "birth"}

    # Live screen: a new "Member ID" row is inserted at row 0, shifting
    # "Date of Birth" down to row 1 while all 5 header tokens stay static.
    member_id_box: Region = (150, 58, 160, 28)
    dob_shifted: Region = (150, 108, 160, 28)
    live_lines_shifted = [
        _FakeOcrLine("Intake Form: Ada Lovelace (MRN-482910)", (20, 18, 360, 16)),
        _FakeOcrLine("Member ID", (20, 66, 96, 16)),
        _FakeOcrLine("Date of Birth", (20, 116, 110, 16)),
    ]

    grounder = ConstellationGrounder(
        {"click dob": constellation},
        ocr_fn=lambda _png: live_lines_shifted,
    )
    match = grounder.locate(b"live", "click dob", ocr_text=None)
    if match is not None:
        assert not _point_in_box(match.point, member_id_box, pad=0)
        assert _point_in_box(match.point, dob_shifted, pad=6)

    picked = disambiguate_candidates(
        b"live",
        [member_id_box, dob_shifted],
        constellation,
        ocr_fn=lambda _png: live_lines_shifted,
    )
    if picked is not None:
        assert not _point_in_box(picked.point, member_id_box, pad=0)
        assert _point_in_box(picked.point, dob_shifted, pad=6)

    # When "Date of Birth" is gone altogether, the 5 static header tokens must
    # still abstain rather than clicking the "Member ID" box.
    live_lines_missing = [
        _FakeOcrLine("Intake Form: Ada Lovelace (MRN-482910)", (20, 18, 360, 16)),
        _FakeOcrLine("Member ID", (20, 66, 96, 16)),
    ]
    missing_grounder = ConstellationGrounder(
        {"click dob": constellation},
        ocr_fn=lambda _png: live_lines_missing,
    )
    assert missing_grounder.locate(b"live", "click dob", ocr_text=None) is None
    assert (
        disambiguate_candidates(
            b"live",
            [member_id_box],
            constellation,
            ocr_fn=lambda _png: live_lines_missing,
        )
        is None
    )


def test_occluded_blank_input_covered_by_calendar_popup_abstains() -> None:
    """A recorded blank input covered by a calendar popup grid must abstain."""
    lines = [
        _FakeOcrLine("Appointment Details", (24, 18, 180, 16)),
        _FakeOcrLine("Date of Birth", (24, 66, 110, 16)),
        _FakeOcrLine("Optional", (345, 66, 72, 16)),
    ]
    dob_box: Region = (150, 58, 180, 32)

    def _render_form(*, dark: bool = False, occlusion: str = "none") -> bytes:
        bg = (28, 30, 34) if dark else (248, 248, 250)
        fg = (235, 238, 242) if dark else (28, 30, 34)
        box_bg = (42, 46, 52) if dark else (255, 255, 255)
        img = Image.new("RGB", (460, 220), bg)
        draw = ImageDraw.Draw(img)
        x0, y0, w, h = dob_box
        draw.rectangle([x0, y0, x0 + w, y0 + h], outline=fg, width=2, fill=box_bg)

        if occlusion == "calendar":
            # Draw a rendered calendar popup grid covering the input box.
            px0, py0, px1, py1 = x0 - 6, y0 - 8, x0 + w + 12, y0 + h + 96
            draw.rectangle(
                [px0, py0, px1, py1],
                outline=(60, 64, 72),
                width=2,
                fill=(252, 252, 255),
            )
            # Calendar header strip inside the upper portion of the input box.
            draw.rectangle([px0 + 2, py0 + 2, px1 - 2, py0 + 22], fill=(45, 90, 165))
            # 7x5 calendar grid lines and cell glyphs across the target box.
            cols, rows = 7, 5
            cell_w = (px1 - px0) / cols
            cell_h = (py1 - (py0 + 24)) / rows
            for c in range(1, cols):
                gx = int(round(px0 + c * cell_w))
                draw.line([(gx, py0 + 24), (gx, py1)], fill=(140, 145, 155), width=1)
            for r in range(rows + 1):
                gy = int(round(py0 + 24 + r * cell_h))
                draw.line([(px0, gy), (px1, gy)], fill=(140, 145, 155), width=1)
            for r in range(rows):
                for c in range(cols):
                    tx = int(round(px0 + c * cell_w + 5))
                    ty = int(round(py0 + 24 + r * cell_h + 4))
                    draw.text((tx, ty), str((r * cols + c) % 28 + 1), fill=(25, 28, 32))
        elif occlusion == "dark_modal":
            # Solid dark modal chrome covering the input box on a light page.
            draw.rectangle(
                [x0 - 10, y0 - 10, x0 + w + 10, y0 + h + 40],
                fill=(32, 36, 44),
            )

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    clean_png = _render_form(dark=False, occlusion="none")
    constellation = capture_constellation(
        clean_png,
        dob_box,
        intent="click dob",
        ocr_fn=lambda _png: lines,
    )
    assert constellation.target_is_blank is True

    grounder = ConstellationGrounder(
        {"click dob": constellation},
        ocr_fn=lambda _png: lines,
    )

    # Unoccluded light and dark frames still hit cleanly.
    light_hit = grounder.locate(clean_png, "click dob", ocr_text=None)
    assert light_hit is not None
    assert _point_in_box(light_hit.point, dob_box, pad=4)

    dark_png = _render_form(dark=True, occlusion="none")
    dark_hit = grounder.locate(dark_png, "click dob", ocr_text=None)
    assert dark_hit is not None
    assert _point_in_box(dark_hit.point, dob_box, pad=4)

    # Calendar popup covering the blank input must abstain.
    calendar_png = _render_form(dark=False, occlusion="calendar")
    assert grounder.locate(calendar_png, "click dob", ocr_text=None) is None
    assert (
        disambiguate_candidates(
            calendar_png,
            [dob_box],
            constellation,
            ocr_fn=lambda _png: lines,
        )
        is None
    )

    # Solid dark popup chrome covering the blank input on a light page abstains.
    modal_png = _render_form(dark=False, occlusion="dark_modal")
    assert grounder.locate(modal_png, "click dob", ocr_text=None) is None


# ---------------------------------------------------------------------------
# 4. Integration with Grounder protocol and build_grounder chain
# ---------------------------------------------------------------------------


class _StubFallback:
    MAY_EGRESS = False

    def __init__(self, match: GrounderMatch | None) -> None:
        self._match = match
        self.calls = 0

    def locate(self, screen_png: bytes, intent: str, ocr_text: str | None = None):
        self.calls += 1
        return self._match


def test_grounder_protocol_and_build_grounder_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_lines, sub_date, _pat_date = _subscriber_vs_patient_form()
    constellation = capture_constellation(
        b"record",
        sub_date,
        label="Date",
        ocr_fn=lambda _png: record_lines,
    )
    cg = ConstellationGrounder(
        {"click subscriber date": constellation},
        ocr_fn=lambda _png: record_lines,
    )
    assert isinstance(cg, Grounder)
    assert cg.MAY_EGRESS is False

    monkeypatch.setattr(OCRAnchorGrounder, "available", staticmethod(lambda: None))
    stub_match = GrounderMatch(point=(1, 1), region=(0, 0, 2, 2), confidence=0.5)
    fallback = _StubFallback(stub_match)

    chain = build_grounder(fallback=fallback, constellation=cg)
    assert isinstance(chain, FallbackGrounder)
    assert isinstance(chain, Grounder)
    assert component_may_egress(chain) is False

    # Registered intent is resolved by ConstellationGrounder before fallback.
    hit = chain.locate(b"live", "click subscriber date", ocr_text="Date")
    assert hit is not None
    assert _point_in_box(hit.point, sub_date)
    assert fallback.calls == 0

    # Unregistered intent abstains in ConstellationGrounder and falls through.
    fb_hit = chain.locate(b"live", "unregistered step", ocr_text=None)
    assert fb_hit == stub_match
    assert fallback.calls == 1


def test_build_grounder_orders_constellation_ahead_of_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConstellationGrounder runs before LocalGroundingModel and short-circuits on hit."""
    record_lines, sub_date, _pat_date = _subscriber_vs_patient_form()
    constellation = capture_constellation(
        b"record",
        sub_date,
        label="Date",
        ocr_fn=lambda _png: record_lines,
    )
    cg = ConstellationGrounder(
        {"click subscriber date": constellation},
        ocr_fn=lambda _png: record_lines,
    )

    class _StubLocalModel:
        MAY_EGRESS = False

        def __init__(self, match: GrounderMatch) -> None:
            self._match = match
            self.locate_calls = 0
            self.anchor_calls = 0

        def locate(self, screen_png: bytes, intent: str, ocr_text: str | None = None):
            self.locate_calls += 1
            return self._match

        def locate_anchor(self, screen_png: bytes, template_png: bytes):
            self.anchor_calls += 1
            return ["anchor-candidate"]

    monkeypatch.setattr(OCRAnchorGrounder, "available", staticmethod(lambda: None))
    local_match = GrounderMatch(point=(99, 99), region=(90, 90, 18, 18), confidence=0.8)
    local_model = _StubLocalModel(local_match)

    chain = build_grounder(local_model=local_model, constellation=cg)
    assert isinstance(chain, FallbackGrounder)
    assert chain._grounders[0] is cg
    assert chain._grounders[1] is local_model
    assert component_may_egress(chain) is False

    # Constellation hit short-circuits before local_model (0 calls to DINOv2).
    hit = chain.locate(b"live", "click subscriber date", ocr_text="Date")
    assert hit is not None
    assert _point_in_box(hit.point, sub_date)
    assert local_model.locate_calls == 0

    # Constellation miss falls back to local_model.
    fb_hit = chain.locate(b"live", "icon-only target", ocr_text=None)
    assert fb_hit == local_match
    assert local_model.locate_calls == 1

    # locate_anchor still delegates through to local_model.
    assert chain.locate_anchor(b"live", b"crop") == ["anchor-candidate"]
    assert local_model.anchor_calls == 1


# ---------------------------------------------------------------------------
# 5. End-to-end rendered image test with real RapidOCR (guarded)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not fx.ocr_available(), reason="RapidOCR / font unavailable")
def test_rendered_subscriber_vs_patient_date_real_ocr_dark_and_scaled() -> None:
    """Real PIL + RapidOCR: duplicate Date fields survive 1.5x scale + dark theme."""

    def render_form(*, scale: float = 1.0, dark: bool = False) -> tuple[bytes, Region]:
        w, h = int(520 * scale), int(340 * scale)
        bg = (28, 30, 34) if dark else (248, 248, 250)
        fg = (235, 238, 242) if dark else (28, 30, 34)
        box_bg = (42, 46, 52) if dark else (255, 255, 255)
        img = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(img)
        font = fx._font(max(12, int(round(18 * scale))))
        draw.text(
            (int(30 * scale), int(25 * scale)), "Subscriber Section", font=font, fill=fg
        )
        draw.text((int(30 * scale), int(65 * scale)), "Date", font=font, fill=fg)
        sub_box = (
            int(120 * scale),
            int(58 * scale),
            int(160 * scale),
            int(32 * scale),
        )
        draw.rectangle(
            [sub_box[0], sub_box[1], sub_box[0] + sub_box[2], sub_box[1] + sub_box[3]],
            outline=fg,
            width=2,
            fill=box_bg,
        )
        draw.text(
            (int(30 * scale), int(185 * scale)), "Patient Section", font=font, fill=fg
        )
        draw.text((int(30 * scale), int(225 * scale)), "Date", font=font, fill=fg)
        pat_box = (
            int(120 * scale),
            int(218 * scale),
            int(160 * scale),
            int(32 * scale),
        )
        draw.rectangle(
            [pat_box[0], pat_box[1], pat_box[0] + pat_box[2], pat_box[1] + pat_box[3]],
            outline=fg,
            width=2,
            fill=box_bg,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), sub_box

    clean_png, sub_box_clean = render_form(scale=1.0, dark=False)
    constellation = capture_constellation(clean_png, sub_box_clean, intent="sub_date")
    assert len(constellation.tokens) >= 2

    dark_scaled_png, sub_box_scaled = render_form(scale=1.5, dark=True)
    grounder = ConstellationGrounder({"sub_date": constellation})
    match = grounder.locate(dark_scaled_png, "sub_date", ocr_text=None)
    assert match is not None
    assert _point_in_box(match.point, sub_box_scaled, pad=10)
