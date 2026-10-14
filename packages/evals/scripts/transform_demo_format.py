#!/usr/bin/env python3
"""Transform VLM-enriched demos to multi-level conditioning format.

Converts the rigid {Observation, Intent, Action, Result} step format
into an adaptive {Think, Action, Expect} format with a high-level PLAN,
following the ShowUI-Aloha and Plan-and-Act approaches from the literature.

The current rigid demo format HURTS agent performance because when the
actual UI state doesn't match the described observations, the agent gets
confused.  The multi-level format (Option D from our eval analysis) adds:

  - A high-level PLAN section for strategic guidance
  - Think fields with reasoning (why + how, not just what)
  - Goal-oriented actions (less UI-state-specific)
  - Forward-looking Expect fields (not retrospective observations)

Two modes:
  - LLM-assisted (default): uses VLM for semantic transformation
  - Rule-based (--no-llm): regex-based, no API calls needed

Usage:
    # LLM-assisted transformation (default)
    python scripts/transform_demo_format.py demo_prompts_vlm/04d9aeaf-...-WOS.txt

    # Rule-based (no API calls)
    python scripts/transform_demo_format.py --no-llm demo_prompts_vlm/04d9aeaf-...-WOS.txt

    # Custom output path
    python scripts/transform_demo_format.py -o output.txt demo_prompts_vlm/04d9aeaf-...-WOS.txt

    # Dry run (print to stdout, don't write)
    python scripts/transform_demo_format.py --dry-run demo_prompts_vlm/04d9aeaf-...-WOS.txt
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("openadapt_evals.scripts.transform_demo")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_demo(demo_text: str) -> dict:
    """Parse existing DEMONSTRATION format into structured dict.

    Handles the format produced by the WAA annotation pipeline::

        DEMONSTRATION:
        Task: <task description>

        Step N:
          Observation: <text>
          Intent: <text>
          Action: <text>
          Result: <text>

    Returns:
        {
            "task": str,
            "steps": [
                {
                    "observation": str,
                    "intent": str,
                    "action": str,
                    "result": str,
                },
                ...
            ]
        }
    """
    # Strip trailing separator lines (e.g., "---")
    demo_text = re.sub(r"\n---+\s*$", "", demo_text.strip())

    lines = demo_text.splitlines()

    # Extract task description
    task = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Task:"):
            task = stripped[len("Task:"):].strip()
            break

    if not task:
        logger.warning("No 'Task:' line found in demo text")

    # Split into step blocks using "Step N:" pattern
    step_pattern = re.compile(r"^Step\s+\d+:\s*$", re.MULTILINE)
    step_starts = list(step_pattern.finditer(demo_text))

    steps = []
    for i, match in enumerate(step_starts):
        start = match.end()
        end = step_starts[i + 1].start() if i + 1 < len(step_starts) else len(demo_text)
        block = demo_text[start:end].strip()
        step = _parse_step_block(block)
        steps.append(step)

    logger.info("Parsed %d steps from demo", len(steps))
    return {"task": task, "steps": steps}


def _parse_step_block(block: str) -> dict:
    """Parse a single step block into its fields.

    Handles multi-line field values by collecting text until the next
    field label or end of block.
    """
    fields = {
        "observation": "",
        "intent": "",
        "action": "",
        "result": "",
    }

    # Map field label prefixes to dict keys
    label_map = {
        "Observation:": "observation",
        "Intent:": "intent",
        "Action:": "action",
        "Result:": "result",
    }

    current_key = None
    current_lines: list[str] = []

    for line in block.splitlines():
        stripped = line.strip()
        matched_label = False
        for label, key in label_map.items():
            if stripped.startswith(label):
                # Save previous field
                if current_key is not None:
                    fields[current_key] = " ".join(current_lines).strip()
                # Start new field
                current_key = key
                current_lines = [stripped[len(label):].strip()]
                matched_label = True
                break
        if not matched_label and current_key is not None:
            # Continuation line for current field
            if stripped:
                current_lines.append(stripped)

    # Save last field
    if current_key is not None:
        fields[current_key] = " ".join(current_lines).strip()

    # Clean any trailing separator artifacts from the last field
    for key in fields:
        fields[key] = re.sub(r"\s*-{3,}\s*$", "", fields[key]).strip()

    return fields


# ---------------------------------------------------------------------------
# LLM-assisted transformation
# ---------------------------------------------------------------------------


def generate_plan_llm(task: str, steps: list[dict], *, model: str = "gpt-4.1-mini") -> str:
    """Use VLM to generate high-level plan from step sequence."""
    from openadapt_evals.vlm import vlm_call

    steps_text = "\n".join(
        f"Step {i + 1}: {s['action']}" for i, s in enumerate(steps)
    )
    prompt = f"""Given this task and the sequence of actions taken to complete it, \
