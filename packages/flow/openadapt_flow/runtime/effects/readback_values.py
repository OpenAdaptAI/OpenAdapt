"""Compare a value read back off a screen with the value the run typed.

A read-back field is read one of three ways: OCR of a screen region, the
page structure (DOM), or the native accessibility tree. This module decides,
for one field, whether the read shows the expected value. It has three
answers:

- ``match``: the read shows the expected value;
- ``mismatch``: the read is legible and shows another value;
- ``ambiguous``: the read differs from the expected value only in characters
  OCR can't tell apart (``O``/``0``, ``l``/``1``/``I``), is a date that reads
  two ways, or holds the expected value among other text. The verifier turns
  this into ``INDETERMINATE``, never a pass.

Values are compared after normalizing by kind:

- ``text``: case and runs of whitespace are ignored;
- ``code`` (an MRN, claim number, order code): case, spaces, hyphens, dots
  and slashes are ignored, and nothing else;
- ``date``: both sides are parsed to a calendar date. ``03/04/2026`` is
  ambiguous unless the field declares the order (``date_order``);
- ``currency``: both sides are parsed to a decimal amount, ignoring the
  currency symbol, valid thousands separators and a trailing ``.00``. Two
  amounts that name different currencies (``€250``, ``$250``) differ; ``$``
  is ``USD``;
- ``number`` (declared only, never guessed): both sides must be plain
  numbers, compared by value (``1,250.00`` is ``1250``). A value with a
  leading zero (``007``) is an identifier, compared as written. A ``text``
  field is compared as text even when it holds digits: ``250.0`` is not
  ``250.00`` (two different codes).

The comparison is of the whole field, never by substring, prefix or
similarity: ``Ann Smithers`` is not ``Ann Smith`` and ``$1,250.00`` is not
``250``. Numbers parse strictly (valid thousands grouping, or a decimal comma
with two places), and two codes split by separators in different places
(``12-345``, ``123-45``) are different codes.

``exact=False`` is for a read that may hold more than the field (an OCR
region that also shows a label). It never confirms more than ``exact=True``
does: a value found inside a longer read, as whole tokens, is ``ambiguous``
(it locates the value but doesn't prove the field holds only that value), and
anything else that isn't the whole field is ``mismatch``.

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
    "ReadbackKind",
    "ValueKind",
    "compare_value",
    "infer_value_kind",
    "normalize_value",
    "token_occurrences",
]

ValueKind = Literal["text", "code", "date", "currency", "number", "phone", "email"]
#: The kinds an on-screen read-back field compares in (``ReadbackField``).
ReadbackKind = Literal["text", "code", "date", "currency", "number"]
Comparison = Literal["match", "mismatch", "ambiguous"]
ReadSource = Literal["ocr", "structure"]

#: Characters OCR collapses on a pixel-only display, folded to one
#: representative. Two values equal only after this folding are ambiguous.
OCR_CONFUSABLE = str.maketrans({"o": "0", "l": "1", "i": "1", "|": "1"})

_CODE_SEPARATORS = re.compile(r"[\s\-./]+")
#: A well-formed amount: an optional currency symbol or code, an optional
#: sign or parentheses, and digits with valid thousands grouping and decimals,
#: or a decimal comma with two places. ``1,2,5`` and ``12 50`` don't parse.
_STRICT_AMOUNT = re.compile(
    r"^(?P<pre>[$€£¥]|USD|CAD|EUR|GBP)?\s?(?P<open>\()?(?P<sign>[+-])?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\d+,\d{2})"
    r"(?P<close>\))?\s?(?P<post>USD|CAD|EUR|GBP)?$",
    re.IGNORECASE | re.ASCII,
)
#: A plain number: digits, optional thousands commas, optional decimals.
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$", re.ASCII)
_CURRENCY_NAMES = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_CODE_PARTS = re.compile(r"[^\s\-./]+")
_TOKEN_EDGES = ".,:;!?()[]{}\"'"

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
    """A well-formed amount or number as a Decimal, else None."""

    match = _STRICT_AMOUNT.match(value.strip())
    if match is None or bool(match["open"]) != bool(match["close"]):
        return None
    if match["pre"] and match["post"]:
        return None  # "$250 USD": two currencies is not one amount
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


def _currency(value: str) -> Optional[str]:
    """The currency an amount names (``$`` is ``USD``), or None."""

    match = _STRICT_AMOUNT.match(value.strip())
    if match is None:
        return None
    name = (match["pre"] or match["post"] or "").upper()
    return _CURRENCY_NAMES.get(name, name) or None


def _leading_zero(value: str) -> bool:
    """``007``, ``0012345``: an identifier, not a number."""

    return re.match(r"^[+-]?0\d", value.strip()) is not None


def _code_edges(value: str) -> tuple[str, str]:
    """The separators a code starts and ends with (usually none)."""

    text = value.strip()
    lead = re.match(r"^[\-./]*", text)
    trail = re.search(r"[\-./]*$", text)
    return (lead.group() if lead else "", trail.group() if trail else "")


def _same_code_layout(a: str, b: str) -> bool:
    """False when both codes are split by separators, in different places.

    Codes are compared without separators, so ``AC1042`` matches ``AC-1042``.
    ``12-345`` and ``123-45`` are different codes, though: when both sides
    have separators, the parts between them must line up, and a leading or
    trailing separator is part of the code.
    """

    if _code_edges(a) != _code_edges(b):
        return False  # "-250" is not "250"
    parts_a = _CODE_PARTS.findall(a.lower())
    parts_b = _CODE_PARTS.findall(b.lower())
    if len(parts_a) < 2 or len(parts_b) < 2:
        return True
    return parts_a == parts_b


def _tokens(value: str) -> list[str]:
    return [t.strip(_TOKEN_EDGES) for t in value.split() if t.strip(_TOKEN_EDGES)]


def token_occurrences(seen: str, want: str) -> int:
    """How many times ``want`` appears in ``seen`` as a run of whole tokens,
    case and edge punctuation ignored. ``Smith`` is not in ``Smithers`` and
    ``250`` is not in ``$1,250.00``.

    This locates a value; it never proves a field holds only that value.
    """

    seen_tokens = [t.lower() for t in _tokens(seen)]
    want_tokens = [t.lower() for t in _tokens(want)]
    n = len(want_tokens)
    if not n or n > len(seen_tokens):
        return 0
    return sum(
        1
        for i in range(len(seen_tokens) - n + 1)
        if seen_tokens[i : i + n] == want_tokens
    )


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
    if kind in ("currency", "number"):
        if kind == "number" and not _PLAIN_NUMBER.match(value.strip()):
            return None
        amount = _amount(value)
        return None if amount is None else format(amount.normalize(), "f")
    return _text(value) or None


def _squash(value: str) -> str:
    return "".join(value.split())


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

    ``match`` only when the whole read equals the expected value under the
    kind's normalization (see the module docstring). ``exact=False`` is for a
    read that may hold more than the field: the expected value found inside it
    as whole tokens, or several times, is ``ambiguous``, never ``match``.
    """

    ocr = source == "ocr"
    whole = _compare_whole(observed, expected, kind, ocr, date_order)
    if whole != "mismatch" or exact:
        return whole
    # The read holds more than the field: finding the value in it locates
    # the value, it doesn't prove what was saved.
    found = token_occurrences(observed, expected)
    if not found and ocr:
        found = token_occurrences(
            observed.lower().translate(OCR_CONFUSABLE),
            expected.lower().translate(OCR_CONFUSABLE),
        )
    return "ambiguous" if found else "mismatch"


