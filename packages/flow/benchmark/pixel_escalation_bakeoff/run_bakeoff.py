#!/usr/bin/env python3
"""Reproducible 4-arm pixel-only grounding escalation bakeoff harness.

Evaluates four grounding arms on pixel-only UI fixtures (scaled, dark-themed,
blank-field, look-alike rows, and pixel-cropped choice_widgets captures):

- Arm A (`arm_a_local_template_dinov2`): Local OpenCV template match plus
  DINOv2-small ONNX (`LocalGroundingModel`) with strict count-never-pick
  look-alike abstention.
- Arm C (`arm_c_lan_remote_vlm`): LAN `RemoteGrounder` when `OPENADAPT_VLM_URL`
  (or `OPENADAPT_FLOW_VLM_URL`) is configured; otherwise `skipped_no_credentials`.
- Arm D Raw (`arm_d_zdr_vlm_raw`): `ZdrVlmGrounder` without Synthetic-Twin
  masking when `OPENADAPT_ZDR_VLM_URL` is configured; otherwise
  `skipped_no_credentials`.
- Arm D Masked (`arm_d_zdr_vlm_masked`): `ZdrVlmGrounder` with
  `SyntheticTwinProjector` visual masking and `HealedAnchorCache` when
  `OPENADAPT_ZDR_VLM_URL` is configured; otherwise `skipped_no_credentials`.

Hard safety gate: `wrong_clicks` must equal 0 for every measured arm.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from openadapt_flow.ir import Region  # noqa: E402
from openadapt_flow.runtime.grounder import GrounderMatch  # noqa: E402
from openadapt_flow.runtime.local_grounder import (  # noqa: E402
    LocalGroundingModel,
    WeightsUnavailable,
)
from openadapt_flow.runtime.remote_vlm import (  # noqa: E402
    RemoteGrounder,
    RemoteVLMClient,
)
from openadapt_flow.runtime.synthetic_twin_mask import (  # noqa: E402
    OcrTextBox,
    SyntheticTwinProjector,
    mask_screenshot_png,
)
from openadapt_flow.runtime.zdr_vlm_grounder import (  # noqa: E402
    HealedAnchorCache,
    ZdrVlmGrounder,
)

SCHEMA = "openadapt.pixel-escalation-bakeoff.v1"

SAMPLE_DOSSIER: dict[str, str] = {
    "patient_name": "Ada Lovelace",
    "dob": "1988-04-14",
    "mrn": "MRN-482910",
    "ssn": "123-45-6789",
    "phone": "(415) 555-0194",
    "email": "ada.lovelace@clinic.example",
    "copay": "$45.00",
}


@dataclass(frozen=True)
class BakeoffCase:
    """One pixel-only grounding test case."""

    case_id: str
    category: str
    intent: str
    ocr_text: Optional[str]
    template_png: bytes
    live_png: bytes
    target_box: Region
    distractor_boxes: tuple[Region, ...]
    ocr_boxes: tuple[OcrTextBox, ...]
    dossier: dict[str, str]


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _crop_png(img: Image.Image, box: Region) -> bytes:
    x, y, w, h = box
    return _to_png(img.crop((x, y, x + w, y + h)))


def _point_in_box(point: tuple[int, int], box: Region, pad: int = 2) -> bool:
    px, py = point
    x, y, w, h = box
    return (x - pad) <= px <= (x + w + pad) and (y - pad) <= py <= (y + h + pad)


def _render_scaled_submit_case() -> BakeoffCase:
    """100% recorded form vs 150% scaled live form with PHI header."""
    # Recorded frame at 100%
    rec = Image.new("RGB", (640, 400), (246, 248, 250))
    d = ImageDraw.Draw(rec)
    d.rectangle((0, 0, 640, 52), fill=(25, 65, 115))
    d.text(
        (16, 16),
        "Patient: Ada Lovelace  MRN-482910  DOB: 1988-04-14",
        fill=(255, 255, 255),
        font=_font(15),
    )
    d.rectangle(
        (40, 90, 600, 320), fill=(255, 255, 255), outline=(200, 208, 218), width=2
    )
    d.text((64, 114), "Copay Due: $45.00", fill=(30, 35, 45), font=_font(15))
    # Distinct Submit button with checkmark glyph
    rec_btn: Region = (430, 256, 140, 42)
    bx, by, bw, bh = rec_btn
    d.rounded_rectangle(
        (bx, by, bx + bw, by + bh),
        radius=6,
        fill=(24, 128, 72),
        outline=(14, 92, 50),
        width=2,
    )
    d.line(
        (bx + 16, by + 22, bx + 23, by + 29, bx + 35, by + 13),
        fill=(255, 255, 255),
        width=3,
    )
    d.text((bx + 44, by + 13), "Submit", fill=(255, 255, 255), font=_font(16))
    template_png = _crop_png(rec, rec_btn)

    # Live frame at 150% scale with slight vertical shift
    scale = 1.5
    lw, lh = int(640 * scale), int(400 * scale)
    live = Image.new("RGB", (lw, lh), (246, 248, 250))
    ld = ImageDraw.Draw(live)
    ld.rectangle((0, 0, lw, 78), fill=(25, 65, 115))
    ld.text(
        (24, 24),
        "Patient: Ada Lovelace  MRN-482910  DOB: 1988-04-14",
        fill=(255, 255, 255),
        font=_font(22),
    )
    ld.rectangle(
        (60, 140, 900, 500), fill=(255, 255, 255), outline=(200, 208, 218), width=3
    )
    ld.text((96, 176), "Copay Due: $45.00", fill=(30, 35, 45), font=_font(22))

    # Cancel button distractor
    cancel_box: Region = (410, 404, 190, 63)
    cx, cy, cw, ch = cancel_box
    ld.rounded_rectangle(
        (cx, cy, cx + cw, cy + ch),
        radius=9,
        fill=(190, 55, 55),
        outline=(140, 35, 35),
        width=3,
    )
    ld.text((cx + 52, cy + 20), "Cancel", fill=(255, 255, 255), font=_font(24))

    # Scaled Submit target
    target_box: Region = (645, 404, 210, 63)
    tx, ty, tw, th = target_box
    ld.rounded_rectangle(
        (tx, ty, tx + tw, ty + th),
        radius=9,
        fill=(24, 128, 72),
        outline=(14, 92, 50),
        width=3,
    )
    ld.line(
        (tx + 24, ty + 33, tx + 35, ty + 44, tx + 53, ty + 20),
        fill=(255, 255, 255),
        width=4,
    )
    ld.text((tx + 66, ty + 20), "Submit", fill=(255, 255, 255), font=_font(24))

    ocr_boxes = (
        OcrTextBox("Patient: Ada Lovelace", (20, 20, 320, 32)),
        OcrTextBox("MRN-482910", (345, 20, 170, 32)),
        OcrTextBox("DOB: 1988-04-14", (525, 20, 240, 32)),
        OcrTextBox("Copay Due: $45.00", (92, 172, 260, 32)),
        OcrTextBox("Cancel", cancel_box),
        OcrTextBox("Submit", target_box),
    )
    return BakeoffCase(
        case_id="scaled_150_submit",
        category="scaled",
        intent="Click the Submit button for Ada Lovelace",
        ocr_text="Submit",
        template_png=template_png,
        live_png=_to_png(live),
        target_box=target_box,
        distractor_boxes=(cancel_box,),
        ocr_boxes=ocr_boxes,
        dossier=dict(SAMPLE_DOSSIER),
    )


def _draw_paperclip_icon(
    draw: Any, x: int, y: int, size: int, color: tuple[int, int, int]
) -> None:
    pad = max(4, size // 6)
    draw.arc(
        (x + pad, y + pad, x + size - pad, y + size // 2 + pad),
        180,
        360,
        fill=color,
        width=3,
    )
    draw.line((x + pad, y + size // 3, x + pad, y + size - pad), fill=color, width=3)
    draw.line(
        (x + size - pad, y + size // 3, x + size - pad, y + size - pad - 4),
        fill=color,
        width=3,
    )
    draw.arc(
        (x + pad, y + size // 2, x + size - pad, y + size - pad),
        0,
        180,
        fill=color,
        width=3,
    )


def _draw_trash_icon(
    draw: Any, x: int, y: int, size: int, color: tuple[int, int, int]
) -> None:
    pad = max(4, size // 5)
    draw.line((x + pad, y + pad + 4, x + size - pad, y + pad + 4), fill=color, width=3)
    draw.rectangle(
        (x + pad + 4, y + pad + 6, x + size - pad - 4, y + size - pad),
        outline=color,
        width=3,
    )


def _render_dark_theme_icon_case() -> BakeoffCase:
    """Light-theme toolbar icon recorded vs dark-theme shifted live toolbar."""
    # Recorded light theme
    rec = Image.new("RGB", (640, 360), (245, 245, 248))
    d = ImageDraw.Draw(rec)
    d.rectangle((0, 0, 640, 56), fill=(230, 234, 240))
    d.text(
        (16, 18),
        "Ada Lovelace | SSN 123-45-6789 | (415) 555-0194",
        fill=(20, 20, 25),
        font=_font(14),
    )
    rec_icon: Region = (460, 10, 36, 36)
    d.rectangle(
        (460, 10, 496, 46), fill=(255, 255, 255), outline=(140, 150, 165), width=2
    )
    _draw_paperclip_icon(d, 460, 10, 36, (30, 40, 60))
    template_png = _crop_png(rec, rec_icon)

    # Live dark theme, toolbar shifted down and icon shifted right
    live = Image.new("RGB", (640, 360), (24, 27, 34))
    ld = ImageDraw.Draw(live)
    ld.rectangle((0, 0, 640, 64), fill=(36, 41, 52))
    ld.text(
        (16, 22),
        "Ada Lovelace | SSN 123-45-6789 | (415) 555-0194",
        fill=(230, 234, 242),
        font=_font(14),
    )
    distractor_box: Region = (470, 14, 36, 36)
    ld.rectangle(
        (470, 14, 506, 50), fill=(48, 54, 68), outline=(110, 120, 140), width=2
    )
    _draw_trash_icon(ld, 470, 14, 36, (235, 110, 110))

    target_box: Region = (530, 14, 36, 36)
    ld.rectangle(
        (530, 14, 566, 50), fill=(48, 54, 68), outline=(110, 120, 140), width=2
    )
    _draw_paperclip_icon(ld, 530, 14, 36, (225, 232, 245))

    ocr_boxes = (
        OcrTextBox("Ada Lovelace", (14, 18, 120, 24)),
        OcrTextBox("SSN 123-45-6789", (144, 18, 140, 24)),
        OcrTextBox("(415) 555-0194", (296, 18, 130, 24)),
    )
    return BakeoffCase(
        case_id="dark_theme_attach",
        category="themed",
        intent="Click the Attach paperclip icon in the toolbar",
        ocr_text=None,
        template_png=template_png,
        live_png=_to_png(live),
        target_box=target_box,
        distractor_boxes=(distractor_box,),
        ocr_boxes=ocr_boxes,
        dossier=dict(SAMPLE_DOSSIER),
    )


def _render_blank_field_case() -> BakeoffCase:
    """Three visually identical blank text inputs with different nearby labels."""
    rec = Image.new("RGB", (640, 420), (250, 250, 252))
    d = ImageDraw.Draw(rec)
    d.text(
        (24, 20),
        "Intake Form: Ada Lovelace (MRN-482910)",
        fill=(25, 30, 40),
        font=_font(16),
    )
    rec_field: Region = (200, 90, 260, 34)
    d.text((40, 98), "Date of Birth", fill=(40, 45, 55), font=_font(15))
    d.rectangle(
        (200, 90, 460, 124), fill=(255, 255, 255), outline=(155, 165, 180), width=2
    )
    template_png = _crop_png(rec, rec_field)

    # Live frame has 3 identical blank input boxes, shifted layout
    live = Image.new("RGB", (640, 420), (250, 250, 252))
    ld = ImageDraw.Draw(live)
    ld.text(
        (24, 20),
        "Intake Form: Ada Lovelace (MRN-482910)",
        fill=(25, 30, 40),
        font=_font(16),
    )
    fields = [
        ("Member ID", (210, 96, 260, 34)),
        ("Date of Birth", (210, 166, 260, 34)),
        ("Phone Number", (210, 236, 260, 34)),
    ]
    ocr_boxes: list[OcrTextBox] = [
        OcrTextBox("Intake Form: Ada Lovelace (MRN-482910)", (20, 16, 380, 28))
    ]
    for label, (fx, fy, fw, fh) in fields:
        ld.text((40, fy + 8), label, fill=(40, 45, 55), font=_font(15))
        ld.rectangle(
            (fx, fy, fx + fw, fy + fh),
            fill=(255, 255, 255),
            outline=(155, 165, 180),
            width=2,
        )
        ocr_boxes.append(OcrTextBox(label, (36, fy + 4, 150, 26)))

    return BakeoffCase(
        case_id="blank_field_dob",
        category="blank_field",
        intent="Click the blank Date of Birth input field",
        ocr_text="Date of Birth",
        template_png=template_png,
        live_png=_to_png(live),
        target_box=fields[1][1],
        distractor_boxes=(fields[0][1], fields[2][1]),
        ocr_boxes=tuple(ocr_boxes),
        dossier=dict(SAMPLE_DOSSIER),
    )


def _render_lookalike_rows_case() -> BakeoffCase:
    """Worklist with four identical Open buttons; only Ada Lovelace's row is valid."""
    rows = [
        ("Grace Hopper", "MRN-194022", "1979-12-09"),
        ("Alan Turing", "MRN-775319", "1982-06-23"),
        ("Ada Lovelace", "MRN-482910", "1988-04-14"),
        ("Katherine Johnson", "MRN-603841", "1975-08-26"),
    ]
    # Recorded template from a single Open button
    rec = Image.new("RGB", (720, 360), (255, 255, 255))
    rd = ImageDraw.Draw(rec)
    rec_btn: Region = (590, 90, 86, 30)
    rd.rounded_rectangle(
        (590, 90, 676, 120),
        radius=4,
        fill=(38, 98, 214),
        outline=(24, 72, 168),
        width=2,
    )
    rd.text((614, 97), "Open", fill=(255, 255, 255), font=_font(14))
    template_png = _crop_png(rec, rec_btn)

    live = Image.new("RGB", (720, 360), (255, 255, 255))
    ld = ImageDraw.Draw(live)
    ld.rectangle((0, 0, 720, 44), fill=(235, 240, 248))
    ld.text((24, 14), "Patient Worklist", fill=(25, 35, 50), font=_font(16))

    ocr_boxes: list[OcrTextBox] = [OcrTextBox("Patient Worklist", (20, 10, 180, 28))]
    target_box: Region = (0, 0, 1, 1)
    distractors: list[Region] = []

    for idx, (name, mrn, dob) in enumerate(rows):
        y = 64 + idx * 62
        bg = (248, 250, 253) if idx % 2 == 0 else (255, 255, 255)
        ld.rectangle((16, y, 704, y + 50), fill=bg, outline=(218, 224, 232), width=1)
        ld.text((28, y + 16), name, fill=(25, 30, 40), font=_font(15))
        ld.text((230, y + 16), mrn, fill=(55, 65, 80), font=_font(15))
        ld.text((390, y + 16), dob, fill=(55, 65, 80), font=_font(15))
        btn: Region = (590, y + 10, 86, 30)
        ld.rounded_rectangle(
            (btn[0], btn[1], btn[0] + btn[2], btn[1] + btn[3]),
            radius=4,
            fill=(38, 98, 214),
            outline=(24, 72, 168),
            width=2,
        )
        ld.text((btn[0] + 24, btn[1] + 7), "Open", fill=(255, 255, 255), font=_font(14))

        ocr_boxes.append(OcrTextBox(name, (24, y + 12, 180, 26)))
        ocr_boxes.append(OcrTextBox(mrn, (226, y + 12, 130, 26)))
        ocr_boxes.append(OcrTextBox(dob, (386, y + 12, 130, 26)))
        ocr_boxes.append(OcrTextBox("Open", btn))

        if name == "Ada Lovelace":
            target_box = btn
        else:
            distractors.append(btn)

    return BakeoffCase(
        case_id="lookalike_patient_rows",
        category="look_alike",
        intent="Click Open in the row for Ada Lovelace (MRN-482910)",
        ocr_text="Open",
        template_png=template_png,
        live_png=_to_png(live),
        target_box=target_box,
        distractor_boxes=tuple(distractors),
        ocr_boxes=tuple(ocr_boxes),
        dossier=dict(SAMPLE_DOSSIER),
    )