extract a concise high-level plan with 3-7 numbered steps. Each step should \
describe a logical phase of the task (not individual clicks). Group related \
actions into single plan steps. Use sub-steps (a, b, c) only when needed \
to clarify formulas or repeated patterns.

Task: {task}

Actions performed:
{steps_text}

Return ONLY the numbered plan, no other text."""

    logger.info("Generating plan via LLM (%s)", model)
    return vlm_call(prompt, model=model, max_tokens=512, temperature=0.2)


def transform_step_llm(
    step: dict,
    step_num: int,
    task: str,
    plan: str,
    *,
    model: str = "gpt-4.1-mini",
) -> dict:
    """Use VLM to transform a single step to new format.

    Returns dict with keys: think, action, expect.
    """
    from openadapt_evals.vlm import vlm_call

    prompt = f"""Transform this GUI action step into a more adaptive format.

Task context: {task}

High-level plan:
{plan}

Original step {step_num}:
  Observation: {step['observation']}
  Intent: {step['intent']}
  Action: {step['action']}
  Result: {step['result']}

Transform to the three fields below. Rules:
- Think: Why this action is needed + reasoning about how to do it. Include \
the goal, not specific UI element positions. 1-2 sentences.
- Action: The action, but goal-oriented. Keep specific values (cell refs, \
formulas, text to type) but describe UI interaction generically. 1 sentence.
- Expect: What should happen after, framed as forward-looking expectation. \
1 sentence.

