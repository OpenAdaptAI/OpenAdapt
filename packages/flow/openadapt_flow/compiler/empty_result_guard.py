"""The empty result a person confirmed through, mined as a check.

Many systems put a check between a form and its save: a duplicate search, a
conflict check, an eligibility lookup. The dialog it opens reports the result
("No matches were found.") above a Confirm button. A person who clicks
Confirm on that dialog has read the result; had it listed a match, they would
have stopped. The empty result is the precondition of the save.

Generic text mining keeps one line per step and may pick another line of the
dialog, so the precondition was lost: a run on a possible duplicate passed the
dialog and saved. This module mines it back from the recorded frames.

It mines only on the step that opens a confirmation dialog (an
``(opener, confirm)`` pair from :func:`openadapt_flow.risk.confirmation_opener_step_ids`),
and only a statement of an empty result:

- the statement reads as one, by its words alone ("no matches were found",
  "no matching records found", "nothing found", "0 results"), as a whole
  line, possibly wrapped over two or three OCR lines. Every word comes from
  a fixed vocabulary, so a line that adds anything else (a name, a search
  term) is not one, and a value the run changes is never frozen;
- it is new after the opener (not on the screen before the click) and still
  on the screen the person clicked Confirm on (the confirm step's before
  frame);
- every line it is read from is confidently read, and the caller's
  ``reject`` (parameter values, PHI) does not refuse it.

Other dialog text is not mined. A dialog that lists what it found (a match,
a conflict) changes with the data, and freezing what the demonstration showed
would halt healthy runs or, worse, require the match the person saw.

The statement becomes a ``TEXT_PRESENT`` postcondition of the opener: the run
halts after opening the dialog, before the Confirm click, when the dialog does
not report the empty result.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

from openadapt_flow.ir import Postcondition, PostconditionKind
from openadapt_flow.vision.ocr import OcrLine, normalize_text

#: A line must be read at least this confidently to be part of a statement.
MIN_CONFIDENCE = 0.8
#: The dialog's result can arrive after the dialog itself (it is a search),
#: so the check waits longer than a step's default 5 s.
GUARD_TIMEOUT_S = 10.0
#: At most this many OCR lines are joined into one wrapped statement.
MAX_WRAPPED_LINES = 3

_NOUN = (
    r"(?:match(?:es)?|results?|records?|duplicates?|hits?|entries|entry|items?"
    r"|conflicts?|candidates?|patients?|persons?|people|customers?|accounts?"
    r"|users?|contacts?|members?|documents?)"
)
#: Words allowed between "no" and the noun. A fixed list: a free word there
#: could be a name or a search term.
_MODIFIERS = (
    r"(?:(?:matching|possible|potential|duplicate|similar|existing|other|related"
    r"|exact|such|more|new) ){0,2}"
)
_BE = r"(?: (?:were|was|are|is|have been|has been|could be))?"
_FOUND = r"(?: (?:found|returned|located|matched|exist|available))?"
_SCOPE = r"(?: (?:in|for) (?:this|the|your) (?:search|list|table|system|database))?"
_STATEMENT = re.compile(
    r"^(?:"
    rf"(?:there (?:are|were|is|was) )?no {_MODIFIERS}{_NOUN}{_BE}{_FOUND}{_SCOPE}"
    rf"|(?:nothing|none) (?:was |were )?found{_SCOPE}"
    rf"|(?:0|zero) {_MODIFIERS}{_NOUN}{_BE}{_FOUND}{_SCOPE}"
    rf"|(?:we )?found no {_MODIFIERS}{_NOUN}{_SCOPE}"
    r")$"
)


def _key(text: str) -> str:
    """Letters and digits only, lowercased: how the runtime compares text."""

    return "".join(ch for ch in text.lower() if ch.isalnum())


def is_empty_result_statement(text: str) -> bool:
    """Whether ``text`` is, as a whole, a statement that nothing was found."""

    norm = normalize_text(text).rstrip(" .!")
    return bool(norm) and _STATEMENT.fullmatch(norm) is not None


def _continues(upper: OcrLine, lower: OcrLine) -> bool:
    """Whether ``lower`` is the wrapped continuation of ``upper``."""

    ux, uy, _uw, uh = upper.region
    lx, ly, _lw, _lh = lower.region
    gap = ly - (uy + uh)
    return abs(lx - ux) <= max(6, uh // 2) and -2 <= gap <= max(4, (uh * 4) // 5)


def empty_result_statements(lines: Sequence[OcrLine]) -> list[str]:
    """Every empty-result statement in one frame's OCR, wrapped lines joined.

    A statement starts at the beginning of a line. When a wrapped reading and
    a shorter one both qualify, the longest wins.
    """

    confident = [line for line in lines if line.confidence >= MIN_CONFIDENCE]
    found: list[str] = []
    for start in confident:
        chain = [start]
        best: Optional[str] = None
        while True:
            text = " ".join(line.text.strip() for line in chain)
            if is_empty_result_statement(text):
                best = text
            if len(chain) == MAX_WRAPPED_LINES:
                break
            following = [
                line
                for line in confident
                if line is not chain[-1] and _continues(chain[-1], line)
            ]
            if len(following) != 1:
                break
            chain.append(following[0])
        if best is not None:
            found.append(best)
    return found


def mine_empty_result_guards(
    opener_before: Sequence[OcrLine],
    opener_after: Sequence[OcrLine],
    confirm_before: Sequence[OcrLine],
    *,
    reject: Callable[[str], bool] = lambda _text: False,
) -> list[Postcondition]:
    """``TEXT_PRESENT`` checks for the empty results the person confirmed through.

    Args:
        opener_before: OCR of the frame before the click that opened the dialog.
        opener_after: OCR of the frame after it (the dialog).
        confirm_before: OCR of the frame the person clicked Confirm on.
        reject: Refuses a statement the program must not assert (one that
            carries a parameter value or PHI).

    Returns:
        One postcondition per distinct statement, in screen order.
    """

    seen_before = {_key(text) for text in empty_result_statements(opener_before)}
    still_shown = {_key(text) for text in empty_result_statements(confirm_before)}
    out: list[Postcondition] = []
    kept: set[str] = set()
    for text in empty_result_statements(opener_after):
        key = _key(text)
        if key in seen_before or key not in still_shown or key in kept:
            continue
        if reject(text):
            continue
        kept.add(key)
        out.append(
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text=text,
                timeout_s=GUARD_TIMEOUT_S,
            )
        )
    return out


def carries_value(text: str, values: Sequence[str]) -> bool:
    """Whether ``text`` contains one of ``values`` (letters and digits, exact).

    A statement is built from a fixed vocabulary, so the compiler's fuzzy
    parameter screen is the wrong test for it: that screen counts scattered
    letters and flags "No matches were found." against a name like "Marsh".
    A value can only be in a statement as one of its words; this finds it.
    """

    key = _key(text)
    return any(len(_key(v)) >= 3 and _key(v) in key for v in values)


def has_text_check(expect: Sequence[Postcondition], text: str) -> bool:
    """Whether ``expect`` already checks for ``text`` (letters and digits)."""

    key = _key(text)
    return any(
        pc.kind is PostconditionKind.TEXT_PRESENT and pc.text and _key(pc.text) == key
        for pc in expect
    )


__all__ = [
    "GUARD_TIMEOUT_S",
    "carries_value",
    "empty_result_statements",
    "has_text_check",
    "is_empty_result_statement",
    "mine_empty_result_guards",
]
