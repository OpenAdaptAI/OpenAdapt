#!/usr/bin/env python3
"""Enrich existing demo steps with GroundingTarget data.

Populates GroundingTarget fields on each click step in a demo.json, using
screenshot crops and OCR when real screenshots are available, or falling back
to description-derived heuristics when they are not.

Idempotent: re-running overwrites existing grounding_target data rather than
duplicating it.

Usage:
    # Offline enrichment (no VM needed, populates from descriptions)
    python scripts/enrich_demo_targets.py \
        --demo-dir demos/custom-clear-chrome-data

    # With a specific demo subdirectory
    python scripts/enrich_demo_targets.py \
        --demo-dir demos/custom-clear-chrome-data \
        --demo-id manual

    # With a live WAA server for OCR (optional)
    python scripts/enrich_demo_targets.py \
        --demo-dir demos/custom-clear-chrome-data \
        --server-url http://localhost:5001

    # Custom crop size (pixels, used when screenshots are available)
    python scripts/enrich_demo_targets.py \
        --demo-dir demos/custom-clear-chrome-data \
        --crop-size 120

    # Assumed screen resolution for converting normalized coords to pixels
    python scripts/enrich_demo_targets.py \
        --demo-dir demos/custom-clear-chrome-data \
        --resolution 1920x1080
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import fire

# Allow running from repo root without install
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openadapt_evals.grounding import GroundingTarget

logger = logging.getLogger(__name__)

# Default assumed screen resolution when converting normalized coordinates
# to pixel coordinates for crop bounding boxes.
DEFAULT_RESOLUTION = (1920, 1080)

# Default crop half-size in pixels (around the click point).
DEFAULT_CROP_HALF = 60

# Minimum file size to consider a screenshot "real" (not a placeholder).
_MIN_SCREENSHOT_BYTES = 100

# Keywords for inferring target_type from step descriptions.
_TARGET_TYPE_KEYWORDS: dict[str, list[str]] = {
    "button": ["button", "btn", "clear", "ok", "cancel", "submit", "apply",
               "delete", "save", "close", "open", "confirm", "accept",
               "dismiss", "next", "back", "done", "yes", "no"],
    "icon": ["icon", "shortcut", "logo", "desktop icon"],
    "checkbox": ["checkbox", "check box", "toggle"],
    "tab": ["tab"],
    "text_field": ["text field", "input", "search bar", "address bar",
                   "text area", "textbox"],
    "link": ["link", "hyperlink", "url"],
    "menu_item": ["menu", "dropdown", "option", "select"],
    "dialog": ["dialog", "modal", "popup", "prompt"],
}


def _infer_target_type(description: str) -> str:
    """Infer the UI target type from a step description string."""
    desc_lower = description.lower()
    for target_type, keywords in _TARGET_TYPE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in desc_lower:
                return target_type
    return "unknown"


def _extract_nearby_text(description: str) -> list[str]:
    """Extract likely UI labels from a step description.

    Pulls quoted strings and capitalized phrases that likely correspond to
    visible text labels on the screen.
    """
    labels: list[str] = []

    # Quoted strings (single or double quotes)
    for match in re.finditer(r"""['"]([^'"]+)['"]""", description):
        text = match.group(1).strip()
        if text and len(text) > 1:
            labels.append(text)

    # If no quoted strings, split on common stop words and extract
    # capitalized phrases that might be UI labels
    if not labels:
        # Look for sequences of capitalized words (potential UI labels)
        for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b",
                                 description):
            text = match.group(1).strip()
            # Skip common non-label words
            if text.lower() not in {
                "click", "press", "type", "open", "close", "the", "double",
                "right", "chrome", "google", "windows",
            } and len(text) > 2:
                labels.append(text)

    return labels


def _has_real_screenshot(demo_dir: Path, screenshot_path: str) -> bool:
    """Check if a screenshot path points to a real (non-placeholder) image."""
    if not screenshot_path:
        return False
    full_path = demo_dir / screenshot_path
    if not full_path.exists():
        return False
    return full_path.stat().st_size > _MIN_SCREENSHOT_BYTES


def _crop_screenshot(
    image_path: Path,
    center_x: int,
    center_y: int,
    half_size: int,
    output_path: Path,
) -> tuple[int, int, int, int] | None:
    """Crop a region around (center_x, center_y) and save it.

    Returns the crop bounding box (x1, y1, x2, y2) or None on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; skipping crop extraction")
        return None

    try:
        img = Image.open(image_path)
    except Exception as exc:
        logger.warning("Failed to open screenshot %s: %s", image_path, exc)
        return None

    w, h = img.size
    x1 = max(0, center_x - half_size)
    y1 = max(0, center_y - half_size)
    x2 = min(w, center_x + half_size)
    y2 = min(h, center_y + half_size)

    crop = img.crop((x1, y1, x2, y2))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output_path, "PNG")
    logger.info("Saved crop: %s (%dx%d)", output_path, x2 - x1, y2 - y1)
    return (x1, y1, x2, y2)


