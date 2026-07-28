"""Arm-independent success criteria for the benchmarks.

Both arms — compiled replay and the computer-use agent — are judged by the
exact same check, applied to a screenshot of the final state. Neither arm's
internal notion of success (the replayer's ``RunReport.success``, the
agent's own claim of completion) is used.

Two checks live here, one per benchmark target:

- :func:`verify_encounter_saved` (MockMed): OCR must find (a) the
  ``Encounter saved — <note>`` banner and (b) the saved encounter row
  (``<type> — <note>``), whose categorical type field and free-text note
  are checked separately. MockMed renders both only on the patient screen
  after a successful save, so the check passes only in the
  navigated-back-to-patient state a user would accept as "done".
- :func:`verify_note_saved` (OpenEMR): OCR of the final screen must show
  the run's parameterized note text in the patient-message list.
"""

from __future__ import annotations

import difflib

from pydantic import BaseModel

from openadapt_flow.vision import OcrLine, find_text, normalize_text, ocr, upscale_png

BANNER_PREFIX = "Encounter saved"
#: MockMed truncation limits (static/app.js): banner shows note[:40], the
#: encounters list row shows note[:60].
BANNER_NOTE_CHARS = 40
ROW_NOTE_CHARS = 60
#: Every encounter type MockMed can store (the segmented control in
#: ``mockmed/static/app.js``). The type field is CATEGORICAL, so it is
#: decided by which member of this enumeration the row names — never by a
#: similarity score against one expected string.
ENCOUNTER_TYPES = ("Triage", "Consult")
#: Dash-like characters rapidocr emits for the row's em-dash separator
#: (``<type> — <note>``). Includes the CJK forms it substitutes when the
#: note is short and the glyph is read in isolation.
_ROW_SEPARATORS = "-‐‑‒–—―−一─－"
#: Leading list decoration rapidocr reads from the ``<li>`` bullet.
_ROW_DECORATION = " \t.·•‣●▪*"


class VerifyResult(BaseModel):
    """Outcome of the shared success check.

    Attributes:
        success: True iff the banner and a correctly-typed encounter row
            were found and no wrong-type row carries this run's note.
        banner_found: The ``Encounter saved — <note>`` banner was located.
        note_found: An encounter row was located whose type field is the
            requested type AND whose note field is the requested note.
        wrong_type_row: Some encounter row carries this run's note under a
            DIFFERENT known encounter type — a silent wrong-target write.
    """

    success: bool
    banner_found: bool
    note_found: bool
    wrong_type_row: bool = False


def _split_row_line(line: str, known_types: tuple[str, ...]) -> list[tuple[str, str]]:
    """Candidate ``(type_field, note_field)`` splits of one OCR line.

    The encounters list renders ``<li><type> — <note[:60]></li>``, so the
    two fields are separated by an em dash. OCR is not reliable about that
    glyph (it drops it, or reads it as a hyphen or a CJK character), and
    the note itself contains hyphens, so the split is attempted two ways
    and every candidate is scored:

    1. at the first dash-like character, and
    2. positionally, after exactly ``len(t)`` characters, for each known
       type ``t`` — which still parses a row whose separator OCR dropped.

    A candidate that does not name a known type simply scores badly and is
    discarded by the caller; producing extra candidates cannot weaken the
    check because both fields must pass on the SAME candidate.
    """
    text = normalize_text(line).lstrip(_ROW_DECORATION)
    candidates: list[tuple[str, str]] = []
    for index, char in enumerate(text):
        if char in _ROW_SEPARATORS:
            candidates.append((text[:index], text[index + 1 :]))
            break
    for known in known_types:
        head, tail = text[: len(known)], text[len(known) :]
        candidates.append((head, tail.lstrip(_ROW_DECORATION + _ROW_SEPARATORS)))
    return [
        (head.strip(_ROW_DECORATION + _ROW_SEPARATORS), tail.strip(_ROW_DECORATION))
        for head, tail in candidates
    ]