def _render_choice_widget_select2_case() -> BakeoffCase:
    """Pixel-cropped Select2 dropdown widget capture (125% scale)."""
    # Recorded 100% option row
    rec = Image.new("RGB", (420, 280), (255, 255, 255))
    rd = ImageDraw.Draw(rec)
    rec_opt: Region = (30, 128, 340, 32)
    rd.rectangle(
        (30, 128, 370, 160), fill=(232, 242, 255), outline=(120, 165, 230), width=1
    )
    rd.text((42, 136), "Cardiology", fill=(20, 40, 80), font=_font(15))
    template_png = _crop_png(rec, rec_opt)

    # Live 125% cropped Select2 capture with patient header and 4 options
    live = Image.new("RGB", (525, 350), (248, 250, 252))
    ld = ImageDraw.Draw(live)
    ld.text(
        (24, 18),
        "Department for Ada Lovelace (1988-04-14)",
        fill=(30, 35, 45),
        font=_font(17),
    )
    ld.rectangle(
        (24, 56, 480, 96), fill=(255, 255, 255), outline=(90, 130, 210), width=2
    )
    ld.text((36, 68), "Select department...", fill=(110, 120, 135), font=_font(16))
    ld.rectangle(
        (24, 98, 480, 310), fill=(255, 255, 255), outline=(180, 190, 205), width=2
    )

    options = ["Neurology", "Cardiology", "Oncology", "Pediatrics"]
    ocr_boxes: list[OcrTextBox] = [
        OcrTextBox("Department for Ada Lovelace (1988-04-14)", (20, 14, 430, 28))
    ]
    target_box: Region = (0, 0, 1, 1)
    distractors: list[Region] = []

    for idx, opt in enumerate(options):
        oy = 110 + idx * 46
        box: Region = (34, oy, 436, 40)
        fill = (232, 242, 255) if opt == "Cardiology" else (255, 255, 255)
        ld.rectangle(
            (box[0], box[1], box[0] + box[2], box[1] + box[3]),
            fill=fill,
            outline=(220, 226, 235),
            width=1,
        )
        ld.text((box[0] + 14, box[1] + 10), opt, fill=(20, 35, 65), font=_font(18))
        ocr_boxes.append(OcrTextBox(opt, box))
        if opt == "Cardiology":
            target_box = box
        else:
            distractors.append(box)

    return BakeoffCase(
        case_id="choice_widget_select2_crop",
        category="choice_widgets_crop",
        intent="Select Cardiology in the Select2 department dropdown",
        ocr_text="Cardiology",
        template_png=template_png,
        live_png=_to_png(live),
        target_box=target_box,
        distractor_boxes=tuple(distractors),
        ocr_boxes=tuple(ocr_boxes),
        dossier=dict(SAMPLE_DOSSIER),
    )


