"""Run-time checks of clock-derived screen text and the first-local fence's risk view.

Kept out of ``replayer.py`` (size ratchet, docs/design/DECOMPOSITION.md); the
replayer calls these with its own clock, vision, backend and authorization.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Optional

from openadapt_flow import clock_text as _clock_text

#: The OCR confidence a clock text's line needs (as the exact text check).
MIN_CONFIDENCE = 0.8


def run_today(clock: Optional[Callable[[], datetime]]) -> date:
    """The run clock's local date (``clock``, else the system clock)."""
    return _clock_text.run_clock_dates(clock() if callable(clock) else None)[0]


def clock_texts(
    pc: Any, clock: Optional[Callable[[], datetime]]
) -> Optional[list[str]]:
    """The texts a clock-derived postcondition expects now, else None.

    None for an ordinary postcondition (its ``text`` is the literal to check).
    For ``text_clock``, every rendering of the template on the run clock's
    dates (:func:`clock_text.run_clock_dates`: the local date and, around
    midnight, the UTC date), each one specific date.
    """
    template = getattr(pc, "text_clock", None)
    if not template:
        return None
    now = clock() if callable(clock) else None
    return _clock_text.renderings(template, _clock_text.run_clock_dates(now))


def clock_text_shown(vision: Any, text: str, frame_png: bytes) -> bool:
    """A clock-rendered text is a whole OCR line, field for field.

    The fuzzy OCR match would take "October 2025" for "October 2026", and a
    letters-and-digits key or a substring reads "11/3/2026" in "1/13/2026" or
    "Nov 03" in "Nov 03, 1990". So a clock text passes only when one
    confidently read OCR line has exactly its field tokens
    (:func:`clock_text.field_tokens`), on the frame or, when the frame reads
    none, at 2x. The DOM's substring proof is never used. A vision without
    OCR fails closed.
    """
    reader = getattr(vision, "ocr", None)
    if not callable(reader):
        return False
    want = _clock_text.field_tokens(text)
    if not want:
        return False
    upscale = getattr(vision, "upscale_png", None)
    frames = [frame_png]
    if callable(upscale):
        frames.append(upscale(frame_png, 2))
    for png in frames:
        for line in reader(png):
            if (
                getattr(line, "confidence", 0.0) >= MIN_CONFIDENCE
                and _clock_text.field_tokens(line.text) == want
            ):
                return True
    return False


def first_local_consequential(authorization: Any, step: Any, workflow: Any) -> bool:
    """Whether the first-local delivery fence treats ``step`` as a write.

    The same step set the outcome classifier uses for the run
    (``execution_profiles.classify_execution_outcome``). In a qualification
    case bound to the bundle's own qualification project (its id, revision
    and contract digest, checked by the case authorization), the project's
    operator-confirmed risk decisions count before a certification exists,
    because the campaign is what certifies them. Every other run needs a
    current certification for an operator down-classification, checked
    against the policy the run was admitted under (the bundle's own policy
    copy is never that authority). Without this, neither a campaign nor a
    certified run could deliver a write-shaped but reversible step the
    operator classified (a button that only opens a confirmation dialog): the
    fence refused it for having no effect contract.
    """
    from openadapt_flow.run_gate import is_consequential

    review = bool(
        authorization is not None
        and authorization.qualification_case_id is not None
        and authorization._qualification_binding_error(workflow) is None
    )
    return is_consequential(
        step,
        workflow,
        require_current_risk_certification=not review,
        certifying_policy_sha256=(
            authorization.admitted_policy_contract_sha256
            if authorization is not None
            else None
        ),
    )


def clock_text_check(
    replayer: Any, pc: Any, kind: str, frame_png: bytes
) -> Optional[bool]:
    """A clock-derived text postcondition's verdict, or None for any other.

    ``text_present``: some rendering is a whole OCR line, field for field
    (:func:`clock_text_shown`).
    ``text_absent``: no rendering is present, by the fuzzy reader (a near
    reading counts as present, the safe direction).
    """
    if kind not in ("text_present", "text_absent") or not getattr(pc, "text", None):
        return None
    texts = clock_texts(pc, replayer.clock)
    if texts is None:
        return None
    if kind == "text_absent":
        return not any(replayer.vision.text_present(frame_png, t) for t in texts)
    return any(clock_text_shown(replayer.vision, t, frame_png) for t in texts)


def describe(pc: Any, clock: Optional[Callable[[], datetime]]) -> Optional[str]:
    """How a failed clock text check reads in a report, or None."""
    texts = clock_texts(pc, clock)
    if not texts:
        return None
    return f"{texts[0]!r} (from the run's clock; recorded as {pc.text!r})"
