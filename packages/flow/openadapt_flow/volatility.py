"""Volatility classification for mined text evidence.

Postconditions and identity evidence must SELECT FOR STABILITY: text that
names a *moment* (clock times, freshly-dated rows), a *count* (counters,
badges), or noise (short/low-entropy fragments) cannot survive replay
against live data — a fresh OpenEMR recording once mined ``text_present
':01'`` (a clock-minute fragment) and every later replay halted on it the
moment the clock moved on (docs/validation/VALIDATION.md, Track D).

The classifier here is deliberately asymmetric about dates: a date NEAR the
recording date is content chronology ("last updated", log rows, message
lists) and volatile by construction, while a date FAR from the recording
date is identity data (a date of birth, a contract date) — exactly the text
a robust identity assertion wants. The old blanket timestamp filter ate
patient banners because a DOB matches a date pattern; the near/far split
fixes that without readmitting clocks.

Everything here is import-light (stdlib only) so both the compiler and the
runtime identity module can share one vocabulary of volatility.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

# Clock times are volatile at replay latency (the minute hand moves between
# record and replay). The second alternative catches bare OCR fragments like
# ':01' — a colon-led minute with no hour digit, which slipped past the old
# combined pattern and false-halted three live OpenEMR replays.
CLOCK_RE = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"  # 18:38, 6:05, 12:45:59
    r"|(?<![\w:]):\d{2}(?!\d)"  # bare ':01' OCR fragments
)

# European dot-separated clocks ('Last updated 18.38'). Ambiguity with
# version numbers and section numbers is real ('v2.0', '2.10'), so a dot
# clock only counts when it is unambiguous: a TWO-digit hour in valid range
# ('18.38', '08.30' — never 'v2.0': minutes require two digits, and a
# leading letter like 'v2.10' is blocked by the lookbehind), an am/pm
# suffix, or a time-ish context word ('updated 8.30') for one-digit hours.
DOT_CLOCK_RE = re.compile(
    r"(?<![\w.])(?:[01]\d|2[0-3])\.[0-5]\d(?![\d.])"  # 18.38, 08.30
    r"|(?<![\w.])(?:[01]?\d|2[0-3])\.[0-5]\d\s*(?:am|pm)\b"  # 8.30 pm
    r"|\b(?:at|updated?|update[ds]|time|kl\.?|klo)\s+"
    r"(?:[01]?\d|2[0-3])\.[0-5]\d(?![\d.])",  # updated 8.30
    re.IGNORECASE,
)

# Relative-time phrases name a moment by distance from NOW — the distance
# has changed by replay time ('3 min ago', '2 hours ago', 'just now').
RELATIVE_TIME_RE = re.compile(
    r"(?<!\d)\d+\s*"
    r"(?:sec(?:ond)?s?|min(?:ute)?s?|h(?:ou)?rs?|days?|w(?:ee)?ks?"
    r"|months?|mos?|y(?:ea)?rs?|[smhdw])"
    r"\s*ago(?![a-z])"
    r"|\bjust\s*now\b"
    r"|\bmoments?\s*ago\b",
    re.IGNORECASE,
)

# Bare day-words are volatile only standalone ('Yesterday' as a message-list
# group header); embedded they are usually stable chrome ("Today's
# Appointments" is a label that always says Today's Appointments).
_RELATIVE_DAY_WORDS = frozenset({"today", "yesterday", "tomorrow", "now"})

# Counts name the current size of something — pagination position, result
# ranges, badge-style '5 new messages'. All navigation/volume state, none of
# it identity: '0 to 0 of 0 entries' is exactly the drift these must catch,
# so none of it may be frozen into an assertion. Whitespace is optional
# throughout because OCR routinely drops it ('Showing1to1of1entries').
_COUNT_NOUNS = (
    r"(?:entr(?:y|ies)|results?|records?|rows?|items?|matches"
    r"|messages?|notifications?|unread)"
)
COUNT_RE = re.compile(
    r"(?<!\d)\d[\d,]*\s*(?:to\s*\d[\d,]*\s*)?of\s*\d[\d,]*(?!\d)"  # 1 to 1 of 1, 2 of 9
    r"|(?<!\d)\d[\d,]*\s*(?:(?:total|new|unread|more)\s*)?"
    + _COUNT_NOUNS
    + r"(?![a-z\d])"  # 56 total entries, 5 new messages
    r"|\bpage\s*\d+(?!\d)",  # Page 2 (pagination position; see tests)
    re.IGNORECASE,
)

# Parenthesized bare integers are live counters ('Inbox (2)', 'Messages
# (14)') — decoration on an otherwise-stable label. See `classify_text`'s
# strip-and-test.
COUNTER_PAREN_RE = re.compile(r"\(\s*\d+\s*\)")

# Date-like fragments; whether one is volatile depends on its distance from
# the recording date (see `date_fragments_near`).
DATE_RE = re.compile(
    r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"  # 2026-07-08, 2026/7/8
    r"|(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\d)"  # 07/08/2026, 8.7.26
)

# Month-name dates ('Jul 8, 2026', '08 Jul 2026', 'July 2026', 'Updated
# Jul 8', 'Wednesday July 8') — the numeric-only DATE_RE missed all of
# these, and OpenEMR's post-login calendar renders exactly this class: a
# mined 'July 2026' header would false-halt every replay the next month.
# A bare month WORD is not a date ('May I help you'): at least one of
# day/year must be attached. Whitespace between tokens is optional (OCR).
_MONTH_PAT = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?"
    r"|nov(?:ember)?|dec(?:ember)?)"
)
_WEEKDAY_PAT = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?"
MONTH_DATE_RE = re.compile(
    rf"\b(?:{_WEEKDAY_PAT}[,\s]*)?"
    rf"(?:(?<!\d)(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?[\s.]*)?"
    rf"(?P<mon>{_MONTH_PAT})\.?"
    rf"(?:\s*(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"(?:,?\s*(?P<yr>\d{{4}}))?\b",
    re.IGNORECASE,
)

# Backwards-compatible combined pattern (previously defined in
# runtime.identity). Matches any clock or date fragment, with no near/far
# discrimination — callers that can supply a reference date should prefer
# `classify_text` / `is_volatile_line`.
TIMESTAMP_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}:\d{2}"
)

# A date within this many days of the recording date is treated as content
# chronology (volatile); farther away it is identity data (stable). One year
# covers "last week's message" and calendar chrome; DOBs and document dates
# are typically years out. (An entity whose identity date falls inside the
# window — e.g. an infant's DOB — is filtered as volatile: the cost is a
# weaker assertion, never a wrong one.)
DATE_VOLATILITY_WINDOW_DAYS = 366

# Minimum squashed (lowercased, whitespace-free) length for a stable text
# candidate; shorter fragments match everywhere by accident.
MIN_SQUASHED_LEN = 4

# Minimum count of alphabetic characters: candidates dominated by digits and
# punctuation ('#1234', '(3)', '42', ':01') name counts and moments, not
# screen semantics.
MIN_ALPHA_CHARS = 3

# Minimum distinct characters in the squashed form (rejects OCR noise like
# '----' or 'aaaa').
MIN_DISTINCT_CHARS = 3


_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _squash(text: str) -> str:
    return "".join(text.lower().split())


def _candidate_dates(fragment: str) -> list[date]:
    """All plausible calendar dates a date-like fragment could denote.

    Both field orders are tried for the ambiguous forms (D/M/Y vs M/D/Y,
    Y-M-D vs Y-D-M); two-digit years are read as 20xx. Invalid combinations
    are silently skipped.
    """
    numbers = [int(n) for n in re.findall(r"\d+", fragment)]
    if len(numbers) != 3:
        return []
    candidates: list[tuple[int, int, int]] = []
    if numbers[0] >= 1000:  # Y-M-D family
        y, a, b = numbers
        candidates = [(y, a, b), (y, b, a)]
    else:  # D/M/Y or M/D/Y family
        a, b, y = numbers
        if y < 100:
            y += 2000
        candidates = [(y, b, a), (y, a, b)]
    dates: list[date] = []
    for y, m, d in candidates:
        try:
            dates.append(date(y, m, d))
        except ValueError:
            continue
    return dates


def _month_fragments(text: str) -> list[re.Match]:
    """MONTH_DATE_RE matches that carry at least one attached number.

    A bare month word ('May I help you', a 'March' menu entry) is not a
    date fragment; a day or a year must be attached.
    """
    return [
        m
        for m in MONTH_DATE_RE.finditer(text)
        if m.group("d1") or m.group("d2") or m.group("yr")
    ]


def _month_fragment_near(match: re.Match, reference_date: date) -> bool:
    """Whether a month-name date fragment sits near the reference date.

    A month-day with no year ('Updated Jul 8') recurs annually and names
    chronology, never identity — always near/volatile. A month-year with no
    day ('July 2026' — a calendar header) is measured against the month's
    span. Unparseable combinations are conservatively near.
    """
    month = _MONTH_NUMBERS[match.group("mon").lower()[:3]]
    day = match.group("d1") or match.group("d2")
    year_s = match.group("yr")
    if year_s is None:
        return True  # recurring month-day: chronology, volatile
    year = int(year_s)
    try:
        if day is not None:
            nearest = abs((date(year, month, int(day)) - reference_date).days)
        else:
            first = date(year, month, 1)
            last = date(year, month, 28)
            if first <= reference_date <= last:
                nearest = 0
            else:
                nearest = min(
                    abs((first - reference_date).days),
                    abs((last - reference_date).days),
                )
    except ValueError:
        return True  # unparseable: assume volatile
    return nearest <= DATE_VOLATILITY_WINDOW_DAYS


def has_date_fragment(text: str) -> bool:
    """Whether ``text`` carries any date fragment (numeric or month-name)."""
    return bool(DATE_RE.search(text)) or bool(_month_fragments(text))


def date_fragments_near(text: str, reference_date: date) -> Optional[bool]:
    """Whether ``text``'s date fragments sit near the reference date.

    Both numeric fragments ('2026-07-08') and month-name fragments
    ('Jul 8, 2026', 'July 2026', 'Wednesday July 8') are considered.

    Returns:
        None when the text contains no date fragment; True when ANY fragment
        is within ``DATE_VOLATILITY_WINDOW_DAYS`` of ``reference_date`` (or
        cannot be parsed at all — conservative: unparseable means volatile);
        False when every fragment parses and all are far.
    """
    found = False
    for match in DATE_RE.finditer(text):
        found = True
        candidates = _candidate_dates(match.group(0))
        if not candidates:
            return True  # date-like but unparseable: assume volatile
        nearest = min(abs((d - reference_date).days) for d in candidates)
        if nearest <= DATE_VOLATILITY_WINDOW_DAYS:
            return True
    for match in _month_fragments(text):
        found = True
        if _month_fragment_near(match, reference_date):
            return True
    return None if not found else False


def _is_standalone_relative_day(squashed: str) -> bool:
    """Whether squashed text IS a bare relative day-word ('Yesterday')."""
    return squashed.strip(".,:;!?") in _RELATIVE_DAY_WORDS


def _residual_reason(squashed: str) -> Optional[str]:
    """The digit-dominance/entropy rules — the tail of `classify_text`."""
    if sum(1 for c in squashed if c.isalpha()) < MIN_ALPHA_CHARS:
        return "digit_dominated"
    if len(set(squashed)) < MIN_DISTINCT_CHARS:
        return "low_entropy"
    return None


def classify_text(text: str, *, reference_date: Optional[date] = None) -> Optional[str]:
    """Classify a text fragment's volatility for use as mined evidence.

    Args:
        text: Raw OCR line text.
        reference_date: The recording date, enabling the near/far date
            split. When None, ALL date-bearing text is treated as volatile
            (the conservative pre-split behavior).

    Returns:
        None when the text is a stable candidate, else a short reason:
        ``too_short``, ``clock_time`` (colon or unambiguous dot clocks),
        ``relative_time`` ('3 min ago', 'just now', a standalone
        'Yesterday'), ``date`` (no reference available), ``near_date``,
        ``count`` (result ranges, 'N total entries', pagination position),
        ``counter`` (a parenthesized live counter decorating a label —
        'Inbox (2)'), ``digit_dominated``, or ``low_entropy``.
    """
    squashed = _squash(text)
    if len(squashed) < MIN_SQUASHED_LEN:
        return "too_short"
    if CLOCK_RE.search(text) or DOT_CLOCK_RE.search(text):
        return "clock_time"
    if RELATIVE_TIME_RE.search(text) or _is_standalone_relative_day(squashed):
        return "relative_time"
    if has_date_fragment(text):
        if reference_date is None:
            return "date"
        if date_fragments_near(text, reference_date):
            return "near_date"
        # Far dates (DOB-class identity data) are stable; fall through so
        # the surrounding text still has to carry real content.
    if COUNT_RE.search(text):
        return "count"
    if COUNTER_PAREN_RE.search(text):
        # Strip-and-test: if removing the parenthesized number(s) leaves
        # the classification unchanged, the counter is volatile decoration
        # on the label and the composite must not be asserted verbatim
        # ('Inbox (2)' reads 'Inbox (3)' tomorrow). The label WITHOUT the
        # number may still be mined separately as stable text.
        stripped = COUNTER_PAREN_RE.sub(" ", text).strip()
        if classify_text(stripped, reference_date=reference_date) == _residual_reason(
            squashed
        ):
            return "counter"
    return _residual_reason(squashed)


def is_volatile_line(text: str, *, reference_date: Optional[date] = None) -> bool:
    """Whether a whole OCR line is too volatile to serve as evidence.

    Used by the identity-context extractor: a line carrying a clock time
    (colon or dot form), a relative-time phrase, a count/counter, or a
    near/unparseable date is dropped wholesale (its remainder may itself be
    day-fresh content, and a 'Messages (14)' badge recorded into a band
    false-halts the moment the count ticks); a line whose only date is FAR
    from the recording date (a DOB in an identity banner) is kept intact —
    including the date, which is discriminative identity data. Dropping a
    line can only weaken a band (or honestly disable the check), never
    admit a wrong action.

    Args:
        text: Raw OCR line text.
        reference_date: The recording date; when None all dates are volatile
            (conservative).
    """
    if CLOCK_RE.search(text) or DOT_CLOCK_RE.search(text):
        return True
    if RELATIVE_TIME_RE.search(text) or _is_standalone_relative_day(_squash(text)):
        return True
    if COUNT_RE.search(text) or COUNTER_PAREN_RE.search(text):
        return True
    if reference_date is None:
        return has_date_fragment(text)
    return date_fragments_near(text, reference_date) is True