def _render_choice_widget_aria_listbox_case() -> BakeoffCase:
    """Pixel-cropped ARIA listbox widget capture in dark theme."""
    rec = Image.new("RGB", (400, 260), (255, 255, 255))
    rd = ImageDraw.Draw(rec)
    rec_opt: Region = (28, 108, 320, 34)
    rd.rectangle(
        (28, 108, 348, 142), fill=(240, 244, 250), outline=(140, 150, 170), width=1
    )
    rd.ellipse((40, 118, 54, 132), outline=(40, 90, 200), width=2)
    rd.text((64, 117), "Female", fill=(25, 30, 40), font=_font(15))
    template_png = _crop_png(rec, rec_opt)

    live = Image.new("RGB", (460, 310), (28, 31, 38))
    ld = ImageDraw.Draw(live)
    ld.text(
        (20, 16),
        "Patient: Ada Lovelace  ada.lovelace@clinic.example",
        fill=(228, 232, 240),
        font=_font(14),
    )
    ld.text((20, 52), "Gender (ARIA listbox)", fill=(180, 190, 205), font=_font(15))
    ld.rectangle((20, 82, 430, 266), fill=(38, 42, 52), outline=(95, 105, 125), width=2)

    options = ["Male", "Female", "Other", "Decline to state"]
    ocr_boxes: list[OcrTextBox] = [
        OcrTextBox("Patient: Ada Lovelace", (16, 12, 180, 24)),
        OcrTextBox("ada.lovelace@clinic.example", (202, 12, 240, 24)),
    ]
    target_box: Region = (0, 0, 1, 1)
    distractors: list[Region] = []

    for idx, opt in enumerate(options):
        oy = 92 + idx * 42
        box: Region = (30, oy, 390, 34)
        ld.rectangle(
            (box[0], box[1], box[0] + box[2], box[1] + box[3]),
            fill=(46, 52, 64),
            outline=(85, 95, 115),
            width=1,
        )
        ld.ellipse(
            (box[0] + 12, box[1] + 10, box[0] + 26, box[1] + 24),
            outline=(130, 175, 255),
            width=2,
        )
        ld.text((box[0] + 38, box[1] + 9), opt, fill=(235, 238, 245), font=_font(15))
        ocr_boxes.append(OcrTextBox(opt, box))
        if opt == "Female":
            target_box = box
        else:
            distractors.append(box)

    return BakeoffCase(
        case_id="choice_widget_aria_listbox_crop",
        category="choice_widgets_crop",
        intent="Choose Female in the Gender ARIA listbox",
        ocr_text="Female",
        template_png=template_png,
        live_png=_to_png(live),
        target_box=target_box,
        distractor_boxes=tuple(distractors),
        ocr_boxes=tuple(ocr_boxes),
        dossier=dict(SAMPLE_DOSSIER),
    )


