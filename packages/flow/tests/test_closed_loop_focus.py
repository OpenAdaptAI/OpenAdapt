"""Unit tests and real-RapidOCR benchmark for closed-loop focus-before-write."""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Callable

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.ir import Point, Region
from openadapt_flow.pixel_prepare_card import OcrWord
from openadapt_flow.runtime.closed_loop_focus import (
    ClosedLoopFieldWriter,
    CommitProbingRefusedError,
    diff_focus_region,
    verify_focus_target,
)
from openadapt_flow.validation.hardening import fixtures as fx


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class _FormFieldSpec:
    label: str
    label_pos: Point
    box: Region
    value: str = ""
    stacked: bool = False


@dataclass
class _ModalSpec:
    box: Region
    title: str
    body: str
    buttons: tuple[tuple[str, Region], ...]
    input_box: Region | None = None
    input_label: str | None = None
    input_value: str = ""


@dataclass
class _InteractiveFormSim:
    """Crisp Pillow-rendered multi-row form simulator for focus-before-write tests.

    Renders real anti-aliased DejaVuSans glyphs, authentic blue focus rings,
    input carets, typed field text, Citrix dropped clicks, Citrix dropped
    characters, static modals covering target fields, and focus-steal popups.
    """

    fields: list[_FormFieldSpec]
    width: int = 640
    height: int = 420
    title: str = "Patient Intake - Dental Benefits"
    dark_mode: bool = False
    drop_clicks_remaining: int = 0
    drop_char_once: bool = False
    static_modal: _ModalSpec | None = None
    steal_modal_on_click: _ModalSpec | None = None
    focused_field_idx: int | None = None
    focused_modal_input: bool = False
    focused_modal_button: int | None = None
    active_modal: _ModalSpec | None = None
    click_log: list[Point] = field(default_factory=list)
    typed_log: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.static_modal is not None:
            self.active_modal = self.static_modal

    def _point_in(self, x: int, y: int, box: Region) -> bool:
        bx, by, bw, bh = box
        return bx <= x <= bx + bw and by <= y <= by + bh

    def click(self, x: int, y: int) -> None:
        self.click_log.append((x, y))
        if self.drop_clicks_remaining > 0:
            self.drop_clicks_remaining -= 1
            return

        if self.steal_modal_on_click is not None and self.active_modal is None:
            self.active_modal = self.steal_modal_on_click
            self.focused_field_idx = None
            self.focused_modal_input = False
            self.focused_modal_button = 0 if self.active_modal.buttons else None
            return

        if self.active_modal is not None and self._point_in(
            x, y, self.active_modal.box
        ):
            self.focused_field_idx = None
            if self.active_modal.input_box is not None and self._point_in(
                x, y, self.active_modal.input_box
            ):
                self.focused_modal_input = True
                self.focused_modal_button = None
                return
            for idx, (_btn_text, btn_box) in enumerate(self.active_modal.buttons):
                if self._point_in(x, y, btn_box):
                    self.focused_modal_button = idx
                    self.focused_modal_input = False
                    return
            # Clicking non-interactive modal chrome swallows the click.
            self.focused_modal_input = False
            self.focused_modal_button = None
            return

        for idx, spec in enumerate(self.fields):
            if self._point_in(x, y, spec.box):
                self.focused_field_idx = idx
                self.focused_modal_input = False
                self.focused_modal_button = None
                return

        self.focused_field_idx = None
        self.focused_modal_input = False
        self.focused_modal_button = None

    def clear_focused(self) -> None:
        if self.focused_modal_input and self.active_modal is not None:
            self.active_modal.input_value = ""
        elif self.focused_field_idx is not None:
            self.fields[self.focused_field_idx].value = ""

    def type_text(self, text: str) -> None:
        delivered = text
        if self.drop_char_once and len(text) >= 4:
            self.drop_char_once = False
            # Simulate a Citrix dropped character in the middle of the string.
            mid = len(text) // 2
            delivered = text[:mid] + text[mid + 1 :]

        if self.focused_modal_input and self.active_modal is not None:
            self.active_modal.input_value += delivered
            self.typed_log.append(("modal_input", delivered))
            return
        if self.focused_field_idx is not None:
            target = self.fields[self.focused_field_idx]
            target.value += delivered
            self.typed_log.append((target.label, delivered))
            return
        target_name = "modal_chrome" if self.active_modal is not None else "unfocused"
        self.typed_log.append((target_name, delivered))

    def screenshot(self) -> bytes:
        bg = (28, 32, 38) if self.dark_mode else (245, 247, 250)
        card_bg = (38, 43, 52) if self.dark_mode else (255, 255, 255)
        fg = (234, 238, 244) if self.dark_mode else (24, 30, 42)
        border = (92, 102, 118) if self.dark_mode else (148, 163, 184)
        input_bg = (48, 54, 65) if self.dark_mode else (255, 255, 255)
        focus_ring = (59, 130, 246) if self.dark_mode else (37, 99, 235)
        focus_fill = (52, 64, 84) if self.dark_mode else (239, 246, 255)

        img = Image.new("RGB", (self.width, self.height), bg)
        draw = ImageDraw.Draw(img)
        title_font = fx._font(18)
        label_font = fx._font(16)
        value_font = fx._font(16)

        # Top header bar
        draw.rectangle((0, 0, self.width, 44), fill=(30, 58, 108))
        draw.text((20, 12), self.title, font=title_font, fill=(255, 255, 255))

        # Main form card
        draw.rectangle(
            (20, 60, self.width - 20, self.height - 20),
            fill=card_bg,
            outline=border,
            width=1,
        )

        for idx, spec in enumerate(self.fields):
            is_focused = self.focused_field_idx == idx
            draw.text(spec.label_pos, spec.label, font=label_font, fill=fg)

            bx, by, bw, bh = spec.box
            if is_focused:
                draw.rectangle(
                    (bx - 2, by - 2, bx + bw + 2, by + bh + 2),
                    fill=focus_fill,
                    outline=focus_ring,
                    width=3,
                )
            else:
                draw.rectangle(
                    (bx, by, bx + bw, by + bh),
                    fill=input_bg,
                    outline=border,
                    width=1,
                )

            text_x = bx + 10
            text_y = by + max(4, (bh - 18) // 2)
            if spec.value:
                draw.text((text_x, text_y), spec.value, font=value_font, fill=fg)
            if is_focused:
                # Draw subtle caret spaced away from typed glyphs.
                if spec.value:
                    bbox = draw.textbbox((text_x, text_y), spec.value, font=value_font)
                    caret_x = min(bx + bw - 6, bbox[2] + 6)
                else:
                    caret_x = text_x
                draw.line(
                    [(caret_x, by + 6), (caret_x, by + bh - 6)],
                    fill=focus_ring,
                    width=2,
                )

        if self.active_modal is not None:
            mx, my, mw, mh = self.active_modal.box
            draw.rectangle(
                (mx, my, mx + mw, my + mh),
                fill=(250, 251, 253),
                outline=(51, 65, 85),
                width=2,
            )
            draw.rectangle((mx + 2, my + 2, mx + mw - 2, my + 36), fill=(180, 35, 24))
            draw.text(
                (mx + 14, my + 8),
                self.active_modal.title,
                font=label_font,
                fill=(255, 255, 255),
            )
            draw.text(
                (mx + 18, my + 54),
                self.active_modal.body,
                font=label_font,
                fill=(24, 30, 42),
            )
            if self.active_modal.input_box is not None:
                ibx, iby, ibw, ibh = self.active_modal.input_box
                if self.active_modal.input_label:
                    draw.text(
                        (mx + 18, iby + 5),
                        self.active_modal.input_label,
                        font=label_font,
                        fill=(24, 30, 42),
                    )
                if self.focused_modal_input:
                    draw.rectangle(
                        (ibx - 2, iby - 2, ibx + ibw + 2, iby + ibh + 2),
                        fill=(239, 246, 255),
                        outline=(37, 99, 235),
                        width=3,
                    )
                else:
                    draw.rectangle(
                        (ibx, iby, ibx + ibw, iby + ibh),
                        fill=(255, 255, 255),
                        outline=(148, 163, 184),
                        width=1,
                    )
                if self.active_modal.input_value:
                    draw.text(
                        (ibx + 8, iby + 5),
                        self.active_modal.input_value,
                        font=value_font,
                        fill=(24, 30, 42),
                    )

            for b_idx, (btn_label, btn_box) in enumerate(self.active_modal.buttons):
                bx, by, bw, bh = btn_box
                is_btn_focused = self.focused_modal_button == b_idx
                draw.rectangle(
                    (bx, by, bx + bw, by + bh),
                    fill=(37, 99, 235) if b_idx == 0 else (226, 232, 240),
                    outline=(15, 23, 42) if is_btn_focused else (100, 116, 139),
                    width=3 if is_btn_focused else 1,
                )
                draw.text(
                    (bx + 14, by + 7),
                    btn_label,
                    font=label_font,
                    fill=(255, 255, 255) if b_idx == 0 else (24, 30, 42),
                )

        return _encode_png(img)


def _center(box: Region) -> Point:
    x, y, w, h = box
    return (x + w // 2, y + h // 2)


def _make_standard_form(
    *,
    inserted_row_label: str | None = None,
    target_label: str = "Date of Birth",
    dark_mode: bool = False,
    drop_clicks: int = 0,
    drop_char_once: bool = False,
    static_modal: _ModalSpec | None = None,
    steal_modal: _ModalSpec | None = None,
) -> tuple[_InteractiveFormSim, Region, Region | None]:
    """Build a multi-row clinical form simulator.

    Returns ``(sim, target_box, inserted_row_box)``.
    """
    rows: list[str] = []
    if inserted_row_label is not None:
        rows.append(inserted_row_label)
    rows.extend([target_label, "Provider NPI", "Claim Amount"])

    fields: list[_FormFieldSpec] = []
    target_box: Region = (0, 0, 0, 0)
    inserted_box: Region | None = None
    start_y = 92
    row_step = 62

    for idx, label in enumerate(rows):
        y = start_y + idx * row_step
        box: Region = (240, y - 6, 260, 34)
        fields.append(_FormFieldSpec(label=label, label_pos=(46, y), box=box))
        if label == target_label:
            target_box = box
        if inserted_row_label is not None and idx == 0:
            inserted_box = box

    sim = _InteractiveFormSim(
        fields=fields,
        dark_mode=dark_mode,
        drop_clicks_remaining=drop_clicks,
        drop_char_once=drop_char_once,
        static_modal=static_modal,
        steal_modal_on_click=steal_modal,
    )
    return sim, target_box, inserted_box


def _run_open_loop(
    sim: _InteractiveFormSim,
    candidate: Point,
    value: str,
) -> float:
    """Open-loop baseline: click candidate #1 and type blindly."""
    t0 = time.perf_counter()
    sim.click(candidate[0], candidate[1])
    sim.type_text(value)
    return (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# 1. Unit tests for diff_focus_region
# ---------------------------------------------------------------------------


def test_diff_focus_region_isolates_ring_and_handles_focus_transfer() -> None:
    sim, dob_box, member_box = _make_standard_form(inserted_row_label="Member ID")
    assert member_box is not None

    frame0 = sim.screenshot()
    sim.click(*_center(member_box))
    frame1 = sim.screenshot()

    box1 = diff_focus_region(frame0, frame1)
    assert box1 is not None
    assert abs(box1[0] - member_box[0]) <= 6
    assert abs(box1[1] - member_box[1]) <= 6
    assert abs(box1[2] - member_box[2]) <= 12
    assert abs(box1[3] - member_box[3]) <= 12

    # Move focus from Member ID -> Date of Birth: BOTH boxes change pixels.
    sim.click(*_center(dob_box))
    frame2 = sim.screenshot()

    # Works both with explicit click_point and without click_point (chroma gain).
    for cp in (_center(dob_box), None):
        box2 = diff_focus_region(frame1, frame2, click_point=cp)
        assert box2 is not None
        assert abs(box2[0] - dob_box[0]) <= 6
        assert abs(box2[1] - dob_box[1]) <= 6


def test_diff_focus_region_returns_none_on_identical_tiny_or_invalid_frames() -> None:
    sim, _, _ = _make_standard_form()
    frame = sim.screenshot()
    assert diff_focus_region(frame, frame) is None
    assert diff_focus_region(b"", frame) is None
    assert diff_focus_region(b"not-a-png", frame) is None

    # Tiny 4x4 artifact (area 16 < min_area_px 80)
    with Image.open(io.BytesIO(frame)) as img:
        mutated = img.copy()
    ImageDraw.Draw(mutated).rectangle((10, 10, 13, 13), fill=(255, 0, 0))
    assert diff_focus_region(frame, _encode_png(mutated), min_area_px=80) is None

    # Mismatched dimensions
    smaller = Image.new("RGB", (200, 100), (255, 255, 255))
    assert diff_focus_region(frame, _encode_png(smaller)) is None


# ---------------------------------------------------------------------------
# 2. Unit tests for verify_focus_target (fast synthetic OCR)
# ---------------------------------------------------------------------------


def _fake_ocr_for_sim(sim: _InteractiveFormSim) -> Callable[[bytes], list[OcrWord]]:
    def _ocr(_png: bytes) -> list[OcrWord]:
        words: list[OcrWord] = []
        for spec in sim.fields:
            lx, ly = spec.label_pos
            words.append(
                OcrWord(
                    text=spec.label,
                    x=lx,
                    y=ly,
                    width=max(40, len(spec.label) * 9),
                    height=18,
                )
            )
            if spec.value:
                bx, by, _bw, _bh = spec.box
                words.append(
                    OcrWord(
                        text=spec.value,
                        x=bx + 8,
                        y=by + 6,
                        width=max(30, len(spec.value) * 9),
                        height=18,
                    )
                )
        if sim.active_modal is not None:
            mx, my, _mw, _mh = sim.active_modal.box
            words.append(
                OcrWord(
                    text=sim.active_modal.title,
                    x=mx + 14,
                    y=my + 8,
                    width=180,
                    height=18,
                )
            )
            words.append(
                OcrWord(
                    text=sim.active_modal.body,
                    x=mx + 18,
                    y=my + 54,
                    width=240,
                    height=18,
                )
            )
            if sim.active_modal.input_box is not None and sim.active_modal.input_label:
                _ibx, iby, _ibw, _ibh = sim.active_modal.input_box
                words.append(
                    OcrWord(
                        text=sim.active_modal.input_label,
                        x=mx + 18,
                        y=iby + 5,
                        width=120,
                        height=18,
                    )
                )
            for btn_text, (bx, by, bw, bh) in sim.active_modal.buttons:
                words.append(
                    OcrWord(
                        text=btn_text, x=bx + 10, y=by + 6, width=bw - 20, height=18
                    )
                )
        return words

    return _ocr


def test_verify_focus_target_accepts_matching_left_and_stacked_labels() -> None:
    # Horizontal left-label form
    sim, dob_box, member_box = _make_standard_form(inserted_row_label="Member ID")
    assert member_box is not None
    before = sim.screenshot()
    sim.click(*_center(dob_box))
    after = sim.screenshot()

    verdict = verify_focus_target(
        before_png=before,
        after_png=after,
        expected_label="Date of Birth",
        ocr_fn=_fake_ocr_for_sim(sim),
    )
    assert verdict.verified is True
    assert verdict.reason == "verified"
    assert verdict.label_position == "left"
    assert verdict.observed_label == "Date of Birth"
    assert verdict.focus_box is not None

    # Stacked above-label form
    stacked_box: Region = (80, 140, 220, 34)
    stacked_sim = _InteractiveFormSim(
        fields=[
            _FormFieldSpec(
                label="Date of Birth",
                label_pos=(80, 114),
                box=stacked_box,
                stacked=True,
            )
        ]
    )
    s_before = stacked_sim.screenshot()
    stacked_sim.click(*_center(stacked_box))
    s_after = stacked_sim.screenshot()

    s_verdict = verify_focus_target(
        before_png=s_before,
        after_png=s_after,
        expected_label="Date of Birth",
        ocr_fn=_fake_ocr_for_sim(stacked_sim),
    )
    assert s_verdict.verified is True
    assert s_verdict.label_position == "above"


@pytest.mark.parametrize(
    ("focused_label", "expected_label"),
    [
        ("Member ID", "Date of Birth"),
        ("Billing Street Address", "Mailing Street Address"),
        ("Phone 1", "Phone 2"),
        ("Date of Death", "Date of Birth"),
        ("Subscriber Date", "Patient Date"),
    ],
)
def test_verify_focus_target_refuses_wrong_or_confusable_field_labels(
    focused_label: str,
    expected_label: str,
) -> None:
    sim, _target_box, wrong_box = _make_standard_form(
        inserted_row_label=focused_label,
        target_label=expected_label,
    )
    assert wrong_box is not None
    before = sim.screenshot()
    sim.click(*_center(wrong_box))
    after = sim.screenshot()

    verdict = verify_focus_target(
        before_png=before,
        after_png=after,
        expected_label=expected_label,
        ocr_fn=_fake_ocr_for_sim(sim),
    )
    assert verdict.verified is False
    assert verdict.reason == "label_mismatch"
    assert verdict.observed_label == focused_label


def test_verify_focus_target_refuses_dropped_click_and_focus_steal_popup() -> None:
    # Dropped click -> no_focus_change
    sim_drop, dob_box, _ = _make_standard_form(drop_clicks=1)
    before = sim_drop.screenshot()
    sim_drop.click(*_center(dob_box))
    after = sim_drop.screenshot()

    verdict = verify_focus_target(
        before_png=before,
        after_png=after,
        expected_label="Date of Birth",
        ocr_fn=_fake_ocr_for_sim(sim_drop),
    )
    assert verdict.verified is False
    assert verdict.reason == "no_focus_change"
    assert verdict.focus_box is None

    # Focus-steal popup modal -> unexpected_popup_size / content
    modal = _ModalSpec(
        box=(190, 90, 380, 190),
        title="Session Warning",
        body="Idle timeout in 60 seconds.",
        buttons=(("Continue", (220, 220, 120, 34)), ("Logout", (360, 220, 110, 34))),
    )
    sim_modal, dob_box2, _ = _make_standard_form(steal_modal=modal)
    m_before = sim_modal.screenshot()
    sim_modal.click(*_center(dob_box2))
    m_after = sim_modal.screenshot()

    m_verdict = verify_focus_target(
        before_png=m_before,
        after_png=m_after,
        expected_label="Date of Birth",
        ocr_fn=_fake_ocr_for_sim(sim_modal),
    )
    assert m_verdict.verified is False
    assert m_verdict.reason in {"unexpected_popup_size", "unexpected_popup_content"}


# ---------------------------------------------------------------------------
# 3. Unit tests for ClosedLoopFieldWriter safety and fallback regrounding
# ---------------------------------------------------------------------------


def test_closed_loop_writer_refuses_commit_button_probing() -> None:
    sim, dob_box, _ = _make_standard_form()
    writer = ClosedLoopFieldWriter(
        screenshot_fn=sim.screenshot,
        click_fn=sim.click,
        type_fn=sim.type_text,
        clear_fn=sim.clear_focused,
        ocr_fn=_fake_ocr_for_sim(sim),
    )
    with pytest.raises(CommitProbingRefusedError):
        writer.write_field(
            expected_label="Submit Claim",
            value="",
            candidates=[_center(dob_box)],
            is_commit=True,
        )
    assert sim.click_log == []
    assert sim.typed_log == []


def test_closed_loop_writer_self_corrects_via_reground_fallback_and_retries_dropped_chars() -> (
    None
):
    sim, dob_box, member_box = _make_standard_form(
        inserted_row_label="Member ID",
        drop_char_once=True,
    )
    assert member_box is not None

    def smart_ocr(png_bytes: bytes) -> list[OcrWord]:
        # Distinguish full-frame screenshot vs tight-crop read-back by image size.
        with Image.open(io.BytesIO(png_bytes)) as img:
            w, h = img.size
        if w < sim.width or h < sim.height:
            val = sim.fields[1].value
            return [OcrWord(text=val, x=4, y=4, width=max(10, len(val) * 8), height=16)]
        return _fake_ocr_for_sim(sim)(png_bytes)

    writer = ClosedLoopFieldWriter(
        screenshot_fn=sim.screenshot,
        click_fn=sim.click,
        type_fn=sim.type_text,
        clear_fn=sim.clear_focused,
        ocr_fn=smart_ocr,
    )

    # Pass ONLY the stale coordinate (now on Member ID) plus a fallback regrounder.
    result = writer.write_field(
        expected_label="Date of Birth",
        value="1988-07-14",
        candidates=[_center(member_box)],
        reground_fn=lambda _png, _lbl, _failed: _center(dob_box),
    )

    assert result.succeeded is True
    assert result.readback_verified is True
    assert result.typing_attempts == 2  # initial dropped char + 1 clean retry
    assert len(result.attempts) == 2
    assert result.attempts[0].verdict.verified is False
    assert result.attempts[0].verdict.observed_label == "Member ID"
    assert result.attempts[1].verdict.verified is True
    # Zero characters typed into Member ID!
    assert sim.fields[0].value == ""
    assert sim.fields[1].value == "1988-07-14"


# ---------------------------------------------------------------------------
# 4. Real-RapidOCR Benchmark (24 trials across 4 realistic failure scenarios)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BenchmarkTrialSpec:
    scenario: str
    trial_id: str
    target_label: str
    value: str
    inserted_row_label: str | None = None
    dark_mode: bool = False
    drop_clicks: int = 0
    drop_char_once: bool = False
    modal_kind: str = "none"  # "none", "static_chrome", "static_input", "focus_steal"


def _build_benchmark_suite() -> list[_BenchmarkTrialSpec]:
    """24 trials (6 per scenario) comparing Open-Loop vs Closed-Loop Focus-Before-Write."""
    trials: list[_BenchmarkTrialSpec] = []

    # Scenario A: Clean form (6 trials, light + dark themes)
    clean_cases = [
        ("Date of Birth", "1988-07-14", False),
        ("Date of Birth", "1974-11-03", True),
        ("Patient Phone", "416-555-0142", False),
        ("Member Zip", "94107", False),
        ("Date of Birth", "1992-02-28", False),
        ("Group Number", "GRP-4821", True),
    ]
    for idx, (lbl, val, dark) in enumerate(clean_cases, start=1):
        trials.append(
            _BenchmarkTrialSpec(
                scenario="clean_form",
                trial_id=f"clean_{idx}",
                target_label=lbl,
                value=val,
                dark_mode=dark,
            )
        )

    # Scenario B: Row inserted above target (6 trials)
    # Candidate #1 hits newly inserted row; Candidate #2 hits shifted target row.
    inserted_cases = [
        ("Member ID", "Date of Birth", "1988-07-14", False),
        ("Policy Number", "Date of Birth", "1979-05-21", True),
        ("Subscriber ID", "Date of Birth", "1965-12-09", False),
        ("Referral Code", "Patient Phone", "416-555-0198", False),
        ("Account ID", "Group Number", "GRP-7740", False),
        ("Member ID", "Date of Birth", "1995-08-30", True),
    ]
    for idx, (ins, lbl, val, dark) in enumerate(inserted_cases, start=1):
        trials.append(
            _BenchmarkTrialSpec(
                scenario="row_inserted_above",
                trial_id=f"inserted_{idx}",
                target_label=lbl,
                value=val,
                inserted_row_label=ins,
                dark_mode=dark,
            )
        )

    # Scenario C: Citrix dropped first click (6 trials, 2 also drop a char once)
    dropped_cases = [
        ("Date of Birth", "1984-03-19", False, False),
        ("Date of Birth", "1991-09-25", True, False),
        ("Patient Phone", "647-555-0173", False, True),
        ("Group Number", "GRP-3319", False, False),
        ("Date of Birth", "1972-06-11", False, True),
        ("Member Zip", "10018", True, False),
    ]
    for idx, (lbl, val, dark, drop_char) in enumerate(dropped_cases, start=1):
        trials.append(
            _BenchmarkTrialSpec(
                scenario="citrix_dropped_click",
                trial_id=f"dropped_{idx}",
                target_label=lbl,
                value=val,
                dark_mode=dark,
                drop_clicks=1,
                drop_char_once=drop_char,
            )
        )

    # Scenario D: Popup modal covers target field or steals focus on click (6 trials)
    popup_cases = [
        ("Date of Birth", "1988-07-14", "static_chrome", False),
        ("Date of Birth", "1977-04-16", "static_input", False),
        ("Patient Phone", "416-555-0129", "focus_steal", False),
        ("Date of Birth", "1990-10-08", "static_chrome", True),
        ("Group Number", "GRP-9051", "static_input", False),
        ("Date of Birth", "1981-01-27", "focus_steal", True),
    ]
    for idx, (lbl, val, modal_kind, dark) in enumerate(popup_cases, start=1):
        trials.append(
            _BenchmarkTrialSpec(
                scenario="popup_modal_covers_target",
                trial_id=f"popup_{idx}",
                target_label=lbl,
                value=val,
                dark_mode=dark,
                modal_kind=modal_kind,
            )
        )

    return trials


def _instantiate_trial(
    spec: _BenchmarkTrialSpec,
) -> tuple[_InteractiveFormSim, list[Point], int]:
    """Return ``(sim, ranked_candidates, target_field_index)`` for one trial."""
    static_modal: _ModalSpec | None = None
    steal_modal: _ModalSpec | None = None

    if spec.modal_kind == "static_chrome":
        static_modal = _ModalSpec(
            box=(160, 68, 410, 195),
            title="Coverage Notice",
            body="Secondary payer alert active.",
            buttons=(
                ("Continue", (200, 206, 120, 34)),
                ("Cancel", (360, 206, 110, 34)),
            ),
        )
    elif spec.modal_kind == "static_input":
        # Modal input box sits right over the target input coordinates!
        static_modal = _ModalSpec(
            box=(120, 62, 460, 210),
            title="Supervisor Override",
            body="Enter override code to proceed.",
            input_box=(240, 86, 260, 34),
            input_label="Override Code",
            buttons=(
                ("Approve", (200, 212, 120, 34)),
                ("Dismiss", (360, 212, 110, 34)),
            ),
        )
    elif spec.modal_kind == "focus_steal":
        steal_modal = _ModalSpec(
            box=(160, 72, 410, 195),
            title="Session Timeout",
            body="Click Continue to keep working.",
            buttons=(
                ("Continue", (200, 210, 120, 34)),
                ("Logout", (360, 210, 110, 34)),
            ),
        )

    sim, target_box, inserted_box = _make_standard_form(
        inserted_row_label=spec.inserted_row_label,
        target_label=spec.target_label,
        dark_mode=spec.dark_mode,
        drop_clicks=spec.drop_clicks,
        drop_char_once=spec.drop_char_once,
        static_modal=static_modal,
        steal_modal=steal_modal,
    )

    if inserted_box is not None:
        # Stale candidate #1 points to old row 0 (now the inserted row);
        # Candidate #2 points to row 1 (shifted target field).
        candidates = [_center(inserted_box), _center(target_box)]
        target_idx = 1
    else:
        candidates = [_center(target_box)]
        target_idx = 0

    return sim, candidates, target_idx


def _wrong_writes_in_sim(sim: _InteractiveFormSim, target_idx: int) -> int:
    """Count fields or stray modal targets that received non-empty typed text."""
    wrong = 0
    for idx, spec in enumerate(sim.fields):
        if idx != target_idx and spec.value:
            wrong += 1
    if sim.active_modal is not None and sim.active_modal.input_value:
        wrong += 1
    # Also count blind keystrokes emitted into modal chrome or unfocused space.
    for target_name, delivered in sim.typed_log:
        if delivered and target_name in {"modal_chrome", "unfocused"}:
            wrong += 1
            break
    return wrong


def run_closed_loop_focus_benchmark() -> dict[str, dict[str, float | int]]:
    """Run all 24 real-RapidOCR trials and return per-scenario summary metrics."""
    # Warm up the process-wide RapidOCR ONNX session once so per-scenario
    # timings measure steady-state focus verification overhead.
    warm_sim, _, _ = _make_standard_form()
    from openadapt_flow.vision.ocr import ocr as local_ocr

    local_ocr(warm_sim.screenshot())

    trials = _build_benchmark_suite()
    summary: dict[str, dict[str, float | int]] = {}

    for scenario in (
        "clean_form",
        "row_inserted_above",
        "citrix_dropped_click",
        "popup_modal_covers_target",
    ):
        scenario_trials = [t for t in trials if t.scenario == scenario]
        ol_complete = 0
        ol_wrong_writes = 0
        ol_wrong_field_writes = 0
        ol_ms: list[float] = []

        cl_complete = 0
        cl_clean_halts = 0
        cl_self_corrected = 0
        cl_wrong_writes = 0
        cl_ms: list[float] = []

        for spec in scenario_trials:
            # --- Open-Loop arm ---
            ol_sim, ol_cands, ol_target_idx = _instantiate_trial(spec)
            ol_elapsed = _run_open_loop(ol_sim, ol_cands[0], spec.value)
            ol_ms.append(ol_elapsed)
            ol_wrong = _wrong_writes_in_sim(ol_sim, ol_target_idx)
            ol_wrong_writes += 1 if ol_wrong > 0 else 0
            if any(
                idx != ol_target_idx and f.value for idx, f in enumerate(ol_sim.fields)
            ) or (ol_sim.active_modal is not None and ol_sim.active_modal.input_value):
                ol_wrong_field_writes += 1
            if (
                ol_sim.fields[ol_target_idx].value == spec.value
                and ol_wrong == 0
                and spec.modal_kind == "none"
            ):
                ol_complete += 1

            # --- Closed-Loop Focus-Before-Write arm (real RapidOCR) ---
            cl_sim, cl_cands, cl_target_idx = _instantiate_trial(spec)
            writer = ClosedLoopFieldWriter(
                screenshot_fn=cl_sim.screenshot,
                click_fn=cl_sim.click,
                type_fn=cl_sim.type_text,
                clear_fn=cl_sim.clear_focused,
                ocr_fn=None,  # Uses real local RapidOCR
                max_attempts=3,
                max_type_retries=1,
            )
            res = writer.write_field(
                expected_label=spec.target_label,
                value=spec.value,
                candidates=cl_cands,
            )
            cl_ms.append(res.elapsed_ms)
            cl_wrong = _wrong_writes_in_sim(cl_sim, cl_target_idx)
            cl_wrong_writes += 1 if cl_wrong > 0 else 0

            if spec.modal_kind != "none":
                if not res.succeeded and cl_wrong == 0 and cl_sim.typed_log == []:
                    cl_clean_halts += 1
            else:
                if (
                    res.succeeded
                    and res.readback_verified
                    and cl_sim.fields[cl_target_idx].value == spec.value
                    and cl_wrong == 0
                ):
                    cl_complete += 1
                    if len(res.attempts) >= 2:
                        cl_self_corrected += 1

        n = len(scenario_trials)
        summary[scenario] = {
            "trials": n,
            "open_loop_completed": ol_complete,
            "open_loop_wrong_writes": ol_wrong_writes,
            "open_loop_wrong_field_writes": ol_wrong_field_writes,
            "open_loop_mean_ms": round(sum(ol_ms) / n, 2),
            "closed_loop_completed": cl_complete,
            "closed_loop_clean_halts": cl_clean_halts,
            "closed_loop_self_corrected": cl_self_corrected,
            "closed_loop_wrong_writes": cl_wrong_writes,
            "closed_loop_mean_ms": round(sum(cl_ms) / n, 2),
        }

    return summary


@pytest.mark.skipif(not fx.ocr_available(), reason="RapidOCR / font unavailable")
def test_benchmark_open_loop_vs_closed_loop_focus_real_rapidocr() -> None:
    """24-trial real-RapidOCR benchmark comparing Open-Loop vs Closed-Loop."""
    summary = run_closed_loop_focus_benchmark()

    # Scenario A: Clean form -> both complete 6/6 with 0 wrong writes.
    clean = summary["clean_form"]
    assert clean["trials"] == 6
    assert clean["open_loop_completed"] == 6
    assert clean["closed_loop_completed"] == 6
    assert clean["closed_loop_wrong_writes"] == 0

    # Scenario B: Row inserted above -> Open-Loop commits 6/6 wrong writes;
    # Closed-Loop catches focus on wrong row (0 wrong writes) and recovers 6/6!
    inserted = summary["row_inserted_above"]
    assert inserted["trials"] == 6
    assert inserted["open_loop_completed"] == 0
    assert inserted["open_loop_wrong_field_writes"] == 6
    assert inserted["closed_loop_completed"] == 6
    assert inserted["closed_loop_self_corrected"] == 6
    assert inserted["closed_loop_wrong_writes"] == 0

    # Scenario C: Citrix dropped first click -> Open-Loop types into nowhere (0/6);
    # Closed-Loop detects missing focus delta, retries, and completes 6/6!
    dropped = summary["citrix_dropped_click"]
    assert dropped["trials"] == 6
    assert dropped["open_loop_completed"] == 0
    assert dropped["open_loop_wrong_writes"] == 6
    assert dropped["closed_loop_completed"] == 6
    assert dropped["closed_loop_self_corrected"] == 6
    assert dropped["closed_loop_wrong_writes"] == 0

    # Scenario D: Popup modal covers target -> Open-Loop clicks modal and types blindly;
    # Closed-Loop halts cleanly 6/6 with 0 wrong writes!
    popup = summary["popup_modal_covers_target"]
    assert popup["trials"] == 6
    assert popup["open_loop_wrong_writes"] == 6
    assert popup["closed_loop_clean_halts"] == 6
    assert popup["closed_loop_wrong_writes"] == 0
