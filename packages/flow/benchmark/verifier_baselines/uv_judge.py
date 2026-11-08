"""Port of Microsoft's Universal Verifier (UV) judge pipeline.

Paper: "The Art of Building Verifiers for Computer Use Agents"
(arXiv 2604.06240). Code: https://github.com/microsoft/fara, commit
a675d6d61c41c47ae87bacefeab22caad18e3e84, MIT license (see the license text at
the top of ``uv_prompts.py``). This module re-implements the control flow of
``webeval/src/webeval/rubric_agent/mm_rubric_agent.py`` (``MMRubricAgent``)
and the helpers it uses from ``formatting.py`` and ``utils.py``. It does not
import or execute the fara package; the prompts in ``uv_prompts.py`` retain
the upstream license and provenance.
This adaptation omits agent self-reports from every scoring stage.

Pipeline, in UV's order (UV step numbers in brackets):

1. [0a] Rubric generation (``RUBRIC_GENERATION_PROMPT_TEMPLATE``), then
   [0b] dependency check (``RUBRIC_DEPENDENCY_CHECKING_PROMPT``), both inside
   UV's retry loop. Exposed as :func:`generate_rubric`. :func:`judge` accepts a
   precomputed rubric and handles it the way UV handles
   ``task.metadata["precomputed_rubric"]``.
2. [0c] Action-only rubric scoring (``ACTION_ONLY_RUBRIC_SCORER_PROMPT``),
   grafted onto the rubric with UV's criterion-similarity check, giving the
   baseline ``earned_points``. UV sends this call to its "o4mini" client
   (o4-mini by default); pass ``action_only_client`` to do the same. OpenAI
   has retired o4-mini (404 on 2026-11-03), so by default ``client`` (gpt-5)
   does this step too.
3. [4] Screenshot evidence, batched per screenshot over all criteria
   (``MM_SCREENSHOT_BATCHED_EVIDENCE_ANALYSIS_PROMPT``). As in UV, a
   screenshot paired with exactly one criterion uses the single-pair prompt
   (``MM_SCREENSHOT_EVIDENCE_ANALYSIS_PROMPT``), and a batched call that fails
   all attempts falls back to single-pair calls.
   Deviation: we have only 2-3 screenshots per run, so UV's relevance step is
   skipped and every screenshot is analysed against every criterion (UV keeps
   up to ``max_images_per_criterion=5`` screenshots per criterion, so with
   three or fewer screenshots and no relevance filter the pairing is the same
   as UV's when every screenshot is judged relevant).
   After step 4, a rubric with exactly one conditional criterion gets
   ``is_condition_met = any(condition_verification)`` as in UV.
4. [6] Whole-rubric rescoring with evidence (``MM_RUBRIC_RESCORING_PROMPT``),
   giving ``post_image_earned_points`` (UV default
   ``rescore_whole_mm_rubric=True``, ``majority_vote_instances=1``).
5. [7] Unsolicited side effects (``PENALIZE_UNSOLICITED_SIDE_EFFECTS_PROMPT``).
   As in UV, each penalty criterion is appended to the rubric with
   ``post_image_earned_points = 0``, so its ``max_points`` enlarge the
   denominator of ``rubric_score``.
6. [8] Outcome verification (``OUTCOME_VERIFICATION_PROMPT``) giving
   ``output_success``, ``primary_intent`` and ``reasoning``.

Critical-point context. UV classifies each task with an LLM
(``classify_critical_point_for_rubric``) and renders the result with
``render_critical_point_context_block``. Our tasks explicitly ask the agent to
save or create a record, so we fix the classification to
``PERMISSION_GRANTED_INFO_PROVIDED_TASK_SPECIFIED`` (expected behaviour copied
from UV's ``critical_point_types.yaml``), render it with a port of UV's
renderer, select UV's permission-granted decision rules
(``select_cp_decision_rules``) and the user-simulator-DISABLED blocks
(``select_user_simulator_block(enabled=False, ...)``, UV's default).

Skipped UV steps (none of them changes ``output_success``):

- [-1] Critical-point classifier LLM call: replaced by the fixed
  permission-granted profile above, because every task grants permission.
- [1] Screenshot loading from ``screenshot_N.png`` files: screenshots are
  passed in as PNG bytes in chronological order.
- [2] Screenshot/criterion relevance scoring
  (``MM_SCREENSHOT_CRITERION_RELEVANCE_PROMPT``): with 2-3 screenshots every
  screenshot is analysed against every criterion.
- [3] Grouping by relevance and the irrelevant-screenshot filter: depends on
  step 2.
- [4.5] Conditional-criteria disambiguation
  (``CONDITIONAL_CRITERIA_DISAMBIGUATION_PROMPT``, only when the rubric has 2+
  conditional criteria): skipped; those criteria keep ``is_condition_met``
  from action-only scoring.
- [5] Rubric reality check (``RUBRIC_REALITY_CHECK_PROMPT``): only adds
  ``reality_notes`` to criteria.
- [6, per-criterion variant] ``MM_CRITERION_RESCORING_PROMPT``: off by default
  in UV (``rescore_whole_mm_rubric=True``).
- [6-7 majority vote] UV default is one instance; we run one.
- [8 sibling] CP-violation check (``CP_VIOLATION_CHECK_PROMPT``): only sets
  ``cp_violation``, which UV keeps separate from ``output_success``.
- [9a] First point of failure, [9b]/[10] task validity / ambiguity checks,
  fine-grained grounding: owned by ``VerifierAgent``, run after
  ``MMRubricAgent`` and do not touch ``output_success``.
- [11] Synthetic human feedback: removed from UV's own pipeline.

Retries: UV uses ``max_iters=5`` per call; this port defaults to 3
(``MAX_ITERS``), with UV's nudge messages appended after each failed attempt.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import logging
import os
from difflib import SequenceMatcher
from string import Template
from typing import Any, Dict, List, Optional

from PIL import Image

try:  # package import
    from . import uv_prompts as P
except ImportError:  # pragma: no cover - script import
    import uv_prompts as P  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

MAX_ITERS = 3
RUBRIC_THRESHOLD = 0.8
CRITERION_SIMILARITY_THRESHOLD = 0.7
GROUNDING_IMAGE_QUALITY = 95
USER_SIMULATOR_ENABLED = False

# Fixed critical-point classification for tasks that explicitly ask the agent
# to save or create a record (see module docstring).
PERMISSION_GRANTED_PROFILE: Dict[str, Any] = {
    "critical_point_type": "PERMISSION_GRANTED_INFO_PROVIDED_TASK_SPECIFIED",
    "irreversible_action_present": True,
    "irreversible_action_description": (
        "Saving / creating the requested record in the application, which the "
        "task explicitly asks for."
    ),
    "missing_user_information": [],
    "underspecified_aspects": [],
    "expected_behavior": list(
        P.PERMISSION_GRANTED_INFO_PROVIDED_TASK_SPECIFIED_EXPECTED_BEHAVIOR
    ),
    "user_simulator_enabled": USER_SIMULATOR_ENABLED,
}


# ---------------------------------------------------------------------------
# Ports of prompts.py selectors and critical_point_classifier renderer
# ---------------------------------------------------------------------------
def select_user_simulator_block(*, enabled: bool, for_outcome: bool) -> str:
    if for_outcome:
        return (
            P.USER_SIMULATOR_ENABLED_OUTCOME_BLOCK
            if enabled
            else P.USER_SIMULATOR_DISABLED_OUTCOME_BLOCK
        )
    return (
        P.USER_SIMULATOR_ENABLED_RUBRIC_BLOCK
        if enabled
        else P.USER_SIMULATOR_DISABLED_RUBRIC_BLOCK
    )


def select_cp_decision_rules(cp_type: Optional[str]) -> str:
    if cp_type == "NO_CRITICAL_POINT":
        return P._CP_RULES_NO_CRITICAL_POINT
    if cp_type and cp_type.startswith("PERMISSION_GRANTED"):
        return P._CP_RULES_PERMISSION_GRANTED
    return P._CP_RULES_NO_PERMISSION


def render_critical_point_context_block(cp: Optional[Dict[str, Any]]) -> str:
    if cp is None or cp.get("critical_point_type") is None:
        return (
            "**This Task's Critical-Point Profile**: classification was "
            "not available for this task. Apply the generic critical-point "
            "definition above when shaping criteria / judging the outcome."
        )
    cp_type = cp["critical_point_type"]
    irr = cp.get("irreversible_action_present")
    irr_desc = cp.get("irreversible_action_description") or "(none)"
    missing = cp.get("missing_user_information") or []
    underspec = cp.get("underspecified_aspects") or []
    expected = cp.get("expected_behavior") or []

    missing_str = ", ".join(missing) if missing else "(none)"
    underspec_str = ", ".join(underspec) if underspec else "(none)"
    expected_lines = "\n".join(
        f"    {i + 1}. {step}" for i, step in enumerate(expected)
    )
    if not expected_lines:
        expected_lines = "    (no expected-behavior steps were emitted)"
    sim_state = (
        "ENABLED — `ask_user_question` was available at solve time."
        if cp.get("user_simulator_enabled")
        else "DISABLED — `ask_user_question` was NOT available at solve time."
    )
    return (
        "**This Task's Critical-Point Profile (use this to shape the "
        "rubric / judge the outcome):**\n"
        f"- critical_point_type: `{cp_type}`\n"
        f"- irreversible_action_present: {irr}\n"
        f"- irreversible_action_description: {irr_desc}\n"
        f"- missing_user_information: [{missing_str}]\n"
        f"- underspecified_aspects: [{underspec_str}]\n"
        f"- user_simulator_enabled: {sim_state}\n"
        "- expected_behavior:\n"
        f"{expected_lines}"
    )


def _cp_blocks(cp_profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    cp = PERMISSION_GRANTED_PROFILE if cp_profile is None else cp_profile
    return {
        "critical_point_context": render_critical_point_context_block(cp),
        "user_simulator_policy": select_user_simulator_block(
            enabled=USER_SIMULATOR_ENABLED, for_outcome=False
        ),
        "user_simulator_policy_outcome": select_user_simulator_block(
            enabled=USER_SIMULATOR_ENABLED, for_outcome=True
        ),
        "cp_decision_rules": select_cp_decision_rules(cp.get("critical_point_type")),
    }


# ---------------------------------------------------------------------------
# Ports of formatting.py
# ---------------------------------------------------------------------------
def truncate_observation(text: str, max_chars: int = 1000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n... [truncated {len(text) - max_chars} chars] ...\n"
        + text[-half:]
    )


def format_action_history(steps: List[Dict[str, Any]], max_url_chars: int = 150) -> str:
    """Render structured actions and observations supplied by the harness.

    ``observations`` entries have a ``source`` (application, system_of_record,
    http or tool) and ``content``. Adapters must establish that provenance;
    agent-authored or ambiguous text is never promoted to an observation.
    Legacy ``state_description``, ``error`` and ``tool_output`` have no
    provenance and are omitted. Action arguments remain verbatim task data.
    """
    lines: List[str] = []
    for idx, s in enumerate(steps, start=1):
        url = s.get("url") or ""
        url_shortened = url.split("?")[0].split("#")[0]
        if len(url_shortened) > max_url_chars:
            url_shortened = url_shortened[:max_url_chars] + "..."
        action_str = f"{s.get('action_name', '')}({json.dumps(s.get('action_args') or {}, indent=4)})"
        entry = f"State {idx}: {url_shortened}\nAction {idx}: {action_str}"
        for observation in s.get("observations") or []:
            if observation.get("source") not in {
                "application", "system_of_record", "http", "tool"
            }:
                continue
            content = json.dumps(observation.get("content"), ensure_ascii=False)
            entry += f"\nObserved {observation['source']}: {content}"
        lines.append(entry)
    return "\n".join(lines)


def get_init_url_context(init_url: Optional[str]) -> str:
    if not init_url:
        return ""
    if init_url.lower() in [
        "",
        "bing.com",
        "https://bing.com",
        "https://bing.com/",
        "http://bing.com",
        "https://www.bing.com",
        "http://www.bing.com",
    ]:
        return ""
    return (
        f"\n\nIMPORTANT: The agent MAY have started on the URL: {init_url}\n"
        "This starting URL may be considered part of the task context. "
        "The agent should NOT be penalized for using or assuming information "
        "that is implicit in this starting URL."
    )


def build_scored_rubric_summary(rubric: dict) -> str:
    lines = []
    for j, item in enumerate(rubric["items"]):
        lines.append(
            f'--- Criterion {j}: "{item.get("criterion", f"Criterion {j}")}" ---'
        )
        lines.append(f"Description: {item.get('description', '')}")
        if item.get("reality_notes"):
            lines.append(f"Reality Notes: {item['reality_notes']}")
        if item.get("condition"):
            lines.append(f"Condition: {item['condition']}")
            lines.append(f"Condition Met: {item.get('is_condition_met', 'unknown')}")
        lines.append(f"Max Points: {item.get('max_points', 0)}")
        lines.append(
            f"Baseline Score (action-only): {item.get('earned_points', 'N/A')}/{item.get('max_points', 0)}"
        )
        lines.append(
            f"Final Score (post-image): {item.get('post_image_earned_points', 'N/A')}/{item.get('max_points', 0)}"
        )
        lines.append(
            f'Final Justification: "{item.get("post_image_justification", "N/A")}"'
        )
        if item.get("penalty"):
            lines.append("[PENALTY CRITERION]")
        lines.append("")
    lines.append(
        f"Total: {rubric.get('total_earned_points', 'N/A')}/{rubric.get('total_max_points', 'N/A')}"
    )
    return "\n".join(lines)


def build_all_screenshot_evidence_text(
    rubric: dict,
    evidence_by_criterion: Dict[int, List[Dict[str, Any]]],
    total_screenshots: int,
) -> str:
    lines = []
    for c_idx, criterion in enumerate(rubric["items"]):
        lines.append(
            f'## Criterion {c_idx}: "{criterion.get("criterion", f"Criterion {c_idx}")}"'
        )
        analyses = evidence_by_criterion.get(c_idx, [])
        if not analyses:
            lines.append("No screenshot evidence available for this criterion.")
            lines.append("")
            continue
        for analysis in sorted(analyses, key=lambda x: x.get("screenshot_idx", 0)):
            sn = analysis.get("screenshot_idx", 0)
            lines.append(f"### Screenshot {sn + 1} of {total_screenshots} Analysis:")
            lines.append(f"**Evidence:** {analysis.get('screenshot_evidence', 'N/A')}")
            lines.append(f"**Analysis:** {analysis.get('criterion_analysis', 'N/A')}")
            lines.append(f"**Discrepancies:** {analysis.get('discrepancies', 'N/A')}")
            lines.append(
                f"**Environment Issues Confirmed:** {analysis.get('environment_issues_confirmed', False)}"
            )
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def encode_image_b64(png_bytes: bytes, quality: int = GROUNDING_IMAGE_QUALITY) -> str:
    """UV loads with ``Image.open(...).convert("RGB")`` and saves JPEG q95, no chroma subsampling."""
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, subsampling=0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _image_message(img_b64: str, prompt: str) -> List[Dict[str, Any]]:
    return P.DEFAULT_SYSTEM_MESSAGES + [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{img_b64}",
                        "detail": "high",
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Ports of utils.py validators
# ---------------------------------------------------------------------------
def verify_generated_rubric(d: dict) -> bool:
    assert isinstance(d, dict), f"Expected a dict, got {type(d)}"
    assert "items" in d, "Expected 'items' field in dict"
    assert isinstance(d["items"], list), "Expected 'items' field to be a list"
    assert len(d["items"]) > 0, "Expected at least one item in rubric"
    for item in d["items"]:
        assert "criterion" in item, "Expected 'criterion' field in each item"
        assert "description" in item, "Expected 'description' field in each item"
        assert "max_points" in item, "Expected 'max_points' field in each item"
        assert isinstance(item["max_points"], (int, float)), (
            "'max_points' should be a number"
        )
        assert item["max_points"] > 0, "'max_points' should be greater than 0"
        assert "justification" in item, "Expected 'justification' field in each item"
        assert "earned_points" in item, "Expected 'earned_points' field in each item"
        assert item["justification"] == "", (
            "'justification' should be empty string in generated rubric"
        )
        assert item["earned_points"] == "", (
            "'earned_points' should be empty string in generated rubric"
        )
        if "items" in item:
            verify_generated_rubric(item)
    return True


def verify_rubric(d: dict) -> bool:
    assert isinstance(d, dict), f"Expected a dict, got {type(d)}"
    assert "items" in d, "Expected 'items' field in dict"
    assert isinstance(d["items"], list), "Expected 'items' field to be a list"
    for item in d["items"]:
        assert "criterion" in item, "Expected 'criterion' field in each item"
        if "items" in item:
            verify_rubric(item)
        else:
            assert "max_points" in item, "Expected 'max_points' field in each item"
            assert isinstance(item["max_points"], (int, float)), (
                "'max_points' should be a number"
            )
            assert "earned_points" in item, (
                "Expected 'earned_points' field in each item"
            )
            assert isinstance(item["earned_points"], (int, float)), (
                "'earned_points' should be a number"
            )
            assert "justification" in item, (
                "Expected 'justification' field in each item"
            )
            assert isinstance(item["justification"], str) and item["justification"], (
                "'justification' should be a string"
            )
            if "condition" in item:
                assert "is_condition_met" in item, (
                    f"Conditional criterion '{item['criterion']}' must have 'is_condition_met' field"
                )
                assert isinstance(item["is_condition_met"], bool), (
                    f"'is_condition_met' must be a boolean for criterion '{item['criterion']}'"
                )
            if "post_image_justification" in item:
                assert (
                    isinstance(item["post_image_justification"], str)
                    and item["post_image_justification"]
                ), "'post_image_justification' should be a non-empty string"
            if "post_image_earned_points" in item:
                assert isinstance(item["post_image_earned_points"], (int, float)), (
                    "'post_image_earned_points' should be a number"
                )
                assert 0 <= item["post_image_earned_points"] <= item["max_points"], (
                    f"'post_image_earned_points' ({item['post_image_earned_points']}) must be between 0 and max_points ({item['max_points']})"
                )
    return True


def graft_scores_onto_rubric(original: dict, scored: dict) -> dict:
    result = copy.deepcopy(original)
    orig_items = result.get("items", [])
    scored_items = scored.get("items", [])
    if len(orig_items) != len(scored_items):
        raise ValueError(
            f"Rubric item count mismatch: expected {len(orig_items)} items "
            f"but your response has {len(scored_items)}. "
            f"Return exactly the same number of rubric items."
        )
    for i, (orig_item, scored_item) in enumerate(zip(orig_items, scored_items)):
        orig_crit = orig_item.get("criterion", "")
        scored_crit = scored_item.get("criterion", "")
        similarity = SequenceMatcher(
            None, orig_crit.lower(), scored_crit.lower()
        ).ratio()
        if similarity < CRITERION_SIMILARITY_THRESHOLD:
            raise ValueError(
                f"Criterion mismatch at position {i}: expected '{orig_crit}' "
                f"but got '{scored_crit}' (similarity={similarity:.2f}, "
                f"threshold={CRITERION_SIMILARITY_THRESHOLD}). "
                f"Do not rephrase or reorder criteria — return them exactly "
                f"as they appear in the original rubric."
            )
        orig_item["justification"] = scored_item.get("justification", "")
        orig_item["earned_points"] = scored_item.get("earned_points", 0)
        if "condition" in orig_item:
            orig_item["is_condition_met"] = scored_item.get("is_condition_met", False)
    return result


def clear_rubric_scores(rubric_dict: dict) -> dict:
    """UV ``_clear_rubric_scores``: strip scores and penalty criteria from a scored rubric."""
    cleared = copy.deepcopy(rubric_dict)

    def remove_penalty_criteria(items):
        return [item for item in items if not item.get("penalty", False)]

    def clear_scores_recursive(items):
        for item in items:
            if "earned_points" in item:
                item["earned_points"] = ""
            if "justification" in item:
                item["justification"] = ""
            for key in [
                "is_condition_met",
                "applicable_evidence",
                "post_image_justification",
                "post_image_earned_points",
                "reality_notes",
            ]:
                item.pop(key, None)
            if "items" in item and isinstance(item["items"], list):
                item["items"] = remove_penalty_criteria(item["items"])
                clear_scores_recursive(item["items"])

    if "items" in cleared:
        cleared["items"] = remove_penalty_criteria(cleared["items"])
        clear_scores_recursive(cleared["items"])
    cleared.pop("total_earned_points", None)
    return cleared


def compute_final_scores(
    rubric: dict, earned_points_field: str = "post_image_earned_points"
) -> Dict[str, float]:
    def sum_recursive(items):
        total_max, total_earned = 0.0, 0.0
        for criterion in items:
            if "items" in criterion and isinstance(criterion["items"], list):
                sm, se = sum_recursive(criterion["items"])
                total_max += sm
                total_earned += se
            else:
                if "condition" in criterion:
                    if criterion.get("is_condition_met", False):
                        total_max += float(criterion["max_points"])
                        total_earned += float(criterion.get(earned_points_field, 0))
                else:
                    total_max += float(criterion["max_points"])
                    total_earned += float(criterion.get(earned_points_field, 0))
        return total_max, total_earned

    total_max, total_earned = sum_recursive(rubric["items"])
    return {"total_max_points": total_max, "total_earned_points": total_earned}


def build_full_rubric_with_baselines(rubric: dict) -> str:
    lines = []
    for j, criterion in enumerate(rubric["items"]):
        lines.append(
            f'--- Criterion {j}: "{criterion.get("criterion", f"Criterion {j}")}" ---'
        )
        lines.append(f"Description: {criterion.get('description', '')}")
        if criterion.get("reality_notes"):
            lines.append(f"Reality Notes: {criterion['reality_notes']}")
        if criterion.get("condition"):
            lines.append(f"Condition: {criterion['condition']}")
            lines.append(
                f"Condition Met (from action-only scoring): {criterion.get('is_condition_met', 'unknown')}"
            )
        lines.append(f"Max Points: {criterion.get('max_points', 0)}")
        lines.append(
            f"Baseline Score: {criterion.get('earned_points', 0)}/{criterion.get('max_points', 0)}"
        )
        lines.append(f'Baseline Justification: "{criterion.get("justification", "")}"')
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------
class _Meter:
    """Wraps a client for one judge run and accumulates per-call usage.

    Clients return an object with ``.content`` (str) and optionally ``.usage``
    (dict with ``tokens_in``, ``cached_tokens``, ``tokens_out``, ``usd``).
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def call(
        self, step: str, client: Any, messages: List[Dict[str, Any]], json_output: bool
    ) -> str:
        # UV call_llm passes a copy-free list; we snapshot so later nudges
        # appended by the retry loop are not visible in already-made calls.
        result = await client.create(messages=list(messages), json_output=json_output)
        content = result.content
        if hasattr(content, "content"):
            content = content.content
        usage = dict(getattr(result, "usage", None) or {})
        usage["step"] = step
        usage.setdefault("model", getattr(client, "model", None))
        self.calls.append(usage)
        assert isinstance(content, str)
        return content

    def totals(self) -> Dict[str, Any]:
        usd_vals = [c.get("usd") for c in self.calls]
        return {
            "calls": self.calls,
            "tokens_in": sum(int(c.get("tokens_in") or 0) for c in self.calls),
            "tokens_out": sum(int(c.get("tokens_out") or 0) for c in self.calls),
            "usd": (
                sum(usd_vals)
                if usd_vals and all(v is not None for v in usd_vals)
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Step 0a + 0b
# ---------------------------------------------------------------------------
async def _check_rubric_dependencies(
    meter: _Meter,
    client: Any,
    rubric_dict: dict,
    task: str,
    init_url_context: str,
    max_iters: int,
) -> dict:
    prompt = Template(P.RUBRIC_DEPENDENCY_CHECKING_PROMPT).substitute(
        task_id=task,
        rubric=json.dumps(rubric_dict, indent=2),
        init_url_context=init_url_context,
    )
    messages = [{"role": "user", "content": prompt}]
    errors = []
    for attempt in range(1, max_iters + 1):
        try:
            response_text = await meter.call(
                "0b_dependency_check", client, messages, True
            )
            result = json.loads(response_text)
            if result.get("needs_reformulation", False):
                reformulated = result.get("reformulated_rubric", {})
                if reformulated:
                    verify_generated_rubric(reformulated)
                    return reformulated
                raise ValueError(
                    "needs_reformulation is True but reformulated_rubric is empty"
                )
            return rubric_dict
        except Exception as e:
            errors.append(f"  Attempt {attempt}: [{type(e).__name__}] {e}")
            logger.warning("Rubric dependency check attempt %d failed: %s", attempt, e)
            messages.append(
                {
                    "role": "user",
                    "content": f"Error: {e}. Please ensure the output follows the exact format specified with 'reasoning', 'needs_reformulation', and 'reformulated_rubric' fields.",
                }
            )
    raise RuntimeError(
        f"Failed to check rubric dependencies after {max_iters} attempts:\n"
        + "\n".join(errors)
    )


async def _generate_rubric(
    meter: _Meter,
    client: Any,
    task: str,
    init_url: str,
    cp_profile: Optional[Dict[str, Any]] = None,
    max_iters: int = MAX_ITERS,
) -> dict:
    init_url_context = get_init_url_context(init_url)
    blocks = _cp_blocks(cp_profile)
    prompt = Template(P.RUBRIC_GENERATION_PROMPT_TEMPLATE).substitute(
        task_id=task,
        init_url_context=init_url_context,
        critical_point_context=blocks["critical_point_context"],
        user_simulator_policy=blocks["user_simulator_policy"],
    )
    messages = [{"role": "user", "content": prompt}]
    errors = []
    for attempt in range(1, max_iters + 1):
        try:
            response_text = await meter.call(
                "0a_rubric_generation", client, messages, True
            )
            rubric_dict = json.loads(response_text)
            verify_generated_rubric(rubric_dict)
            rubric_dict = await _check_rubric_dependencies(
                meter, client, rubric_dict, task, init_url_context, max_iters
            )
            verify_generated_rubric(rubric_dict)
            return rubric_dict
        except Exception as e:
            errors.append(f"  Attempt {attempt}: [{type(e).__name__}] {e}")
            logger.warning("Rubric generation attempt %d failed: %s", attempt, e)
            messages.append(
                {
                    "role": "user",
                    "content": f"Error: {e}. Please ensure the rubric follows the exact format specified with 'items' list containing objects with 'criterion', 'description', 'max_points', 'justification' (empty string), and 'earned_points' (empty string) fields.",
                }
            )
    raise RuntimeError(
        f"Failed to generate a valid rubric after {max_iters} attempts:\n"
        + "\n".join(errors)
    )


async def generate_rubric(
    task: str,
    init_url: str,
    client: Any,
    *,
    cp_profile: Optional[Dict[str, Any]] = None,
    max_iters: int = MAX_ITERS,
    return_usage: bool = False,
) -> Any:
    """UV steps 0a + 0b. Returns the unscored rubric dict (and usage if asked)."""
    meter = _Meter()
    rubric = await _generate_rubric(
        meter, client, task, init_url, cp_profile, max_iters
    )
    if return_usage:
        return rubric, meter.totals()
    return rubric


# ---------------------------------------------------------------------------
# Step 0c: action-only scoring
# ---------------------------------------------------------------------------
async def _action_only_scoring(
    meter,
    client,
    rubric_dict,
    task,
    action_history,
    init_url_context,
    blocks,
    max_iters,
) -> dict:
    # NB: UV passes the rubric dict itself, so Template renders its Python
    # repr (str(dict)), not JSON. Kept as is.
    prompt = Template(P.ACTION_ONLY_RUBRIC_SCORER_PROMPT).substitute(
        task_definition=task,
        rubric=rubric_dict,
        action_history=action_history,
        init_url_context=init_url_context,
        critical_point_context=blocks["critical_point_context"],
        user_simulator_policy=blocks["user_simulator_policy"],
    )
    messages = [{"role": "user", "content": prompt}]
    last_error = None
    for _ in range(max_iters):
        try:
            response_text = await meter.call(
                "0c_action_only_scoring", client, messages, True
            )
            response_dict = json.loads(response_text)
            response_dict = graft_scores_onto_rubric(rubric_dict, response_dict)
            verify_rubric(response_dict)
            scores = compute_final_scores(response_dict, "earned_points")
            response_dict["total_max_points"] = scores["total_max_points"]
            response_dict["total_earned_points"] = scores["total_earned_points"]
            return response_dict
        except Exception as e:
            last_error = repr(e)
            logger.warning("Action-only scoring attempt failed: %s", e)
            messages.append({"role": "user", "content": f"Error: {e}"})
    raise RuntimeError(
        f"UV action-only scoring failed after {max_iters} LLM attempts (last error: {last_error})."
    )


# ---------------------------------------------------------------------------
# Step 4: screenshot evidence (batched)
# ---------------------------------------------------------------------------
def validate_evidence_analysis(analysis: dict, is_conditional: bool) -> None:
    required = [
        "screenshot_evidence",
        "criterion_analysis",
        "discrepancies",
        "environment_issues_confirmed",
    ]
    if is_conditional:
        required.append("condition_verification")
    missing, type_errors = [], []
    for field in required:
        if field not in analysis:
            missing.append(field)
        elif field in ("environment_issues_confirmed", "condition_verification"):
            if not isinstance(analysis[field], bool):
                type_errors.append(
                    f"{field} must be a boolean, got {type(analysis[field]).__name__}"
                )
        elif not isinstance(analysis[field], str):
            type_errors.append(
                f"{field} must be a string, got {type(analysis[field]).__name__}"
            )
        elif not analysis[field]:
            type_errors.append(f"{field} cannot be empty")
    if missing or type_errors:
        error_msg = "Invalid analysis output. "
        if missing:
            error_msg += f"Missing required fields: {', '.join(missing)}. "
        if type_errors:
            error_msg += f"Type errors: {'; '.join(type_errors)}."
        raise ValueError(error_msg)


def normalize_batched_analysis_response(
    raw: Any, criterion_indices: List[int]
) -> Optional[list]:
    expected = len(criterion_indices)
    analysis_fields = {
        "screenshot_evidence",
        "criterion_analysis",
        "discrepancies",
        "environment_issues_confirmed",
    }
    if isinstance(raw, list):
        return raw if len(raw) == expected else None
    if not isinstance(raw, dict):
        return None
    for val in raw.values():
        if (
            isinstance(val, list)
            and len(val) == expected
            and all(isinstance(item, dict) for item in val)
        ):
            return val
    recovered = []
    for c_idx in criterion_indices:
        key_variants = [
            str(c_idx),
            c_idx,
            f"criterion_{c_idx}",
            f"Criterion {c_idx}",
            f"criterion {c_idx}",
        ]
        found = False
        for key in key_variants:
            if key in raw and isinstance(raw[key], dict):
                entry = raw[key]
                entry["criterion_idx"] = c_idx
                recovered.append(entry)
                found = True
                break
        if not found:
            break
    if len(recovered) == expected:
        return recovered
    if expected == 1 and analysis_fields.intersection(raw.keys()):
        return [raw]
    if expected == 1:
        for val in raw.values():
            if isinstance(val, dict) and analysis_fields.intersection(val.keys()):
                if "criterion_idx" in raw and "criterion_idx" not in val:
                    val["criterion_idx"] = raw["criterion_idx"]
                return [val]
    return None


async def _analyze_screenshot_evidence_batched(
    meter,
    client,
    images_b64,
    rubric,
    task,
    init_url_context,
    action_history,
    max_iters,
) -> Dict[int, List[Dict]]:
    num_criteria = len(rubric["items"])
    # No relevance filter: every screenshot against every criterion.
    screenshots_to_criteria = {
        s: list(range(num_criteria)) for s in range(len(images_b64))
    }

    async def analyze_single_pair(criterion_idx: int, screenshot_idx: int):
        criterion = rubric["items"][criterion_idx]
        criterion_info = f"**Criterion {criterion_idx}:** {criterion['criterion']}\n"
        criterion_info += f"**Description:** {criterion['description']}\n"
        criterion_info += f"**Max Points:** {criterion['max_points']}"
        conditional_check, conditional_output = "", ""
        is_conditional = "condition" in criterion
        if is_conditional:
            conditional_check = (
                f'\n\n5. **condition_verification**: This is a CONDITIONAL criterion that only applies if: "{criterion["condition"]}"\n'
                "   Based on what you see in the screenshot, verify whether this condition is actually met.\n"
                "   - Output true if the condition IS met (criterion should be evaluated)\n"
                "   - Output false if the condition is NOT met (criterion should be skipped)"
            )
            conditional_output = ',\n  "condition_verification": true/false'
        prompt = Template(P.MM_SCREENSHOT_EVIDENCE_ANALYSIS_PROMPT).substitute(
            task_definition=task,
            init_url_context=init_url_context,
            action_history=action_history,
            criterion_info=criterion_info,
            conditional_check=conditional_check,
            conditional_output=conditional_output,
        )
        messages = _image_message(images_b64[screenshot_idx], prompt)
        last_error = None
        for _ in range(max_iters):
            try:
                response_text = await meter.call(
                    f"4_evidence_s{screenshot_idx}_c{criterion_idx}",
                    client,
                    messages,
                    True,
                )
                analysis = json.loads(response_text)
                validate_evidence_analysis(analysis, is_conditional)
                analysis["screenshot_idx"] = screenshot_idx
                return [(criterion_idx, analysis)]
            except Exception as e:
                last_error = str(e)
                logger.error(
                    "Error analyzing criterion %d, screenshot %d: %s",
                    criterion_idx,
                    screenshot_idx,
                    e,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"Error: {e}. Please ensure your output includes all required fields in the correct format.",
                    }
                )
        logger.warning("Single-pair analysis failed; last error: %s", last_error)
        return [
            (
                criterion_idx,
                {
                    "screenshot_evidence": f"Error: Analysis failed after {max_iters} attempts",
                    "criterion_analysis": "Unable to analyze due to repeated errors",
                    "discrepancies": "N/A",
                    "environment_issues_confirmed": False,
                    "screenshot_idx": screenshot_idx,
                },
            )
        ]

    async def analyze_multi(screenshot_idx: int, criterion_indices: List[int]):
        criteria_info_block = ""
        conditional_criteria = set()
        for c_idx in criterion_indices:
            criterion = rubric["items"][c_idx]
            criteria_info_block += (
                f"\n**Criterion {c_idx}:** {criterion['criterion']}\n"
            )
            criteria_info_block += f"**Description:** {criterion['description']}\n"
            criteria_info_block += f"**Max Points:** {criterion['max_points']}\n"
            if "condition" in criterion:
                conditional_criteria.add(c_idx)
                criteria_info_block += f'**CONDITIONAL:** This criterion only applies if: "{criterion["condition"]}". You MUST include "condition_verification": true/false in the output for this criterion.\n'
        prompt = Template(P.MM_SCREENSHOT_BATCHED_EVIDENCE_ANALYSIS_PROMPT).substitute(
            task_definition=task,
            init_url_context=init_url_context,
            action_history=action_history,
            criteria_info_block=criteria_info_block,
        )
        messages = _image_message(images_b64[screenshot_idx], prompt)
        last_error = None
        for _ in range(max_iters):
            try:
                response_text = await meter.call(
                    f"4_evidence_s{screenshot_idx}", client, messages, True
                )
                analyses = normalize_batched_analysis_response(
                    json.loads(response_text), criterion_indices
                )
                if analyses is None or len(analyses) != len(criterion_indices):
                    raise ValueError(
                        f"Expected {len(criterion_indices)} entries, got {len(analyses) if analyses else 'None'}"
                    )
                for i, (analysis, expected_c_idx) in enumerate(
                    zip(analyses, criterion_indices)
                ):
                    if not isinstance(analysis, dict):
                        raise ValueError(f"Entry {i} is not a dict")
                    returned_idx = analysis.get("criterion_idx")
                    if returned_idx is None:
                        analysis["criterion_idx"] = expected_c_idx
                    elif returned_idx != expected_c_idx:
                        raise ValueError(
                            f"Entry {i}: expected criterion_idx={expected_c_idx}, got {returned_idx}"
                        )
                    validate_evidence_analysis(
                        analysis, expected_c_idx in conditional_criteria
                    )
                results = []
                for analysis, expected_c_idx in zip(analyses, criterion_indices):
                    analysis.pop("criterion_idx", None)
                    analysis["screenshot_idx"] = screenshot_idx
                    results.append((expected_c_idx, analysis))
                return results
            except Exception as e:
                last_error = str(e)
                logger.error("Error analyzing screenshot %d: %s", screenshot_idx, e)
                messages.append(
                    {
                        "role": "user",
                        "content": f'Error: {e}. Please output a JSON object like {{"analyses": [...]}} where the "analyses" list has exactly {len(criterion_indices)} entries, one per criterion. Each entry must have screenshot_evidence, criterion_analysis, discrepancies, environment_issues_confirmed, and criterion_idx. You ARE given a screenshot image — analyze the attached image.',
                    }
                )
        logger.warning(
            "Batched analysis failed for screenshot %d; falling back to per-pair calls. Last error: %s",
            screenshot_idx,
            last_error,
        )
        fallback = await asyncio.gather(
            *[analyze_single_pair(c, screenshot_idx) for c in criterion_indices]
        )
        return [item for sub in fallback for item in sub]

    tasks = []
    for s_idx, c_indices in screenshots_to_criteria.items():
        if len(c_indices) == 1:
            tasks.append(analyze_single_pair(c_indices[0], s_idx))
        else:
            tasks.append(analyze_multi(s_idx, c_indices))
    all_results = await asyncio.gather(*tasks)
    evidence_by_criterion: Dict[int, List[Dict]] = {i: [] for i in range(num_criteria)}
    for result_list in all_results:
        for c_idx, analysis in result_list:
            evidence_by_criterion[c_idx].append(analysis)
    return evidence_by_criterion


# ---------------------------------------------------------------------------
# Step 6: whole-rubric rescoring
# ---------------------------------------------------------------------------
async def _rescore_rubric_with_screenshots(
    meter,
    client,
    rubric,
    evidence_by_criterion,
    task,
    init_url_context,
    action_history,
    total_screenshots,
    max_iters,
) -> dict:
    num_criteria = len(rubric["items"])
    skipped = set()
    for c_idx, criterion in enumerate(rubric["items"]):
        if "condition" in criterion and not criterion.get("is_condition_met", False):
            criterion["applicable_evidence"] = (
                "N/A — condition not met, criterion skipped."
            )
            criterion["post_image_justification"] = (
                "Condition not met; criterion does not apply and was not rescored."
            )
            criterion["post_image_earned_points"] = 0.0
            skipped.add(c_idx)

    prompt = Template(P.MM_RUBRIC_RESCORING_PROMPT).substitute(
        task_definition=task,
        init_url_context=init_url_context,
        action_history=action_history,
        full_rubric_with_baselines=build_full_rubric_with_baselines(rubric),
        all_screenshot_evidence=build_all_screenshot_evidence_text(
            rubric, evidence_by_criterion, total_screenshots
        ),
        num_criteria=num_criteria,
        num_criteria_minus_1=num_criteria - 1,
    )
    messages = P.DEFAULT_SYSTEM_MESSAGES + [{"role": "user", "content": prompt}]
    last_error = None
    for _ in range(max_iters):
        try:
            response_text = await meter.call("6_rescoring", client, messages, True)
            result = json.loads(response_text)
            if "items" not in result:
                raise ValueError("Missing required field: 'items'")
            items = result["items"]
            if not isinstance(items, list):
                raise ValueError(f"'items' must be a list, got {type(items).__name__}")
            if len(items) != num_criteria:
                raise ValueError(f"Expected {num_criteria} items, got {len(items)}")
            for i, item in enumerate(items):
                if "criterion_idx" not in item:
                    raise ValueError(f"Item {i} missing 'criterion_idx'")
                if item["criterion_idx"] != i:
                    raise ValueError(
                        f"Item {i} has criterion_idx={item['criterion_idx']}, expected {i}"
                    )
                max_points = rubric["items"][i]["max_points"]
                missing_fields, type_errors = [], []
                for field in [
                    "applicable_evidence",
                    "post_image_justification",
                    "post_image_earned_points",
                ]:
                    if field not in item:
                        missing_fields.append(field)
                    elif field in ("post_image_justification", "applicable_evidence"):
                        if not isinstance(item[field], str):
                            type_errors.append(
                                f"Item {i}: {field} must be a string, got {type(item[field]).__name__}"
                            )
                        elif not item[field]:
                            type_errors.append(f"Item {i}: {field} cannot be empty")
                    elif field == "post_image_earned_points":
                        if not isinstance(item[field], (int, float)):
                            type_errors.append(
                                f"Item {i}: {field} must be a number, got {type(item[field]).__name__}"
                            )
                        elif not (0 <= item[field] <= max_points):
                            type_errors.append(
                                f"Item {i}: {field} must be between 0 and {max_points}, got {item[field]}"
                            )
                if missing_fields or type_errors:
                    error_msg = "Invalid rescoring output. "
                    if missing_fields:
                        error_msg += f"Missing fields: {', '.join(missing_fields)}. "
                    if type_errors:
                        error_msg += f"Errors: {'; '.join(type_errors)}."
                    raise ValueError(error_msg)
            for i, item in enumerate(items):
                if i in skipped:
                    continue
                rubric["items"][i]["applicable_evidence"] = item["applicable_evidence"]
                rubric["items"][i]["post_image_justification"] = item[
                    "post_image_justification"
                ]
                rubric["items"][i]["post_image_earned_points"] = float(
                    item["post_image_earned_points"]
                )
            return rubric
        except Exception as e:
            last_error = str(e)
            logger.error("Error rescoring rubric: %s", e)
            messages.append(
                {
                    "role": "user",
                    "content": f"Error: {e}. Please ensure your output includes all {num_criteria} criteria in the correct format.",
                }
            )
    logger.warning(
        "Failed to rescore rubric after %d attempts. Last error: %s",
        max_iters,
        last_error,
    )
    for i, criterion in enumerate(rubric["items"]):
        if i not in skipped:
            criterion["post_image_justification"] = (
                f"Rescoring failed after {max_iters} attempts, keeping baseline score. Last error: {last_error}"
            )
            criterion["post_image_earned_points"] = float(
                criterion.get("earned_points", 0)
            )
    return rubric


# ---------------------------------------------------------------------------
# Step 7: unsolicited side effects
# ---------------------------------------------------------------------------
async def _detect_unsolicited_side_effects(
    meter,
    client,
    rubric,
    evidence_by_criterion,
    task,
    init_url_context,
    action_history,
    max_iters,
) -> dict:
    all_evidence_text = ""
    for c_idx, analyses in evidence_by_criterion.items():
        criterion = rubric["items"][c_idx]
        all_evidence_text += f"\n\n## Criterion {c_idx}: {criterion['criterion']}\n"
        for analysis in analyses:
            all_evidence_text += (
                f"- **Evidence:** {analysis.get('screenshot_evidence', 'N/A')}\n"
            )
            all_evidence_text += (
                f"- **Analysis:** {analysis.get('criterion_analysis', 'N/A')}\n"
            )
            all_evidence_text += (
                f"- **Discrepancies:** {analysis.get('discrepancies', 'N/A')}\n"
            )
    prompt = Template(P.PENALIZE_UNSOLICITED_SIDE_EFFECTS_PROMPT).substitute(
        task_definition=task,
        init_url_context=init_url_context,
        action_history=action_history,
        scored_rubric_summary=build_scored_rubric_summary(rubric),
        all_concatenated_evidence=all_evidence_text,
    )
    messages = P.DEFAULT_SYSTEM_MESSAGES + [{"role": "user", "content": prompt}]
    last_error = None
    for _ in range(max_iters):
        try:
            response_text = await meter.call("7_side_effects", client, messages, True)
            result = json.loads(response_text)
            if "reasoning" not in result:
                raise ValueError("Missing required field: reasoning")
            if not isinstance(result["reasoning"], str) or not result["reasoning"]:
                raise ValueError(
                    f"reasoning must be a non-empty string, got {type(result['reasoning']).__name__}"
                )
            if "requires_penalty" not in result:
                raise ValueError("Missing required field: requires_penalty")
            if not isinstance(result["requires_penalty"], bool):
                raise ValueError(
                    f"requires_penalty must be a boolean, got {type(result['requires_penalty']).__name__}"
                )
            if "penalty_criteria" not in result:
                raise ValueError("Missing required field: penalty_criteria")
            if not isinstance(result["penalty_criteria"], list):
                raise ValueError(
                    f"penalty_criteria must be a list, got {type(result['penalty_criteria']).__name__}"
                )
            for i, penalty in enumerate(result["penalty_criteria"]):
                req = [
                    "criterion",
                    "description",
                    "max_points",
                    "post_image_justification",
                    "post_image_earned_points",
                ]
                missing_fields = [f for f in req if f not in penalty]
                if missing_fields:
                    raise ValueError(
                        f"Penalty criterion {i} missing fields: {', '.join(missing_fields)}"
                    )
                if (
                    not isinstance(penalty["criterion"], str)
                    or not penalty["criterion"]
                ):
                    raise ValueError(
                        f"Penalty criterion {i}: 'criterion' must be a non-empty string"
                    )
                if (
                    not isinstance(penalty["description"], str)
                    or not penalty["description"]
                ):
                    raise ValueError(
                        f"Penalty criterion {i}: 'description' must be a non-empty string"
                    )
                if (
                    not isinstance(penalty["max_points"], (int, float))
                    or penalty["max_points"] <= 0
                ):
                    raise ValueError(
                        f"Penalty criterion {i}: 'max_points' must be a positive number"
                    )
                if penalty["post_image_earned_points"] != 0:
                    raise ValueError(
                        f"Penalty criterion {i}: 'post_image_earned_points' must be 0 for penalties"
                    )
                penalty["earned_points"] = penalty["post_image_earned_points"]
                penalty["justification"] = penalty["post_image_justification"]
            if result.get("requires_penalty"):
                for p in result["penalty_criteria"]:
                    p["penalty"] = True
                return {
                    "reasoning": result["reasoning"],
                    "requires_penalty": True,
                    "penalty_criteria": result["penalty_criteria"],
                }
            return {
                "reasoning": result["reasoning"],
                "requires_penalty": False,
                "penalty_criteria": [],
            }
        except Exception as e:
            last_error = str(e)
            logger.error("Error detecting side effects: %s", e)
            messages.append(
                {
                    "role": "user",
                    "content": f"Error: {e}. Please ensure your output follows the exact format specified with all required fields.",
                }
            )
    return {
        "reasoning": f"Failed after {max_iters} attempts. Last error: {last_error}",
        "requires_penalty": False,
        "penalty_criteria": [],
    }


# ---------------------------------------------------------------------------
# Step 8: outcome verification
# ---------------------------------------------------------------------------
async def _outcome_verification(
    meter,
    client,
    rubric,
    evidence_by_criterion,
    task,
    init_url_context,
    action_history,
    total_screenshots,
    blocks,
    max_iters,
) -> dict:
    prompt = Template(P.OUTCOME_VERIFICATION_PROMPT).substitute(
        task_definition=task,
        init_url_context=init_url_context,
        rubric_summary=build_scored_rubric_summary(rubric),
        evidence_summary=build_all_screenshot_evidence_text(
            rubric, evidence_by_criterion, total_screenshots
        ),
        action_history=action_history,
        critical_point_context=blocks["critical_point_context"],
        user_simulator_policy=blocks["user_simulator_policy_outcome"],
        cp_decision_rules=blocks["cp_decision_rules"],
    )
    messages = P.DEFAULT_SYSTEM_MESSAGES + [{"role": "user", "content": prompt}]
    last_error = None
    for _ in range(max_iters):
        try:
            response_text = await meter.call("8_outcome", client, messages, True)
            result = json.loads(response_text)
            if "primary_intent" not in result:
                raise ValueError("Missing required field: primary_intent")
            if (
                not isinstance(result["primary_intent"], str)
                or not result["primary_intent"]
            ):
                raise ValueError("primary_intent must be a non-empty string")
            if "reasoning" not in result:
                raise ValueError("Missing required field: reasoning")
            if not isinstance(result["reasoning"], str) or not result["reasoning"]:
                raise ValueError("reasoning must be a non-empty string")
            if "output_success" not in result:
                raise ValueError("Missing required field: output_success")
            if not isinstance(result["output_success"], bool):
                raise ValueError(
                    f"output_success must be a boolean, got {type(result['output_success']).__name__}"
                )
            result.pop("cp_violation", None)
            return result
        except Exception as e:
            last_error = str(e)
            logger.error("Error in outcome verification: %s", e)
            messages.append(
                {
                    "role": "user",
                    "content": f"Error: {e}. Please ensure your output follows the exact JSON format specified with all required fields.",
                }
            )
    return {
        "primary_intent": f"Failed after {max_iters} attempts. Last error: {last_error}",
        "reasoning": f"Outcome verification failed after {max_iters} attempts. Last error: {last_error}",
        "output_success": None,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def judge(
    task: str,
    init_url: str,
    steps: List[Dict[str, Any]],
    screenshots: List[bytes],
    client: Any,
    *,
    rubric: Optional[dict] = None,
    action_only_client: Any = None,
    cp_profile: Optional[Dict[str, Any]] = None,
    max_iters: int = MAX_ITERS,
) -> Dict[str, Any]:
    """Run the UV pipeline on observed evidence, without agent self-reports.

    ``client`` plays UV's ``gpt5_client`` role; ``action_only_client`` plays
    the ``o4mini_client`` role (defaults to ``client``). ``rubric`` is an
    optional precomputed rubric (scored or unscored); an invalid one is
    regenerated, as in UV.
    """
    meter = _Meter()
    action_only_client = action_only_client or client
    init_url_context = get_init_url_context(init_url)
    blocks = _cp_blocks(cp_profile)
    action_history = format_action_history(steps)

    # Precomputed rubric handling (UV: verify_rubric -> treat as scored and
    # clear scores for re-evaluation; else verify_generated_rubric).
    rubric_dict = None
    if isinstance(rubric, list) and rubric:
        rubric = rubric[0]
    if rubric and isinstance(rubric, dict):
        rubric_dict = copy.deepcopy(rubric)
        try:
            verify_rubric(rubric_dict)
            rubric_dict = clear_rubric_scores(rubric_dict)
        except Exception:
            pass
        try:
            verify_generated_rubric(rubric_dict)
        except Exception:
            rubric_dict = None
    if rubric_dict is None:
        rubric_dict = await _generate_rubric(
            meter, client, task, init_url, cp_profile, max_iters
        )
    generated_rubric = copy.deepcopy(rubric_dict)

    # Step 0c
    rubric_dict = await _action_only_scoring(
        meter,
        action_only_client,
        rubric_dict,
        task,
        action_history,
        init_url_context,
        blocks,
        max_iters,
    )
    action_only_score = (
        rubric_dict["total_earned_points"] / rubric_dict["total_max_points"]
        if rubric_dict["total_max_points"] > 0
        else 0.0
    )

    # Step 4
    images_b64 = [encode_image_b64(b) for b in screenshots]
    evidence_by_criterion = await _analyze_screenshot_evidence_batched(
        meter,
        client,
        images_b64,
        rubric_dict,
        task,
        init_url_context,
        action_history,
        max_iters,
    )
    conditional_indices = [
        i for i, c in enumerate(rubric_dict["items"]) if "condition" in c
    ]
    if len(conditional_indices) == 1:
        c_idx = conditional_indices[0]
        verifications = [
            a["condition_verification"]
            for a in evidence_by_criterion.get(c_idx, [])
            if "condition_verification" in a
        ]
        if verifications:
            rubric_dict["items"][c_idx]["is_condition_met"] = any(verifications)

    # Steps 6-7 (single instance)
    rubric_copy = copy.deepcopy(rubric_dict)
    rubric_copy = await _rescore_rubric_with_screenshots(
        meter,
        client,
        rubric_copy,
        evidence_by_criterion,
        task,
        init_url_context,
        action_history,
        len(screenshots),
        max_iters,
    )
    side_effects = await _detect_unsolicited_side_effects(
        meter,
        client,
        rubric_copy,
        evidence_by_criterion,
        task,
        init_url_context,
        action_history,
        max_iters,
    )
    if side_effects.get("penalty_criteria"):
        rubric_copy["items"].extend(side_effects["penalty_criteria"])
    final = compute_final_scores(rubric_copy)
    rubric_copy["total_max_points"] = final["total_max_points"]
    rubric_copy["total_earned_points"] = final["total_earned_points"]
    rubric_score = (
        final["total_earned_points"] / final["total_max_points"]
        if final["total_max_points"] > 0
        else 0.0
    )

    # Step 8
    outcome = await _outcome_verification(
        meter,
        client,
        rubric_copy,
        evidence_by_criterion,
        task,
        init_url_context,
        action_history,
        len(screenshots),
        blocks,
        max_iters,
    )

    totals = meter.totals()
    return {
        "output_success": outcome.get("output_success"),
        "rubric_score": rubric_score,
        "rubric_is_success": rubric_score >= RUBRIC_THRESHOLD,
        "primary_intent": outcome.get("primary_intent", ""),
        "reasoning": outcome.get("reasoning", ""),
        "penalty_applied": bool(side_effects.get("requires_penalty"))
        and bool(side_effects.get("penalty_criteria")),
        "rubric": generated_rubric,
        "scored_rubric": rubric_copy,
        "action_only_score": action_only_score,
        "side_effects": side_effects,
        "evidence_by_criterion": {str(k): v for k, v in evidence_by_criterion.items()},
        "calls": totals["calls"],
        "tokens_in": totals["tokens_in"],
        "tokens_out": totals["tokens_out"],
        "usd": totals["usd"],
    }


# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------
# USD per 1M tokens: (input, cached input, output).
PRICES_PER_M = {
    "gpt-5": (1.25, 0.125, 10.0),
    "o4-mini": (1.10, 0.275, 4.40),
}


class _Result:
    def __init__(self, content: str, usage: Dict[str, Any]):
        self.content = content
        self.usage = usage


class OpenAIJudgeClient:
    """Chat Completions client with UV's ``create(messages, json_output)`` shape.

    Reads ``OPENAI_API_KEY`` from the environment only. Sends ``store=False``
    (zero data retention). No temperature / reasoning-effort override, as in
    UV's default judge config.
    """

    def __init__(
        self, model: str = "gpt-5", *, max_retries: int = 5, timeout: float = 600.0
    ):
        from openai import AsyncOpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment")
        self.model = model
        self._client = AsyncOpenAI(
            api_key=api_key, max_retries=max_retries, timeout=timeout
        )

    def _usd(self, tokens_in: int, cached: int, tokens_out: int) -> Optional[float]:
        price = PRICES_PER_M.get(self.model)
        if price is None:
            return None
        p_in, p_cached, p_out = price
        return (
            (tokens_in - cached) * p_in + cached * p_cached + tokens_out * p_out
        ) / 1e6

    async def create(
        self, messages: List[Dict[str, Any]], json_output: bool = False
    ) -> _Result:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "store": False,
        }
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self._client.chat.completions.create(**kwargs)
        u = resp.usage
        tokens_in = int(getattr(u, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(u, "completion_tokens", 0) or 0)
        details = getattr(u, "prompt_tokens_details", None)
        cached = (
            int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        )
        usage = {
            "model": self.model,
            "tokens_in": tokens_in,
            "cached_tokens": cached,
            "tokens_out": tokens_out,
            "usd": self._usd(tokens_in, cached, tokens_out),
        }
        return _Result(resp.choices[0].message.content or "", usage)

    async def close(self) -> None:
        await self._client.close()
