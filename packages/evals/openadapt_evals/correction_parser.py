"""Parse a human correction capture into a PlanStep.

Uses a VLM call to compare before/after screenshots and describe what
the human did in the same format as a plan step (think/action/expect).
"""

from __future__ import annotations

import json
import logging

from openadapt_evals.vlm import vlm_call

logger = logging.getLogger(__name__)

_PARSE_PROMPT = """\
The agent was trying to perform a step but failed. A human then completed the step manually.

Failed step description: {step_action}
Failure explanation: {failure_explanation}

Compare the BEFORE screenshot (when the agent failed) and the AFTER screenshot \
(after the human completed the step). Describe what the human did to complete the step.

Respond in this exact JSON format:
{{
  "think": "reasoning about what needed to happen and why the agent failed",
  "action": "concrete description of what the human did (e.g., 'Click the Display button in the left sidebar')",
  "expect": "what the screen looks like after the action"
}}

Respond with ONLY the JSON object, no other text."""


def parse_correction(
    step_action: str,
    failure_explanation: str,
    before_screenshot: bytes,
    after_screenshot: bytes,
    model: str = "gpt-4.1-mini",
    provider: str = "openai",
) -> dict:
    """Parse before/after screenshots into a PlanStep dict.

    Returns dict with keys: think, action, expect.
    """
    prompt = _PARSE_PROMPT.format(
        step_action=step_action,
        failure_explanation=failure_explanation,
    )

    response = vlm_call(
        prompt,
        images=[before_screenshot, after_screenshot],
        model=model,
        provider=provider,
        max_tokens=512,
        cost_label="correction_parser",
    )

    # Extract JSON from response
    try:
        # Try direct parse first
        result = json.loads(response)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        import re

        match = re.search(r"\{[^}]+\}", response, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            logger.error("Failed to parse VLM response as JSON: %s", response[:200])
            result = {
                "think": f"Human corrected the step: {step_action}",
                "action": step_action,
                "expect": "Step completed successfully",
            }

    # Ensure required keys exist
    for key in ("think", "action", "expect"):
        if key not in result:
            result[key] = ""

    logger.info("Parsed correction: action=%s", result["action"][:80])
    return result
