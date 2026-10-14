"""Annotation data classes, prompts, and utilities for WAA demo annotation.

Migrated from ``openadapt_ml.experiments.demo_prompt.annotate`` so that the
eval workflow (record -> annotate -> demo -> agent -> evaluate) does not
require a cross-repo dependency on ``openadapt-ml`` for annotation.

What stays in openadapt-ml:
    - ``coalesce_steps()`` — depends on Episode schema, used by training
    - ``annotate_episode()`` — for local capture annotation (not WAA)
    - ``render_click_marker()`` — used by ``annotate_episode()``
    - CLI commands for local capture annotation
    - ``format_demo.py`` — used by multiple openadapt-ml modules
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "0.1"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AnnotatedStep:
    """A single annotated step in a demo trace."""

    step_index: int
    timestamp_ms: int | None
    observation: str
    intent: str
    action: str
    action_raw: str
    action_px: list[int] | None
    result_observation: str
    expected_result: str


@dataclass
class AnnotatedDemo:
    """A fully annotated demo trace, produced by VLM annotation."""

    schema_version: str
    task_id: str | None
    instruction: str
    source: str  # "recorded"
    annotator: dict[str, str]
    recording_meta: dict[str, Any]
    steps: list[AnnotatedStep]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        logger.info(f"Saved annotated demo to {path}")

    @classmethod
    def load(cls, path: str | Path) -> AnnotatedDemo:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = [AnnotatedStep(**s) for s in data.pop("steps")]
        return cls(**data, steps=steps)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ANNOTATION_SYSTEM_PROMPT = """\
You are annotating a human GUI demonstration for a task automation system.
Your annotations will be used to guide an AI agent performing the same task on a different screen.
Be precise about UI element names, labels, and visual landmarks.
Always respond with valid JSON only — no markdown, no extra text."""

ANNOTATION_STEP_PROMPT = """\
Task: {instruction}
Step {step_num} of {total_steps}.

The user performed: {action_raw}
A red marker on the BEFORE image shows where the user clicked/interacted.

{previous_context}
Describe this step:

- OBSERVATION: Describe what is visible on the BEFORE image. Include:
  - Application/window name
  - Current panel, page, or dialog
  - 3-6 key visible UI elements with relative positions

- INTENT: Why is the user performing this action? (1 sentence)

- ACTION: Describe which element was interacted with. Name the element by its visible label/text, not by coordinates. Reference the red marker to identify the target.

- RESULT: Describe what actually changed between the BEFORE and AFTER images.{no_after_note}

- EXPECTED_RESULT: What should the screen look like after this action?

Respond with valid JSON only:
{{"observation": "...", "intent": "...", "action": "...", "result_observation": "...", "expected_result": "..."}}"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_annotation_response(response: str) -> dict[str, str]:
    """Parse VLM JSON response, tolerant of minor formatting issues."""
    text = response.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

    logger.warning(f"Failed to parse VLM response as JSON: {text[:200]}")
    return {
        "observation": text[:200] if text else "",
        "intent": "",
        "action": "",
        "result_observation": "",
        "expected_result": "",
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_annotations(demo: AnnotatedDemo) -> list[str]:
    """Check annotation quality. Returns list of warnings.

    Checks:
    - All key fields non-empty
    - Action doesn't contain raw coordinates (should be semantic)
    - Result_observation differs from observation
    - No obvious platform mismatch
    """
    warnings: list[str] = []

    for step in demo.steps:
        prefix = f"Step {step.step_index}"

        if not step.observation:
            warnings.append(f"{prefix}: empty observation")
        if not step.intent:
            warnings.append(f"{prefix}: empty intent")
        if not step.action:
            warnings.append(f"{prefix}: empty action")
        if not step.result_observation and not step.expected_result:
            warnings.append(f"{prefix}: no result_observation or expected_result")

        # Check if action still contains raw coordinates
        if re.search(r"CLICK\(\s*0\.\d+\s*,\s*0\.\d+\s*\)", step.action):
            warnings.append(f"{prefix}: action contains raw coordinates: {step.action}")

        # Check for platform mismatches (Windows recording described with macOS terms)
        platform = (demo.recording_meta or {}).get("platform", "")
        if "win" in platform.lower():
            mac_terms = ["finder", "dock", "spotlight", "cmd+", "command+"]
            action_lower = step.action.lower() + " " + step.observation.lower()
            for term in mac_terms:
                if term in action_lower:
                    warnings.append(
                        f"{prefix}: macOS term '{term}' in Windows recording"
                    )

    return warnings


# ---------------------------------------------------------------------------
# Formatting for prompt injection
# ---------------------------------------------------------------------------


def format_annotated_demo(demo: AnnotatedDemo, compact: bool = True) -> str:
    """Format AnnotatedDemo as text for prompt injection.

    If compact=True (default), uses brief observation, action, and result.
    If compact=False, includes full observation and intent.

    Args:
        demo: AnnotatedDemo to format.
        compact: If True, use compact format for agent prompt.

    Returns:
        Formatted demo string.
    """
    lines = [
        "DEMONSTRATION:",
        f"Goal: {demo.instruction}",
        "",
    ]

    for step in demo.steps:
        lines.append(f"Step {step.step_index + 1}:")

        if compact:
            # Use first sentence of observation for compactness
            obs = _first_sentence(step.observation)
            lines.append(f"  [Screen: {obs}]")
        else:
            lines.append(f"  [Screen: {step.observation}]")
            lines.append(f"  [Intent: {step.intent}]")

        lines.append(f"  [Action: {step.action}]")

        # Prefer result_observation (grounded), fall back to expected_result
        result = step.result_observation or step.expected_result
        if result:
            lines.append(f"  [Result: {result}]")

        lines.append("")

    return "\n".join(lines)


def _first_sentence(text: str) -> str:
    """Extract first sentence from text."""
    if not text:
        return ""
    # Split on period followed by space or end
    for i, ch in enumerate(text):
        if ch == "." and (i + 1 >= len(text) or text[i + 1] == " "):
            return text[: i + 1]
    return text
