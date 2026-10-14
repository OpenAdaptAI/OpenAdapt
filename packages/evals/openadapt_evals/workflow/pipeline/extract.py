"""Pass 2: Extract structured Workflow objects from episode transcripts.

Takes an EpisodeTranscript (Pass 1 output) and uses a VLM to identify
high-level workflow steps, merging related transcript entries into
WorkflowStep objects and returning a complete Workflow.

See docs/design/workflow_extraction_pipeline.md Section 4.4.2 for the
design specification.
"""

from __future__ import annotations

import logging
from typing import Any

from openadapt_evals.workflow.models import (
    ActionType,
    EpisodeTranscript,
    RecordingSource,
    Workflow,
    WorkflowStep,
)

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You are a workflow extraction expert. Given a transcript of a desktop \
recording session, extract the high-level workflow steps.

Task description: {task_description}
Episode summary: {episode_summary}
Primary goal: {primary_goal}
Applications used: {apps_used}

The transcript contains {entry_count} raw actions. Your job is to merge \
related actions into meaningful workflow steps. For example, "click cell A1" \
followed by "type 'Year'" should become a single step: "Enter the header \
'Year' in cell A1".

Transcript entries:
{transcript_text}

Output a JSON object with these fields:
- name: Short name for the workflow (e.g., "Enter spreadsheet headers")
- description: Detailed description of what this workflow accomplishes
- goal: The end goal of this workflow
- domain: Domain classification (e.g., "spreadsheet", "document", \
"web_browser", "file_management", "system_settings")
- complexity: "simple" (1-3 steps), "medium" (4-8 steps), or "complex" (9+)
- tags: List of relevant tags (e.g., ["data-entry", "headers"])
- steps: Array of step objects, each with:
  - description: What this step accomplishes (e.g., "Type the header 'Year' \
in cell A1")
  - think: The reasoning behind this step (e.g., "Need to label the first \
column")
  - action: The concrete action(s) taken (e.g., "Click cell A1, type 'Year'")
  - expect: Expected result after this step (e.g., "Cell A1 contains 'Year'")
  - action_type: One of: click, double_click, right_click, drag, type, key, \
key_combo, scroll, wait, app_switch, unknown
  - app_name: Application used for this step
  - ui_element: Target UI element (e.g., "Cell A1 in the spreadsheet")
  - is_prerequisite: Is this a setup step? (true/false)
  - is_verification: Is this checking a result? (true/false)
  - is_optional: Could this step be skipped? (true/false)
  - source_entry_indices: Array of transcript entry indices that this step \
was derived from
  - parameters: Object with extracted parameters (e.g., {{"cell": "A1", \
"value": "Year"}})

Merge consecutive related actions into single steps where it makes sense. \
Skip corrective actions (mistakes and their fixes) unless they are part of \
the core workflow. Focus on goal-directed actions.

