"""Deterministic field normalization, formatting, and equivalence codecs for form_tx.

Zero model calls. Fail-closed on ambiguous dates, malformed numbers, and
unmapped enum labels.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Sequence

from openadapt_flow.form_tx.presets import load_words
from openadapt_flow.form_tx.types import (
    FieldCodecSpec,
    FormCodecKind,
    ObservedOption,
)

_WHITESPACE_RE = re.compile(r"\s+")
_ISO_DATE_RE = re.compile(r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$")
_SLASH_YMD_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_SPELLED_DMY_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$")
_SPELLED_MDY_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$")
_CURRENCY_RE = re.compile(
    r"^(?P<unit>[$€£₹])?\s*(?P<amount>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)$"
)
_PHONE_RE = re.compile(r"^\+?[0-9 ()\t.-]+$")
#: The default yes/no texts (data: ``presets/words/true_tokens.json`` and
#: ``false_tokens.json``). A codec may replace them.
_TRUTHY_TOKENS = frozenset(load_words("true_tokens"))
_FALSY_TOKENS = frozenset(load_words("false_tokens"))


class CodecValueError(ValueError):
    """Raised when a value cannot be unambiguously normalized by its codec."""


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _checked_iso_date(year: int, month: int, day: int) -> str:
    if not (1850 <= year <= 2200):
        raise CodecValueError(f"Year out of supported range: {year}")
    if not (1 <= month <= 12):
        raise CodecValueError(f"Invalid month: {month}")
    _, max_day = calendar.monthrange(year, month)
    if not (1 <= day <= max_day):
        raise CodecValueError(
            f"Invalid calendar date: {year:04d}-{month:02d}-{day:02d}"
        )
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_iso_date_strict(raw: Any, date_order: str = "YMD") -> str:
    """Normalize a date value to ``YYYY-MM-DD`` or raise ``CodecValueError``."""
    if isinstance(raw, datetime):
        raise CodecValueError("A timestamp cannot establish a calendar date")
    if isinstance(raw, date):
        return _checked_iso_date(raw.year, raw.month, raw.day)
    if date_order not in {"YMD", "MDY", "DMY"}:
        raise CodecValueError(f"Unsupported date order: {date_order!r}")
    if not isinstance(raw, str):
        raise CodecValueError(f"Unsupported date type: {type(raw).__name__}")
    s = collapse_whitespace(raw)
    if not s:
        raise CodecValueError("Empty date string")

    m = _ISO_DATE_RE.match(s)
    if m:
        return _checked_iso_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _SLASH_YMD_RE.match(s)
    if m:
        return _checked_iso_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _SPELLED_DMY_RE.match(s)
    if m:
        mon = _MONTH_MAP.get(m.group(2).lower())
        if mon is None:
            raise CodecValueError(f"Unrecognized month name in {raw!r}")
        return _checked_iso_date(int(m.group(3)), mon, int(m.group(1)))

    m = _SPELLED_MDY_RE.match(s)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower())
        if mon is None:
            raise CodecValueError(f"Unrecognized month name in {raw!r}")
        return _checked_iso_date(int(m.group(3)), mon, int(m.group(2)))

    m = _NUMERIC_DATE_RE.match(s)
    if m:
        p1, p2, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if date_order == "MDY":
            return _checked_iso_date(yr, p1, p2)
        if date_order == "DMY":
            return _checked_iso_date(yr, p2, p1)
        if p1 == p2:
            return _checked_iso_date(yr, p1, p2)
        if p1 > 12 and p2 <= 12:
            return _checked_iso_date(yr, p2, p1)
        if p2 > 12 and p1 <= 12:
            return _checked_iso_date(yr, p1, p2)
        raise CodecValueError(
            f"Ambiguous numeric date {raw!r} without explicit MDY/DMY date_order"
        )

    raise CodecValueError(f"Unparseable date string: {raw!r}")


def normalize_phone_digits(raw: Any) -> str:
    """Normalize telephone formatting, refusing text or unparsed qualifiers."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise CodecValueError(f"Unsupported phone value type: {type(raw).__name__}")
    s = collapse_whitespace(str(raw))
    if not _PHONE_RE.fullmatch(s):
        raise CodecValueError(f"Invalid phone number representation: {raw!r}")
    if s.count("(") != s.count(")") or s.count("(") > 1:
        raise CodecValueError(f"Invalid phone number parentheses: {raw!r}")
    if "(" in s and s.index("(") > s.index(")"):
        raise CodecValueError(f"Invalid phone number parentheses: {raw!r}")
    digits = "".join(ch for ch in s if ch in "0123456789")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 7 or len(digits) > 15:
        raise CodecValueError(
            f"Invalid phone number digit length ({len(digits)}): {raw!r}"
        )
    return digits


