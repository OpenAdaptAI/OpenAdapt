"""Compare a value read back off a screen with the value the run typed.

A read-back field is read one of three ways: OCR of a screen region, the
page structure (DOM), or the native accessibility tree. This module decides,
for one field, whether the read shows the expected value. It has three
answers:

- ``match``: the read shows the expected value;
- ``mismatch``: the read is legible and shows another value;
- ``ambiguous``: the read differs from the expected value only in characters
  OCR can't tell apart (``O``/``0``, ``l``/``1``/``I``), or a date that reads
  two ways. The verifier turns this into ``INDETERMINATE``, never a pass.

Values are compared after normalizing by kind:

- ``text``: case and runs of whitespace are ignored;
- ``code`` (an MRN, claim number, order code): case, spaces, hyphens, dots
  and slashes are ignored, and nothing else;
- ``date``: both sides are parsed to a calendar date. ``03/04/2026`` is
  ambiguous unless the field declares the order (``date_order``);
- ``currency``: both sides are parsed to a decimal amount, ignoring the
  currency symbol, thousands separators and a trailing ``.00``;
- ``number``: both sides are parsed to a decimal number. An expected value
  that is a plain number (``250``, ``1,250.00``) is compared as a number even
  when the field is declared ``text``.

Exact after normalization, never by substring. A value read inside a longer
read (``exact=False``: an OCR region that also shows a label) must appear as
whole tokens, or for a number or an amount as a whole number: ``250`` is not
inside ``$1,250.00``. A value that appears more than once in the read is
``ambiguous``: two rows showing it are two records, not one confirmation.

Glyph confusions are only considered for OCR. Structure and accessibility
reads return the real characters, so a ``0`` that should be an ``O`` there is
a mismatch.

No model calls: string normalization, ``datetime.strptime`` and ``Decimal``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

__all__ = [
    "OCR_CONFUSABLE",
    "Comparison",
    "ValueKind",
    "compare_value",
    "infer_value_kind",
    "normalize_value",
    "token_occurrences",
]

ValueKind = Literal["text", "code", "date", "currency", "number"]
Comparison = Literal["match", "mismatch", "ambiguous"]
ReadSource = Literal["ocr", "structure"]

#: Characters OCR collapses on a pixel-only display, folded to one
#: representative. Two values equal only after this folding are ambiguous.
OCR_CONFUSABLE = str.maketrans({"o": "0", "l": "1", "i": "1", "|": "1"})

_CODE_SEPARATORS = re.compile(r"[\s\-./]+")
_CURRENCY = re.compile(r"^[^\d\-(]*\(?(-?)\s*([\d.,\s]+)\)?[^\d]*$")

#: Unambiguous layouts, tried on both sides.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d-%b-%Y",
    "%Y%m%d",
)
_DAY_MONTH = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")
_MONTH_DAY = ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y")


#: A plain number: digits, optional thousands commas, optional decimals.
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_TOKEN_EDGES = ".,:;!?()[]{}\"'"


def _plain_number(value: str) -> bool:
    """A value that is only a number (and no identifier with a leading zero)."""

    text = value.strip()
    return bool(_PLAIN_NUMBER.match(text)) and not _leading_zero(text)


def _tokens(value: str) -> list[str]:
    return [t.strip(_TOKEN_EDGES) for t in value.split() if t.strip(_TOKEN_EDGES)]


def _token_runs(seen: list[str], want: list[str]) -> int:
    """How many times ``want`` appears as a run of whole tokens in ``seen``."""

    if not want or len(want) > len(seen):
        return 0
    n = len(want)
    return sum(1 for i in range(len(seen) - n + 1) if seen[i : i + n] == want)


def _amount_runs(seen: str, want: Decimal, *, keep_zeros: bool = False) -> int:
    """How many whole tokens of ``seen`` are amounts equal to ``want``.

    A number joined to a hyphen or a letter (``250-1``, ``A250``) is part of a
    code, not a number. With ``keep_zeros`` (a value compared as a number only
    because it looks like one), a token with a leading zero is an identifier
    and never equals a number.
    """

    words = seen.split()
    tokens = [w.strip(_TOKEN_EDGES.replace("(", "").replace(")", "")) for w in words]
    # A currency symbol or code may stand apart from its number: "USD 1,250".
    joined = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    count = 0
    for token in tokens:
        if keep_zeros and _leading_zero(token.lstrip("$€£¥")):
            continue
        if _amount(token) == want:
            count += 1
    if count == 0:
        count = sum(1 for t in joined[len(tokens) :] if _amount(t) == want)
    return count


def token_occurrences(seen: str, want: str, kind: ValueKind = "text") -> int:
    """How many times ``want`` appears in ``seen`` as whole tokens (or, for a
    number or an amount, as a whole number), after normalizing by kind."""

    declared = kind in ("currency", "number")
    if declared or _plain_number(want):
        amount = _amount(want)
        return (
            0 if amount is None else _amount_runs(seen, amount, keep_zeros=not declared)
        )
    norm = _code if kind == "code" else _text
    return _token_runs(
        [norm(t) for t in _tokens(seen)], [norm(t) for t in _tokens(want)]
    )


def _text(value: str) -> str:
    return " ".join(value.lower().split())


def _code(value: str) -> str:
    return _CODE_SEPARATORS.sub("", value.lower())


def _dates(value: str, order: Optional[str]) -> set[date]:
    """Every calendar date ``value`` can mean."""

    cleaned = " ".join(value.split())
    found: set[date] = set()
    for layout in _DATE_FORMATS:
        try:
            found.add(datetime.strptime(cleaned, layout).date())
        except ValueError:
            continue
    numeric: tuple[str, ...]
    if order == "dmy":
        numeric = _DAY_MONTH
    elif order == "mdy":
        numeric = _MONTH_DAY
    else:
        numeric = _DAY_MONTH + _MONTH_DAY
    for layout in numeric:
        try:
            found.add(datetime.strptime(cleaned, layout).date())
        except ValueError:
            continue
    return found


#: A well-formed amount: an optional currency symbol or code, an optional
#: sign or parentheses, and digits with valid thousands grouping and decimals,
#: or a decimal comma with two places. ``1,2,5`` and ``12 50`` don't parse.
_STRICT_AMOUNT = re.compile(
    r"^(?P<pre>[$€£¥]|USD|CAD|EUR|GBP)?\s?(?P<open>\()?(?P<sign>[+-])?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\d+,\d{2})"
    r"(?P<close>\))?\s?(?P<post>USD|CAD|EUR|GBP)?$",
    re.IGNORECASE,
)


def _amount(value: str) -> Optional[Decimal]:
    """A well-formed amount or number as a Decimal, else None."""

    match = _STRICT_AMOUNT.match(value.strip())
    if match is None or bool(match["open"]) != bool(match["close"]):
        return None
    num = match["num"]
    if re.fullmatch(r"\d+,\d{2}", num):
        num = num.replace(",", ".")  # 12,50 written with a decimal comma
    else:
        num = num.replace(",", "")
    try:
        amount = Decimal(num)
    except InvalidOperation:
        return None
    negative = match["sign"] == "-" or bool(match["open"])
    return -amount if negative else amount


def _leading_zero(value: str) -> bool:
    """``007``, ``0012345``: an identifier, not a number."""

    return re.match(r"^[+-]?0\d", value.strip()) is not None


def normalize_value(
    value: str, kind: ValueKind = "text", *, date_order: Optional[str] = None
) -> Optional[str]:
    """The canonical form of ``value`` for ``kind``, or None if it doesn't parse.

    A date that reads two ways normalizes to None; use :func:`compare_value`
    to tell an ambiguous date from a wrong one.
    """

    if kind == "code":
        return _code(value) or None
    if kind == "date":
        found = _dates(value, date_order)
        return found.pop().isoformat() if len(found) == 1 else None
    if kind in ("currency", "number"):
        amount = _amount(value)
        return None if amount is None else format(amount.normalize(), "f")
    return _text(value) or None


def _folded_equal(observed: str, expected: str) -> bool:
    return observed.translate(OCR_CONFUSABLE) == expected.translate(OCR_CONFUSABLE)


def compare_value(
    observed: str,
    expected: str,
    *,
    kind: ValueKind = "text",
    source: ReadSource = "ocr",
    date_order: Optional[str] = None,
    exact: bool = True,
) -> Comparison:
    """Judge one field read against its expected value.

    ``exact`` is for ``text`` and ``code``: True compares the whole field (a
    structure read of one element); False accepts the expected value inside a
    longer read (an OCR region that also shows a label such as ``MRN:``). Fuzzy OCR
    matching for free text stays in
    :func:`openadapt_flow.runtime.effects.onscreen.value_matches`; this
    function only adds the kinds and the glyph rule.
    """

    ocr = source == "ocr"
    promoted = kind == "text" and _plain_number(expected)
    if promoted:
        kind = "number"
    if kind == "date":
        seen = _dates(observed, date_order)
        want = _dates(expected, date_order)
        if not seen or not want:
            if ocr and _dates(observed.translate(OCR_CONFUSABLE), date_order) & want:
                return "ambiguous"
            return "mismatch"
        if not seen & want:
            return "mismatch"
        # A read that can mean the expected date or another date proves nothing.
        return "match" if len(seen) == 1 and len(want) == 1 else "ambiguous"
    if kind in ("currency", "number"):
        want_amount = _amount(expected)
        if want_amount is None:
            return "mismatch"
        if exact:
            whole = observed.strip()
            if promoted and (not _PLAIN_NUMBER.match(whole) or _leading_zero(whole)):
                seen_amount = None  # "MRN 12345", "0012345": not this number
            else:
                seen_amount = _amount(whole)
            if seen_amount is not None:
                return "match" if seen_amount == want_amount else "mismatch"
        else:
            runs = _amount_runs(observed, want_amount, keep_zeros=promoted)
            if runs == 1:
                return "match"
            if runs > 1:
                return "ambiguous"  # the same number twice: two records
        if ocr:
            folded = observed.lower().translate(OCR_CONFUSABLE)
            if exact:
                if _amount(folded) == want_amount:
                    return "ambiguous"
            elif _amount_runs(folded, want_amount, keep_zeros=promoted):
                return "ambiguous"
        return "mismatch"
    if kind == "code":
        seen_code, want_code = _code(observed), _code(expected)
        if not want_code:
            return "mismatch"
        if seen_code == want_code:
            return "match"
        if not exact:
            runs = token_occurrences(observed, expected, "code")
            if runs == 1:
                return "match"
            if runs > 1:
                return "ambiguous"
        if ocr and (
            _folded_equal(seen_code, want_code)
            or (
                not exact
                and token_occurrences(
                    observed.lower().translate(OCR_CONFUSABLE),
                    expected.lower().translate(OCR_CONFUSABLE),
                    "code",
                )
            )
        ):
            return "ambiguous"
        return "mismatch"
    seen_text, want_text = _text(observed), _text(expected)
    if not want_text:
        return "mismatch"
    if seen_text == want_text:
        return "match"
    if not exact:
        runs = token_occurrences(observed, expected, "text")
        if runs == 1:
            return "match"
        if runs > 1:
            return "ambiguous"
    if ocr:
        folded_seen = seen_text.translate(OCR_CONFUSABLE)
        folded_want = want_text.translate(OCR_CONFUSABLE)
        if folded_seen == folded_want or (
            not exact and token_occurrences(folded_seen, folded_want, "text")
        ):
            return "ambiguous"
    return "mismatch"


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_DATE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}$")
_MONEY = re.compile(
    r"^[$€£¥]\s?-?[\d,]+(\.\d{2})?$|^-?[\d,]+\.\d{2}\s?(USD|CAD|EUR|GBP)$"
)
_CODE_SHAPE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9\-./]{4,}$")


def infer_value_kind(value: str) -> ValueKind:
    """Guess the kind of a demonstrated value, conservatively.

    Only clear shapes are recognized: an ISO or numeric date, an amount with a
    currency symbol or code, and a single token mixing letters and digits (a
    code). Anything else is ``text``.
    """

    stripped = value.strip()
    if _ISO_DATE.match(stripped) or _NUMERIC_DATE.match(stripped):
        return "date"
    if _MONEY.match(stripped):
        return "currency"
    if " " not in stripped and _CODE_SHAPE.match(stripped):
        return "code"
    return "text"
