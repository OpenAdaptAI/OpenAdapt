"""Healing: refresh anchors from the live frame after non-template resolution.

When a step succeeds via any rung other than ``template``, the runtime emits
a :class:`~openadapt_flow.ir.HealEvent` carrying a refreshed anchor: a new
template crop taken from the live frame at the resolved location, an updated
region/click_point, and re-OCRed ``ocr_text``. The event is applied to the
in-memory workflow, persisted under ``run_dir/heals/<step_id>/``, and — when
``save_healed_to`` is set on the run — folded into a full healed bundle.
"""

from __future__ import annotations

import io
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_flow.ir import (
    HealEvent,
    Point,
    Region,
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.privacy import scrub_image_bytes as _scrub_png
from openadapt_flow.runtime import identity as identity_mod

_HEAL_TEMPLATE_NAME = "template.png"
_HEAL_SCREEN_NAME = "screen.png"
_HEAL_JSON_NAME = "heal.json"


def _crop_png(frame_png: bytes, region: Region) -> bytes:
    """Crop ``region`` out of a PNG frame and return PNG bytes."""
    x, y, w, h = region
    with Image.open(io.BytesIO(frame_png)) as image:
        crop = image.convert("RGB").crop((x, y, x + w, y + h))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        return buffer.getvalue()


def _frame_size(frame_png: bytes) -> tuple[int, int]:
    """Return (width, height) of a PNG frame."""
    with Image.open(io.BytesIO(frame_png)) as image:
        return image.size


def _clamped_region(
    center: Point, size: tuple[int, int], frame: tuple[int, int]
) -> Region:
    """Region of ``size`` centered at ``center``, clamped inside ``frame``."""
    fw, fh = frame
    w = min(size[0], fw)
    h = min(size[1], fh)
    x = min(max(0, center[0] - w // 2), max(0, fw - w))
    y = min(max(0, center[1] - h // 2), max(0, fh - h))
    return (x, y, w, h)


def _reocr_text(
    vision: Any,
    frame_png: bytes,
    region: Region,
    click_y: int | None = None,
) -> str | None:
    """Re-OCR ``region`` of the frame; return the target's label text.

    OCR often splits one label into several fragments (e.g. ``Submit`` +
    ``Encounter``); fragments whose boxes vertically overlap ``click_y`` are
    joined left-to-right so the healed ``ocr_text`` carries the full label.
    Falls back to the single most confident line when no fragment overlaps
    the click row (or ``click_y`` is not given). Returns None if nothing was
    recognized.
    """
    try:
        lines = vision.ocr(frame_png, region=region)
    except Exception:
        return None
    lines = [
        line
        for line in lines
        if isinstance(getattr(line, "text", None), str) and line.text.strip()
    ]
    if not lines:
        return None
    if click_y is not None:
        row = [
            line
            for line in lines
            if line.region[1] <= click_y <= line.region[1] + line.region[3]
        ]
        if row:
            row.sort(key=lambda line: line.region[0])
            return " ".join(line.text.strip() for line in row)
    best = max(lines, key=lambda line: getattr(line, "confidence", 0.0))
    return best.text.strip()


def _recontext(
    vision: Any,
    frame_png: bytes,
    region: Region,
    click_point: Point,
    frame: tuple[int, int],
) -> str | None:
    """Re-derive the anchor's identity context band from the live frame.

    A healed anchor lives at a NEW position; its recorded context band
    (neighbouring text on the target's row) may no longer describe the new
    surroundings, so it is refreshed from the same frame the heal was
    derived from — exactly what a re-record at this position would capture.
    Returns None (disabling the identity check for the step, honestly) when
    the band yields no usable text.

    The volatility reference date is *today*: the band is being re-recorded
    NOW, so near/far date discrimination anchors on heal time exactly as it
    anchors on the recording date at compile time. Without it every
    date-bearing line is conservatively dropped — a healed anchor in a
    patient banner would silently lose its DOB line, the band's most
    discriminative identity evidence.
    """
    try:
        lines = vision.ocr(frame_png)
    except Exception:
        return None
    return identity_mod.context_from_lines(
        lines,
        exclude_region=region,
        band=identity_mod.band_region(click_point, region[3], frame),
        # Same row refinement the compiler applies: record only the click
        # point's own text row, matching what verification reads back.
        point=click_point,
        reference_date=date.today(),
    )


def build_heal_event(
    step: Step,
    resolution: Resolution,
    matched_region: Region,
    frame_png: bytes,
    vision: Any,
) -> tuple[HealEvent, bytes]:
    """Build a HealEvent (and its new template crop) for a resolved step.

    The new anchor region is the matched region itself for template-based
    rungs; for ocr/geometry/grounder rungs it is a template-sized region
    (same size as the old anchor region) centered on the resolved point,
    clamped to the frame. The new click point is the resolved point, and
    ``ocr_text`` is re-OCRed from the new region (keeping the old text if
    OCR finds nothing).

    Args:
        step: The step that resolved via a non-``template`` rung. Must have
            an anchor.
        resolution: The successful resolution.
        matched_region: Screen region the evidence matched.
        frame_png: The live frame the step resolved against.
        vision: Namespace-like object exposing ``ocr(png, region=...)``.

    Returns:
        ``(event, crop_png)`` — the (not yet applied/persisted) HealEvent and
        the new template crop as PNG bytes.

    Raises:
        ValueError: If ``step`` has no anchor.
    """
    if step.anchor is None:
        raise ValueError(f"step {step.id!r} has no anchor to heal")
    old_anchor = step.anchor
    frame = _frame_size(frame_png)

    if resolution.rung in ("template", "template_global"):
        new_region = matched_region
    else:
        new_region = _clamped_region(
            resolution.point,
            (old_anchor.region[2], old_anchor.region[3]),
            frame,
        )

    crop_png = _crop_png(frame_png, new_region)
    new_text = _reocr_text(vision, frame_png, new_region, click_y=resolution.point[1])
    new_context = _recontext(vision, frame_png, new_region, resolution.point, frame)
    new_anchor = old_anchor.model_copy(
        update={
            "region": new_region,
            "click_point": resolution.point,
            "ocr_text": new_text if new_text is not None else old_anchor.ocr_text,
            "context_text": new_context,
        }
    )
    event = HealEvent(
        step_id=step.id,
        rung_used=resolution.rung,
        old_anchor=old_anchor,
        new_anchor=new_anchor,
    )
    return event, crop_png


def apply_heal(workflow: Workflow, event: HealEvent) -> None:
    """Apply a HealEvent to the in-memory workflow (marks it applied).

    Args:
        workflow: The workflow being replayed; the matching step's anchor is
            replaced with ``event.new_anchor``.
        event: The heal event to apply.

    Raises:
        ValueError: If the workflow has no step with ``event.step_id``.
    """
    for step in workflow.steps:
        if step.id == event.step_id:
            step.anchor = event.new_anchor
            event.applied = True
            return
    raise ValueError(f"workflow has no step {event.step_id!r}")


def persist_heal(
    event: HealEvent,
    crop_png: bytes,
    frame_png: bytes,
    run_dir: Path,
) -> Path:
    """Write heal artifacts under ``run_dir/heals/<step_id>/``.

    Writes the new template crop, the live frame the heal was derived from,
    and ``heal.json`` (the serialized event, with its ``screenshot`` field
    set to the run-dir-relative frame path).

    Args:
        event: The heal event (mutated: ``screenshot`` is set).
        crop_png: New template crop bytes.
        frame_png: Live frame bytes.
        run_dir: The run output directory.

    Returns:
        The heal directory path.
    """
    heal_dir = Path(run_dir) / "heals" / event.step_id
    heal_dir.mkdir(parents=True, exist_ok=True)
    # Heal artifacts (crop + full frame) are embedded by relative path into the
    # shareable REPORT.md. Redact PII/PHI when opt-in image redaction is on
    # (OPENADAPT_FLOW_SCRUB_IMAGES=1); no-op otherwise (see openadapt_flow.privacy).
    (heal_dir / _HEAL_TEMPLATE_NAME).write_bytes(_scrub_png(crop_png))
    (heal_dir / _HEAL_SCREEN_NAME).write_bytes(_scrub_png(frame_png))
    event.screenshot = f"heals/{event.step_id}/{_HEAL_SCREEN_NAME}"
    (heal_dir / _HEAL_JSON_NAME).write_text(event.model_dump_json(indent=2))
    return heal_dir


def write_healed_bundle(
    workflow: Workflow,
    src_bundle_dir: Path,
    dest_bundle_dir: Path,
    new_crops: dict[str, bytes],
) -> Path:
    """Write a full healed bundle: updated workflow + new/unchanged crops.

    Copies every template from the source bundle, overwrites the templates
    of healed steps with their new crops, and writes the (healed, in-memory)
    workflow as ``workflow.json``.

    Args:
        workflow: The healed in-memory workflow.
        src_bundle_dir: The original bundle directory (source of unchanged
            template crops).
        dest_bundle_dir: Destination bundle directory (created if needed).
        new_crops: Mapping of step id -> new template crop PNG bytes for
            healed steps.

    Returns:
        The destination bundle directory path.
    """
    src = Path(src_bundle_dir)
    dest = Path(dest_bundle_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "templates").mkdir(parents=True, exist_ok=True)

    src_templates = src / "templates"
    if src_templates.is_dir():
        for path in sorted(src_templates.rglob("*")):
            if path.is_file():
                target = dest / "templates" / path.relative_to(src_templates)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

    steps_by_id = {step.id: step for step in workflow.steps}
    for step_id, crop in new_crops.items():
        step = steps_by_id.get(step_id)
        if step is None or step.anchor is None:
            continue
        target = dest / step.anchor.template
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(crop)

    workflow.save(dest)
    return dest
