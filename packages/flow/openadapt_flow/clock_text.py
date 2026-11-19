"""Screen text that was read from the clock, compiled relative to the run's clock.

A recorded screen often shows the date of the recording: a date picker opens
on the current month ("October 2026"), a form pre-fills today ("2026-10-20"),
a header names the day ("Tuesday, Oct 20"). Asserting that text as a literal
makes a program that halts the first day, month or year after it was
recorded. Dropping the assertion would weaken the check. This module keeps
the check and moves the date: a text whose date fragments all equal the
recording date, at the precision the text shows, becomes a template, and the
runtime renders it from the run's clock. The check still refuses a wrong date
("December 2026" when the run's clock says November), because the rendered
text is a specific date, never a pattern that matches any month.

Only fragments that EQUAL the recording date convert. A date near the
recording date but not on it ("last updated Oct 18") is content chronology
and stays volatile; a date far from it (a date of birth) stays literal
identity data (see :mod:`openadapt_flow.volatility`). A fragment whose field
order cannot be decided from the recording date ("10/10/2026": month first or
day first?) does not convert either, so the miner drops it as before.

Template syntax: literal text with ``{TOKEN}`` placeholders (a literal brace
is doubled, as in :meth:`str.format`). Tokens:

- ``YYYY`` / ``YY``: the year, four or two digits;
- ``MM`` / ``M`` / ``M?``: the month number, zero-padded, unpadded, or either
  (a recorded ``10`` does not say which);
- ``DD`` / ``D`` / ``D?``: the day of the month, the same three ways;
- ``Month`` / ``MONTH`` / ``month``: the English month name in title, upper or
  lower case; ``Mon`` / ``MON`` / ``mon``: its three-letter abbreviation
  (another spelling, such as ``Sept``, does not convert);
- ``Weekday`` / ``WEEKDAY`` / ``weekday`` and ``Wkd`` / ``WKD`` / ``wkd``: the
  day of the week, full or three letters.

Stdlib only, so the compiler, the runtime and the IR can share it.
"""

from __future__ import annotations

import re
import string
from datetime import date, datetime, timezone
from itertools import product
from typing import Any, Iterable, Optional

from openadapt_flow import volatility

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_CASED_WORD_TOKENS = ("Month", "Mon", "Weekday", "Wkd")
_NUMERIC_TOKENS = ("YYYY", "YY", "MM", "M", "M?", "DD", "D", "D?")


def _cased(base: str) -> tuple[str, str, str]:
    return (base, base.upper(), base.lower())


TOKENS: frozenset[str] = frozenset(
    {*_NUMERIC_TOKENS, *(t for base in _CASED_WORD_TOKENS for t in _cased(base))}
)

# Upper bound on the candidate renderings of one template for one date (each
# "either padding" token doubles it); a template past it is refused.
MAX_RENDERINGS = 16


class ClockTemplateError(ValueError):
    """A clock template is malformed or names an unknown token."""


def template_tokens(template: str) -> list[str]:
    """The ``{TOKEN}`` names in ``template``, in order.

    Raises:
        ClockTemplateError: on a malformed template, an unknown token, a
            format spec or conversion, or a template with no token at all.
    """
    tokens: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as error:
        raise ClockTemplateError(f"malformed clock template: {error}") from error
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if spec or conversion:
            raise ClockTemplateError("clock template tokens take no format spec")
        if field not in TOKENS:
            raise ClockTemplateError(f"unknown clock template token {field!r}")
        tokens.append(field)
    if not tokens:
        raise ClockTemplateError("a clock template must name at least one token")
    if 2 ** sum(1 for t in tokens if t.endswith("?")) > MAX_RENDERINGS:
        raise ClockTemplateError("clock template has too many ambiguous tokens")
    return tokens


def _word(base_value: str, token: str) -> str:
    if token.isupper():
        return base_value.upper()
    if token.islower():
        return base_value.lower()
    return base_value


def _token_values(token: str, on: date) -> tuple[str, ...]:
    if token == "YYYY":
        return (f"{on.year:04d}",)
    if token == "YY":
        return (f"{on.year % 100:02d}",)
    if token in ("MM", "M", "M?"):
        padded, bare = f"{on.month:02d}", str(on.month)
        return {"MM": (padded,), "M": (bare,)}.get(
            token, tuple(dict.fromkeys((padded, bare)))
        )
    if token in ("DD", "D", "D?"):
        padded, bare = f"{on.day:02d}", str(on.day)
        return {"DD": (padded,), "D": (bare,)}.get(
            token, tuple(dict.fromkeys((padded, bare)))
        )
    lowered = token.lower()
    if lowered == "month":
        return (_word(_MONTHS[on.month - 1], token),)
    if lowered == "mon":
        return (_word(_MONTHS[on.month - 1][:3], token),)
    if lowered == "weekday":
        return (_word(_WEEKDAYS[on.weekday()], token),)
    if lowered == "wkd":
        return (_word(_WEEKDAYS[on.weekday()][:3], token),)
    raise ClockTemplateError(f"unknown clock template token {token!r}")