def _parse_currency(raw: Any) -> tuple[str | None, Decimal]:
    """Return the explicit unit and amount without inferring a currency."""
    if isinstance(raw, bool):
        raise CodecValueError("Boolean is not a currency value")
    unit: str | None = None
    if isinstance(raw, (int, float, Decimal)):
        dec = Decimal(str(raw))
    elif isinstance(raw, str):
        match = _CURRENCY_RE.fullmatch(raw.strip())
        if match is None:
            raise CodecValueError(f"Invalid currency representation: {raw!r}")
        unit = match.group("unit")
        try:
            dec = Decimal(match.group("amount").replace(",", ""))
        except InvalidOperation as exc:
            raise CodecValueError(f"Invalid currency string {raw!r}") from exc
    else:
        raise CodecValueError(f"Unsupported currency type: {type(raw).__name__}")
    if not dec.is_finite():
        raise CodecValueError("Currency amount must be finite")
    return unit, dec


def normalize_currency_cents(raw: Any) -> int:
    """Return exact minor units; comparison separately preserves currency identity."""
    _, dec = _parse_currency(raw)
    try:
        numerator, denominator = dec.as_integer_ratio()
        cents, remainder = divmod(numerator * 100, denominator)
        if remainder:
            raise CodecValueError("Currency amount has fractional minor units")
        return cents
    except (InvalidOperation, OverflowError, ValueError) as exc:
        if isinstance(exc, CodecValueError):
            raise
        raise CodecValueError(f"Invalid currency amount: {raw!r}") from exc


def normalize_boolean_yes_no(
    raw: Any,
    true_tokens: Optional[Sequence[str]] = None,
    false_tokens: Optional[Sequence[str]] = None,
) -> str:
    """Normalize boolean/yes-no values strictly to ``'yes'`` or ``'no'``.

    ``true_tokens`` and ``false_tokens`` replace the default texts (a codec's
    override); a text in both is ambiguous and raises.
    """
    truthy = (
        _TRUTHY_TOKENS
        if true_tokens is None
        else frozenset(collapse_whitespace(t).lower() for t in true_tokens)
    )
    falsy = (
        _FALSY_TOKENS
        if false_tokens is None
        else frozenset(collapse_whitespace(t).lower() for t in false_tokens)
    )
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    if isinstance(raw, int) and raw in (0, 1):
        return "yes" if raw == 1 else "no"
    if isinstance(raw, str):
        tok = collapse_whitespace(raw).lower()
        if tok in truthy and tok in falsy:
            raise CodecValueError(f"Ambiguous boolean/yes-no value: {raw!r}")
        if tok in truthy:
            return "yes"
        if tok in falsy:
            return "no"
    raise CodecValueError(f"Ambiguous boolean/yes-no value: {raw!r}")


def normalize_with_codec(value: Any, spec: FieldCodecSpec) -> Any:
    """Normalize ``value`` into its canonical comparable representation."""
    if value is None:
        raise CodecValueError("Cannot normalize None")

    kind = spec.kind
    if kind == FormCodecKind.EXACT:
        return str(value)
    if kind == FormCodecKind.WHITESPACE_COLLAPSE:
        return collapse_whitespace(str(value))
    if kind == FormCodecKind.CASEFOLD:
        return collapse_whitespace(str(value)).casefold()
    if kind == FormCodecKind.TITLE_CASE:
        return collapse_whitespace(str(value)).title()
    if kind == FormCodecKind.UPPER:
        return collapse_whitespace(str(value)).upper()
    if kind == FormCodecKind.PHONE_DIGITS:
        return normalize_phone_digits(value)
    if kind == FormCodecKind.ISO_DATE:
        return parse_iso_date_strict(value, date_order=spec.date_order)
    if kind == FormCodecKind.INTEGER:
        if isinstance(value, bool):
            raise CodecValueError("Boolean is not an integer")
        cleaned = str(value).replace(",", "").strip()
        try:
            return int(cleaned)
        except ValueError as exc:
            raise CodecValueError(f"Invalid integer value: {value!r}") from exc
    if kind == FormCodecKind.CURRENCY_CENTS:
        unit, _ = _parse_currency(value)
        if spec.currency_symbol is not None and unit not in (
            None,
            spec.currency_symbol,
        ):
            raise CodecValueError("Currency unit contradicts the field contract")
        return normalize_currency_cents(value)
    if kind == FormCodecKind.BOOLEAN_YES_NO:
        return normalize_boolean_yes_no(value, spec.true_tokens, spec.false_tokens)
    if kind == FormCodecKind.ENUM_MAP:
        probe = collapse_whitespace(str(value)).casefold()
        matches: list[str] = []
        for canonical_code, aliases in spec.enum_aliases.items():
            candidates = {canonical_code.casefold(), *(a.casefold() for a in aliases)}
            if probe in candidates:
                matches.append(canonical_code)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CodecValueError(
                f"Ambiguous enum value {value!r} matched multiple codes: {matches}"
            )
        if not spec.enum_aliases:
            return probe
        raise CodecValueError(
            f"Unmapped enum value {value!r}; allowed codes: {sorted(spec.enum_aliases)}"
        )
    raise CodecValueError(f"Unsupported codec kind: {kind}")


