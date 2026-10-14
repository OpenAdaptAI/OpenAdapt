#!/usr/bin/env python3
"""Generate a markdown review artifact for the demo recording pipeline.

Reads a WAA recording (meta.json + screenshots), creates thumbnail images,
and produces a markdown file showing the pipeline output for each step.
The markdown is suitable for embedding in docs or PR descriptions and
renders on GitHub with relative image paths. Thumbnails link to full-resolution
originals when available.

Usage:
    python scripts/generate_demo_review.py \
        --recording waa_recordings/04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS \
        --text-demo demo_prompts/04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS.txt \
        --vlm-demo demo_prompts_vlm/04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS.txt \
        --output docs/demo_review.md
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from PIL import Image

THUMBNAIL_WIDTH = 600


def _parse_demo_steps(demo_text: str) -> dict[int, str]:
    """Parse a demo .txt file into a dict mapping step number -> step content.

    Handles both text-only and VLM-enriched formats. Returns the full text
    block for each step (everything between "Step N:" markers).
    """
    steps: dict[int, str] = {}
    # Split on "Step N:" headers, capturing the step number
    parts = re.split(r'^(Step \d+:)\s*$', demo_text, flags=re.MULTILINE)

    # parts looks like: [preamble, "Step 1:", content, "Step 2:", content, ...]
    for i in range(1, len(parts) - 1, 2):
        header = parts[i]  # e.g. "Step 3:"
        content = parts[i + 1]
        step_num = int(re.search(r'\d+', header).group())
        # Strip trailing blank lines / separators but preserve internal structure
        content = content.strip()
        # Remove trailing "---" if it's the last step
        if content.endswith("---"):
            content = content[:-3].strip()
        steps[step_num] = content

    return steps


def _create_thumbnail(src: Path, dst: Path, width: int = THUMBNAIL_WIDTH) -> None:
    """Resize an image to the given width, preserving aspect ratio.

    Saves as JPEG for smaller file sizes (thumbnails are for preview only).
    """
    with Image.open(src) as img:
        if img.width <= width:
            out = img.convert("RGB") if img.mode in ("RGBA", "P") else img
            out.save(dst, format="JPEG", quality=80, optimize=True)
            return
        ratio = width / img.width
        new_height = int(img.height * ratio)
        resized = img.resize((width, new_height), Image.LANCZOS)
        if resized.mode in ("RGBA", "P"):
            resized = resized.convert("RGB")
        resized.save(dst, format="JPEG", quality=80, optimize=True)


def _relpath(target: Path, start: Path) -> str:
    """Compute a relative path from start to target, suitable for markdown."""
    try:
        return str(target.resolve().relative_to(start.resolve()))
    except ValueError:
        return os.path.relpath(target.resolve(), start.resolve())


def _escape_md(text: str) -> str:
    """Minimal escaping so that text doesn't break markdown tables."""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _indent_block(text: str, prefix: str = "> ") -> str:
    """Indent every line of text with the given prefix."""
    return "\n".join(prefix + line for line in text.split("\n"))