def render(template: str, on: date) -> list[str]:
    """Every text ``template`` stands for on ``on``, in a stable order.

    Usually one text; a ``M?`` or ``D?`` token adds the other padding of the
    SAME date (``3`` and ``03``), never another date.
    """
    template_tokens(template)  # validate
    parts = list(string.Formatter().parse(template))
    choices: list[tuple[str, ...]] = []
    for literal, field, _spec, _conversion in parts:
        choices.append((literal,))
        if field is not None:
            choices.append(_token_values(field, on))
    return list(dict.fromkeys("".join(combo) for combo in product(*choices)))


def run_clock_dates(now: Optional[datetime] = None) -> list[date]:
    """The dates the run's clock reads: local, then UTC when it differs.

    An app may render dates in the browser's local zone or in the server's
    UTC; around midnight those are one day apart, and both are this run's
    clock. Both are specific dates, so a wrong date still fails.
    """
    moment = now if now is not None else datetime.now(timezone.utc).astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    local = moment.date()
    utc = moment.astimezone(timezone.utc).date()
    return [local] if utc == local else [local, utc]


def renderings(template: str, dates: Iterable[date]) -> list[str]:
    """The union of :func:`render` over ``dates``, first date first."""
    out: list[str] = []
    for on in dates:
        out.extend(render(template, on))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Compiling a recorded text into a template
# ---------------------------------------------------------------------------

_SEP = r"[\s\-/.,]*"


def _escape(literal: str) -> str:
    return literal.replace("{", "{{").replace("}", "}}")


def _number_token(recorded: str, kind: str) -> str:
    """``MM``/``M``/``M?`` (``kind`` "M") or the day equivalents for a number."""
    if len(recorded) == 1:
        return kind
    if recorded.startswith("0"):
        return kind * 2
    return f"{kind}?"


def _word_case_token(recorded: str, base: str) -> str:
    if recorded.isupper():
        return base.upper()
    if recorded.islower():
        return base.lower()
    return base


def _numeric_fragment(match: re.Match[str], on: date) -> Optional[str]:
    """Template for a numeric date fragment that equals ``on``, else None.

    The field order must be decidable: when more than one reading of the
    fragment equals ``on`` under DIFFERENT orders that render differently on
    another day (``10/10/2026``), the fragment is ambiguous and stays None.
    """
    fragment = match.group(0)
    pieces = re.split(r"([-/.])", fragment)
    numbers, seps = pieces[0::2], pieces[1::2]
    if len(numbers) != 3 or len(seps) != 2:
        return None
    orders: list[tuple[str, str, str]]
    if len(numbers[0]) == 4:
        orders = [("Y", "M", "D")]  # year-first dates are year-month-day
    else:
        orders = [("D", "M", "Y"), ("M", "D", "Y")]
    templates: set[str] = set()
    for order in orders:
        fields = dict(zip(order, numbers))
        year_s = fields["Y"]
        year = int(year_s) + (2000 if len(year_s) == 2 else 0)
        if len(year_s) not in (2, 4):
            continue
        try:
            reading = date(year, int(fields["M"]), int(fields["D"]))
        except ValueError:
            continue
        if reading != on:
            continue
        tokens = []
        for name, recorded in zip(order, numbers):
            if name == "Y":
                tokens.append("YYYY" if len(recorded) == 4 else "YY")
            else:
                tokens.append(_number_token(recorded, name))
        templates.add(
            "{%s}%s{%s}%s{%s}" % (tokens[0], seps[0], tokens[1], seps[1], tokens[2])
        )
    # A day equal to the month reads as ``on`` in both orders and gives two
    # templates that render differently on any other day: undecidable.
    if len(templates) != 1:
        return None
    return templates.pop()