def values_equivalent(intended: Any, observed: Any, spec: FieldCodecSpec) -> bool:
    """Return True iff ``intended`` and ``observed`` normalize identically."""
    try:
        if spec.kind == FormCodecKind.CURRENCY_CENTS:
            intended_unit, _ = _parse_currency(intended)
            observed_unit, _ = _parse_currency(observed)
            declared_unit = getattr(spec, "currency_symbol", None)
            if declared_unit is not None:
                if intended_unit not in (None, declared_unit) or observed_unit not in (
                    None,
                    declared_unit,
                ):
                    return False
                intended_unit = observed_unit = declared_unit
            if intended_unit != observed_unit:
                return False
        norm_intended = normalize_with_codec(intended, spec)
        norm_observed = normalize_with_codec(observed, spec)
    except CodecValueError:
        return False
    return norm_intended == norm_observed


def resolve_unique_option(
    intended_value: Any,
    options: Sequence[ObservedOption],
    spec: FieldCodecSpec,
    placeholder_values: Sequence[str] = ("", "-- Select --", "Select...", "Choose..."),
) -> ObservedOption:
    """Resolve exactly one ``ObservedOption`` matching ``intended_value`` or fail closed."""
    placeholders_cf = {collapse_whitespace(p).casefold() for p in placeholder_values}
    real_options = [
        opt
        for opt in options
        if collapse_whitespace(opt.label).casefold() not in placeholders_cf
        and collapse_whitespace(opt.value).casefold() not in placeholders_cf
    ]
    matched: list[ObservedOption] = []
    for opt in real_options:
        if values_equivalent(intended_value, opt.value, spec) or values_equivalent(
            intended_value, opt.label, spec
        ):
            matched.append(opt)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise CodecValueError(
            f"Ambiguous option match for {intended_value!r}: {[o.label for o in matched]}"
        )
    raise CodecValueError(
        f"No option matched {intended_value!r} among {[o.label for o in real_options]}"
    )


def format_for_text_entry(intended_value: Any, spec: FieldCodecSpec) -> str:
    """Format a grounded value cleanly for typing into a text/date control."""
    if spec.kind == FormCodecKind.ISO_DATE:
        return parse_iso_date_strict(intended_value, date_order=spec.date_order)
    if spec.kind == FormCodecKind.CURRENCY_CENTS:
        unit, _ = _parse_currency(intended_value)
        if unit is not None and spec.currency_symbol is None:
            raise CodecValueError("Currency entry requires a declared field unit")
        cents = normalize_with_codec(intended_value, spec)
        sign = "-" if cents < 0 else ""
        whole, minor = divmod(abs(cents), 100)
        if minor == 0:
            return f"{sign}{whole}"
        return f"{sign}{whole}.{minor:02d}"
    if spec.kind == FormCodecKind.BOOLEAN_YES_NO:
        yes = normalize_with_codec(intended_value, spec) == "yes"
        if spec.true_tokens and spec.false_tokens:
            # A codec with its own words types its own first word.
            return spec.true_tokens[0] if yes else spec.false_tokens[0]
        return "Yes" if yes else "No"
    if spec.kind in (FormCodecKind.PHONE_DIGITS, FormCodecKind.INTEGER):
        return str(normalize_with_codec(intended_value, spec))
    if isinstance(intended_value, (int, float)):
        return str(intended_value)
    return collapse_whitespace(str(intended_value))