def build_cases() -> list[BakeoffCase]:
    """Construct the fixed suite of pixel-only bakeoff fixtures."""
    return [
        _render_scaled_submit_case(),
        _render_dark_theme_icon_case(),
        _render_blank_field_case(),
        _render_lookalike_rows_case(),
        _render_choice_widget_select2_case(),
        _render_choice_widget_aria_listbox_case(),
    ]


def _score_point(
    case: BakeoffCase,
    match: Optional[GrounderMatch],
) -> str:
    """Classify a grounder output as `resolved`, `abstained`, or `wrong_click`."""
    if match is None:
        return "abstained"
    if _point_in_box(match.point, case.target_box):
        return "resolved"
    for box in case.distractor_boxes:
        if _point_in_box(match.point, box):
            return "wrong_click"
    return "wrong_click"


def _template_match_only(
    screen_png: bytes, template_png: bytes
) -> Optional[GrounderMatch]:
    """Strict local OpenCV template matcher (abstains on low score or look-alikes)."""
    frame = cv2.imdecode(np.frombuffer(screen_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    tmpl = cv2.imdecode(np.frombuffer(template_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None or tmpl is None:
        return None
    fh, fw = frame.shape[:2]
    th, tw = tmpl.shape[:2]
    if th >= fh or tw >= fw:
        return None
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    _min, peak, _minloc, (px, py) = cv2.minMaxLoc(res)
    if peak < 0.85:
        return None
    # Suppress top peak and check for look-alike second peak (count, never pick).
    suppressed = res.copy()
    suppressed[
        max(0, py - th // 2) : py + th // 2 + 1, max(0, px - tw // 2) : px + tw // 2 + 1
    ] = -1.0
    _min2, peak2, _minloc2, _loc2 = cv2.minMaxLoc(suppressed)
    if peak2 >= 0.80 or (peak - peak2) < 0.12:
        return None
    return GrounderMatch(
        point=(px + tw // 2, py + th // 2),
        region=(px, py, tw, th),
        confidence=float(peak),
    )


def _run_arm_a(cases: list[BakeoffCase]) -> dict[str, Any]:
    """Arm A: local template matching + DINOv2-small ONNX (count, never pick)."""
    local_model: Optional[LocalGroundingModel] = None
    execution_mode = "local_template_dinov2"
    try:
        local_model = LocalGroundingModel.load(download=False)
    except (WeightsUnavailable, Exception):
        execution_mode = "local_template_only"

    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []
    resolved = 0
    abstained = 0
    wrong_clicks = 0

    for case in cases:
        t0 = time.perf_counter()
        match = _template_match_only(case.live_png, case.template_png)
        source = "template" if match is not None else "none"
        candidates_count = 0

        if match is None and local_model is not None:
            cands = local_model.locate_anchor(case.live_png, case.template_png)
            candidates_count = len(cands)
            # Count, never pick: only accept when exactly 1 unique match exists.
            if len(cands) == 1:
                c = cands[0]
                match = GrounderMatch(
                    point=c.point,
                    region=c.region,
                    confidence=c.score,
                )
                source = "dinov2"

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(elapsed_ms)
        outcome = _score_point(case, match)
        if outcome == "resolved":
            resolved += 1
        elif outcome == "abstained":
            abstained += 1
        else:
            wrong_clicks += 1

        per_case.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "outcome": outcome,
                "source": source,
                "dinov2_candidates": candidates_count,
                "latency_ms": round(elapsed_ms, 2),
                "point": list(match.point) if match is not None else None,
            }
        )

    total = len(cases)
    return {
        "arm": "arm_a_local_template_dinov2",
        "execution_mode": execution_mode,
        "status": "measured",
        "cases_total": total,
        "resolved": resolved,
        "abstained": abstained,
        "wrong_clicks": wrong_clicks,
        "resolved_rate": round(resolved / total, 4) if total else 0.0,
        "human_interventions_per_100": round((abstained / total) * 100.0, 2)
        if total
        else 0.0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2)
        if latencies
        else 0.0,
        "cache_hit_rate": 0.0,
        "per_case": per_case,
    }


def _skipped_arm(
    arm: str, execution_mode: str, env_var: str, total: int
) -> dict[str, Any]:
    return {
        "arm": arm,
        "execution_mode": execution_mode,
        "status": "skipped_no_credentials",
        "reason": f"{env_var} is not set",
        "cases_total": total,
        "resolved": None,
        "abstained": None,
        "wrong_clicks": None,
        "resolved_rate": None,
        "human_interventions_per_100": None,
        "avg_latency_ms": None,
        "cache_hit_rate": None,
        "per_case": [],
    }


def _run_arm_c(cases: list[BakeoffCase]) -> dict[str, Any]:
    """Arm C: LAN RemoteGrounder when OPENADAPT_VLM_URL is set."""
    url = (
        os.environ.get("OPENADAPT_VLM_URL")
        or os.environ.get("OPENADAPT_FLOW_VLM_URL")
        or ""
    ).strip()
    if not url:
        return _skipped_arm(
            "arm_c_lan_remote_vlm", "lan_remote_vlm", "OPENADAPT_VLM_URL", len(cases)
        )

    token = (
        os.environ.get("OPENADAPT_VLM_TOKEN")
        or os.environ.get("OPENADAPT_FLOW_VLM_TOKEN")
        or ""
    ).strip()
    timeout = float(os.environ.get("OPENADAPT_VLM_TIMEOUT", "10.0"))
    grounder = RemoteGrounder(RemoteVLMClient(url, token=token, timeout=timeout))

    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []
    resolved = 0
    abstained = 0
    wrong_clicks = 0

    for case in cases:
        t0 = time.perf_counter()
        match = grounder.locate(case.live_png, case.intent, ocr_text=case.ocr_text)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(elapsed_ms)
        outcome = _score_point(case, match)
        if outcome == "resolved":
            resolved += 1
        elif outcome == "abstained":
            abstained += 1
        else:
            wrong_clicks += 1
        per_case.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "outcome": outcome,
                "latency_ms": round(elapsed_ms, 2),
                "point": list(match.point) if match is not None else None,
            }
        )

    total = len(cases)
    return {
        "arm": "arm_c_lan_remote_vlm",
        "execution_mode": "lan_remote_vlm",
        "status": "measured",
        "cases_total": total,
        "resolved": resolved,
        "abstained": abstained,
        "wrong_clicks": wrong_clicks,
        "resolved_rate": round(resolved / total, 4) if total else 0.0,
        "human_interventions_per_100": round((abstained / total) * 100.0, 2)
        if total
        else 0.0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2)
        if latencies
        else 0.0,
        "cache_hit_rate": 0.0,
        "per_case": per_case,
    }


