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
  currency symbol, thousands separators and a trailing ``.00``.

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
]

ValueKind = Literal["text", "code", "date", "currency", "phone", "email"]
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


#: A phone number: digits and the punctuation people type between them, with
#: at most one extension at the end. Anything else (a word, a note) makes the
#: value not a phone number, so it never matches one.
_PHONE = re.compile(
    r"^\s*(?P<main>[\d\s()+\-./]+?)\s*"
    r"(?:(?:x|ext\.?|extension)\s*(?P<ext>\d+))?\s*$",
    re.IGNORECASE,
)
#: An ISO date, alone or followed by a time of day only.
_ISO_WITH_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?$"
)


def _phone(value: str) -> Optional[str]:
    """The digits, with an extension kept apart (``6175550142x12``)."""

    match = _PHONE.match(value)
    if match is None:
        return None
    digits = re.sub(r"\D", "", match["main"])
    if not digits:
        return None
    return digits + (f"x{match['ext']}" if match["ext"] else "")


def _email(value: str) -> Optional[str]:
    """One address, case folded (the local part too: in practice mail
    systems treat it case-insensitively, though the RFCs don't require it)."""

    text = value.strip()
    if text.count("@") != 1 or any(c.isspace() for c in text):
        return None
    return text.lower()


def _text(value: str) -> str:
    return " ".join(value.lower().split())


def _code(value: str) -> str:
    return _CODE_SEPARATORS.sub("", value.lower())


def _dates(value: str, order: Optional[str]) -> set[date]:
    """Every calendar date ``value`` can mean."""

    cleaned = " ".join(value.split())
    stamped = _ISO_WITH_TIME.match(cleaned)
    if stamped:
        cleaned = stamped.group(1)  # a stored date-time: its date
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


def _amount(value: str) -> Optional[Decimal]:
    match = _CURRENCY.match(value.strip())
    if match is None:
        return None
    sign, digits = match.groups()
    digits = digits.replace(" ", "")
    if "," in digits and "." in digits:
        digits = digits.replace(",", "")
    elif digits.count(",") == 1 and len(digits.split(",")[1]) == 2:
        digits = digits.replace(",", ".")  # 12,50 written with a decimal comma
    else:
        digits = digits.replace(",", "")
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return None
    negative = bool(sign) or value.strip().startswith("(")
    return -amount if negative else amount


def normalize_value(
    value: str, kind: ValueKind = "text", *, date_order: Optional[str] = None
) -> Optional[str]:
    """The canonical form of ``value`` for ``kind``, or None if it doesn't parse.

    A date that reads two ways normalizes to None; use :func:`compare_value`
    to tell an ambiguous date from a wrong one.
    """

    if kind == "code":
        return _code(value) or None
    if kind == "phone":
        return _phone(value)
    if kind == "email":
        return _email(value)
    if kind == "date":
        found = _dates(value, date_order)
        return found.pop().isoformat() if len(found) == 1 else None
    if kind == "currency":
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
    if kind in ("phone", "email"):
        seen_form = normalize_value(observed, kind)
        want_form = normalize_value(expected, kind)
        if seen_form is not None and seen_form == want_form:
            return "match"
        return "mismatch"
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
    if kind == "currency":
        seen_amount = _amount(observed)
        want_amount = _amount(expected)
        if seen_amount is not None and want_amount is not None:
            if seen_amount == want_amount:
                return "match"
            return "mismatch"
        if ocr and want_amount is not None:
            folded = _amount(observed.lower().translate(OCR_CONFUSABLE))
            if folded == want_amount:
                return "ambiguous"
        return "mismatch"
    if kind == "code":
        seen_code, want_code = _code(observed), _code(expected)
        if not want_code:
            return "mismatch"
        if seen_code == want_code or (not exact and want_code in seen_code):
            return "match"
        if ocr and (
            _folded_equal(seen_code, want_code)
            or (
                not exact
                and want_code.translate(OCR_CONFUSABLE)
                in seen_code.translate(OCR_CONFUSABLE)
            )
        ):
            return "ambiguous"
        return "mismatch"
    seen_text, want_text = _text(observed), _text(expected)
    if not want_text:
        return "mismatch"
    if seen_text == want_text or (not exact and want_text in seen_text):
        return "match"
    if ocr:
        folded_seen = seen_text.translate(OCR_CONFUSABLE)
        folded_want = want_text.translate(OCR_CONFUSABLE)
        if folded_seen == folded_want or (not exact and folded_want in folded_seen):
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
