"""Pixel-halt to zero-PHI PrepareCard extractor for RDP and Citrix surfaces.

Isolates newly opened modal dialogs or dropdown popups from before/after
screenshots, extracts prompt text and discrete option/button lines via local
OCR, scrubs known PHI and sensitive patterns, refuses to emit a card if any
raw PHI or secret material survives, and produces a canonical ``PrepareCard``
JSON dict compatible with :mod:`openadapt_flow.cloud_prepare_bridge` and
``openadapt-cloud`` (``POST /api/prepare``).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from openadapt_flow.cloud_prepare_bridge import (
    _RAW_EMAIL_RE,
    _RAW_PHONE_RE,
    _RAW_SECRET_RE,
    _RAW_SSN_RE,
    ZeroPhiViolationError,
    assert_prepare_card_zero_phi,
    assert_zero_phi_value,
)
from openadapt_flow.copilot_bridge import is_secret_key_name
from openadapt_flow.copilot_protocol import (
    FormHaltItem,
    FormHaltKind,
    ObservedOption,
)
from openadapt_flow.report_redaction import REDACTED, find_unredacted

_BLOCK_SIZE = 8
_PROMPT_CUE_WORDS = frozenset(
    {
        "alert",
        "choose",
        "confirm",
        "confirmation",
        "detected",
        "error",
        "failed",
        "notice",
        "please",
        "proceed",
        "required",
        "select",
        "unable",
        "warning",
    }
)


@dataclass(frozen=True)
class OcrWord:
    """One OCR word or phrase with bounding box in crop pixel coordinates."""

    text: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: float = 1.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_pil_and_numpy() -> tuple[Any, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow and numpy are required for pixel diffing. "
            "Install the 'vision' extra (openadapt-flow[vision])."
        ) from exc
    return Image, np


def diff_popup_region(
    before_png: bytes,
    after_png: bytes,
    *,
    min_area_px: int = 400,
    pixel_threshold: int = 18,
    pad_px: int = 2,
) -> tuple[int, int, int, int] | None:
    """Isolate the bounding box ``(x, y, w, h)`` of a newly popped dialog or dropdown.

    Compares ``before_png`` and ``after_png`` using Pillow and numpy, clusters
    changed pixels on a coarse 8x8 grid to ignore small distant cursor or clock
    artifacts, and refines the largest changed cluster to its exact pixel
    bounding box. Returns ``None`` when image sizes differ, nothing changed,
    or the isolated bounding box area is smaller than ``min_area_px``.
    """
    if min_area_px < 1:
        raise ValueError("min_area_px must be at least 1.")
    if pixel_threshold < 1:
        raise ValueError("pixel_threshold must be at least 1.")
    if pad_px < 0:
        raise ValueError("pad_px must be non-negative.")

    Image, np = _load_pil_and_numpy()

    with Image.open(io.BytesIO(before_png)) as before_img:
        before_rgb = before_img.convert("RGB")
        before_arr = np.asarray(before_rgb, dtype=np.int16)
    with Image.open(io.BytesIO(after_png)) as after_img:
        after_rgb = after_img.convert("RGB")
        after_arr = np.asarray(after_rgb, dtype=np.int16)

    if before_arr.shape != after_arr.shape:
        return None

    img_h, img_w = after_arr.shape[:2]
    diff = np.abs(after_arr - before_arr).max(axis=2)
    mask = diff >= pixel_threshold
    if not mask.any():
        return None

    # Downsample to coarse 8x8 blocks and pick the 8-connected component with
    # the largest number of changed pixels so a distant caret blink cannot
    # inflate the popup bounding box.
    grid_h = (img_h + _BLOCK_SIZE - 1) // _BLOCK_SIZE
    grid_w = (img_w + _BLOCK_SIZE - 1) // _BLOCK_SIZE
    pad_h = grid_h * _BLOCK_SIZE - img_h
    pad_w = grid_w * _BLOCK_SIZE - img_w
    if pad_h or pad_w:
        padded = np.pad(mask, ((0, pad_h), (0, pad_w)), constant_values=False)
    else:
        padded = mask
    block_counts = padded.reshape(grid_h, _BLOCK_SIZE, grid_w, _BLOCK_SIZE).sum(
        axis=(1, 3)
    )
    active = block_counts > 0

    visited = np.zeros((grid_h, grid_w), dtype=bool)
    best_score = 0
    best_blocks: list[tuple[int, int]] = []

    active_ys, active_xs = np.nonzero(active)
    for start_y_np, start_x_np in zip(active_ys, active_xs):
        sy, sx = int(start_y_np), int(start_x_np)
        if visited[sy, sx]:
            continue
        queue: deque[tuple[int, int]] = deque([(sy, sx)])
        visited[sy, sx] = True
        comp_blocks: list[tuple[int, int]] = []
        comp_score = 0
        while queue:
            cy, cx = queue.popleft()
            comp_blocks.append((cy, cx))
            comp_score += int(block_counts[cy, cx])
            for dy in (-1, 0, 1):
                ny = cy + dy
                if ny < 0 or ny >= grid_h:
                    continue
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    nx = cx + dx
                    if 0 <= nx < grid_w and active[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
        if comp_score > best_score:
            best_score = comp_score
            best_blocks = comp_blocks

    if not best_blocks:
        return None

    by_min = min(by for by, _ in best_blocks)
    by_max = max(by for by, _ in best_blocks)
    bx_min = min(bx for _, bx in best_blocks)
    bx_max = max(bx for _, bx in best_blocks)

    roi_y0 = by_min * _BLOCK_SIZE
    roi_y1 = min(img_h, (by_max + 1) * _BLOCK_SIZE)
    roi_x0 = bx_min * _BLOCK_SIZE
    roi_x1 = min(img_w, (bx_max + 1) * _BLOCK_SIZE)

    # Restrict to pixels inside the winning component's blocks only.
    comp_mask = np.zeros((roi_y1 - roi_y0, roi_x1 - roi_x0), dtype=bool)
    for by, bx in best_blocks:
        py0 = by * _BLOCK_SIZE - roi_y0
        py1 = min(img_h, (by + 1) * _BLOCK_SIZE) - roi_y0
        px0 = bx * _BLOCK_SIZE - roi_x0
        px1 = min(img_w, (bx + 1) * _BLOCK_SIZE) - roi_x0
        comp_mask[py0:py1, px0:px1] = mask[
            roi_y0 + py0 : roi_y0 + py1,
            roi_x0 + px0 : roi_x0 + px1,
        ]

    ys, xs = np.nonzero(comp_mask)
    if ys.size == 0:
        return None

    x0 = roi_x0 + int(xs.min())
    x1 = roi_x0 + int(xs.max()) + 1
    y0 = roi_y0 + int(ys.min())
    y1 = roi_y0 + int(ys.max()) + 1

    raw_w = x1 - x0
    raw_h = y1 - y0
    if raw_w * raw_h < min_area_px:
        return None

    cx0 = max(0, x0 - pad_px)
    cy0 = max(0, y0 - pad_px)
    cx1 = min(img_w, x1 + pad_px)
    cy1 = min(img_h, y1 + pad_px)
    return (cx0, cy0, cx1 - cx0, cy1 - cy0)


def _crop_png(png_bytes: bytes, region: tuple[int, int, int, int]) -> bytes:
    Image, _ = _load_pil_and_numpy()
    x, y, w, h = region
    with Image.open(io.BytesIO(png_bytes)) as img:
        cropped = img.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()


def _default_ocr_fn(png_bytes: bytes) -> list[OcrWord]:
    try:
        from openadapt_flow.vision.ocr import ocr as local_ocr
    except ImportError as exc:
        raise RuntimeError(
            "Local OCR backend is unavailable. Install openadapt-flow[vision] "
            "or pass a custom ocr_fn."
        ) from exc

    lines = local_ocr(png_bytes)
    words: list[OcrWord] = []
    for line in lines:
        rx, ry, rw, rh = line.region
        words.append(
            OcrWord(
                text=line.text,
                x=int(rx),
                y=int(ry),
                width=int(rw),
                height=int(rh),
                confidence=float(line.confidence),
            )
        )
    return words


def _normalize_ocr_word(item: Any) -> OcrWord | None:
    if isinstance(item, OcrWord):
        text = " ".join(item.text.split())
        if not text:
            return None
        return OcrWord(
            text=text,
            x=int(item.x),
            y=int(item.y),
            width=int(item.width),
            height=int(item.height),
            confidence=float(item.confidence),
        )
    if isinstance(item, Mapping):
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            return None
        return OcrWord(
            text=text,
            x=int(item.get("x", 0)),
            y=int(item.get("y", 0)),
            width=int(item.get("width", 0)),
            height=int(item.get("height", 0)),
            confidence=float(item.get("confidence", 1.0)),
        )
    if hasattr(item, "text") and hasattr(item, "region"):
        text = " ".join(str(item.text).split())
        if not text:
            return None
        rx, ry, rw, rh = item.region
        conf = float(getattr(item, "confidence", 1.0))
        return OcrWord(
            text=text,
            x=int(rx),
            y=int(ry),
            width=int(rw),
            height=int(rh),
            confidence=conf,
        )
    raise TypeError(f"Unsupported OCR item type: {type(item).__name__}")


def _group_ocr_rows(words: Sequence[OcrWord]) -> list[list[str]]:
    """Group OCR words into visual rows, preserving wide horizontal gaps as distinct segments."""
    if not words:
        return []

    has_geometry = any(
        w.x != 0 or w.y != 0 or w.width != 0 or w.height != 0 for w in words
    )
    if not has_geometry:
        return [[w.text] for w in words]

    ordered = sorted(words, key=lambda w: (w.y, w.x))
    rows: list[list[OcrWord]] = []
    row_centers: list[float] = []
    row_heights: list[float] = []

    for word in ordered:
        word_h = float(word.height) if word.height > 0 else 16.0
        word_cy = float(word.y) + word_h / 2.0
        placed = False
        for idx, (rcy, rh) in enumerate(zip(row_centers, row_heights)):
            tolerance = max(10.0, 0.6 * min(word_h, rh))
            if abs(word_cy - rcy) <= tolerance:
                rows[idx].append(word)
                count = len(rows[idx])
                row_centers[idx] = ((rcy * (count - 1)) + word_cy) / count
                row_heights[idx] = ((rh * (count - 1)) + word_h) / count
                placed = True
                break
        if not placed:
            rows.append([word])
            row_centers.append(word_cy)
            row_heights.append(word_h)

    row_segments: list[list[str]] = []
    for row, rh in zip(rows, row_heights):
        row_sorted = sorted(row, key=lambda w: w.x)
        gap_threshold = max(18.0, 1.5 * rh)
        segments: list[list[OcrWord]] = [[row_sorted[0]]]
        for word in row_sorted[1:]:
            prev = segments[-1][-1]
            prev_right = prev.x + prev.width
            gap = word.x - prev_right
            if prev.width > 0 and gap > gap_threshold:
                segments.append([word])
            else:
                segments[-1].append(word)
        row_segments.append(
            [" ".join(tok.text for tok in seg).strip() for seg in segments]
        )

    return row_segments


def _looks_like_prompt_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith((".", "?", "!", ":")):
        return True
    tokens = re.findall(r"[a-zA-Z]+", stripped.lower())
    if len(tokens) > 6:
        return True
    return any(tok in _PROMPT_CUE_WORDS for tok in tokens)


def _split_prompt_and_options(
    row_segments: Sequence[Sequence[str]],
    *,
    field_label: str,
) -> tuple[str, list[str], bool]:
    """Split grouped OCR rows into ``(prompt_text, allowed_enum_options, is_modal)``."""
    non_empty_rows = [
        [seg.strip() for seg in row if seg.strip()]
        for row in row_segments
        if any(seg.strip() for seg in row)
    ]
    if not non_empty_rows:
        return f"Pixel halt requires input for {field_label}.", [], False

    # Case 1: Modal dialog with side-by-side action buttons on the bottom row.
    if len(non_empty_rows[-1]) >= 2:
        options = _dedupe_preserve_order(non_empty_rows[-1])
        prompt_lines = [
            " ".join(row) for row in non_empty_rows[:-1] if any(s for s in row)
        ]
        prompt_text = (
            " ".join(prompt_lines).strip() or f"Confirm action for {field_label}."
        )
        return prompt_text, options, True

    flat_lines = [" ".join(row).strip() for row in non_empty_rows]
    if len(flat_lines) == 1:
        return flat_lines[0], [], False

    # Case 2: Prompt header line(s) followed by stacked option/button lines.
    prompt_prefix_len = 0
    for line in flat_lines:
        if _looks_like_prompt_line(line):
            prompt_prefix_len += 1
        else:
            break

    if 0 < prompt_prefix_len < len(flat_lines):
        remaining = _dedupe_preserve_order(flat_lines[prompt_prefix_len:])
        if len(remaining) >= 2:
            prompt_text = " ".join(flat_lines[:prompt_prefix_len]).strip()
            return prompt_text, remaining, True

    # Case 3: Clicked-open dropdown popup containing discrete option lines.
    options = _dedupe_preserve_order(flat_lines)
    return f"Select an option for {field_label}.", options, False


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _assert_no_secrets_in_text(text: str, context_label: str) -> None:
    if _RAW_SECRET_RE.search(text):
        raise ZeroPhiViolationError(
            f"Refused unscrubbable secret token in {context_label}; keep credentials local."
        )
    unredacted = find_unredacted({"text": text})
    if unredacted:
        reasons = ", ".join(r.reason for r in unredacted[:3])
        raise ZeroPhiViolationError(
            f"Refused secret or credential material in {context_label} ({reasons})."
        )


def _scrub_ocr_text(
    text: str,
    *,
    known_phi_values: Sequence[str],
    context_label: str,
) -> str:
    """Scrub known PHI and SSN/email/phone patterns; reject unscrubbable secrets."""
    _assert_no_secrets_in_text(text, context_label)

    scrubbed = text
    cleaned_phi = sorted(
        {
            val.strip()
            for val in known_phi_values
            if isinstance(val, str) and val.strip()
        },
        key=len,
        reverse=True,
    )
    for phi_val in cleaned_phi:
        pattern = re.compile(re.escape(phi_val), re.IGNORECASE)
        scrubbed = pattern.sub(REDACTED, scrubbed)

    scrubbed = _RAW_SSN_RE.sub(REDACTED, scrubbed)
    scrubbed = _RAW_EMAIL_RE.sub(REDACTED, scrubbed)
    scrubbed = _RAW_PHONE_RE.sub(REDACTED, scrubbed)
    scrubbed = " ".join(scrubbed.split())

    assert_zero_phi_value(scrubbed, context_label)
    lowered = scrubbed.casefold()
    for phi_val in cleaned_phi:
        if phi_val.casefold() in lowered:
            raise ZeroPhiViolationError(
                f"Refused PrepareCard: known PHI survived scrubbing in {context_label}."
            )
    return scrubbed


def _assert_no_known_phi_in_card(
    card: Mapping[str, Any], known_phi_values: Sequence[str]
) -> None:
    cleaned_phi = [
        val.strip() for val in known_phi_values if isinstance(val, str) and val.strip()
    ]
    if not cleaned_phi:
        return
    serialized = json.dumps(card, ensure_ascii=False).casefold()
    for phi_val in cleaned_phi:
        if phi_val.casefold() in serialized:
            raise ZeroPhiViolationError(
                "Refused PrepareCard: known PHI value survived in final card payload."
            )


def extract_pixel_prepare_card(
    *,
    before_png: bytes | None,
    after_png: bytes,
    portal_origin: str,
    field_label: str,
    card_id: str | None = None,
    ocr_fn: Callable[[bytes], Sequence[OcrWord]] | None = None,
    known_phi_values: Sequence[str] = (),
    staged_bindings: Mapping[int, str] | None = None,
    workflow_summary: str | None = None,
) -> dict[str, Any]:
    """Extract a canonical zero-PHI ``PrepareCard`` JSON dict from a pixel-only halt.

    When ``before_png`` is provided, ``diff_popup_region`` isolates the newly
    opened popup or modal dialog and crops ``after_png`` strictly to that box
    before running ``ocr_fn`` (defaulting to OpenAdapt's local OCR backend).
    Any ``known_phi_values`` plus SSNs, emails, and phone numbers in OCR text
    are scrubbed to ``<redacted>``. Unscrubbable secrets or any surviving raw
    PHI cause ``ZeroPhiViolationError`` to be raised before a card is emitted.
    """
    clean_origin = portal_origin.strip()
    if not clean_origin:
        raise ValueError("portal_origin must be a non-empty string.")
    parsed_origin = urllib.parse.urlsplit(clean_origin)
    if parsed_origin.username or parsed_origin.password:
        raise ZeroPhiViolationError("Refused credentials embedded in portal_origin.")

    clean_field_label = field_label.strip()
    if not clean_field_label:
        raise ValueError("field_label must be a non-empty string.")
    if is_secret_key_name(clean_field_label):
        raise ZeroPhiViolationError(
            "Refused to send a secret or credential field to Cloud Prepare; handle it locally."
        )
    _assert_no_secrets_in_text(clean_field_label, "field_label")
    assert_zero_phi_value(clean_field_label, "field_label")

    if not after_png:
        raise ValueError("after_png must be non-empty PNG bytes.")

    ocr_input_png = after_png
    if before_png is not None:
        region = diff_popup_region(before_png, after_png)
        if region is not None:
            ocr_input_png = _crop_png(after_png, region)

    runner = ocr_fn if ocr_fn is not None else _default_ocr_fn
    raw_items = runner(ocr_input_png)
    normalized_words = [
        word for item in raw_items if (word := _normalize_ocr_word(item)) is not None
    ]

    row_segments = _group_ocr_rows(normalized_words)
    raw_prompt, raw_options, is_modal = _split_prompt_and_options(
        row_segments, field_label=clean_field_label
    )

    scrubbed_prompt = _scrub_ocr_text(
        raw_prompt,
        known_phi_values=known_phi_values,
        context_label="pixel prompt text",
    )
    scrubbed_options = _dedupe_preserve_order(
        [
            _scrub_ocr_text(
                opt,
                known_phi_values=known_phi_values,
                context_label=f"pixel option[{idx}]",
            )
            for idx, opt in enumerate(raw_options)
        ]
    )

    staged: dict[int, str] = {
        int(idx): str(val) for idx, val in (staged_bindings or {}).items()
    }
    control_idx = (max(staged.keys()) + 1) if staged else 0

    resolved_card_id = (card_id or "").strip()
    if not resolved_card_id:
        digest = hashlib.sha256(
            after_png
            + b"|"
            + clean_origin.encode("utf-8")
            + b"|"
            + clean_field_label.encode("utf-8")
        ).hexdigest()[:12]
        resolved_card_id = f"prep_pixel_{digest}"

    input_mode = "choice" if scrubbed_options else "typed"
    if input_mode == "typed":
        pause_reason = "missing_required_field"
        pause_category = "Missing Info"
    elif is_modal:
        pause_reason = "high_risk_approval"
        pause_category = "Policy Sign-Off"
    else:
        pause_reason = "policy_conflict"
        pause_category = "Policy Sign-Off"

    unresolved_slot: dict[str, Any] = {
        "control_idx": control_idx,
        "control_label": clean_field_label,
        "reason": scrubbed_prompt,
        "candidate_vars": [],
        "allowed_enum_options": scrubbed_options,
        "expected_field_type": "exact",
        "input_mode": input_mode,
    }

    card: dict[str, Any] = {
        "card_id": resolved_card_id,
        "portal_origin": clean_origin,
        "staged_bindings": staged,
        "unresolved_slots": [unresolved_slot],
        "control_labels": {control_idx: clean_field_label},
        "control_selectors": {},
        "created_at_utc": _utc_now_iso(),
        "commit_mode": "DRAFT_TO_COMMIT",
        "redacted_preview": True,
        "pause_reason": pause_reason,
        "pause_category": pause_category,
        "session_state": "live",
    }
    if workflow_summary and workflow_summary.strip():
        clean_summary = workflow_summary.strip()
        _assert_no_secrets_in_text(clean_summary, "workflow_summary")
        card["workflow_summary"] = clean_summary

    assert_prepare_card_zero_phi(card)
    unredacted = find_unredacted(card)
    if unredacted:
        reasons = ", ".join(r.reason for r in unredacted[:3])
        raise ZeroPhiViolationError(
            f"Refused PrepareCard: unredacted credential material detected ({reasons})."
        )
    _assert_no_known_phi_in_card(card, known_phi_values)
    return card


def pixel_prepare_card_to_form_halt(card: Mapping[str, Any]) -> FormHaltItem:
    """Build a ``FormHaltItem`` matching a pixel-extracted ``PrepareCard``.

    Pairs with :func:`openadapt_flow.cloud_prepare_bridge.prepare_resolution_to_human_input`
    so a polled Cloud Prepare resolution converts back into a local
    ``HumanResolutionInput``.
    """
    slots = card.get("unresolved_slots")
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], Mapping):
        raise ValueError("PrepareCard must contain at least one unresolved slot.")
    slot = slots[0]
    card_id = str(card.get("card_id") or "").strip()
    if not card_id:
        raise ValueError("PrepareCard must contain a non-empty card_id.")

    pause_reason = str(card.get("pause_reason") or "").strip()
    if pause_reason == "high_risk_approval":
        kind = FormHaltKind.MODAL_CONFIRMATION_REQUIRED
    elif pause_reason == "missing_required_field":
        kind = FormHaltKind.UNGROUNDED_REQUIRED_FIELD
    else:
        kind = FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET

    raw_options = slot.get("allowed_enum_options") or ()
    options = tuple(
        ObservedOption(value=str(opt), label=str(opt))
        for opt in raw_options
        if str(opt).strip()
    )
    control_idx = slot.get("control_idx")
    control_id = str(control_idx) if control_idx is not None else None
    field_label = str(slot.get("control_label") or "").strip()
    message = (
        str(slot.get("reason") or "").strip() or "Pixel halt requires operator input."
    )

    return FormHaltItem(
        halt_id=card_id,
        kind=kind,
        control_id=control_id,
        field_label=field_label,
        message=message,
        available_options=options,
    )


__all__ = [
    "OcrWord",
    "diff_popup_region",
    "extract_pixel_prepare_card",
    "pixel_prepare_card_to_form_halt",
]
