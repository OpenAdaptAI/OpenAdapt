"""Compile-time risk classification.

Heuristically infers whether a compiled :class:`~openadapt_flow.ir.Step`
performs a CONSEQUENTIAL, hard-to-undo write (``risk="irreversible"``) versus
a benign, repeatable action (``risk="reversible"``), from the step's intent and
its target's label / OCR text.

Why this exists
---------------
Historically every step compiled as ``reversible`` unless a human passed
``risk_overrides`` (see ``docs/LIMITS.md`` — "Risk classification is opt-in and
never auto-assigned"). That left the irreversible-step safeguards
(below-OCR-rung refusal, unreadable-identity-band refusal in
:class:`~openadapt_flow.runtime.Replayer`) UNREACHABLE from a default compile —
so a wrong-patient write behind an unreadable identity band proceeded with a
green report. This classifier turns those safeguards ON by default for
write-shaped steps. ``risk_overrides`` still wins (an operator can always force
a step either way).

Heuristic
---------
Pointer submissions, Enter/Delete, hotkeys, and drags can classify irreversible.
The step's ``intent`` and its anchor's ``ocr_text`` (the button label) are
scanned for a consequential-write verb — create / update / delete / submit /
save / confirm and their siblings — matched on WORD boundaries so ``address``
does not trip ``add`` and ``postal`` does not trip ``post``. Unlabelled pointer
controls are recorded as provisionally reversible so tutorials can replay, but
carry ``risk_review_required`` and cannot be certified until qualification
applies an explicit override. The same rule applies to an Enter/Delete key,
hotkey, or drag whose retained context does not prove a write: demo replay
remains usable, while Standard/Regulated certification requires an operator to
classify the business effect. Known submit/save/delete context remains
irreversible immediately.

False-positive posture
----------------------
DELIBERATELY biased toward ``irreversible`` on write-shaped steps. A false
irreversible costs AVAILABILITY (an over-strict refusal, or a ``certify``
failure a human must clear); a false reversible costs SAFETY (a wrong,
unguarded write reported as success). We take the cheap error. Concretely,
labels like "Apply filter" or "Add to favourites" classify irreversible even
though they are cheap to undo — the safe direction. Qualification can override
these classifications with a recorded explanation; compilation does not infer
that an ambiguous actuator is harmless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from openadapt_flow.ir import ActionKind, Step


@dataclass(frozen=True)
class RiskInference:
    """Compile-time classification plus its qualification disposition."""

    risk: Literal["reversible", "irreversible"]
    explanation: str
    requires_review: bool = False


# Consequential-write verb stems. Each is matched case-insensitively on WORD
# boundaries against the step's combined text, so `add` matches "+Add" and
# "Add note" but NOT "address", and `post` (were it present) would not match
# "postal". Ordered roughly by how common they are on real controls.
#
# Deliberately EXCLUDED to avoid noisy false positives on navigation chrome:
# bare "sign" (collides with "sign in"; the writing sense is covered by
# save/submit/confirm and the explicit "sign up" below), bare "post"/"order"/
# "book"/"complete" (collide with "Posts"/"Orders"/"Bookings"/"Completed" tabs).
_WRITE_STEMS: tuple[str, ...] = (
    r"sav(?:e|es|ing)",
    r"submit(?:s|ted|ting)?",
    r"confirm(?:s|ed|ing)?",
    r"continue(?:s|d|ing)?",
    r"next",
    r"cancel(?:s|ed|ing)?",
    r"creat(?:e|es|ing)",
    r"delet(?:e|es|ing)",
    r"remov(?:e|es|ing)",
    r"updat(?:e|es|ing)",
    r"send(?:s|ing)?",
    r"publish(?:es|ed|ing)?",
    r"pay(?:s|ing)?",
    r"sign[\s\-_]?up",
    r"signup",
    r"regist(?:er|ers|ering|ration)",
    r"enroll(?:s|ed|ing|ment)?",
    r"add(?:s|ed|ing)?",
    r"insert(?:s|ed|ing)?",
    r"appl(?:y|ies|ied)",
    r"approv(?:e|es|ed|ing)",
    r"accept(?:s|ed|ing)?",
    r"transfer(?:s|red|ring)?",
    r"upload(?:s|ed|ing)?",
    r"overwrit(?:e|es|ing)",
    r"discard(?:s|ed|ing)?",
    r"archiv(?:e|es|ing)",
    r"finaliz(?:e|es|ing)",
    r"finalise",
    r"checkout",
    r"check[\s\-_]?out",
    r"purchas(?:e|es|ing)",
    r"place[\s\-_]order",
)

# One alternation, word-boundary anchored. `\b` around a leading/trailing
# non-word char (e.g. "+add") still matches because the boundary sits between
# the "+" and "a".
_WRITE_RE = re.compile(r"\b(?:" + "|".join(_WRITE_STEMS) + r")\b", re.IGNORECASE)


def is_write_shaped(text: str) -> bool:
    """True if ``text`` names a consequential-write action (see module doc)."""
    return bool(text) and _WRITE_RE.search(text) is not None


def step_text(step: Step) -> str:
    """The text a click step's risk is inferred from: its intent plus its
    target's OCR label (the intent already embeds the label for labelled
    clicks, but an unlabelled coordinate click carries none, and a healed
    anchor may carry a fresher label than the frozen intent)."""
    parts = [step.intent or ""]
    if step.anchor is not None and step.anchor.ocr_text:
        parts.append(step.anchor.ocr_text)
    return " ".join(parts)


def infer_step_risk(step: Step) -> RiskInference:
    """Infer risk while retaining why qualification must review ambiguity."""

    if step.action is ActionKind.DRAG:
        if is_write_shaped(step_text(step)):
            return RiskInference(
                "irreversible",
                "drag context names a consequential state change",
            )
        return RiskInference(
            "reversible",
            "drag may select, move, reorder, or mutate application state",
            requires_review=True,
        )
    if step.action is ActionKind.HOTKEY:
        key = (step.key or "").lower()
        modifiers = {value.lower() for value in step.modifiers}
        write_chord = (
            (
                key in {"enter", "return"}
                and bool(modifiers & {"control", "ctrl", "meta", "command", "cmd"})
            )
            or (
                key == "s"
                and bool(modifiers & {"control", "ctrl", "meta", "command", "cmd"})
            )
            or (key in {"delete", "backspace"} and "shift" in modifiers)
        )
        if write_chord or is_write_shaped(step_text(step)):
            return RiskInference(
                "irreversible",
                "hotkey chord or retained context names a consequential write",
            )
        return RiskInference(
            "reversible",
            "hotkey may navigate, select, edit, submit, or mutate application state",
            requires_review=True,
        )
    if step.action is ActionKind.KEY:
        ambiguous = (step.key or "").lower() in {
            "enter",
            "return",
            "delete",
            "backspace",
        }
        consequential = ambiguous and is_write_shaped(step_text(step))
        return RiskInference(
            "irreversible" if consequential else "reversible",
            (
                "submission/deletion key has retained consequential-write context"
                if consequential
                else (
                    "submission/deletion key may edit locally or commit application state"
                    if ambiguous
                    else "key is not a known submission or deletion key"
                )
            ),
            requires_review=ambiguous and not consequential,
        )
    if step.action not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        return RiskInference("reversible", "action is not a pointer submission")
    if step.anchor is None or not (step.anchor.ocr_text or "").strip():
        # An unlabelled primary click may be a focus/navigation action or an
        # icon-only write. Demo replay remains usable, but no policy may certify
        # the unresolved classification until qualification supplies an
        # explicit override.
        return RiskInference(
            "reversible",
            "unlabelled pointer control may be navigation, focus, or a state change",
            requires_review=True,
        )
    text = step_text(step)
    if is_write_shaped(text):
        return RiskInference(
            "irreversible",
            "control label contains a consequential-write verb",
        )
    return RiskInference("reversible", "control label is not write-shaped")


def classify_step_risk(step: Step) -> Literal["reversible", "irreversible"]:
    """Return the inferred risk; use :func:`infer_step_risk` for rationale.

    Proven write-shaped drags/hotkeys/key presses and write-shaped labelled
    clicks classify irreversible. Ambiguous rich actions and icon-only primary
    clicks remain provisionally reversible but require explicit qualification
    review before certification.
    """
    return infer_step_risk(step).risk
