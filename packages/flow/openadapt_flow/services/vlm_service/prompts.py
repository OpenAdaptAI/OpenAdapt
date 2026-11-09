"""Prompts and answer parsers for the VLM service endpoints.

The identity same/different prompt and its VETO-ONLY parser are lifted verbatim
from the validated identity probe (PR #28,
``openadapt_flow/validation/vlm_identity_probe.py``): on the collapse surface
(Qwen3-VL-4B) that prompt/parse combination produced a 0% false-accept rate,
veto-only. Reusing it here keeps the server's judgement identical to the
experiment that was actually validated -- the service must not silently drift
from the probe.

Contract reminder: the comparator MAY ONLY VETO. Anything but a clean, confident
SAME is reported as ``different`` (the parser) and MUST be treated by the client
as a veto (halt). The server never authorizes an action; it only reports the
comparison.
"""

from __future__ import annotations

# --- Identity same/different (verbatim from the PR #28 probe) --------------

IDENTITY_PROMPT = (
    "The image shows TWO magnified identifier codes: code A on top and code B "
    "below it. These are patient record identifiers; a single different "
    "character means a different patient. Do A and B contain EXACTLY the same "
    "sequence of characters? Answer with ONE word only: SAME or DIFFERENT."
)


def parse_identity_veto(text: str) -> str:
    """Parse a VLM answer to ``same`` / ``different`` under VETO-ONLY rules.

    Verbatim port of ``vlm_identity_probe.parse_veto``. Only a clean, confident
    SAME grants a pass. Everything else -- an explicit DIFFERENT, a
    degenerate/looping answer, an empty or unparseable answer -- is treated as
    DIFFERENT (veto -> halt), because the comparator may only veto, never grant
    a pass a string-compare would not.
    """
    t = (text or "").strip().upper()
    if t.startswith("SAME") or t.startswith("YES"):
        return "same"
    return "different"  # DIFFERENT, NO, garbled, empty -> veto


# --- Grounding -------------------------------------------------------------

GROUND_PROMPT = (
    "You are grounding a UI automation target on a screenshot.\n"
    "Target intent: {intent}\n"
    "Target text label (may be stale): {ocr_text}\n\n"
    "Reply with ONLY a JSON object of pixel coordinates for the point to "
    'click, e.g. {{"x": 123, "y": 45}}. If the target is not visible, reply '
    'with ONLY {{"x": null, "y": null}}.'
)


# --- State verification (drift-oracle postcondition) -----------------------

VERIFY_STATE_PROMPT = (
    "You are verifying whether a UI automation step reached its intended "
    "state, judging MEANING not exact pixels (tolerate font, scale, theme, and "
    "layout drift).\n"
    "Expected state after the step: {expected_state}\n\n"
    "Does the screenshot show that this state HOLDS? Answer with ONE word "
    "only: YES, NO, or UNCERTAIN. Answer UNCERTAIN if you cannot tell."
)


def parse_state_answer(text: str) -> str:
    """Parse a state-verification answer to ``yes`` / ``no`` / ``uncertain``.

    Fail-safe parse: only a clean leading YES/NO is taken at face value; any
    hedge, empty, or unparseable answer collapses to ``uncertain`` so the
    caller degrades to the safe (halt) direction rather than assuming success.
    """
    t = (text or "").strip().upper()
    if t.startswith("YES"):
        return "yes"
    if t.startswith("NO"):
        return "no"
    return "uncertain"