def _row_type(
    line: str,
    note_text: str,
    known_types: tuple[str, ...],
    min_ratio: float,
) -> str | None:
    """Return the encounter type of the row this OCR line renders, if any.

    The line is parsed into its two fields and each is judged on its own
    terms: the categorical type field must name exactly one member of
    ``known_types`` (highest similarity, strictly ahead of every other
    member, above ``min_ratio``), and the free-text note field must match
    the requested note at ``min_ratio``. A line that is not a row, or
    whose note is absent, yields ``None``.
    """
    expected_note = normalize_text(note_text[:ROW_NOTE_CHARS])
    normalized_line = normalize_text(line).lstrip(_ROW_DECORATION)
    bare_note_ratio = difflib.SequenceMatcher(
        None,
        normalized_line,
        expected_note,
    ).ratio()
    for type_field, note_field in _split_row_line(line, known_types):
        scores = {
            known: difflib.SequenceMatcher(
                None, type_field, normalize_text(known)
            ).ratio()
            for known in known_types
        }
        best = max(scores, key=lambda known: scores[known])
        runner_up = max((v for k, v in scores.items() if k != best), default=0.0)
        if scores[best] < min_ratio or scores[best] <= runner_up:
            continue
        note_ratio = difflib.SequenceMatcher(None, note_field, expected_note).ratio()
        # A banner can segment its bare note into a separate OCR line. When a
        # note itself begins with a known type, positional splitting can make
        # that line look like ``<type><note>`` even though no row exists. The
        # row hypothesis must therefore explain the line better than the bare
        # note hypothesis. A real row includes an additional type prefix, so
        # its parsed note field wins; an isolated note line does not.
        if note_ratio >= min_ratio and note_ratio > bare_note_ratio:
            return best
    return None


def verify_encounter_saved(
    screen_png: bytes,
    note_text: str,
    *,
    encounter_type: str = "Triage",
    known_types: tuple[str, ...] = ENCOUNTER_TYPES,
    min_ratio: float = 0.8,
) -> VerifyResult:
    """Check a final-state screenshot for the encounter-saved evidence.

    Two independent pieces of evidence are required: the save banner, and
    an encounter row whose fields are the requested ones.

    **The banner** is a save-happened signal with no parameters of its
    own. ``find_text`` fuzzy-matches whole OCR lines and the engine may
    segment the banner as one line (``Encounter saved — <note>``) or as
    two (the prefix and the note separately), so either form is accepted.

    **The row** is ``<type> — <note>``, one categorical field and one
    free-text field. It is deliberately NOT matched as one fuzzy candidate
    string. A single similarity ratio over the concatenation is decided by
    whichever field contributes more characters, so a long correct note
    outvotes a short wrong type: measured on a real MockMed frame, a
    ``Consult`` row carrying the requested 32-character note scored 0.8471
    against the ``Triage`` row form, above this function's 0.8 threshold.
    No threshold repairs that — on the same frame the note text alone
    scored 0.918 while the banner evidence the check *needs* scored only
    0.875, so any threshold that rejects the wrong-type row also rejects
    every genuine success.

    Each field is therefore judged on its own terms. The type field is
    categorical: it must name exactly one member of ``known_types``, and
    that member must be ``encounter_type``. The note field is free text
    and keeps the unchanged ``min_ratio`` fuzzy tolerance. A row that
    carries the requested note under a different known type sets
    ``wrong_type_row`` and fails the run — a wrong-target write is not a
    success, and reporting it is required by the reliability standard.

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The note the run was asked to enter.
        encounter_type: Encounter type the run was asked to create.
        known_types: Every encounter type the application can store. The
            categorical check is a choice among these, so an incomplete
            enumeration would let an unlisted type go unrecognized.
        min_ratio: Fuzzy-match threshold for the banner, the note field,
            and the type field.

    Returns:
        A :class:`VerifyResult`; ``success`` requires the banner, a
        correctly-typed row, and no wrong-type row.
    """
    if not normalize_text(note_text):
        return VerifyResult(
            success=False,
            banner_found=False,
            note_found=False,
            wrong_type_row=False,
        )
    if encounter_type not in known_types:
        known_types = (*known_types, encounter_type)

    banner_found = any(
        find_text(screen_png, candidate, min_ratio=min_ratio) is not None
        for candidate in (
            f"{BANNER_PREFIX} — {note_text[:BANNER_NOTE_CHARS]}",
            f"{BANNER_PREFIX} —",
        )
    )
    row_types = {
        found
        for found in (
            _row_type(line.text, note_text, known_types, min_ratio)
            for line in ocr(screen_png)
        )
        if found is not None
    }
    note_found = encounter_type in row_types
    wrong_type_row = bool(row_types - {encounter_type})
    return VerifyResult(
        success=banner_found and note_found and not wrong_type_row,
        banner_found=banner_found,
        note_found=note_found,
        wrong_type_row=wrong_type_row,
    )


