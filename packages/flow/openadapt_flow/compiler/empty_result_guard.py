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
    return MinedGuard(
        postcondition=Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text=text,
            timeout_s=GUARD_TIMEOUT_S,
            exact=True,
        ),
        guard=Guard(
            predicate=Predicate(
                kind=PredicateKind.TEXT_PRESENT,
                text=text,
                exact=True,
                intent="the dialog still reports no match",
            ),
            on_unmet="halt",
        ),
        statements=shown,
    )


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
    "empty_result_statements",
    "has_text_check",
    "is_empty_result_check",
    "is_empty_result_statement",
    "mine_empty_result_guard",
]