Output ONLY the JSON object, no additional text.
"""


def _build_transcript_text(transcript: EpisodeTranscript) -> str:
    """Format transcript entries as numbered text for the VLM prompt."""
    lines = []
    for entry in transcript.entries:
        parts = [
            f"[{entry.entry_index}]",
            f"t={entry.timestamp_start:.1f}s",
            f"[{entry.action_type.value}]",
            entry.narration,
        ]
        if entry.intent:
            parts.append(f"(intent: {entry.intent})")
        if entry.app_context:
            parts.append(f"[{entry.app_context}]")
        if entry.is_corrective:
            parts.append("[CORRECTIVE]")
        if entry.is_exploratory:
            parts.append("[EXPLORATORY]")

        lines.append(" ".join(parts))
    return "\n".join(lines)


def _parse_extraction_response(
    raw: str,
    transcript: EpisodeTranscript,
) -> dict[str, Any] | None:
    """Parse VLM response into a workflow dict.

    Uses the shared extract_json utility for robust parsing, with
    a fallback to regex-based extraction.
    """
    from openadapt_evals.vlm import extract_json

    result = extract_json(raw)
    if isinstance(result, dict):
        return result

    logger.warning("Could not parse VLM extraction response")
    return None


def _build_workflow_from_parsed(
    parsed: dict[str, Any],
    transcript: EpisodeTranscript,
    recording_source: RecordingSource,
) -> Workflow:
    """Convert parsed VLM JSON into a Workflow with WorkflowSteps."""
    raw_steps = parsed.get("steps", [])
    steps: list[WorkflowStep] = []

    for i, raw_step in enumerate(raw_steps):
        # Resolve source entry indices for timestamp bounds
        source_indices = raw_step.get("source_entry_indices", [i])
        source_entries = [e for e in transcript.entries if e.entry_index in source_indices]

        if source_entries:
            ts_start = min(e.timestamp_start for e in source_entries)
            ts_end = max(e.timestamp_end or e.timestamp_start for e in source_entries)
        else:
            ts_start = 0.0
            ts_end = 0.0

        # Parse action_type with fallback
        action_type_str = raw_step.get("action_type", "unknown")
        try:
            action_type = ActionType(action_type_str)
        except ValueError:
            action_type = ActionType.UNKNOWN

        # Pick a representative screenshot from source entries
        screenshot_path = None
        for entry in source_entries:
            if entry.screenshot_before_path:
                screenshot_path = entry.screenshot_before_path
                break

        step = WorkflowStep(
            step_index=i,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
            description=raw_step.get("description", f"Step {i + 1}"),
            think=raw_step.get("think", ""),
            action=raw_step.get("action", ""),
            expect=raw_step.get("expect", ""),
            action_type=action_type,
            is_prerequisite=raw_step.get("is_prerequisite", False),
            is_verification=raw_step.get("is_verification", False),
            is_optional=raw_step.get("is_optional", False),
            app_name=raw_step.get("app_name", ""),
            ui_element=raw_step.get("ui_element", ""),
            screenshot_path=screenshot_path,
            source_entry_indices=source_indices,
            parameters=raw_step.get("parameters", {}),
        )
        steps.append(step)

    # Compute total duration from transcript
    if transcript.entries:
        total_duration = (
            transcript.entries[-1].timestamp_start - transcript.entries[0].timestamp_start
        )
    else:
        total_duration = 0.0

    # Derive app_names from steps, falling back to transcript
    step_apps = sorted(set(s.app_name for s in steps if s.app_name))
    app_names = step_apps if step_apps else list(transcript.apps_used)

    workflow = Workflow(
        name=parsed.get("name", transcript.task_description),
        description=parsed.get("description", transcript.episode_summary),
        goal=parsed.get("goal", transcript.primary_goal),
        app_names=app_names,
        domain=parsed.get("domain", transcript.domain_classification or "unknown"),
        complexity=parsed.get("complexity", "medium"),
        tags=parsed.get("tags", []),
        steps=steps,
        total_duration_seconds=total_duration,
        session_id=transcript.session_id,
        transcript_id=transcript.transcript_id,
        recording_source=recording_source,
    )

    return workflow


def _build_fallback_workflow(
    transcript: EpisodeTranscript,
    recording_source: RecordingSource,
) -> Workflow:
    """Build a 1:1 fallback workflow when VLM parsing fails.

    Each transcript entry becomes its own WorkflowStep so downstream
    passes still have something to work with.
    """
    steps: list[WorkflowStep] = []
    for entry in transcript.entries:
        if entry.is_corrective:
            continue
        step = WorkflowStep(
            step_index=len(steps),
            timestamp_start=entry.timestamp_start,
            timestamp_end=entry.timestamp_end or entry.timestamp_start,
            description=entry.narration,
            think=entry.intent,
            action=f"[{entry.action_type.value}] {entry.narration}",
            expect=entry.state_change,
            action_type=entry.action_type,
            app_name=entry.app_context or "",
            ui_element=entry.ui_element_description,
            screenshot_path=entry.screenshot_before_path,
            source_entry_indices=[entry.entry_index],
        )
        steps.append(step)

    if transcript.entries:
        total_duration = (
            transcript.entries[-1].timestamp_start - transcript.entries[0].timestamp_start
        )
    else:
        total_duration = 0.0

    return Workflow(
        name=transcript.task_description,
        description=transcript.episode_summary,
        goal=transcript.primary_goal,
        app_names=list(transcript.apps_used),
        domain=transcript.domain_classification or "unknown",
        complexity="medium",
        tags=[],
        steps=steps,
        total_duration_seconds=total_duration,
        session_id=transcript.session_id,
        transcript_id=transcript.transcript_id,
        recording_source=recording_source,
    )


def extract_workflow(
    transcript: EpisodeTranscript,
    model: str = "gpt-4.1-mini",
    provider: str = "openai",
    recording_source: RecordingSource = RecordingSource.NATIVE_CAPTURE,
    strict: bool = False,
) -> Workflow:
    """Extract a structured Workflow from an EpisodeTranscript.

    Sends the full transcript to a VLM in a single call, asking it to
    identify high-level workflow steps by merging related actions.

    Args:
        transcript: EpisodeTranscript from Pass 1.
        model: VLM model name.
        provider: VLM provider (``"openai"`` or ``"anthropic"``).
        recording_source: Source of the original recording.
        strict: When True, raise errors instead of falling back to 1:1
            transcript-to-step mapping. Use during benchmarking/training
            to ensure VLM extraction is actually working.

    Returns:
        A Workflow with WorkflowStep entries derived from the transcript.
    """
    from openadapt_evals.vlm import vlm_call

    transcript_text = _build_transcript_text(transcript)

    prompt = _EXTRACTION_PROMPT.format(
        task_description=transcript.task_description,
        episode_summary=transcript.episode_summary or "N/A",
        primary_goal=transcript.primary_goal or transcript.task_description,
        apps_used=", ".join(transcript.apps_used) if transcript.apps_used else "Unknown",
        entry_count=len(transcript.entries),
        transcript_text=transcript_text,
    )

    logger.info(
        "Extracting workflow from transcript %s (%d entries)",
        transcript.transcript_id,
        len(transcript.entries),
    )

    try:
        raw = vlm_call(
            prompt,
            model=model,
            provider=provider,
            max_tokens=4096,
            cost_label="workflow_extract",
        )
    except Exception:
        if strict:
            raise
        logger.warning(
            "VLM call failed for transcript %s, using fallback",
            transcript.transcript_id,
            exc_info=True,
        )
        return _build_fallback_workflow(transcript, recording_source)

    parsed = _parse_extraction_response(raw, transcript)

    if parsed is None:
        if strict:
            raise ValueError(
                f"VLM extraction failed to parse response for transcript "
                f"{transcript.transcript_id}. Raw response: {raw!r:.500}"
            )
        logger.warning(
            "VLM extraction failed for transcript %s, using fallback",
            transcript.transcript_id,
        )
        return _build_fallback_workflow(transcript, recording_source)

    workflow = _build_workflow_from_parsed(parsed, transcript, recording_source)

    logger.info(
        "Workflow extracted: %s (%d steps from %d transcript entries)",
        workflow.name,
        workflow.step_count,
        len(transcript.entries),
    )

    return workflow