def main(
    recording: str,
    text_demo: str | None = None,
    vlm_demo: str | None = None,
    output: str = "docs/demo_review.md",
    thumbnail_width: int = THUMBNAIL_WIDTH,
) -> None:
    """Generate a markdown review of the demo pipeline output.

    Args:
        recording: Path to the recording directory (contains meta.json + PNGs).
        text_demo: Path to the text-only demo .txt file.
        vlm_demo: Path to the VLM-enriched demo .txt file.
        output: Output path for the generated markdown file.
        thumbnail_width: Width in pixels for thumbnail images.
    """
    recording_dir = Path(recording)
    output_path = Path(output)

    # --- Validate inputs ---
    meta_path = recording_dir / "meta.json"
    if not meta_path.exists():
        print(f"Error: meta.json not found in {recording_dir}")
        sys.exit(1)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    task_id = meta["task_id"]
    instruction = meta["instruction"]
    num_steps = meta.get("num_steps", len(meta.get("steps", [])))
    steps = meta.get("steps", [])
    recorded_at = meta.get("recorded_at", "unknown")

    # --- Parse demo files ---
    text_steps: dict[int, str] = {}
    vlm_steps: dict[int, str] = {}

    if text_demo:
        text_demo_path = Path(text_demo)
        if text_demo_path.exists():
            text_steps = _parse_demo_steps(
                text_demo_path.read_text(encoding="utf-8")
            )
        else:
            print(f"Warning: text demo not found at {text_demo_path}")

    if vlm_demo:
        vlm_demo_path = Path(vlm_demo)
        if vlm_demo_path.exists():
            vlm_steps = _parse_demo_steps(
                vlm_demo_path.read_text(encoding="utf-8")
            )
        else:
            print(f"Warning: VLM demo not found at {vlm_demo_path}")

    # --- Create JPEG thumbnails and reference originals for full-res ---
    thumb_dir = output_path.parent / "artifacts" / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    thumbnail_map: dict[str, Path] = {}
    full_map: dict[str, Path] = {}
    for i in range(num_steps):
        for suffix in ("before", "after"):
            name = f"step_{i:02d}_{suffix}"
            src = recording_dir / f"{name}.png"
            if src.exists():
                dst = thumb_dir / f"{name}.jpg"
                _create_thumbnail(src, dst, width=thumbnail_width)
                thumbnail_map[name] = dst
                # Link to the original in waa_recordings/ (no copy needed)
                full_map[name] = src

    print(f"Created {len(thumbnail_map)} thumbnails in {thumb_dir}")

    # --- Build markdown ---
    md_dir = output_path.parent
    md_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    # Header
    lines.append("# Demo Pipeline Review")
    lines.append("")
    lines.append(f"**Task ID:** `{task_id}`")
    lines.append("")
    lines.append(f"**Instruction:** {instruction}")
    lines.append("")
    lines.append(f"**Steps:** {num_steps}")
    lines.append("")
    lines.append(f"**Recorded at:** {recorded_at}")
    lines.append("")

    # --- Comparison table (first 3 steps) ---
    compare_count = min(3, num_steps)
    if text_steps or vlm_steps:
        lines.append("## Text vs VLM Comparison (First 3 Steps)")
        lines.append("")
        lines.append(
            "| Step | Ground Truth | Text-Only Demo | VLM-Enriched Demo |"
        )
        lines.append("|------|-------------|----------------|-------------------|")

        for i in range(compare_count):
            step_num = i + 1
            gt = steps[i].get("suggested_step", "") if i < len(steps) else ""
            text_content = _escape_md(text_steps.get(step_num, "*(not available)*"))
            vlm_content = _escape_md(vlm_steps.get(step_num, "*(not available)*"))
            gt_escaped = _escape_md(gt)
            lines.append(
                f"| {step_num} | {gt_escaped} | {text_content} | {vlm_content} |"
            )

        lines.append("")

    # --- Per-step details (expanded, not collapsed) ---
    for i in range(num_steps):
        step_num = i + 1
        gt = steps[i].get("suggested_step", f"(step {step_num})") if i < len(steps) else f"(step {step_num})"

        lines.append(f"### Step {step_num}: {gt}")
        lines.append("")

        # Screenshots — thumbnails that link to full-resolution when available
        before_key = f"step_{i:02d}_before"
        after_key = f"step_{i:02d}_after"
        has_before = before_key in thumbnail_map
        has_after = after_key in thumbnail_map

        if has_before or has_after:
            if has_before and has_after:
                before_thumb = _relpath(thumbnail_map[before_key], md_dir)
                after_thumb = _relpath(thumbnail_map[after_key], md_dir)
                if before_key in full_map:
                    before_full = _relpath(full_map[before_key], md_dir)
                    after_full = _relpath(full_map[after_key], md_dir)
                    lines.append(
                        f"[![before]({before_thumb})]({before_full}) "
                        f"[![after]({after_thumb})]({after_full})"
                    )
                else:
                    lines.append(
                        f"![before]({before_thumb}) "
                        f"![after]({after_thumb})"
                    )
            elif has_before:
                before_thumb = _relpath(thumbnail_map[before_key], md_dir)
                if before_key in full_map:
                    before_full = _relpath(full_map[before_key], md_dir)
                    lines.append(f"[![before]({before_thumb})]({before_full})")
                else:
                    lines.append(f"![before]({before_thumb})")
            elif has_after:
                after_thumb = _relpath(thumbnail_map[after_key], md_dir)
                if after_key in full_map:
                    after_full = _relpath(full_map[after_key], md_dir)
                    lines.append(f"[![after]({after_thumb})]({after_full})")
                else:
                    lines.append(f"![after]({after_thumb})")

            lines.append("")

        # Ground truth
        lines.append(f"**Ground truth:** {gt}")
        lines.append("")

        # Text-only demo output
        if text_steps:
            text_content = text_steps.get(step_num)
            if text_content:
                lines.append(f"**Text demo:** {text_content}")
            else:
                lines.append("**Text demo:** *(not available)*")
            lines.append("")

        # VLM-enriched demo output
        if vlm_steps:
            vlm_content = vlm_steps.get(step_num)
            if vlm_content:
                lines.append("**VLM demo:**")
                lines.append("")
                lines.append(_indent_block(vlm_content))
            else:
                lines.append("**VLM demo:** *(not available)*")
            lines.append("")

        lines.append("---")
        lines.append("")

    # --- Footer ---
    lines.append(
        f"*Generated by `scripts/generate_demo_review.py` from recording "
        f"`{recording_dir.name}`*"
    )
    lines.append("")

    # Write output
    md_text = "\n".join(lines)
    output_path.write_text(md_text, encoding="utf-8")
    print(f"Wrote {len(md_text)} bytes to {output_path}")
    print(f"  {num_steps} steps, {len(thumbnail_map)} thumbnails")
    if text_steps:
        print(f"  Text-only demo: {len(text_steps)} steps parsed")
    if vlm_steps:
        print(f"  VLM-enriched demo: {len(vlm_steps)} steps parsed")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
