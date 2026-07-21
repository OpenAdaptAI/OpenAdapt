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
Only actuating CLICK / DOUBLE_CLICK steps can classify irreversible (they are
the actuators that commit a write; typing into a field and scrolling are
reversible, and a bare key press is not a reliable write signal). The step's
``intent`` and its anchor's ``ocr_text`` (the button label) are scanned for a
consequential-write verb — create / update / delete / submit / save / confirm
and their siblings — matched on WORD boundaries so ``address`` does not trip
``add`` and ``postal`` does not trip ``post``.

False-positive posture
----------------------
DELIBERATELY biased toward ``irreversible`` on write-shaped steps. A false
irreversible costs AVAILABILITY (an over-strict refusal, or a ``certify``
failure a human must clear); a false reversible costs SAFETY (a wrong,
unguarded write reported as success). We take the cheap error. Concretely,
labels like "Apply filter" or "Add to favourites" classify irreversible even
though they are cheap to undo — the safe direction. Known under-classifications
we accept (leaning benign): bare "Cancel" (usually a dialog abort, occasionally
"cancel subscription"), "Next"/"Continue" wizard steps (the final one may
submit), and a submitting Enter key press. Mark those with ``risk_overrides``
when they matter.
"""

from __future__ import annotations

import re
from typing import Literal

from openadapt_flow.ir import ActionKind, Step

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


def classify_step_risk(step: Step) -> Literal["reversible", "irreversible"]:
    """Infer ``"irreversible"`` or ``"reversible"`` for a step.

    Only CLICK / DOUBLE_CLICK steps can classify irreversible (they are the
    actuators that commit a write); every other action kind is reversible. A
    write-shaped label/intent yields ``"irreversible"``; anything else —
    including an unlabelled coordinate click, which carries no signal — stays
    ``"reversible"``.
    """
    if step.action not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        return "reversible"
    return "irreversible" if is_write_shaped(step_text(step)) else "reversible"
