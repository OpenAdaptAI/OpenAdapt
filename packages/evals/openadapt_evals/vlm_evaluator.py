"""VLM-based screenshot evaluation for custom task checks.

Uses a vision-language model to judge whether a screenshot shows
that a condition is met. This is less precise than programmatic
checks but much easier to define — users write one sentence instead
of PowerShell commands.

Usage:
    from openadapt_evals.vlm_evaluator import vlm_judge

    success, confidence = vlm_judge(
        screenshot_bytes,
        "Cell A1 shows text formatted in Arial font",
    )
"""

from __future__ import annotations

import io
import json
import logging
import math

from PIL import Image

from openadapt_evals.errors import RolloutEvaluationError

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """\
Look at this screenshot carefully.

Does this screenshot show that the following condition is met?

Condition: {description}

Important context: Windows 11 applications may look different from their classic \
versions. For example, Notepad in Windows 11 has a modern UI with tabs, a \
dark/light theme, and rounded corners — it no longer has the classic menu bar \
or title-bar icon. It is still Notepad. Similarly, other built-in Windows apps \
(Calculator, Paint, Settings, etc.) have been redesigned with modern styling. \
Judge based on functionality and content, not on visual appearance matching \
a specific OS version.

Respond in this exact JSON format:
{{"verdict": "YES" or "NO", "confidence": 0.0 to 1.0, "explanation": "brief reason"}}

Respond with ONLY the JSON object."""


try:
    from openadapt_evals.integrations.weave_integration import weave_op
except ImportError:
    weave_op = lambda fn: fn  # noqa: E731


@weave_op
def vlm_judge(
    screenshot: bytes,
    description: str,
    model: str = "gpt-4.1-mini",
    provider: str = "openai",
) -> tuple[bool, float]:
    """Judge whether a screenshot satisfies a condition.

    Args:
        screenshot: PNG screenshot bytes.
        description: Natural language condition to check.
        model: VLM model name.
        provider: VLM provider ("openai" or "anthropic").

    Returns:
        Tuple of (success: bool, confidence: float).

    Raises:
        RolloutEvaluationError: If the screenshot or model response cannot be
            measured under the judge contract.
    """
    from openadapt_evals.vlm import vlm_call

    if not isinstance(screenshot, bytes) or not screenshot:
        raise RolloutEvaluationError("VLM judging requires screenshot bytes")
    try:
        with Image.open(io.BytesIO(screenshot)) as image:
            image.load()
    except Exception as exc:
        raise RolloutEvaluationError(
            "VLM judging requires a decodable screenshot"
        ) from exc
    if not isinstance(description, str) or not description.strip():
        raise RolloutEvaluationError("VLM judging requires a condition description")

    prompt = _JUDGE_PROMPT.format(description=description)
    try:
        response = vlm_call(
            prompt,
            images=[screenshot],
            model=model,
            provider=provider,
            max_tokens=256,
            temperature=0.1,
            cost_label="vlm_judge",
        )
    except Exception as exc:
        raise RolloutEvaluationError("VLM judge call failed") from exc

    if not isinstance(response, str):
        raise RolloutEvaluationError("VLM judge returned a non-text response")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        data = json.loads(response.strip(), object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RolloutEvaluationError(
            "VLM judge did not return the required JSON object"
        ) from exc
    if not isinstance(data, dict):
        raise RolloutEvaluationError("VLM judge response must be a JSON object")

    verdict_text = data.get("verdict")
    if verdict_text not in {"YES", "NO"}:
        raise RolloutEvaluationError(
            "VLM judge verdict must be exactly 'YES' or 'NO'"
        )
    confidence_value = data.get("confidence")
    if isinstance(confidence_value, bool) or not isinstance(
        confidence_value, (int, float)
    ):
        raise RolloutEvaluationError("VLM judge confidence must be numeric")
    confidence = float(confidence_value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise RolloutEvaluationError(
            "VLM judge confidence must be finite and within [0, 1]"
        )
    explanation = data.get("explanation", "")
    if not isinstance(explanation, str):
        raise RolloutEvaluationError("VLM judge explanation must be text")

    verdict = verdict_text == "YES"
    logger.info(
        "VLM judge: %s (confidence=%.2f) — %s",
        "PASS" if verdict else "FAIL",
        confidence,
        explanation[:80],
    )
    return verdict, confidence