class NoteVerifyResult(BaseModel):
    """Outcome of the OpenEMR saved-note check.

    Attributes:
        success: True iff the note evidence was found in a saved message row.
        matched_ratio: Fraction of the note's squashed characters that OCR
            matched in the best eligible saved-row OCR line (diagnostic only).
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


def _saved_message_note_lines(lines: list[OcrLine]) -> list[OcrLine]:
    """Return OCR lines that occupy a saved Patient Messages table row.

    OpenEMR renders the saved note in the ``Content`` column and renders a
    ``New`` status in the adjacent ``Status`` column. OCR can return that
    status separately or merge it onto the content line. A wrapped continuation
    can sit one text line below the status. The entry form is above the table,
    so its textarea does not satisfy this geometric row contract.

    The contract uses only pixels and OCR geometry. It does not read the DOM or
    trust the runner's internal result.
    """
    content_headers = [line for line in lines if _squash(line.text) == "content"]
    status_headers = [line for line in lines if _squash(line.text) == "status"]
    candidates: list[OcrLine] = []
    for content in content_headers:
        cx, cy, cw, ch = content.region
        content_center_x = cx + cw / 2
        content_center_y = cy + ch / 2
        for status in status_headers:
            sx, sy, sw, sh = status.region
            status_center_x = sx + sw / 2
            status_center_y = sy + sh / 2
            if status_center_x <= content_center_x:
                continue
            if abs(status_center_y - content_center_y) > 2 * max(ch, sh):
                continue

            header_bottom = max(cy + ch, sy + sh)
            row_status_bands = [
                (ly + lh / 2, lh)
                for line in lines
                for lx, ly, lw, lh in (line.region,)
                if ly > header_bottom
                and (
                    (_squash(line.text) == "new" and lx + lw / 2 >= sx - sw)
                    or (_squash(line.text).endswith("new") and lx + lw >= sx)
                )
            ]
            for line in lines:
                lx, ly, lw, lh = line.region
                line_center_x = lx + lw / 2
                line_center_y = ly + lh / 2
                if ly <= header_bottom:
                    continue
                if not (content_center_x - cw <= line_center_x < status_center_x):
                    continue
                if any(
                    abs(line_center_y - row_status_center_y) <= 2 * max(lh, status_h)
                    for row_status_center_y, status_h in row_status_bands
                ):
                    candidates.append(line)
    return candidates


def verify_note_saved(
    screen_png: bytes,
    note_text: str,
    *,
    min_run: int = 16,
) -> NoteVerifyResult:
    """Check a final-state screenshot for the saved OpenEMR note.

    The message list embeds the note in the ``Content`` column of a saved
    table row and wraps it. A valid evidence line must be below the table's
    ``Content``/``Status`` headers and aligned with that row's ``New`` status.
    Thus, the same note in the unsaved entry form does not pass. RapidOCR drops
    some dense table lines entirely at 1280x800, so when the raw frame does not
    pass, the frame is retried at 2x resolution.

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
        for line in _saved_message_note_lines(ocr(png)):
            result = _score_note(_squash(line.text), needle)
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
