"""The empty search result a person confirmed through, mined as a check.

Many systems put a search between a form and its save: a duplicate search, a
conflict check. The dialog it opens reports the result ("No matches were
found.") above a Confirm button. A person who clicks Confirm on that dialog has
read the result; had it listed a match, they would have stopped. The empty
result is the precondition of the save.

Generic text mining keeps one line per step and may pick another line of the
dialog, so the precondition was lost: a run on a possible duplicate passed the
dialog and saved. This module mines it back from the recorded frames.

It mines only on a confirmation pair (``(opener, confirm)`` from
:func:`openadapt_flow.risk.confirmation_opener_step_ids`), and only a statement
that a search or match found nothing:

- the statement reads as one, by its words alone ("no matches were found",
  "no matching records found", "no possible duplicates", "0 results",
  "nothing found"), as a whole line, possibly wrapped over two or three OCR
  lines. Every word comes from a fixed vocabulary, so a line that adds
  anything else (a name, a search term) is not one, and a value the run
  changes is never frozen. A bare empty list ("No documents", "No records")
  is not a search result and is not mined: an informational section of a
  confirmation dialog is not what the person confirmed through;
- it is on the screen the person clicked Confirm on (the confirm step's before
  frame) and was not on the screen before the opener click. It may appear only
  after the opener's own after frame (the search loads after the dialog);
- it is the only such statement there, shown once. Two statements, or one
  shown twice, mean two result sections; a check on the screen cannot tell
  which one is empty at run time, so nothing is mined (fail closed) and the
  compile records why;
- every line it is read from is confidently read, and the caller's
  ``reject`` (parameter values, PHI) does not refuse it.

The statement becomes two exact checks (read exactly once, never by the fuzzy
OCR match): a ``TEXT_PRESENT`` postcondition of the opener, and a ``TEXT_PRESENT``
guard on the confirm step, checked on the screen right before the Confirm
click (an early empty state that a slower result replaces is caught there).

The vocabulary is English. A statement in any other words is found by
:func:`dialog_statement` instead, which knows no words at all: it takes the
one line the dialog added (not its title, not its button row), when that line
is confidently read, shown once, and carries no run value, PHI, number other
than 0, or email address. One demonstration cannot tell such a line from
record data, so a compile keeps it only as a candidate in its
``no_empty_result_guard`` note. Induction mines it when two or more
demonstrations whose search inputs differ all show the same line
(:mod:`openadapt_flow.compiler.induction`): record data changes with the
search, the dialog's own words do not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from openadapt_flow.ir import (
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
)
from openadapt_flow.vision.ocr import (
    OcrLine,
    normalize_text,
    text_key,
    wrapped_readings,
)

#: A line must be read at least this confidently to be part of a statement.
MIN_CONFIDENCE = 0.8
#: The dialog's result can arrive after the dialog itself (it is a search),
#: so the check waits longer than a step's default 5 s.
GUARD_TIMEOUT_S = 10.0

#: Nouns that are themselves a search or match result.
_RESULT_NOUN = r"(?:match(?:es)?|duplicates?|conflicts?|candidates?|results?|hits?)"
#: Words that make any other listed noun a match result ("matching records").
_MATCH_MODIFIER = r"(?:matching|possible|potential|similar|duplicate)"
_OTHER_NOUN = (
    r"(?:records?|entries|entry|items?|patients?|persons?|people|customers?"
    r"|accounts?|users?|contacts?|members?)"
)
_NOUN_PHRASE = (
    rf"(?:(?:{_MATCH_MODIFIER} ){{0,2}}{_RESULT_NOUN}"
    rf"|(?:{_MATCH_MODIFIER} ){{1,2}}{_OTHER_NOUN})"
)
_BE = r"(?: (?:were|was|are|is|have been|has been|could be))?"
_FOUND = r"(?: (?:found|returned|located|matched))?"
_SCOPE = r"(?: (?:in|for) (?:this|the|your) search)?"
_STATEMENT = re.compile(
    r"^(?:"
    rf"(?:there (?:are|were|is|was) )?no {_NOUN_PHRASE}{_BE}{_FOUND}{_SCOPE}"
    rf"|(?:nothing|none) (?:was |were )?found{_SCOPE}"
    rf"|(?:0|zero) {_NOUN_PHRASE}{_BE}{_FOUND}{_SCOPE}"
    rf"|(?:we )?found no {_NOUN_PHRASE}{_SCOPE}"
    r")$"
)


def is_empty_result_statement(text: str) -> bool:
    """Whether ``text`` is, as a whole, a statement that a search found nothing."""

    norm = normalize_text(text).rstrip(" .!")
    return bool(norm) and _STATEMENT.fullmatch(norm) is not None


def empty_result_statements(lines: Sequence[OcrLine]) -> list[str]:
    """Every empty-result statement in one frame's OCR, wrapped lines joined.

    A statement starts at the beginning of a line; the longest qualifying
    reading from that line wins. One entry per occurrence.
    """

    found: list[str] = []
    for readings in wrapped_readings(lines, min_confidence=MIN_CONFIDENCE):
        best: Optional[str] = None
        for reading in readings:
            if is_empty_result_statement(reading):
                best = reading
        if best is not None:
            found.append(best)
    return found


@dataclass
class MinedGuard:
    """What mining found for one confirmation pair."""

    #: The opener's postcondition, or None when nothing was mined.
    postcondition: Optional[Postcondition] = None
    #: The confirm step's guard (the same statement, checked before Confirm).
    guard: Optional[Guard] = None
    #: Why nothing was mined, when the dialog had something to say.
    note: Optional[str] = None
    statements: list[str] = field(default_factory=list)


def mine_empty_result_guard(
    opener_before: Sequence[OcrLine],
    opener_after: Sequence[OcrLine],
    confirm_before: Sequence[OcrLine],
    *,
    reject: Callable[[str], bool] = lambda _text: False,
) -> MinedGuard:
    """The exact checks for the empty result the person confirmed through.

    Args:
        opener_before: OCR of the frame before the click that opened the dialog.
        opener_after: OCR of the frame after it.
        confirm_before: OCR of the frame the person clicked Confirm on.
        reject: Refuses a statement the program must not assert (one that
            carries a parameter value or PHI).
    """

    seen_before = {text_key(text) for text in empty_result_statements(opener_before)}
    shown = [
        text
        for text in empty_result_statements(confirm_before)
        if text_key(text) not in seen_before
    ]
    if not shown:
        return MinedGuard(note="the dialog showed no empty search result")
    keys = [text_key(text) for text in shown]
    after_keys = [
        text_key(text)
        for text in empty_result_statements(opener_after)
        if text_key(text) not in seen_before
    ]
    if len(keys) > 1 or len(after_keys) > 1:
        return MinedGuard(
            note=(
                f"the dialog showed {max(len(keys), len(after_keys))} empty "
                "search results (more than one result section); a screen check "
                "cannot tell which one is empty at run time, so none is checked"
            ),
            statements=shown,
        )
    text = shown[0]
    if reject(text):
        return MinedGuard(
            note="the dialog's empty search result carries a run value or PHI",
            statements=shown,
        )
    postcondition, guard = exact_checks(text)
    return MinedGuard(postcondition=postcondition, guard=guard, statements=shown)


def exact_checks(text: str) -> tuple[Postcondition, Guard]:
    """The opener's postcondition and the confirm step's guard for ``text``."""

    return (
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text=text,
            timeout_s=GUARD_TIMEOUT_S,
            exact=True,
        ),
        Guard(
            predicate=Predicate(
                kind=PredicateKind.TEXT_PRESENT,
                text=text,
                exact=True,
                intent="the dialog still reports no match",
            ),
            on_unmet="halt",
        ),
    )


def looks_like_data(text: str) -> bool:
    """Whether ``text`` carries a number other than 0, or an email address.

    Counts, dates, ids and addresses change from record to record; a dialog's
    own statement of an empty result needs none of them ("0 Treffer" is fine).
    """

    if "@" in text:
        return True
    return any(run != "0" for run in re.findall(r"\d+", text))


def _overlaps_vertically(a: OcrLine, b: OcrLine) -> bool:
    _ax, ay, _aw, ah = a.region
    _bx, by, _bw, bh = b.region
    return ay < by + bh and by < ay + ah


def _contains(line: OcrLine, point: tuple[int, int]) -> bool:
    x, y, w, h = line.region
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _exact_count(lines: Sequence[OcrLine], key: str) -> int:
    return sum(
        1
        for readings in wrapped_readings(lines, min_confidence=MIN_CONFIDENCE)
        if any(text_key(reading) == key for reading in readings)
    )


def dialog_statement(
    opener_before: Sequence[OcrLine],
    opener_after: Sequence[OcrLine],
    confirm_before: Sequence[OcrLine],
    *,
    confirm_point: tuple[int, int],
    title: Optional[str],
    labels: Sequence[str] = (),
    reject: Callable[[str], bool] = lambda _text: False,
) -> tuple[Optional[str], str]:
    """The one line a dialog added, in any language, or why there is none.

    The line is what the dialog shows besides its title and its button row:
    every line on the Confirm screen that was not on the screen before the
    opener click, other than ``title`` (the dialog's accessible name, recorded
    by the browser), the lines level with the Confirm button (``Cancel``
    beside it), and ``labels`` (the names of controls the demonstration
    clicked). It must be exactly one line, or one line wrapped over up to
    three, every part read at confidence 0.8 or more; shown once on the
    Confirm screen and at most once after the opener; free of numbers other
    than 0 and email addresses (:func:`looks_like_data`); and ``reject``
    (run values, PHI) must not refuse it.

    Returns:
        ``(line, "")``, or ``(None, reason)``. A line returned here is only a
        candidate: nothing on one screen tells a dialog's statement from a
        record it lists. It becomes a check only with the evidence induction
        adds (the same line across demonstrations with different searches).
    """

    if not title or not text_key(title):
        return None, "the recording does not name the dialog, so its title is unknown"
    rows = [line for line in confirm_before if _contains(line, confirm_point)]
    if not rows:
        return None, "the Confirm button's label was not read"
    before = {
        text_key(reading)
        for readings in wrapped_readings(opener_before)
        for reading in readings
    }
    skip = {text_key(title), *(text_key(label) for label in labels)}
    added = [
        line
        for line in confirm_before
        if text_key(line.text)
        and text_key(line.text) not in before
        and text_key(line.text) not in skip
        and not any(_overlaps_vertically(line, row) for row in rows)
    ]
    if not added:
        return None, "the dialog shows no line besides its title and buttons"
    if any(line.confidence < MIN_CONFIDENCE for line in added):
        return None, "a line the dialog shows is not confidently read"
    top = min(added, key=lambda line: (line.region[1], line.region[0]))
    readings = wrapped_readings([top, *(line for line in added if line is not top)])
    if len(readings[0]) != len(added):
        return None, f"the dialog shows {len(added)} lines, not one statement"
    text = readings[0][-1]
    key = text_key(text)
    if _exact_count(confirm_before, key) != 1 or _exact_count(opener_after, key) > 1:
        return None, "the dialog's line is shown more than once"
    if looks_like_data(text) or reject(text):
        return None, "the dialog's line carries a run value, PHI or a number"
    return text, ""


def carries_value(text: str, values: Sequence[str]) -> bool:
    """Whether ``text`` contains one of ``values`` as whole words.

    A statement is built from a fixed vocabulary, so the compiler's fuzzy
    parameter screen is the wrong test for it: that screen counts scattered
    letters and flags "No matches were found." against a title like "Ms.".
    A value can only be in a statement as one or more of its words.
    """

    words = [text_key(word) for word in text.split()]
    words = [word for word in words if word]
    for value in values:
        target = [text_key(word) for word in value.split()]
        target = [word for word in target if word]
        if not target or len("".join(target)) < 3:
            continue
        n = len(target)
        if any(words[i : i + n] == target for i in range(len(words) - n + 1)):
            return True
    return False


def has_text_check(expect: Sequence[Postcondition], text: str) -> bool:
    """Whether ``expect`` already checks for ``text`` exactly."""

    key = text_key(text)
    return any(
        pc.kind is PostconditionKind.TEXT_PRESENT
        and pc.exact
        and pc.text
        and text_key(pc.text) == key
        for pc in expect
    )


def is_empty_result_check(pc: Postcondition) -> bool:
    """Whether ``pc`` is a mined empty-result check."""

    return (
        pc.kind is PostconditionKind.TEXT_PRESENT
        and pc.exact
        and bool(pc.text)
        and is_empty_result_statement(pc.text or "")
    )


__all__ = [
    "GUARD_TIMEOUT_S",
    "MinedGuard",
    "carries_value",
    "dialog_statement",
    "empty_result_statements",
    "exact_checks",
    "has_text_check",
    "is_empty_result_check",
    "is_empty_result_statement",
    "looks_like_data",
    "mine_empty_result_guard",
]