Return ONLY valid JSON with exactly three keys: "think", "action", "expect". \
No other text."""

    logger.debug("Transforming step %d via LLM", step_num)
    response = vlm_call(prompt, model=model, max_tokens=300, temperature=0.1)
    return _parse_step_response(response, step, step_num)


def _parse_step_response(response: str, original_step: dict, step_num: int) -> dict:
    """Parse LLM response for a transformed step.

    Tries JSON extraction first, then falls back to field-prefix parsing,
    and finally to rule-based transformation as a last resort.
    """
    from openadapt_evals.vlm import extract_json

    # Try JSON extraction (handles fences, preamble, etc.)
    parsed = extract_json(response)
    if parsed and isinstance(parsed, dict):
        think = parsed.get("think", parsed.get("Think", ""))
        action = parsed.get("action", parsed.get("Action", ""))
        expect = parsed.get("expect", parsed.get("Expect", ""))
        if think and action and expect:
            return {"think": think, "action": action, "expect": expect}

    # Try field-prefix parsing (e.g., "Think: ...\nAction: ...\nExpect: ...")
    field_pattern = re.compile(
        r"(?:^|\n)\s*(?:Think|think):\s*(.+?)(?=\n\s*(?:Action|action):|\Z)"
        r".*?(?:Action|action):\s*(.+?)(?=\n\s*(?:Expect|expect):|\Z)"
        r".*?(?:Expect|expect):\s*(.+)",
        re.DOTALL,
    )
    m = field_pattern.search(response)
    if m:
        return {
            "think": m.group(1).strip(),
            "action": m.group(2).strip(),
            "expect": m.group(3).strip(),
        }

    # Last resort: rule-based fallback
    logger.warning(
        "Could not parse LLM response for step %d, using rule-based fallback",
        step_num,
    )
    return transform_step_rule_based(original_step)


# ---------------------------------------------------------------------------
# Rule-based transformation
# ---------------------------------------------------------------------------


def generate_plan_rule_based(task: str, steps: list[dict]) -> str:
    """Generate plan from step intents without LLM.

    Groups consecutive steps with similar intents into plan phases,
    then selects the most representative description for each group.
    """
    # First pass: categorise each step by extracting key action verbs/objects
    categories: list[str] = []
    for step in steps:
        cat = _categorise_step(step)
        categories.append(cat)

    # Second pass: merge consecutive steps with the same category
    phases: list[tuple[str, list[int]]] = []  # (category, [step indices])
    for i, cat in enumerate(categories):
        if phases and phases[-1][0] == cat:
            phases[-1][1].append(i)
        else:
            phases.append((cat, [i]))

    # Build plan text: use the first step's intent as the phase description
    plan_lines = []
    for idx, (cat, step_indices) in enumerate(phases):
        # Pick the most descriptive intent from the group (usually the first)
        intent = steps[step_indices[0]]["intent"].strip()
        description = re.sub(r"^To\s+", "", intent, flags=re.IGNORECASE)
        if description:
            description = description[0].upper() + description[1:]
        else:
            description = intent
        # Strip trailing period for consistency
        description = description.rstrip(".")
        plan_lines.append(f"{idx + 1}. {description}")

    return "\n".join(plan_lines)


def _categorise_step(step: dict) -> str:
    """Assign a coarse category to a step for plan-level grouping.

    Categories are broad action phases like "setup headers", "enter years",
    "enter formulas", "fill formulas", "format cells", etc.
    """
    action = step["action"].lower()
    intent = step["intent"].lower()

    # Order matters: check more specific patterns first
    if "header" in intent or ("type" in action and any(
        h in action for h in ['"year"', '"ca ', '"fa ', '"oa ']
    )):
        return "setup_headers"
    if re.search(r"fill handle|drag.*fill|copy.*formula.*down|filled down", action + intent):
        return "fill_formulas"
    if re.search(r"formula|=\(", action):
        return "enter_formulas"
    if re.search(r"press enter", action) and "formula" in intent:
        return "confirm_formulas"
    if re.search(r"percent|%|format", action + intent):
        return "format_cells"
    if re.search(r"select|highlight|click and drag", action + intent) and "formula" not in intent:
        return "select_range"
    if re.search(r"insert sheet|new sheet", action + intent):
        return "create_sheet"
    if re.search(r"type\s+\"?\d{4}", action) or re.search(r"year|annual", intent):
        return "enter_years"
    if re.search(r"press enter", action):
        return "confirm_entry"

    return f"other_{hash(intent) % 100}"


def _themes_similar(a: str, b: str) -> bool:
    """Check if two theme strings are similar enough to merge."""
    # Simple heuristic: share >40% of significant words
    stop = {"the", "a", "an", "in", "of", "for", "to", "and", "with", "as", "by"}
    words_a = {w for w in a.lower().split() if w not in stop and len(w) > 2}
    words_b = {w for w in b.lower().split() if w not in stop and len(w) > 2}
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) > 0.4


def transform_step_rule_based(step: dict) -> dict:
    """Transform step without LLM.

    - Think = Intent text, reframed with reasoning prefix
    - Action = Action text, as-is
    - Expect = Result text, reframed as expectation
    """
    # Build Think: strip leading "To " and add reasoning framing
    intent = step["intent"].strip()
    intent_body = re.sub(r"^To\s+", "", intent, flags=re.IGNORECASE)
    if intent_body:
        # Avoid double period (intent may already end with punctuation)
        body = f"{intent_body[0].lower()}{intent_body[1:]}"
        body = body.rstrip(".")
        think = f"I need to {body}."
    else:
        think = intent

    # Action: use as-is
    action = step["action"].strip()

    # Expect: reframe Result as forward-looking expectation
    result = step["result"].strip()
    if result:
        # Replace past tense indicators with expectation framing
        expect = result
        # Common past-tense patterns -> present/future
        replacements = [
            (r"^The\s+", "The "),
            (r"\bis added\b", "should be added"),
            (r"\bis entered\b", "should be entered"),
            (r"\bis displayed\b", "should be displayed"),
            (r"\bis executed\b", "should be executed"),
            (r"\bis populated\b", "should be populated"),
            (r"\bnow appears\b", "should appear"),
            (r"\bnow contains\b", "should contain"),
            (r"\bnow displays\b", "should display"),
            (r"\bare highlighted\b", "should be highlighted"),
            (r"\bare now displayed\b", "should now be displayed"),
            (r"\bare selected\b", "should be selected"),
        ]
        for pattern, replacement in replacements:
            expect = re.sub(pattern, replacement, expect)
    else:
        expect = "The action completes successfully."

    return {"think": think, "action": action, "expect": expect}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_output(task: str, plan: str, steps: list[dict]) -> str:
    """Format the transformed demo as text.

    Output structure::

        GOAL: <first sentence of task>

        PLAN:
        1. ...
        2. ...

        REFERENCE TRAJECTORY (for disambiguation -- adapt actions to your actual screen):

        Step 1:
          Think: ...
          Action: ...
          Expect: ...
        ...

        If your screen doesn't match what's expected, ...
    """
    # Build GOAL: first sentence of task
    first_sentence_match = re.match(r"([^.!?]+[.!?]?)", task)
    goal = first_sentence_match.group(1).strip() if first_sentence_match else task
    # Remove trailing period for cleaner look if present
    goal = goal.rstrip(".")

    lines = [
        f"GOAL: {goal}",
        "",
        "PLAN:",
        plan,
        "",
        "REFERENCE TRAJECTORY (for disambiguation -- adapt actions to your actual screen):",
        "",
    ]

    for i, step in enumerate(steps, 1):
        lines.append(f"Step {i}:")
        lines.append(f"  Think: {step['think']}")
        lines.append(f"  Action: {step['action']}")
        lines.append(f"  Expect: {step['expect']}")
        lines.append("")

    lines.append(
        "If your screen doesn't match what's expected, re-evaluate based on "
        "the PLAN and decide the best next action."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def transform_demo(
    demo_text: str,
    *,
    use_llm: bool = True,
    model: str = "gpt-4.1-mini",
) -> str:
    """Transform a demo from old format to multi-level conditioning format.

    Args:
        demo_text: Raw text of the existing demo file.
        use_llm: If True, use VLM for semantic transformation. If False,
            use rule-based transformation (no API calls).
        model: Model name for LLM calls (ignored if use_llm is False).

    Returns:
        Transformed demo text in the new format.
    """
    parsed = parse_demo(demo_text)

    if not parsed["steps"]:
        logger.error("No steps found in demo text")
        raise ValueError("No steps found in demo text")

    logger.info(
        "Transforming %d steps (mode=%s)",
        len(parsed["steps"]),
        "llm" if use_llm else "rule-based",
    )

    # Generate plan
    if use_llm:
        plan = generate_plan_llm(parsed["task"], parsed["steps"], model=model)
    else:
        plan = generate_plan_rule_based(parsed["task"], parsed["steps"])

    # Transform steps
    transformed_steps = []
    for i, step in enumerate(parsed["steps"]):
        if use_llm:
            transformed = transform_step_llm(
                step, i + 1, parsed["task"], plan, model=model,
            )
        else:
            transformed = transform_step_rule_based(step)
        transformed_steps.append(transformed)
        logger.debug("Transformed step %d/%d", i + 1, len(parsed["steps"]))

    return format_output(parsed["task"], plan, transformed_steps)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform VLM-enriched demos to multi-level conditioning format",
    )
    parser.add_argument("input", help="Path to existing demo .txt file")
    parser.add_argument(
        "-o", "--output",
        help="Output path (default: <input>_multilevel.txt)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use rule-based transformation (no API calls)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4.1-mini",
        help="Model for LLM transformation (default: gpt-4.1-mini)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print output to stdout without writing a file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        level=level,
    )

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    demo_text = input_path.read_text()
    logger.info("Read %d bytes from %s", len(demo_text), input_path)

    try:
        output = transform_demo(
            demo_text,
            use_llm=not args.no_llm,
            model=args.model,
        )
    except ValueError as e:
        logger.error("Transformation failed: %s", e)
        sys.exit(1)

    if args.dry_run:
        print(output)
        logger.info("Dry run complete (%d bytes)", len(output))
        return

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(
            input_path.stem + "_multilevel" + input_path.suffix,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output)
    logger.info("Wrote %d bytes to %s", len(output), output_path)


if __name__ == "__main__":
    main()