def _run_ocr_on_screenshot(image_path: Path) -> list[dict]:
    """Run OCR on a screenshot file, returning text regions.

    Uses grounding.run_ocr if available, gracefully returns [] otherwise.
    """
    try:
        from openadapt_evals.grounding import run_ocr
        screenshot_bytes = image_path.read_bytes()
        return run_ocr(screenshot_bytes)
    except Exception as exc:
        logger.debug("OCR failed on %s: %s", image_path, exc)
        return []


def _nearby_text_from_ocr(
    ocr_results: list[dict],
    center_x: int,
    center_y: int,
    radius: int = 200,
) -> list[str]:
    """Extract OCR text near a given point."""
    import math

    texts = []
    for item in ocr_results:
        bbox = item.get("bbox")
        text = item.get("text", "").strip()
        if not bbox or not text:
            continue
        # Bounding box center
        bx = (bbox[0] + bbox[2]) / 2
        by = (bbox[1] + bbox[3]) / 2
        dist = math.sqrt((bx - center_x) ** 2 + (by - center_y) ** 2)
        if dist <= radius:
            texts.append(text)
    return texts


def enrich_demo(
    demo_dir: str,
    demo_id: str | None = None,
    server_url: str | None = None,
    crop_size: int = DEFAULT_CROP_HALF * 2,
    resolution: str = "1920x1080",
) -> None:
    """Enrich a demo's click steps with GroundingTarget data.

    Args:
        demo_dir: Path to the demo directory (e.g., demos/custom-clear-chrome-data).
        demo_id: Optional demo subdirectory name (e.g., "manual"). If not given,
            looks for demo.json directly in demo_dir, then tries subdirectories.
        server_url: Optional WAA server URL (unused currently, reserved for
            future OCR-via-server support).
        crop_size: Crop region size in pixels (width and height around click).
        resolution: Assumed screen resolution as WIDTHxHEIGHT (for converting
            normalized coordinates to pixels).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    demo_path = Path(demo_dir)
    if not demo_path.exists():
        logger.error("Demo directory does not exist: %s", demo_path)
        sys.exit(1)

    # Parse resolution
    try:
        res_parts = resolution.lower().split("x")
        screen_w, screen_h = int(res_parts[0]), int(res_parts[1])
    except (ValueError, IndexError):
        logger.error("Invalid resolution format: %s (expected WIDTHxHEIGHT)",
                     resolution)
        sys.exit(1)

    half_size = crop_size // 2

    # Find demo.json
    if demo_id:
        demo_json_path = demo_path / demo_id / "demo.json"
    elif (demo_path / "demo.json").exists():
        demo_json_path = demo_path / "demo.json"
    else:
        # Try to find a subdirectory with demo.json
        candidates = list(demo_path.glob("*/demo.json"))
        if not candidates:
            logger.error("No demo.json found in %s", demo_path)
            sys.exit(1)
        demo_json_path = candidates[0]
        logger.info("Found demo.json at: %s", demo_json_path)

    demo_json_dir = demo_json_path.parent

    # Load demo
    with open(demo_json_path) as f:
        demo_data = json.load(f)

    steps = demo_data.get("steps", [])
    if not steps:
        logger.warning("Demo has no steps: %s", demo_json_path)
        return

    enriched_count = 0
    skipped_count = 0

    for i, step in enumerate(steps):
        action_type = step.get("action_type", "")
        description = step.get("description", "")

        # Only enrich click-type steps
        if action_type not in ("click", "double_click", "right_click"):
            logger.info(
                "Step %d: skipping non-click action (%s)", i, action_type
            )
            skipped_count += 1
            continue

        x_norm = step.get("x")
        y_norm = step.get("y")

        if x_norm is None or y_norm is None:
            logger.info("Step %d: skipping click with no coordinates", i)
            skipped_count += 1
            continue

        # Convert normalized coordinates to pixel coordinates
        px = int(x_norm * screen_w)
        py = int(y_norm * screen_h)

        screenshot_path = step.get("screenshot_path", "")
        has_screenshot = _has_real_screenshot(demo_json_dir, screenshot_path)

        # Determine paths for before/after screenshots
        screenshot_before_path = screenshot_path if has_screenshot else ""
        screenshot_after_path = ""
        if i + 1 < len(steps):
            next_ss = steps[i + 1].get("screenshot_path", "")
            if _has_real_screenshot(demo_json_dir, next_ss):
                screenshot_after_path = next_ss

        # Extract or crop
        crop_path = ""
        crop_bbox: tuple[int, int, int, int] | None = None
        nearby_text: list[str] = []
        surrounding_labels: list[str] = []

        if has_screenshot:
            full_ss_path = demo_json_dir / screenshot_path
            # Extract crop
            crop_filename = f"crop_step_{i:03d}.png"
            crop_output = demo_json_dir / crop_filename
            bbox = _crop_screenshot(
                full_ss_path, px, py, half_size, crop_output
            )
            if bbox is not None:
                crop_path = crop_filename
                crop_bbox = bbox

            # Run OCR for nearby text
            ocr_results = _run_ocr_on_screenshot(full_ss_path)
            if ocr_results:
                nearby_text = _nearby_text_from_ocr(
                    ocr_results, px, py, radius=200
                )
                surrounding_labels = _nearby_text_from_ocr(
                    ocr_results, px, py, radius=400
                )
            else:
                logger.info(
                    "Step %d: no OCR results, falling back to description",
                    i,
                )
                nearby_text = _extract_nearby_text(description)
        else:
            # No real screenshot -- populate from description
            logger.warning(
                "Step %d: no real screenshot (path=%r), using description "
                "heuristics. Record real screenshots for full cascade support.",
                i,
                screenshot_path,
            )
            nearby_text = _extract_nearby_text(description)

            # Compute a synthetic crop bbox from the click coordinates
            x1 = max(0, px - half_size)
            y1 = max(0, py - half_size)
            x2 = min(screen_w, px + half_size)
            y2 = min(screen_h, py + half_size)
            crop_bbox = (x1, y1, x2, y2)

        # Infer target type
        target_type = _infer_target_type(description)

        # Compute click offset relative to crop top-left
        click_offset: tuple[int, int] | None = None
        if crop_bbox is not None:
            click_offset = (px - crop_bbox[0], py - crop_bbox[1])

        # Determine window_title from metadata
        window_title = step.get("metadata", {}).get("window_title", "")

        # Build the GroundingTarget
        gt = GroundingTarget(
            description=description,
            target_type=target_type,
            crop_path=crop_path,
            crop_bbox=crop_bbox,
            click_offset=click_offset,
            nearby_text=nearby_text,
            window_title=window_title,
            surrounding_labels=surrounding_labels,
            screenshot_before_path=screenshot_before_path,
            screenshot_after_path=screenshot_after_path,
            disappearance_text=[],
            appearance_text=[],
            expected_change=description,
        )

        # Serialize and attach to step
        step["grounding_target"] = gt.to_dict()
        enriched_count += 1
        logger.info(
            "Step %d: enriched (type=%s, nearby_text=%s, crop=%s)",
            i,
            target_type,
            nearby_text[:3],
            crop_path or "(none)",
        )

    # Save the enriched demo
    with open(demo_json_path, "w") as f:
        json.dump(demo_data, f, indent=2)

    print(f"\nEnrichment complete: {demo_json_path}")
    print(f"  Enriched: {enriched_count} click steps")
    print(f"  Skipped:  {skipped_count} non-click steps")
    print(f"  Total:    {len(steps)} steps")

    if not any(
        _has_real_screenshot(
            demo_json_dir, s.get("screenshot_path", "")
        )
        for s in steps
    ):
        print(
            "\n  WARNING: No real screenshots found in this demo."
            "\n  Run record_demo_screenshots.py to capture screenshots"
            "\n  from a live WAA VM for full cascade support."
        )


if __name__ == "__main__":
    fire.Fire(enrich_demo)