def _run_arm_d(cases: list[BakeoffCase], *, masked: bool) -> dict[str, Any]:
    """Arm D: ZdrVlmGrounder raw or Synthetic-Twin masked when OPENADAPT_ZDR_VLM_URL is set."""
    arm_name = "arm_d_zdr_vlm_masked" if masked else "arm_d_zdr_vlm_raw"
    mode_name = "zdr_vlm_synthetic_twin_masked" if masked else "zdr_vlm_raw"
    url = (os.environ.get("OPENADAPT_ZDR_VLM_URL") or "").strip()
    if not url:
        return _skipped_arm(arm_name, mode_name, "OPENADAPT_ZDR_VLM_URL", len(cases))

    model = (os.environ.get("OPENADAPT_ZDR_VLM_MODEL") or "gpt-4o").strip()
    api_key = (os.environ.get("OPENADAPT_ZDR_VLM_API_KEY") or "").strip()
    timeout = float(os.environ.get("OPENADAPT_ZDR_VLM_TIMEOUT", "10.0"))
    req_hdr = (
        os.environ.get("OPENADAPT_ZDR_REQUIRE_HEADER") or "1"
    ).strip().lower() not in (
        "0",
        "false",
        "no",
    )

    cache = HealedAnchorCache()
    per_case: list[dict[str, Any]] = []
    latencies: list[float] = []
    resolved = 0
    abstained = 0
    wrong_clicks = 0
    total_masked_boxes = 0
    total_raw_leaks = 0

    for case in cases:
        projector = SyntheticTwinProjector(case.dossier, seed=42) if masked else None
        grounder = ZdrVlmGrounder(
            endpoint_url=url,
            model=model,
            api_key=api_key,
            projector=projector,
            ocr_provider=(lambda _png, boxes=case.ocr_boxes: boxes) if masked else None,
            require_zdr_header=req_hdr,
            timeout=timeout,
            healed_anchor_cache=cache,
        )
        t0 = time.perf_counter()
        match = grounder.locate(case.live_png, case.intent, ocr_text=case.ocr_text)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(elapsed_ms)

        if grounder.last_mask_report is not None:
            total_masked_boxes += grounder.last_mask_report.masked_box_count
            total_raw_leaks += grounder.last_mask_report.raw_dossier_leaks

        outcome = _score_point(case, match)
        # Only commit verified correct clicks to the anchor cache, then check
        # repeat-pass cache reuse at $0.
        if match is not None and outcome == "resolved":
            grounder.record_verified_anchor(
                case.live_png, case.intent, match, ocr_text=case.ocr_text
            )
            _ = grounder.locate(case.live_png, case.intent, ocr_text=case.ocr_text)
        if outcome == "resolved":
            resolved += 1
        elif outcome == "abstained":
            abstained += 1
        else:
            wrong_clicks += 1

        per_case.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "outcome": outcome,
                "latency_ms": round(elapsed_ms, 2),
                "http_calls": grounder.stats.http_calls,
                "cache_hits": grounder.stats.cache_hits,
                "point": list(match.point) if match is not None else None,
            }
        )

    total = len(cases)
    result: dict[str, Any] = {
        "arm": arm_name,
        "execution_mode": mode_name,
        "status": "measured",
        "cases_total": total,
        "resolved": resolved,
        "abstained": abstained,
        "wrong_clicks": wrong_clicks,
        "resolved_rate": round(resolved / total, 4) if total else 0.0,
        "human_interventions_per_100": round((abstained / total) * 100.0, 2)
        if total
        else 0.0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2)
        if latencies
        else 0.0,
        "cache_hit_rate": round(cache.hit_rate, 4),
        "per_case": per_case,
    }
    if masked:
        result["mask_audit"] = {
            "masked_boxes": total_masked_boxes,
            "raw_dossier_leaks": total_raw_leaks,
        }
    return result