def _compare_whole(
    observed: str,
    expected: str,
    kind: ValueKind,
    ocr: bool,
    date_order: Optional[str],
) -> Comparison:
    """The whole read against the expected value, normalized by kind."""

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
    if kind in ("currency", "number"):
        want_amount = _amount(expected)
        if want_amount is None:
            return "mismatch"
        if kind == "number" and (_leading_zero(observed) or _leading_zero(expected)):
            # "007" is an identifier, even in a number field: its digits.
            return "match" if observed.strip() == expected.strip() else "mismatch"
        seen_amount = normalize_value(observed, kind)
        if seen_amount is not None:
            want_form = format(want_amount.normalize(), "f")
            names = {_currency(observed), _currency(expected)} - {None}
            if seen_amount != want_form or len(names) > 1:
                return "mismatch"  # another amount, or another currency
            return "match"
        if ocr:
            folded = observed.lower().translate(OCR_CONFUSABLE)
            if normalize_value(folded, kind) == format(want_amount.normalize(), "f"):
                return "ambiguous"
        return "mismatch"
    if kind == "code":
        seen_code, want_code = _code(observed), _code(expected)
        if not want_code:
            return "mismatch"
        if seen_code == want_code:
            if _same_code_layout(observed, expected):
                return "match"
            # OCR adds and drops spaces, so a layout that differs only in
            # spaces may be a misread; a structure read has the real text.
            return (
                "ambiguous"
                if ocr and _same_code_layout(_squash(observed), _squash(expected))
                else "mismatch"
            )
        if ocr and _folded_equal(seen_code, want_code):
            return "ambiguous"
        return "mismatch"
    seen_text, want_text = _text(observed), _text(expected)
    if not want_text:
        return "mismatch"
    if seen_text == want_text:
        return "match"
    if ocr and _folded_equal(_squash(seen_text), _squash(want_text)):
        # Equal once OCR's added or dropped spaces (or look-alike glyphs)
        # are set aside: a possible misread, never proof either way.
        return "ambiguous"
    return "mismatch"


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_DATE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}$")
_MONEY = re.compile(
    r"^[$€£¥]\s?-?[\d,]+(\.\d{2})?$|^-?[\d,]+\.\d{2}\s?(USD|CAD|EUR|GBP)$"
)
_CODE_SHAPE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9\-./]{4,}$")


def infer_value_kind(value: str) -> ReadbackKind:
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
