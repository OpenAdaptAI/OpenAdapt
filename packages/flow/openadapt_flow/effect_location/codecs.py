"""Read displayed text as a value, by a pack's :class:`Codec`.

Every codec matches the whole displayed text. A prefix, a superstring or a
value with extra words is an error, never a match: a type-ahead's "Jane"
is not "Jane Doe", and "250" is not "$1,250.00". Anything the codec can't
read raises :class:`CodecError`, which the evaluator turns into
``not_checked``.

The module names no language and no application. Month names, yes/no words,
currency signs, units and lookup tables come from the pack.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from openadapt_flow.effect_location.record import SCALAR_KINDS, Codec

__all__ = ["CodecError", "canonical", "decode", "decode_scalar"]


class CodecError(ValueError):
    """The text (or the expected value) can't be read by this codec."""


_WS = re.compile(r"\s+")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_PLAIN_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_DATE_SPLIT = re.compile(r"[\s/.\-,]+")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _need_str(text: Any) -> str:
    if not isinstance(text, str):
        raise CodecError(f"displayed value is a {type(text).__name__}, not text")
    return text


def _iso(year: int, month: int, day: int) -> str:
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        raise CodecError("not a calendar date") from None


def _number(codec: Codec, text: str, *, affixes: tuple[str, ...]) -> Decimal:
    body = _collapse(text)
    for affix in sorted(affixes, key=len, reverse=True):
        if affix and body.startswith(affix):
            body = body.removeprefix(affix).strip()
            break
        if affix and body.endswith(affix):
            body = body.removesuffix(affix).strip()
            break
    sign = ""
    if body.startswith("-"):
        sign, body = "-", body[1:]
    dec = re.escape(codec.decimal_mark) if codec.decimal_mark else None
    frac = rf"(?:{dec}\d+)?" if dec else ""
    plain = rf"\d+{frac}"
    groups = [re.escape(g) for g in codec.group_marks if g]
    if groups:
        grouped = rf"\d{{1,3}}(?:(?:{'|'.join(groups)})\d{{3}})+{frac}"
        pattern = rf"^(?:{plain}|{grouped})$"
    else:
        pattern = rf"^{plain}$"
    if not re.match(pattern, body):
        raise CodecError("not a number in this format")
    for g in codec.group_marks:
        if g:
            body = body.replace(g, "")
    if codec.decimal_mark and codec.decimal_mark != ".":
        body = body.replace(codec.decimal_mark, ".")
    try:
        return Decimal(sign + body)
    except InvalidOperation:
        raise CodecError("not a number") from None


def _month(codec: Codec, token: str) -> int:
    if token.isdigit():
        return int(token)
    folded = token.casefold()
    hits = [
        n
        for n, names in enumerate(codec.month_names, start=1)
        if folded in {name.casefold() for name in names}
    ]
    if len(hits) != 1:
        raise CodecError("not a month this codec knows")
    return hits[0]


def _date(codec: Codec, text: str) -> str:
    body = _collapse(text)
    m = _ISO.match(body)
    if m:
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    order = codec.date_order
    if sorted(order) != ["D", "M", "Y"]:
        raise CodecError("not an ISO date, and the codec gives no date order")
    tokens = [t for t in _DATE_SPLIT.split(body) if t]
    if len(tokens) != 3:
        raise CodecError("not a date in this format")
    parts = dict(zip(order, tokens))
    return _split(codec, parts["Y"], parts["M"], parts["D"])


def _split(codec: Codec, year: Any, month: Any, day: Any) -> str:
    y, mo, d = (_collapse(_need_str(v)) for v in (year, month, day))
    if not (len(y) == 4 and y.isdigit()) or not d.isdigit() or len(d) > 2:
        raise CodecError("not a date in this format")
    if mo.isdigit() and len(mo) > 2:
        raise CodecError("not a date in this format")
    return _iso(int(y), _month(codec, mo), int(d))


