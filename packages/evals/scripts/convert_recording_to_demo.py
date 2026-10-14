#!/usr/bin/env python3
"""Convert WAA recordings (meta.json + screenshots) to demo text files.

Produces demo .txt files in the format expected by eval-suite's --demo-dir.

Two modes:
  --mode text   : Fast, free. Converts step descriptions from meta.json directly.
  --mode vlm    : Richer. Sends screenshots to a VLM for Observation/Intent/Action/Result.

Usage:
    # Text-only (instant, no API calls)
    python scripts/convert_recording_to_demo.py \
        --recordings waa_recordings \
        --output demo_prompts \
        --mode text

    # VLM-enriched (sends screenshots to OpenAI/Anthropic)
    python scripts/convert_recording_to_demo.py \
        --recordings waa_recordings \
        --output demo_prompts \
        --mode vlm \
        --provider openai

    # Override model (e.g. use cheaper model)
    python scripts/convert_recording_to_demo.py \
        --recordings waa_recordings \
        --output demo_prompts \
        --mode vlm \
        --provider openai \
        --model gpt-4.1-mini
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from openadapt_evals.vlm import image_bytes_from_path, vlm_call

# Regex patterns for extracting key references from step descriptions
_CELL_REF_RE = re.compile(r'\b([A-Z]+\d+)\b')
_CELL_RANGE_RE = re.compile(r'\b([A-Z]+\d+)\s*[:\-]\s*([A-Z]+\d+)\b')
_FORMULA_RE = re.compile(r'"(=[^"]+)"')
_QUOTED_TEXT_RE = re.compile(r'"([^"]+)"')


def _extract_references(text: str) -> dict:
    """Extract key references (cell refs, formulas, quoted text) from a step description.

    Returns a dict with:
        cells: set of cell references like {'D2', 'B6'}
        ranges: list of (start, end) tuples like [('B2', 'D6')]
        formulas: list of formula strings
        quoted: list of quoted text values
    """
    formulas = _FORMULA_RE.findall(text)
    # Remove formulas from text before extracting cell refs to avoid double-counting
    text_no_formulas = _FORMULA_RE.sub('', text)
    ranges = _CELL_RANGE_RE.findall(text_no_formulas)
    # Remove ranges before extracting individual cells
    text_no_ranges = _CELL_RANGE_RE.sub('', text_no_formulas)
    cells = set(_CELL_REF_RE.findall(text_no_ranges))
    quoted = [q for q in _QUOTED_TEXT_RE.findall(text) if not q.startswith('=')]
    return {
        'cells': cells,
        'ranges': ranges,
        'formulas': formulas,
        'quoted': quoted,
    }


def _check_action_mismatch(ground_truth: str, vlm_action: str) -> str | None:
    """Check if the VLM's Action line contradicts the ground-truth step.

    Returns a description of the mismatch, or None if consistent.
    """
    gt_refs = _extract_references(ground_truth)
    vlm_refs = _extract_references(vlm_action)

    mismatches = []

    # Check cell references: VLM should not introduce cells that conflict with GT
    if gt_refs['cells'] and vlm_refs['cells']:
        # VLM cells that are NOT in the ground truth (potential hallucinations)
        extra_cells = vlm_refs['cells'] - gt_refs['cells']
        # Only flag if VLM has cells that differ in row/col from GT cells
        # e.g., GT says D2 but VLM says D3
        for gt_cell in gt_refs['cells']:
            gt_col = re.match(r'([A-Z]+)', gt_cell).group(1)
            for vlm_cell in extra_cells:
                vlm_col = re.match(r'([A-Z]+)', vlm_cell).group(1)
                if vlm_col == gt_col and vlm_cell != gt_cell:
                    mismatches.append(
                        f"cell ref {vlm_cell} in VLM vs {gt_cell} in ground truth"
                    )

    # Check formulas
    if gt_refs['formulas'] and vlm_refs['formulas']:
        for gt_f, vlm_f in zip(gt_refs['formulas'], vlm_refs['formulas']):
            if gt_f != vlm_f:
                mismatches.append(
                    f"formula '{vlm_f}' in VLM vs '{gt_f}' in ground truth"
                )

    # Check quoted text values
    if gt_refs['quoted'] and vlm_refs['quoted']:
        gt_set = {q.lower() for q in gt_refs['quoted']}
        for vlm_q in vlm_refs['quoted']:
            if vlm_q.lower() not in gt_set:
                # Check if it's a close but wrong value
                mismatches.append(
                    f"quoted text '{vlm_q}' in VLM not in ground truth"
                )

    return '; '.join(mismatches) if mismatches else None


def _extract_annotation_field(annotation: str, field: str) -> str | None:
    """Extract a specific field value from a VLM annotation string."""
    pattern = re.compile(rf'^{field}:\s*(.+)$', re.MULTILINE | re.IGNORECASE)
    match = pattern.search(annotation)
    return match.group(1).strip() if match else None


def _replace_annotation_field(annotation: str, field: str, new_value: str) -> str:
    """Replace a specific field value in a VLM annotation string."""
    pattern = re.compile(rf'^({field}:\s*)(.+)$', re.MULTILINE | re.IGNORECASE)
    return pattern.sub(rf'\g<1>{new_value}', annotation)


def convert_text(meta: dict) -> str:
    """Convert recording meta.json to demo text using step descriptions only."""
    instruction = meta["instruction"]
    steps = meta.get("steps", [])

    lines = [
        "DEMONSTRATION:",
        f"Task: {instruction}",
        "",
    ]

    for i, step in enumerate(steps):
        desc = step.get("suggested_step", f"(step {i + 1})")
        lines.append(f"Step {i + 1}:")
        lines.append(f"  Action: {desc}")
        lines.append("")

    lines.append("---")
    return "\n".join(lines) + "\n"


def convert_vlm(
    meta: dict,
    task_dir: Path,
    provider: str = "openai",
    model: str | None = None,
) -> str:
    """Convert recording to demo text using VLM to describe screenshots.

    The VLM is instructed to treat the recorded action from meta.json as
    ground truth. After each VLM call, the Action field is validated against
    the ground-truth step description. If key references (cell refs, formulas,
    text values) are contradicted, the Action field is replaced with the
    ground-truth description while preserving the VLM's Observation, Intent,
    and Result fields.
    """
    instruction = meta["instruction"]
    steps = meta.get("steps", [])
    num_steps = meta.get("num_steps", len(steps))

    resolved_model = model or (
        "gpt-4.1" if provider == "openai" else "claude-sonnet-4-20250514"
    )

    lines = [
        "DEMONSTRATION:",
        f"Task: {instruction}",
        "",
    ]

    for i in range(num_steps):
        step_desc = steps[i].get("suggested_step", "") if i < len(steps) else ""
        before_path = task_dir / f"step_{i:02d}_before.png"
        after_path = task_dir / f"step_{i:02d}_after.png"

        if not before_path.exists():
            # Fallback to text-only for this step
            lines.append(f"Step {i + 1}:")
            lines.append(f"  Action: {step_desc}")
            lines.append("")
            continue

        print(f"  Step {i + 1}/{num_steps}...", end=" ", flush=True)

        # Build VLM prompt with strong ground-truth constraint
        images = [image_bytes_from_path(before_path)]
        has_after = after_path.exists()
        if has_after:
            images.append(image_bytes_from_path(after_path))

        prompt = (
            f"You are annotating step {i + 1} of {num_steps} in a desktop task demonstration.\n"
            f"Task: {instruction}\n\n"
            f"GROUND-TRUTH RECORDED ACTION (definitive — do NOT contradict this):\n"
            f"  {step_desc}\n\n"
            f"The recorded action above is the exact action that was performed. It is the\n"
            f"authoritative source for cell references (e.g. D2, B6), text values, formulas,\n"
            f"and action types (CLICK, TYPE, DRAG, KEY). Your Action field MUST match these\n"
            f"details exactly. Do not substitute different cell references, row numbers, or\n"
            f"values based on your visual interpretation — the recording is ground truth.\n\n"
            f"{'The first image is BEFORE the action, the second is AFTER.' if has_after else 'This image shows the screen BEFORE the action.'}\n\n"
            f"Provide a concise annotation with exactly these fields:\n"
            f"Observation: (what the screen shows before the action, ~1 sentence)\n"
            f"Intent: (why this action is being taken, ~1 sentence)\n"
            f"Action: (restate the recorded action above — you may add brief visual context\n"
            f"         but must preserve all cell refs, values, and formulas exactly)\n"
            f"Result: (what changed after the action, ~1 sentence)\n\n"
            f"IMPORTANT: The Action field must be faithful to the ground-truth recorded action.\n"
            f"If the recorded action says cell D2, you must say D2 — not D3 or any other cell."
        )

        try:
            annotation = vlm_call(
                prompt,
                images=images,
                provider=provider,
                model=resolved_model,
                max_tokens=400,
                temperature=0.0,
            )

            # Post-hoc validation: check VLM Action against ground truth
            vlm_action = _extract_annotation_field(annotation, "Action")
            if vlm_action and step_desc:
                mismatch = _check_action_mismatch(step_desc, vlm_action)
                if mismatch:
                    print(f"MISMATCH ({mismatch}), replacing Action...", end=" ")
                    # Replace the Action field with the ground-truth description,
                    # preserving the VLM's Observation/Intent/Result
                    annotation = _replace_annotation_field(
                        annotation, "Action", step_desc
                    )

            lines.append(f"Step {i + 1}:")
            # Parse the annotation — it should have Observation/Intent/Action/Result lines
            for line in annotation.strip().split("\n"):
                line = line.strip()
                if line:
                    lines.append(f"  {line}")
            lines.append("")
            print("done")
        except Exception as e:
            print(f"error: {e}")
            # Fallback to text-only
            lines.append(f"Step {i + 1}:")
            lines.append(f"  Action: {step_desc}")
            lines.append("")

    lines.append("---")
    return "\n".join(lines) + "\n"


def main(
    recordings: str = "waa_recordings",
    output: str = "demo_prompts",
    mode: str = "text",
    provider: str = "openai",
    model: str | None = None,
    task: str | None = None,
) -> None:
    """Convert WAA recordings to demo text files.

    Args:
        recordings: Directory containing recording subdirectories.
        output: Output directory for demo .txt files.
        mode: "text" (free, instant) or "vlm" (richer, uses API).
        provider: VLM provider for vlm mode ("openai" or "anthropic").
        model: Model override (default: gpt-4.1 for openai, claude-sonnet-4 for anthropic).
        task: Specific task ID prefix to convert (default: all).
    """
    recordings_dir = Path(recordings)
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find task directories with meta.json
    task_dirs = sorted(
        d for d in recordings_dir.iterdir()
        if d.is_dir() and (d / "meta.json").exists()
    )

    if task:
        task_dirs = [d for d in task_dirs if d.name.startswith(task)]

    if not task_dirs:
        print(f"No recordings found in {recordings_dir}")
        sys.exit(1)

    effective_model = model
    if not effective_model:
        effective_model = (
            "gpt-4.1" if provider == "openai" else "claude-sonnet-4-20250514"
        )
    print(f"Converting {len(task_dirs)} recording(s) to demo text (mode={mode})")
    if mode == "vlm":
        print(f"  Provider: {provider}, Model: {effective_model}")
    print()

    for task_dir in task_dirs:
        meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        task_id = meta["task_id"]
        num_steps = meta.get("num_steps", len(meta.get("steps", [])))

        print(f"{'=' * 60}")
        print(f"Task: {task_id[:40]}... ({num_steps} steps)")
        print(f"{'=' * 60}")

        if mode == "vlm":
            demo_text = convert_vlm(meta, task_dir, provider, model)
        else:
            demo_text = convert_text(meta)

        txt_path = output_dir / f"{task_id}.txt"
        txt_path.write_text(demo_text, encoding="utf-8")
        print(f"  -> {txt_path}")
        print()

    print("Done. Use with eval-suite:")
    print(f"  openadapt-evals eval-suite --demo-dir {output_dir} --tasks ...")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
