"""Arm-independent success criteria for the benchmarks.

Both arms — compiled replay and the computer-use agent — are judged by the
exact same check, applied to a screenshot of the final state. Neither arm's
internal notion of success (the replayer's ``RunReport.success``, the
agent's own claim of completion) is used.

Two checks live here, one per benchmark target:

- :func:`verify_encounter_saved` (MockMed): OCR must find (a) the
  ``Encounter saved — <note>`` banner and (b) the saved encounter row
  (``<type> — <note>``). MockMed renders both only on the patient screen
  after a successful save, so the check passes only in the
  navigated-back-to-patient state a user would accept as "done".
- :func:`verify_note_saved` (OpenEMR): OCR of the final screen must show
  the run's parameterized note text in the patient-message list.
"""

from __future__ import annotations

import difflib

from pydantic import BaseModel

from openadapt_flow.vision import find_text, ocr, upscale_png

BANNER_PREFIX = "Encounter saved"
#: MockMed truncation limits (static/app.js): banner shows note[:40], the
#: encounters list row shows note[:60].
BANNER_NOTE_CHARS = 40
ROW_NOTE_CHARS = 60


class VerifyResult(BaseModel):
    """Outcome of the shared success check.

    Attributes:
        success: True iff both the banner and the encounter row were found.
        banner_found: The ``Encounter saved — <note>`` banner was located.
        note_found: The ``<type> — <note>`` encounter row was located.
    """

    success: bool
    banner_found: bool
    note_found: bool


def verify_encounter_saved(
    screen_png: bytes,
    note_text: str,
    *,
    encounter_type: str = "Triage",
    min_ratio: float = 0.8,
) -> VerifyResult:
    """Check a final-state screenshot for the encounter-saved evidence.

    ``find_text`` fuzzy-matches whole OCR lines, and the OCR engine may
    segment the banner as one line (``Encounter saved — <note>``) or as two
    (the prefix and the note separately). Each check therefore accepts any
    of a small set of candidate line forms; all candidates describe the same
    on-screen evidence, so this tolerates OCR line segmentation without
    weakening the criterion: the banner prefix exists only after a save,
    and both checks must pass.

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The note the run was asked to enter.
        encounter_type: Encounter type the run was asked to create.
        min_ratio: Fuzzy-match threshold forwarded to ``find_text``.

    Returns:
        A :class:`VerifyResult`; ``success`` requires both checks to pass.
    """

    def any_found(candidates: tuple[str, ...]) -> bool:
        return any(
            find_text(screen_png, c, min_ratio=min_ratio) is not None
            for c in candidates
        )

    banner_found = any_found(
        (
            f"{BANNER_PREFIX} — {note_text[:BANNER_NOTE_CHARS]}",
            f"{BANNER_PREFIX} —",
        )
    )
    note_found = any_found(
        (
            f"{encounter_type} — {note_text[:ROW_NOTE_CHARS]}",
            note_text[:BANNER_NOTE_CHARS],
        )
    )
    return VerifyResult(
        success=banner_found and note_found,
        banner_found=banner_found,
        note_found=note_found,
    )


class NoteVerifyResult(BaseModel):
    """Outcome of the OpenEMR saved-note check.

    Attributes:
        success: True iff the note evidence was found on the final screen.
        matched_ratio: Fraction of the note's squashed characters that OCR
            matched somewhere in the frame's squashed text (diagnostic
            only — non-contiguous matches accumulate from unrelated text
            on a dense screen, so this never decides success).
        longest_run: Longest contiguous matched character run (this is
            the criterion).
    """

    success: bool
    matched_ratio: float
    longest_run: int


def _squash(text: str) -> str:
    """Lowercase and remove all whitespace (OCR-tolerant comparison form)."""
    return "".join(text.lower().split())


def _score_note(hay: str, needle: str) -> NoteVerifyResult:
    """Score squashed OCR text against a squashed note."""
    if needle in hay:
        return NoteVerifyResult(
            success=True, matched_ratio=1.0, longest_run=len(needle)
        )
    # autojunk=False: the default heuristic marks every frequent character
    # of a long OCR haystack as junk, silently collapsing real matches.
    blocks = difflib.SequenceMatcher(
        None, needle, hay, autojunk=False
    ).get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = max((block.size for block in blocks), default=0)
    return NoteVerifyResult(
        success=False,
        matched_ratio=round(matched / len(needle), 4),
        longest_run=longest,
    )


def verify_note_saved(
    screen_png: bytes,
    note_text: str,
    *,
    min_run: int = 16,
) -> NoteVerifyResult:
    """Check a final-state screenshot for the saved OpenEMR note.

    The message list embeds the note inside a longer line (``<timestamp>
    (admin to admin) <note>``) and wraps it, so whole-line fuzzy matching
    misses; and rapidocr drops some dense table lines entirely at
    1280x800, so when the raw frame does not pass, the frame is retried
    at 2x resolution, which recovers most dropped lines.

    The criterion is a **contiguous** matched run of at least ``min_run``
    squashed characters between the note and the frame's OCR text. A
    non-contiguous matched-character fraction is deliberately NOT a
    criterion: on a dense screen full of similar English text, scattered
    subsequence matches accumulate past any sane threshold for notes that
    are not on screen at all (measured 0.9+ for absent notes), while
    contiguous runs separate cleanly (>=29 for present notes vs <=8 for
    absent ones on audited frames). Callers must use note texts whose
    pairwise longest common squashed substring stays below ``min_run`` —
    several runs' notes are visible on the same final screen.

    This is the shared success criterion for BOTH arms of the OpenEMR
    benchmark — the compiled replay and the computer-use agent are judged
    by this exact function on their final screenshots.

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The parameterized note the run was asked to enter.
        min_run: Minimum contiguous matched run length to accept.

    Returns:
        A :class:`NoteVerifyResult`.
    """
    needle = _squash(note_text)
    if not needle:
        return NoteVerifyResult(success=False, matched_ratio=0.0, longest_run=0)

    best = NoteVerifyResult(success=False, matched_ratio=0.0, longest_run=0)
    for png in (screen_png, upscale_png(screen_png)):
        hay = _squash(" ".join(line.text for line in ocr(png)))
        result = _score_note(hay, needle)
        if result.success or result.longest_run >= min_run:
            return NoteVerifyResult(
                success=True,
                matched_ratio=result.matched_ratio,
                longest_run=result.longest_run,
            )
        if (result.longest_run, result.matched_ratio) > (
            best.longest_run,
            best.matched_ratio,
        ):
            best = result
    return best