def decode_scalar(codec: Codec, text: Any) -> Any:
    """The value one displayed text shows, by a scalar codec."""

    text = _need_str(text)
    kind = codec.kind
    if kind not in SCALAR_KINDS:
        raise CodecError(f"codec {kind!r} does not read a single text")
    if kind == "text":
        return _collapse(text)
    if kind == "fold":
        return _collapse(text).casefold()
    if kind == "number":
        return _number(codec, text, affixes=codec.affixes)
    if kind == "money":
        amount = _number(codec, text, affixes=codec.affixes)
        minor = amount * (10**codec.decimals)
        if minor != minor.to_integral_value():
            raise CodecError("more decimal places than the currency has")
        return int(minor)
    if kind == "unit":
        body = _collapse(text)
        for unit in sorted(codec.units, key=len, reverse=True):
            if unit and body.endswith(unit):
                return _number(codec, body.removesuffix(unit), affixes=())
        raise CodecError("not followed by a unit this codec knows")
    if kind == "date":
        return _date(codec, text)
    if kind == "bool":
        folded = _collapse(text).casefold()
        yes = {t.casefold() for t in codec.true_tokens}
        no = {t.casefold() for t in codec.false_tokens}
        if yes & no:
            raise CodecError("a token is both true and false")
        if folded in yes:
            return True
        if folded in no:
            return False
        raise CodecError("not a yes or no this codec knows")
    # enum
    folded = _collapse(text).casefold()
    hits = {
        value for shown, value in codec.table if _collapse(shown).casefold() == folded
    }
    if len(hits) != 1:
        raise CodecError("not an entry this codec's table holds")
    return hits.pop()


def canonical(codec: Codec, expected: Any) -> Any:
    """The expected value in the form :func:`decode_scalar` returns.

    Strict about types: ``True`` is not 1, ``"1250"`` is not 1250 minor units,
    and a float is refused (a pack stores amounts as integers or decimal
    strings).
    """

    kind = codec.kind
    if kind in ("text", "fold", "enum"):
        if not isinstance(expected, str):
            raise CodecError("the expected value is not text")
        if kind == "enum":
            return expected
        return _collapse(expected) if kind == "text" else _collapse(expected).casefold()
    if kind in ("number", "unit"):
        if isinstance(expected, bool):
            raise CodecError("the expected value is a bool, not a number")
        if isinstance(expected, int):
            return Decimal(expected)
        if isinstance(expected, str) and _PLAIN_NUMBER.match(expected):
            return Decimal(expected)
        raise CodecError("the expected value is not a number")
    if kind == "money":
        if isinstance(expected, int) and not isinstance(expected, bool):
            return expected
        raise CodecError("the expected amount is not an integer in minor units")
    if kind in ("date", "split_date"):
        m = _ISO.match(expected) if isinstance(expected, str) else None
        if not m:
            raise CodecError("the expected date is not an ISO date")
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    if kind == "bool":
        if not isinstance(expected, bool):
            raise CodecError("the expected value is not a bool")
        return expected
    raise CodecError(f"codec {kind!r} has no single expected value")


def decode(
    codec: Codec, match: Mapping[str, Any], keys: tuple[str, ...]
) -> dict[str, Any]:
    """Each key's value in one match, or the :class:`CodecError` that stopped it.

    A scalar codec reads the match's ``text`` part as its one key. A
    ``split_date`` codec reads ``year``, ``month`` and ``day`` as its one key.
    A ``template`` codec reads ``text`` with ``pattern`` (the whole text) and
    each part's group as that part's key; when the pattern doesn't match,
    every key is an error. A match with parts the codec doesn't read is an
    error too, so a stray part can't be ignored.
    """

    kind = codec.kind
    if kind == "split_date":
        names = {"year", "month", "day"}
        if len(keys) != 1 or set(match) != names:
            return dict.fromkeys(keys, CodecError("not the parts of a split date"))
        try:
            value: Any = _split(codec, match["year"], match["month"], match["day"])
        except CodecError as exc:
            value = exc
        return {keys[0]: value}
    if set(match) != {"text"}:
        return dict.fromkeys(keys, CodecError("the match is not one displayed text"))
    if kind == "template":
        if set(keys) != set(codec.keys()) or len(codec.keys()) != len(keys):
            return dict.fromkeys(keys, CodecError("template keys do not match"))
        if not all(p.codec.kind in SCALAR_KINDS for p in codec.parts):
            return dict.fromkeys(keys, CodecError("a template part is not scalar"))
        try:
            text = _collapse(_need_str(match["text"]))
            m = re.fullmatch(codec.pattern, text) if codec.pattern else None
        except (CodecError, re.error):
            m = None
        if m is None:
            return dict.fromkeys(keys, CodecError("the text does not fit the template"))
        out: dict[str, Any] = {}
        for part in codec.parts:
            try:
                out[part.key] = decode_scalar(part.codec, m.group(part.group))
            except (CodecError, IndexError) as exc:
                out[part.key] = CodecError(str(exc))
        return out
    if len(keys) != 1:
        return dict.fromkeys(keys, CodecError("a scalar codec shows one key"))
    try:
        return {keys[0]: decode_scalar(codec, match["text"])}
    except CodecError as exc:
        return {keys[0]: exc}
