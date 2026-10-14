"""Pass 1: VLM-based episode transcript generation.

Processes recording sessions through a VLM to generate natural language
transcripts with timestamped narration, intent, and UI context per action.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openadapt_evals.workflow.models import (
    EpisodeTranscript,
    RecordingSession,
    TranscriptEntry,
)

logger = logging.getLogger(__name__)

_TRANSCRIPT_PROMPT = """\
You are annotating a desktop recording. For each action below, provide a JSON \
object describing what the user did.

Task: {task_description}

Actions in this batch (indices {start_idx}-{end_idx}):
{action_descriptions}

For EACH action, output a JSON object with these fields:
- narration: What the user did (natural language, e.g., "Clicks the File menu")
- intent: Why they did it (e.g., "Open file management options")
- ui_element_description: What UI element was targeted (e.g., "File menu button")
- app_context: Current app and window (e.g., "LibreOffice Calc - Budget.ods")
- state_change: What changed on screen (e.g., "File dropdown menu appears")
- is_corrective: Was this fixing a mistake? (true/false)
- is_exploratory: Was this exploring the UI? (true/false)
- vlm_confidence: How confident are you in this annotation? (0.0-1.0)

Output a JSON array with exactly {action_count} objects, one per action, \
in the same order as the actions listed above.
"""


def _build_batch_prompt(
    session: RecordingSession,
    start_idx: int,
    end_idx: int,
) -> tuple[str, list[bytes]]:
    """Build the VLM prompt and images for one batch of actions."""
    batch_actions = session.actions[start_idx:end_idx]

    action_descs = []
    images = []
    for i, action in enumerate(batch_actions):
        desc = (
            f"Action {start_idx + i}: [{action.action_type.value}] "
            f"{action.description}"
        )
        if action.app_name:
            desc += f" (in {action.app_name})"
        if action.typed_text:
            desc += f" text='{action.typed_text}'"
        action_descs.append(desc)

        # Include screenshots if available (as file paths for now)
        if action.screenshot_before_path:
            try:
                with open(action.screenshot_before_path, "rb") as f:
                    images.append(f.read())
            except (FileNotFoundError, OSError):
                pass
        if action.screenshot_after_path:
            try:
                with open(action.screenshot_after_path, "rb") as f:
                    images.append(f.read())
            except (FileNotFoundError, OSError):
                pass

    prompt = _TRANSCRIPT_PROMPT.format(
        task_description=session.task_description,
        start_idx=start_idx,
        end_idx=end_idx - 1,
        action_descriptions="\n".join(action_descs),
        action_count=len(batch_actions),
    )

    return prompt, images if images else None


def _parse_transcript_response(
    raw: str,
    actions_in_batch: int,
) -> list[dict[str, Any]]:
    """Parse VLM response into a list of transcript entry dicts."""
    # Try to find a JSON array in the response
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            entries = json.loads(match.group())
            if isinstance(entries, list):
                return entries
        except json.JSONDecodeError:
            pass

    # Fallback: try to find individual JSON objects
    objects = re.findall(r"\{[^{}]+\}", raw, re.DOTALL)
    entries = []
    for obj_str in objects:
        try:
            entries.append(json.loads(obj_str))
        except json.JSONDecodeError:
            continue

    if entries:
        return entries

    # Last resort: create placeholder entries
    logger.warning("Could not parse VLM transcript response, using placeholders")
    return [
        {
            "narration": "Action performed",
            "intent": "Unknown",
            "ui_element_description": "Unknown",
            "app_context": "Unknown",
            "state_change": "Unknown",
            "is_corrective": False,
            "is_exploratory": False,
            "vlm_confidence": 0.0,
        }
        for _ in range(actions_in_batch)
    ]


def generate_transcript(
    session: RecordingSession,
    model: str = "gpt-4.1-mini",
    provider: str = "openai",
    batch_size: int = 6,
    strict: bool = False,
) -> EpisodeTranscript:
    """Generate a natural language transcript from a recording session.

    Processes screenshots in sliding windows of batch_size steps,
    with 2 screenshots of context overlap between batches.

    Args:
        session: Scrubbed RecordingSession.
        model: VLM model name.
        provider: VLM provider ("openai" or "anthropic").
        batch_size: Number of actions per VLM call.
        strict: When True, raise errors instead of returning partial
            or placeholder results. Use during benchmarking/training
            to ensure VLM transcript generation is actually working.

    Returns:
        EpisodeTranscript with one TranscriptEntry per action.
    """
    from openadapt_evals.vlm import vlm_call

    all_entries: list[TranscriptEntry] = []
    # NOTE: EpisodeTranscript.total_input_tokens / total_output_tokens /
    # generation_cost_usd stay at their 0 defaults. `vlm_call` returns text
    # only and reports its own usage to the global cost tracker, so there is
    # no per-transcript number to accumulate here. Two accumulators used to be
    # declared at this point and were never incremented or passed to the
    # constructor, which read as if the totals were being collected.

    # Process in batches with 2-step overlap for context
    overlap = 2
    idx = 0
    batch_num = 0

    while idx < len(session.actions):
        end_idx = min(idx + batch_size, len(session.actions))
        batch_actions = session.actions[idx:end_idx]

        prompt, images = _build_batch_prompt(session, idx, end_idx)

        logger.info(
            "Transcript batch %d: actions %d-%d (%d images)",
            batch_num, idx, end_idx - 1, len(images) if images else 0,
        )

        try:
            raw = vlm_call(
                prompt,
                images=images,
                model=model,
                provider=provider,
                max_tokens=2048,
                cost_label="transcript",
            )
        except Exception:
            if strict:
                raise
            logger.warning(
                "VLM call failed for batch %d (actions %d-%d), skipping",
                batch_num, idx, end_idx - 1,
                exc_info=True,
            )
            idx = end_idx if end_idx >= len(session.actions) else end_idx - overlap
            batch_num += 1
            continue

        parsed = _parse_transcript_response(raw, len(batch_actions))

        if strict and all(
            entry.get("vlm_confidence", 1.0) == 0.0
            and entry.get("narration") == "Action performed"
            for entry in parsed
        ):
            raise ValueError(
                f"VLM transcript parsing returned only placeholders for batch "
                f"{batch_num} (actions {idx}-{end_idx - 1}). Raw response: {raw!r:.500}"
            )

        # Create TranscriptEntry objects
        for i, (action, entry_data) in enumerate(zip(batch_actions, parsed)):
            entry = TranscriptEntry(
                entry_index=idx + i,
                action_id=action.action_id,
                timestamp_start=action.timestamp,
                timestamp_end=(
                    session.actions[idx + i + 1].timestamp
                    if idx + i + 1 < len(session.actions)
                    else action.timestamp + 1.0
                ),
                narration=entry_data.get("narration", action.description),
                intent=entry_data.get("intent", ""),
                ui_element_description=entry_data.get("ui_element_description", ""),
                app_context=entry_data.get("app_context", action.app_name or ""),
                state_change=entry_data.get("state_change", ""),
                action_type=action.action_type,
                is_corrective=entry_data.get("is_corrective", False),
                is_exploratory=entry_data.get("is_exploratory", False),
                vlm_confidence=float(entry_data.get("vlm_confidence", 0.5)),
                screenshot_before_path=action.screenshot_before_path,
                screenshot_after_path=action.screenshot_after_path,
            )
            all_entries.append(entry)

        # Advance with overlap
        idx = end_idx if end_idx >= len(session.actions) else end_idx - overlap
        batch_num += 1

    # Build summary
    apps_used = sorted(set(
        a.app_name for a in session.actions if a.app_name
    ))

    transcript = EpisodeTranscript(
        session_id=session.session_id,
        task_description=session.task_description,
        entries=all_entries,
        vlm_model=model,
        vlm_provider=provider,
        apps_used=apps_used,
        episode_summary=f"Recording of: {session.task_description}",
        primary_goal=session.task_description,
    )

    logger.info(
        "Transcript generated: %d entries, %d batches",
        len(all_entries), batch_num,
    )
    return transcript


def estimate_transcript_cost(
    session: RecordingSession,
    model: str = "gpt-4.1-mini",
) -> dict[str, Any]:
    """Estimate API cost before processing.

    Returns dict with estimated token counts and cost.
    """
    num_actions = len(session.actions)
    # Rough estimates: ~500 text tokens per batch prompt, ~1000 tokens per screenshot
    num_screenshots = sum(
        1 for a in session.actions
        if a.screenshot_before_path or a.screenshot_after_path
    )

    est_input_tokens = (num_actions * 100) + (num_screenshots * 1000)
    est_output_tokens = num_actions * 150

    # GPT-4.1-mini pricing (approximate)
    cost_per_1k_input = 0.0004 if "mini" in model else 0.003
    cost_per_1k_output = 0.0016 if "mini" in model else 0.015

    est_cost = (
        (est_input_tokens / 1000) * cost_per_1k_input
        + (est_output_tokens / 1000) * cost_per_1k_output
    )

    return {
        "num_actions": num_actions,
        "num_screenshots": num_screenshots,
        "estimated_input_tokens": est_input_tokens,
        "estimated_output_tokens": est_output_tokens,
        "estimated_cost_usd": round(est_cost, 4),
        "model": model,
    }