def _run_local_mask_audit(cases: list[BakeoffCase]) -> dict[str, Any]:
    """Verify Synthetic-Twin visual masking locally across all cases (no API call)."""
    masked_boxes = 0
    unmasked_boxes = 0
    masked_tokens = 0
    raw_dossier_leaks = 0
    latencies: list[float] = []

    for case in cases:
        projector = SyntheticTwinProjector(case.dossier, seed=42)
        t0 = time.perf_counter()
        _masked_png, report = mask_screenshot_png(
            case.live_png, case.ocr_boxes, projector
        )
        latencies.append((time.perf_counter() - t0) * 1000.0)
        masked_boxes += report.masked_box_count
        unmasked_boxes += report.unmasked_box_count
        masked_tokens += report.masked_token_count
        raw_dossier_leaks += report.raw_dossier_leaks

    return {
        "cases_audited": len(cases),
        "masked_boxes": masked_boxes,
        "unmasked_boxes": unmasked_boxes,
        "masked_tokens": masked_tokens,
        "raw_dossier_leaks": raw_dossier_leaks,
        "avg_mask_latency_ms": round(sum(latencies) / len(latencies), 2)
        if latencies
        else 0.0,
    }


def run_bakeoff() -> dict[str, Any]:
    """Run all 4 arms and return the JSON scoreboard."""
    cases = build_cases()
    arms = [
        _run_arm_a(cases),
        _run_arm_c(cases),
        _run_arm_d(cases, masked=False),
        _run_arm_d(cases, masked=True),
    ]
    hard_gate_ok = all(
        arm["wrong_clicks"] == 0 for arm in arms if arm["status"] == "measured"
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": [{"case_id": c.case_id, "category": c.category} for c in cases],
        "hard_gate_zero_wrong_clicks": hard_gate_ok,
        "local_synthetic_twin_mask_audit": _run_local_mask_audit(cases),
        "arms": arms,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the 4-arm pixel-only grounding escalation bakeoff.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the JSON scoreboard.",
    )
    args = parser.parse_args(argv)
    scoreboard = run_bakeoff()
    rendered = json.dumps(scoreboard, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if scoreboard["hard_gate_zero_wrong_clicks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
