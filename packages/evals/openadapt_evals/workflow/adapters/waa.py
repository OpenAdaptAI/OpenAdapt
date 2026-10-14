"""WAA VNC recording adapter.

Parses WAA recording meta.json files and associated step PNG files
into normalized RecordingSession objects.

WAA meta.json format:
    {
        "task_id": "0e763496-...-WOS",
        "instruction": "Change the font to ...",
        "num_steps": 3,
        "steps": [
            {
                "action_hint": null,
                "suggested_step": "Press Ctrl+A to select all text.",
                "step_was_refined": false
            },
            ...
        ],
        "step_plans": [...],
        "server_url": "http://localhost:5001",
        "recorded_at": "2026-03-04T21:34:43.107090+00:00",
        "recording_complete": true
    }

Each step has associated PNG files:
    step_00_before.png, step_00_after.png,
    step_01_before.png, step_01_after.png, ...
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from openadapt_evals.workflow.models import (
    ActionType,
    NormalizedAction,
    RecordingSession,
    RecordingSource,
)


def _classify_action_type(step_text: str) -> ActionType:
    """Infer ActionType from the suggested_step text.

    Uses keyword heuristics to classify the action. Falls back to
    UNKNOWN if no pattern matches.
    """
    text_lower = step_text.lower()

    # Keyboard combos (must check before individual keys)
    if re.search(r"(ctrl|alt|shift)\+\w", text_lower):
        return ActionType.KEY_COMBO

    # Drag actions
    if "drag" in text_lower:
        return ActionType.DRAG

    # Type/enter text
    if any(
        kw in text_lower
        for kw in ["type ", "type\"", "enter ", "input ", "write "]
    ):
        # Distinguish "type text" from "press Enter key"
        if "type " in text_lower or "type\"" in text_lower:
            return ActionType.TYPE
        # "enter" followed by text (not "press enter")
        if "enter " in text_lower and "press" not in text_lower:
            return ActionType.TYPE

    # Press key
    if any(
        kw in text_lower
        for kw in ["press ", "hit ", "tap "]
    ):
        return ActionType.KEY

    # Double-click
    if "double-click" in text_lower or "double click" in text_lower:
        return ActionType.DOUBLE_CLICK

    # Right-click
    if "right-click" in text_lower or "right click" in text_lower:
        return ActionType.RIGHT_CLICK

    # Click (general)
    if "click" in text_lower or "select" in text_lower:
        return ActionType.CLICK

    # Scroll
    if "scroll" in text_lower:
        return ActionType.SCROLL

    return ActionType.UNKNOWN


def _extract_typed_text(step_text: str) -> str | None:
    """Extract the typed text from a step description.

    Looks for quoted strings that follow 'type' or similar verbs.
    """
    # Match: type "something", type 'something'
    match = re.search(
        r'(?:type|enter|input|write)\s+["\'](.+?)["\']',
        step_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Match: type the formula: =TEXT(...)
    match = re.search(
        r'(?:type|enter)\s+(?:the\s+)?(?:formula[:\s]+)?(.+?)(?:\s+and\s+|$)',
        step_text,
        re.IGNORECASE,
    )
    if match:
        text = match.group(1).strip()
        # Only return if it looks like actual content (not another instruction)
        if text and not text.startswith(("the ", "a ", "an ", "on ", "in ")):
            return text

    return None


def _extract_key_name(step_text: str) -> str | None:
    """Extract the key name from a 'press' step description."""
    # Match: Press Enter, Press Tab, Press Ctrl+A
    match = re.search(
        r'(?:press|hit|tap)\s+(\S+(?:\+\S+)*)',
        step_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return None


class WAARecordingAdapter:
    """Adapter for WAA VNC recording meta.json files.

    Parses a WAA recording directory into a RecordingSession with
    NormalizedActions and screenshot paths.
    """

    @classmethod
    def from_meta_json(cls, meta_json_path: str | Path) -> RecordingSession:
        """Parse a WAA recording's meta.json into a RecordingSession.

        Args:
            meta_json_path: Path to the meta.json file inside a WAA
                recording directory. The directory should also contain
                step_XX_before.png and step_XX_after.png files.

        Returns:
            A RecordingSession with NormalizedActions populated from the
            meta.json steps and linked screenshot paths.

        Raises:
            FileNotFoundError: If meta_json_path does not exist.
            json.JSONDecodeError: If meta.json is not valid JSON.
            KeyError: If required fields are missing from meta.json.
        """
        meta_path = Path(meta_json_path)
        recording_dir = meta_path.parent

        with open(meta_path) as f:
            meta = json.load(f)

        # Parse recorded_at timestamp
        recorded_at_str = meta.get("recorded_at")
        if recorded_at_str:
            recorded_at = datetime.fromisoformat(recorded_at_str)
        else:
            recorded_at = datetime.now(timezone.utc)

        # Build NormalizedActions from steps
        actions: list[NormalizedAction] = []
        steps = meta.get("steps", [])

        for idx, step in enumerate(steps):
            suggested_step = step.get("suggested_step", "")
            action_type = _classify_action_type(suggested_step)

            # Build screenshot paths (may not all exist on disk)
            before_png = recording_dir / f"step_{idx:02d}_before.png"
            after_png = recording_dir / f"step_{idx:02d}_after.png"

            # Extract typed text and key name based on action type
            typed_text = None
            key_name = None
            modifiers: list[str] = []

            if action_type == ActionType.TYPE:
                typed_text = _extract_typed_text(suggested_step)
            elif action_type in (ActionType.KEY, ActionType.KEY_COMBO):
                key_name = _extract_key_name(suggested_step)
                # Extract modifiers from key combos like "Ctrl+A"
                if key_name and "+" in key_name:
                    parts = key_name.split("+")
                    modifiers = [p.lower() for p in parts[:-1]]
                    key_name = parts[-1]

            action = NormalizedAction(
                timestamp=float(idx),  # WAA steps are sequential; use index
                action_type=action_type,
                description=suggested_step,
                typed_text=typed_text,
                key_name=key_name,
                modifiers=modifiers,
                screenshot_before_path=(
                    str(before_png) if before_png.exists() else None
                ),
                screenshot_after_path=(
                    str(after_png) if after_png.exists() else None
                ),
                raw_data=step,
            )
            actions.append(action)

        # Compute duration from step count (approximate: 1 second per step)
        duration = float(len(steps))

        return RecordingSession(
            session_id=meta.get("task_id", ""),
            source=RecordingSource.WAA_VNC,
            task_description=meta["instruction"],
            platform="windows",
            recorded_at=recorded_at,
            duration_seconds=duration,
            actions=actions,
            source_path=str(meta_path),
            source_metadata={
                "task_id": meta.get("task_id"),
                "num_steps": meta.get("num_steps"),
                "step_plans": meta.get("step_plans"),
                "server_url": meta.get("server_url"),
                "recording_complete": meta.get("recording_complete", False),
            },
        )