def _month_fragment(match: re.Match[str], on: date) -> Optional[str]:
    """Template for a month-name date fragment that equals ``on``, else None.

    Precision is the fragment's own: a month and year must name ``on``'s
    month; a day and month (with or without a year) must name ``on``'s day;
    a weekday must be ``on``'s weekday.
    """
    month_s = match.group("mon")
    if volatility.month_number(month_s) != on.month:
        return None
    day_s = match.group("d1") or match.group("d2")
    year_s = match.group("yr")
    weekday_s = match.group("wd")
    if day_s is None and year_s is None:
        return None
    if day_s is not None and int(day_s) != on.day:
        return None
    if year_s is not None and int(year_s) != on.year:
        return None
    if weekday_s is not None and (volatility.weekday_number(weekday_s) != on.weekday()):
        return None
    text = match.group(0)
    start = match.start()
    spans: list[tuple[int, int, str]] = []
    if weekday_s is not None:
        wd_start, wd_end = match.span("wd")
        base = "Weekday" if len(weekday_s) > 3 else "Wkd"
        if base == "Weekday" and weekday_s.lower() != _WEEKDAYS[on.weekday()].lower():
            return None  # 'Tues', 'Thur': no token renders that spelling
        spans.append((wd_start, wd_end, _word_case_token(weekday_s, base)))
    for group in ("d1", "d2"):
        if match.group(group) is not None:
            d_start, d_end = match.span(group)
            spans.append((d_start, d_end, _number_token(match.group(group), "D")))
    mon_start, mon_end = match.span("mon")
    if len(month_s) == 3:
        base = "Mon"
    elif month_s.lower() == _MONTHS[on.month - 1].lower():
        base = "Month"
    else:
        return None  # 'Sept': no token renders that spelling
    spans.append((mon_start, mon_end, _word_case_token(month_s, base)))
    if year_s is not None:
        y_start, y_end = match.span("yr")
        spans.append((y_start, y_end, "YYYY"))
    spans.sort()
    out: list[str] = []
    cursor = start
    for s, e, token in spans:
        out.append(_escape(text[cursor - start : s - start]))
        out.append("{%s}" % token)
        cursor = e
    out.append(_escape(text[cursor - start :]))
    return "".join(out)


def compile_clock_text(text: str, recorded_on: Optional[date]) -> Optional[str]:
    """The clock template for ``text`` recorded on ``recorded_on``, or None.

    Returns a template only when ``text`` carries at least one date fragment
    and EVERY date fragment in it equals ``recorded_on`` at its own precision
    (so the fragment was read from the clock), the rest of the text carries no
    other volatile content (clock times, relative times, counts), and the
    template renders ``text`` back exactly on ``recorded_on``.
    """
    if recorded_on is None or not text or "{" in text or "}" in text:
        return None
    if (
        volatility.CLOCK_RE.search(text)
        or volatility.DOT_CLOCK_RE.search(text)
        or volatility.RELATIVE_TIME_RE.search(text)
        or volatility.COUNT_RE.search(text)
        or volatility.COUNTER_PAREN_RE.search(text)
    ):
        return None
    pieces: list[tuple[int, int, str]] = []
    for match in volatility.DATE_RE.finditer(text):
        template = _numeric_fragment(match, recorded_on)
        if template is None:
            return None
        pieces.append((match.start(), match.end(), template))
    for match in volatility.month_fragments(text):
        if any(s < match.end() and match.start() < e for s, e, _ in pieces):
            return None  # overlapping readings: not decidable
        template = _month_fragment(match, recorded_on)
        if template is None:
            return None
        pieces.append((match.start(), match.end(), template))
    if not pieces:
        return None
    pieces.sort()
    out: list[str] = []
    cursor = 0
    for s, e, template in pieces:
        out.append(_escape(text[cursor:s]))
        out.append(template)
        cursor = e
    out.append(_escape(text[cursor:]))
    template = "".join(out)
    try:
        if text not in render(template, recorded_on):
            return None
    except ClockTemplateError:
        return None
    return template


# Tokens whose rendering can be the inside of a longer number on screen: an
# unpadded "3" inside "13" or "30", a two-digit year "26" inside "2026".
_OPEN_ENDED = frozenset({"M", "M?", "D", "D?", "YY"})


def substring_safe(template: str) -> bool:
    """Whether a substring match of a rendering can only be that date.

    A substring reader (the DOM's visible-text check) finds "Nov 3" inside
    "Nov 30" and "1/3/2026" inside "11/3/2026". That happens only when the
    template starts or ends with a number token of open width; such a
    template must be read exactly, as a whole line.
    """
    parts = [
        (literal, field)
        for literal, field, _spec, _conversion in string.Formatter().parse(template)
    ]
    first_literal, first_field = parts[0]
    if not first_literal and first_field in _OPEN_ENDED:
        return False
    last_literal, last_field = parts[-1]
    if last_field is None:
        # The template ends in literal text after its last token.
        trailing_field = parts[-2][1] if len(parts) > 1 else None
        if not any(c.isalnum() for c in last_literal) and trailing_field in _OPEN_ENDED:
            return False
    elif last_field in _OPEN_ENDED:
        return False
    if not any(c.isalnum() for c in first_literal) and first_field in _OPEN_ENDED:
        return False
    return True


def check_postcondition(pc: Any) -> Any:
    """Validate ``Postcondition.text_clock`` (called by the IR's validator).

    The template must parse with known tokens, and belongs only on a
    ``text_present`` / ``text_absent`` check that keeps its recorded text.
    """
    template = getattr(pc, "text_clock", None)
    if template is None:
        return pc
    template_tokens(template)
    kind = getattr(pc.kind, "value", pc.kind)
    if kind not in ("text_present", "text_absent") or not pc.text:
        raise ClockTemplateError(
            "text_clock applies to text_present/text_absent with a recorded text"
        )
    return pc
