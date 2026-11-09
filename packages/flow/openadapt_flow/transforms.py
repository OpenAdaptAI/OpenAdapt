"""A small, safe expression language for data transforms between steps.

The IR already substitutes parameters, checks guards and walks worklists. It
could not compute a value mid-run: match a provider name to the closest
dropdown option, add 30 days to a date, or split and join codes. This module
adds a tiny language for that.

It is safe by construction, not by sandboxing Python:

* Its own tokenizer and parser. Nothing is passed to ``eval``, ``exec``,
  ``compile`` or :mod:`ast`.
* No attribute access, indexing syntax, imports, assignment, lambdas or
  comprehensions. A name is a run parameter and nothing else; a call must name
  a function in the closed standard library below.
* Typed. The whole expression is type-checked before anything runs. There is
  no truthiness and no implicit conversion.
* Total and bounded. Source size, node count, nesting depth, string length,
  list length and integer size are capped. Every function either returns a
  value or refuses; none loops over unbounded input.
* Deterministic. No clock, no randomness, no environment, no I/O, no locale.
* Fail closed. Anything outside the language raises :class:`ExpressionRefused`
  with a closed :class:`RefusalCode`. A fuzzy match below its explicit
  threshold, or tied between two options, refuses rather than guessing.

Values computed by :func:`apply_transforms` come back with one
:class:`TransformRecord` each, for the run receipt.

Standard library (``s`` string, ``i`` int, ``d`` date, ``b`` bool, ``l`` list
of strings; ``LIT`` marks an argument that must be a literal):

* strings: ``lower(s)``, ``upper(s)``, ``trim(s)``, ``collapse_spaces(s)``,
  ``length(s)``, ``replace(s, s, s)``, ``substring(s, i, i)``,
  ``starts_with(s, s)``, ``ends_with(s, s)``, ``contains(s, s)``,
  ``pad_left(s, i, s)``, ``text(x)``, ``to_int(s)``
* lists: ``split(s, s)``, ``join(l, s)``, ``at(l, i)``, ``count(l)``
* dates: ``date(s)`` (ISO ``YYYY-MM-DD``), ``parse_date(s, LIT)``,
  ``format_date(d, LIT)``, ``add_days(d, i)``, ``add_months(d, i)``,
  ``days_between(d, d)``, ``year(d)``, ``month(d)``, ``day(d)``
* regex: ``matches(s, LIT)``, ``extract(s, LIT, LIT)`` (full match only, from
  a conservative pattern subset)
* fuzzy: ``fuzzy_match(s, l, LIT)`` with a threshold in ``(0, 1]``. It
  returns the one option that matches exactly after case and space folding,
  or else the one option whose similarity clears the threshold. None, or more
  than one, refuses.
* control: ``if_else(b, x, x)``

Operators: ``+`` (string + string, int + int), ``-`` and ``*`` (ints),
unary ``-``, ``== !=`` (same type), ``< <= > >=`` (ints or dates), ``and``,
``or``, ``not`` (bools only), parentheses and ``[a, b]`` list literals.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import difflib
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

TRANSFORM_RECORD_SCHEMA = "openadapt.param-transform-record/v1"
#: The language this module implements. Anything that stores an expression
#: (a transform, a generated effect model) binds this and
#: :func:`operator_set_sha256`, so a library change is a new, reviewed version.
LANGUAGE_VERSION = "openadapt.expr/v1"

#: Hard bounds. They are part of the language: an expression that needs more
#: is refused, never truncated.
MAX_SOURCE_CHARS = 2_000
MAX_NODES = 256
MAX_DEPTH = 24
MAX_STRING_CHARS = 10_000
MAX_LIST_ITEMS = 1_000
MAX_INT_ABS = 10**12
MAX_TRANSFORMS = 64
MAX_PATTERN_CHARS = 200
MAX_REGEX_INPUT_CHARS = 512
MAX_UNBOUNDED_QUANTIFIERS = 2
MAX_VARIABLE_QUANTIFIERS = 4
MAX_FUZZY_CHARS = 256
MAX_FUZZY_OPTIONS = 500
MAX_REPEAT_COUNT = 100
#: Upper bound on fuzzy_match() work: the sum over options of
#: len(normalized query) * len(normalized option), the cost of difflib's
#: quadratic match.
MAX_FUZZY_WORK = 2_000_000
#: Deterministic evaluation budget: one unit per node evaluated, plus the
#: estimated cost of each regex and fuzzy call. Running out is a refusal.
MAX_EVAL_WORK = 4_000_000
#: Wall-clock backstop, checked between nodes. It never decides a pass: a
#: slow evaluation is refused, so the worst case is "unknown", not a hang
#: that a caller treats as success.
MAX_EVAL_SECONDS = 2.0
MAX_PARAMS = 1_000
# One model for admission before potentially expanding operations. These are
# conservative work units, not a promise about elapsed time on a given host.
WORK_MODEL = "bounded-branches-and-text/v1"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PARAM_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"


class RefusalCode(str, Enum):
    """Why an expression was refused. Closed: callers may switch on it."""

    SYNTAX = "syntax"
    UNKNOWN_NAME = "unknown_name"
    UNKNOWN_FUNCTION = "unknown_function"
    TYPE = "type"
    BOUNDS = "bounds"
    UNSAFE_PATTERN = "unsafe_pattern"
    INVALID_VALUE = "invalid_value"
    NO_MATCH = "no_match"
    BELOW_THRESHOLD = "below_threshold"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"


class ExpressionRefused(Exception):
    """The expression is outside the language, or its value can't be computed.

    Nothing was produced. The message never repeats parameter values.
    """

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.reason = message


def _refuse(code: RefusalCode, message: str) -> ExpressionRefused:
    return ExpressionRefused(code, message)


class ExprType(str, Enum):
    STRING = "string"
    INT = "int"
    NUMBER = "number"
    BOOL = "bool"
    DATE = "date"
    LIST = "list"


Value = Union[str, int, float, bool, _dt.date, tuple]


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_KEYWORDS = {"and", "or", "not", "true", "false"}
_TWO_CHAR_OPS = {"==", "!=", "<=", ">="}
_ONE_CHAR_OPS = set("+-*()[],<>")
_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t"}


@dataclass(frozen=True)
class _Tok:
    kind: str  # STR INT NUM IDENT KW OP EOF
    text: str
    value: Any
    pos: int


def _tokenize(source: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "'\"":
            quote, j, out = c, i + 1, []
            while True:
                if j >= n:
                    raise _refuse(RefusalCode.SYNTAX, f"unterminated string at {i}")
                ch = source[j]
                if ch == quote:
                    break
                if ch == "\\":
                    if j + 1 >= n:
                        raise _refuse(RefusalCode.SYNTAX, f"unterminated string at {i}")
                    # Any other backslash pair stays as written, so a regex
                    # such as '\d{6}' reads naturally.
                    nxt = source[j + 1]
                    out.append(_ESCAPES.get(nxt, "\\" + nxt))
                    j += 2
                    continue
                out.append(ch)
                j += 1
            s = "".join(out)
            if len(s) > MAX_STRING_CHARS:
                raise _refuse(RefusalCode.BOUNDS, "string literal too long")
            toks.append(_Tok("STR", source[i : j + 1], s, i))
            i = j + 1
            continue
        if c.isdigit() and c.isascii():
            j = i
            while j < n and source[j].isascii() and source[j].isdigit():
                j += 1
            if j < n and source[j] == ".":
                k = j + 1
                while k < n and source[k].isascii() and source[k].isdigit():
                    k += 1
                if k == j + 1:
                    raise _refuse(
                        RefusalCode.SYNTAX,
                        f"'.' at {j}: attribute access is not part of the language",
                    )
                text = source[i:k]
                if len(text) > 20:
                    raise _refuse(RefusalCode.BOUNDS, "number literal too long")
                toks.append(_Tok("NUM", text, float(text), i))
                i = k
                continue
            text = source[i:j]
            if len(text) > 13 or int(text) > MAX_INT_ABS:
                raise _refuse(RefusalCode.BOUNDS, "integer literal too large")
            toks.append(_Tok("INT", text, int(text), i))
            i = j
            continue
        m = _IDENT.match(source, i)
        if m:
            word = m.group(0)
            if word.startswith("__"):
                raise _refuse(RefusalCode.SYNTAX, f"dunder name at {i} is not allowed")
            if len(word) > 128:
                raise _refuse(RefusalCode.BOUNDS, "name too long")
            toks.append(_Tok("KW" if word in _KEYWORDS else "IDENT", word, word, i))
            i = m.end()
            continue
        two = source[i : i + 2]
        if two in _TWO_CHAR_OPS:
            toks.append(_Tok("OP", two, two, i))
            i += 2
            continue
        if c in _ONE_CHAR_OPS:
            if two in ("**", "<<", ">>"):
                raise _refuse(RefusalCode.SYNTAX, f"operator {two!r} at {i}")
            toks.append(_Tok("OP", c, c, i))
            i += 1
            continue
        if c == ".":
            raise _refuse(
                RefusalCode.SYNTAX,
                f"'.' at {i}: attribute access is not part of the language",
            )
        raise _refuse(RefusalCode.SYNTAX, f"unexpected character {c!r} at {i}")
    toks.append(_Tok("EOF", "", None, n))
    return toks


# --------------------------------------------------------------------------
# AST and parser
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Lit:
    value: Value
    type: ExprType


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple["Node", ...]


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Node"


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class ListLit:
    items: tuple["Node", ...]


Node = Union[Lit, Ref, Call, Unary, Binary, ListLit]

_CMP_OPS = {"==", "!=", "<", "<=", ">", ">="}


class _Parser:
    def __init__(self, toks: list[_Tok]) -> None:
        self.toks = toks
        self.i = 0
        self.nodes = 0
        self.depth = 0

    def peek(self) -> _Tok:
        return self.toks[self.i]

    def take(self) -> _Tok:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, text: str) -> None:
        tok = self.take()
        if tok.text != text or tok.kind not in ("OP", "KW"):
            raise _refuse(
                RefusalCode.SYNTAX, f"expected {text!r} at {tok.pos}, got {tok.text!r}"
            )

    def node(self, n: Node) -> Node:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _refuse(RefusalCode.BOUNDS, f"more than {MAX_NODES} nodes")
        return n

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise _refuse(RefusalCode.BOUNDS, f"nested deeper than {MAX_DEPTH}")

    def leave(self) -> None:
        self.depth -= 1

    def parse(self) -> Node:
        n = self.or_()
        tok = self.peek()
        if tok.kind != "EOF":
            raise _refuse(RefusalCode.SYNTAX, f"unexpected {tok.text!r} at {tok.pos}")
        return n

    def _is(self, kind: str, text: str) -> bool:
        tok = self.peek()
        return tok.kind == kind and tok.text == text

    def or_(self) -> Node:
        self.enter()
        n = self.and_()
        while self._is("KW", "or"):
            self.take()
            n = self.node(Binary("or", n, self.and_()))
        self.leave()
        return n

    def and_(self) -> Node:
        n = self.not_()
        while self._is("KW", "and"):
            self.take()
            n = self.node(Binary("and", n, self.not_()))
        return n

    def not_(self) -> Node:
        if self._is("KW", "not"):
            self.take()
            self.enter()
            n = self.node(Unary("not", self.not_()))
            self.leave()
            return n
        return self.cmp()

    def cmp(self) -> Node:
        n = self.add()
        tok = self.peek()
        if tok.kind == "OP" and tok.text in _CMP_OPS:
            self.take()
            n = self.node(Binary(tok.text, n, self.add()))
            nxt = self.peek()
            if nxt.kind == "OP" and nxt.text in _CMP_OPS:
                raise _refuse(RefusalCode.SYNTAX, f"chained comparison at {nxt.pos}")
        return n

    def add(self) -> Node:
        n = self.mul()
        while self.peek().kind == "OP" and self.peek().text in ("+", "-"):
            op = self.take().text
            n = self.node(Binary(op, n, self.mul()))
        return n

    def mul(self) -> Node:
        n = self.unary()
        while self._is("OP", "*"):
            self.take()
            n = self.node(Binary("*", n, self.unary()))
        return n

    def unary(self) -> Node:
        if self._is("OP", "-"):
            self.take()
            self.enter()
            n = self.node(Unary("-", self.unary()))
            self.leave()
            return n
        return self.primary()

    def primary(self) -> Node:
        tok = self.take()
        if tok.kind == "STR":
            return self.node(Lit(tok.value, ExprType.STRING))
        if tok.kind == "INT":
            return self.node(Lit(tok.value, ExprType.INT))
        if tok.kind == "NUM":
            return self.node(Lit(tok.value, ExprType.NUMBER))
        if tok.kind == "KW" and tok.text in ("true", "false"):
            return self.node(Lit(tok.text == "true", ExprType.BOOL))
        if tok.kind == "IDENT":
            if self._is("OP", "("):
                self.take()
                self.enter()
                args = self._items(")")
                self.leave()
                return self.node(Call(tok.text, args))
            return self.node(Ref(tok.text))
        if tok.kind == "OP" and tok.text == "(":
            self.enter()
            n = self.or_()
            self.leave()
            self.expect(")")
            return n
        if tok.kind == "OP" and tok.text == "[":
            self.enter()
            items = self._items("]")
            self.leave()
            return self.node(ListLit(items))
        raise _refuse(
            RefusalCode.SYNTAX, f"unexpected {tok.text or 'end'!r} at {tok.pos}"
        )

    def _items(self, close: str) -> tuple[Node, ...]:
        items: list[Node] = []
        if self._is("OP", close):
            self.take()
            return ()
        while True:
            items.append(self.or_())
            tok = self.take()
            if tok.kind == "OP" and tok.text == close:
                return tuple(items)
            if not (tok.kind == "OP" and tok.text == ","):
                raise _refuse(
                    RefusalCode.SYNTAX,
                    f"expected ',' or {close!r} at {tok.pos}, got {tok.text!r}",
                )


def parse(source: str) -> Node:
    """Parse ``source`` into an AST, or refuse. Does not type-check."""

    if not isinstance(source, str):
        raise _refuse(RefusalCode.TYPE, "expression must be a string")
    if len(source) > MAX_SOURCE_CHARS:
        raise _refuse(RefusalCode.BOUNDS, f"longer than {MAX_SOURCE_CHARS} chars")
    _check_text(source, "expression")
    if not source.strip():
        raise _refuse(RefusalCode.SYNTAX, "empty expression")
    return _Parser(_tokenize(source)).parse()


# --------------------------------------------------------------------------
# Bounded value helpers
# --------------------------------------------------------------------------


def _check_str(s: str) -> str:
    if len(s) > MAX_STRING_CHARS:
        raise _refuse(RefusalCode.BOUNDS, f"string longer than {MAX_STRING_CHARS}")
    return s


def _check_text(s: str, what: str) -> str:
    """Refuse text that is not valid Unicode (a lone surrogate, say), so it
    can't crash hashing or encoding later with an untyped exception."""

    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise _refuse(
            RefusalCode.INVALID_VALUE, f"{what} is not valid Unicode"
        ) from None
    return s


def _check_int(i: int) -> int:
    if abs(i) > MAX_INT_ABS:
        raise _refuse(RefusalCode.BOUNDS, f"integer beyond +/-{MAX_INT_ABS}")
    return i


def _check_list(items: tuple) -> tuple:
    if len(items) > MAX_LIST_ITEMS:
        raise _refuse(RefusalCode.BOUNDS, f"list longer than {MAX_LIST_ITEMS}")
    return items


# --------------------------------------------------------------------------
# Dates: a closed, locale-free format mini-language
# --------------------------------------------------------------------------

_DATE_TOKENS = ("YYYY", "MM", "DD", "M", "D")
_DATE_SEPARATORS = set("-/. ")
_DATE_TOKEN_RE = {
    "YYYY": r"(\d{4})",
    "MM": r"(\d{2})",
    "DD": r"(\d{2})",
    "M": r"(\d{1,2})",
    "D": r"(\d{1,2})",
}


def _date_format(fmt: str) -> list[str]:
    parts: list[str] = []
    i = 0
    while i < len(fmt):
        for tok in _DATE_TOKENS:
            if fmt.startswith(tok, i):
                parts.append(tok)
                i += len(tok)
                break
        else:
            if fmt[i] in _DATE_SEPARATORS:
                parts.append(fmt[i])
                i += 1
            else:
                raise _refuse(
                    RefusalCode.INVALID_VALUE,
                    "date format may use only YYYY, MM, DD, M, D and - / . space",
                )
    fields = [p for p in parts if p in _DATE_TOKENS]
    kinds = sorted(p[0] for p in fields)
    if kinds != ["D", "M", "Y"]:
        raise _refuse(
            RefusalCode.INVALID_VALUE,
            "date format needs exactly one year, one month and one day",
        )
    return parts


def _parse_date(s: str, fmt: str) -> _dt.date:
    parts = _date_format(fmt)
    pattern = "".join(_DATE_TOKEN_RE.get(p, re.escape(p)) for p in parts)
    m = re.fullmatch(pattern, s.strip(), flags=re.ASCII)
    if not m:
        raise _refuse(RefusalCode.INVALID_VALUE, f"value is not a date in {fmt!r}")
    got = dict(zip([p[0] for p in parts if p in _DATE_TOKENS], m.groups()))
    try:
        return _dt.date(int(got["Y"]), int(got["M"]), int(got["D"]))
    except ValueError:
        raise _refuse(RefusalCode.INVALID_VALUE, "no such calendar date") from None


def _format_date(d: _dt.date, fmt: str) -> str:
    out = []
    for p in _date_format(fmt):
        if p == "YYYY":
            out.append(f"{d.year:04d}")
        elif p == "MM":
            out.append(f"{d.month:02d}")
        elif p == "DD":
            out.append(f"{d.day:02d}")
        elif p == "M":
            out.append(str(d.month))
        elif p == "D":
            out.append(str(d.day))
        else:
            out.append(p)
    return "".join(out)


def _add_days(d: _dt.date, n: int) -> _dt.date:
    try:
        return d + _dt.timedelta(days=n)
    except OverflowError:
        raise _refuse(RefusalCode.BOUNDS, "date out of range") from None


def _add_months(d: _dt.date, n: int) -> _dt.date:
    """Calendar months; a day past the month's end clamps to its last day."""

    total = d.year * 12 + (d.month - 1) + n
    year, month0 = divmod(total, 12)
    if not 1 <= year <= 9999:
        raise _refuse(RefusalCode.BOUNDS, "date out of range")
    last = calendar.monthrange(year, month0 + 1)[1]
    return _dt.date(year, month0 + 1, min(d.day, last))


# --------------------------------------------------------------------------
# Regex: a conservative, fail-closed subset
# --------------------------------------------------------------------------


_REPEAT = re.compile(r"\{(\d*)(,(\d*))?\}[?+]?")


def _quantifier_at(p: str, i: int) -> tuple[int, bool, bool]:
    """Return (length, variable, unbounded) of a quantifier at ``p[i]``, or
    (0, False, False) when there is none."""

    if i >= len(p):
        return 0, False, False
    c = p[i]
    if c in "*+":
        n = 2 if p[i + 1 : i + 2] in ("?", "+") else 1
        return n, True, True
    if c == "?":
        n = 2 if p[i + 1 : i + 2] in ("?", "+") else 1
        return n, True, False
    if c == "{":
        # Match every brace form ``re`` reads as a repeat, with no digit cap:
        # ``{1000}``, ``{0100}``, ``{,5}`` and ``{,}`` all repeat. ``{}`` is a
        # literal in ``re``.
        m = _REPEAT.match(p, i)
        if not m or (m.group(1) == "" and m.group(2) is None):
            return 0, False, False
        lo = int(m.group(1) or 0)
        if lo > MAX_REPEAT_COUNT:
            raise _refuse(
                RefusalCode.UNSAFE_PATTERN, f"repeat count above {MAX_REPEAT_COUNT}"
            )
        if m.group(2) is None:
            return m.end() - i, False, False
        if m.group(3) in (None, ""):
            return m.end() - i, True, True
        hi = int(m.group(3))
        if hi > MAX_REPEAT_COUNT:
            raise _refuse(
                RefusalCode.UNSAFE_PATTERN, f"repeat bound above {MAX_REPEAT_COUNT}"
            )
        return m.end() - i, hi > lo, False
    return 0, False, False


@dataclass
class _PatternWork:
    """Saturating upper bounds for the existing regex scanner's branches.

    Concatenation multiplies possible backtracking paths; alternation adds
    them; a repeat composes its atom repeatedly. Steps bound a single path.
    Unlike input-length times pattern-length, this accounts for independent
    optional groups, alternatives and nested fixed repeats before calling re.
    """

    paths: int = 1
    alternatives: int = 0
    steps: int = 0

    def append(self, paths: int, steps: int) -> None:
        self.paths = min(MAX_EVAL_WORK + 1, self.paths * paths)
        self.steps = min(MAX_EVAL_WORK + 1, self.steps + steps)

    def branch(self) -> None:
        self.alternatives = min(MAX_EVAL_WORK + 1, self.alternatives + self.paths)
        self.paths = 1

    def finish(self) -> tuple[int, int]:
        return min(MAX_EVAL_WORK + 1, self.alternatives + self.paths), self.steps + 1


def _repeat_work(q: str, size: int, paths: int, steps: int) -> tuple[int, int]:
    if q[0] == "?":
        lo, hi = 0, 1
    elif q[0] in "*+":
        lo, hi = (0 if q[0] == "*" else 1), size + 1
    else:
        match = _REPEAT.match(q)
        assert match is not None
        lo = int(match.group(1) or 0)
        hi = (
            lo
            if match.group(2) is None
            else int(match.group(3))
            if match.group(3)
            else max(lo, size + 1)
        )
    # The scanner already limits explicit counts. Saturate each arithmetic
    # operation: an attacker cannot turn this estimator into a big-int job.
    limit = MAX_EVAL_WORK + 1
    power, total = 1, int(lo == 0)
    for count in range(1, hi + 1):
        power = min(limit, power * paths)
        if count >= lo:
            total = min(limit, total + power)
        if total == limit:
            break
    # Variable repeat lengths already contribute one path per length. Count
    # the mandatory prefix here rather than charging that traversal twice.
    return max(1, total), min(limit, (steps + 1) * max(1, lo) + 1)


def _check_pattern(
    p: str, *, input_size: Optional[int] = None, ctx: Optional[_Ctx] = None
) -> re.Pattern[str]:
    """Refuse patterns that can backtrack badly: back-references, look-around,
    conditionals, a repeated group that itself repeats or alternates, and more
    than a few variable-length quantifiers."""

    if len(p) > MAX_PATTERN_CHARS:
        raise _refuse(RefusalCode.UNSAFE_PATTERN, "pattern too long")
    # Each open group: [contains a quantifier, contains an alternation].
    stack: list[list[bool]] = [[False, False]]
    costs = [_PatternWork()]
    variable = unbounded = 0
    i = 0
    last_group: Optional[list[bool]] = None
    while i < len(p):
        c = p[i]
        just_closed: Optional[list[bool]] = None
        paths, steps = 1, 1
        if c == "\\":
            nxt = p[i + 1 : i + 2]
            if nxt == "" or nxt.isdigit() or nxt in ("g", "k"):
                raise _refuse(RefusalCode.UNSAFE_PATTERN, "back-reference")
            i += 2
        elif c == "[":
            j = i + 1
            if p[j : j + 1] == "^":
                j += 1
            if p[j : j + 1] == "]":
                j += 1
            while j < len(p) and p[j] != "]":
                j += 2 if p[j] == "\\" else 1
            if j >= len(p):
                raise _refuse(RefusalCode.SYNTAX, "unterminated character class")
            i = j + 1
        elif c == "(":
            if p.startswith("(?", i):
                if p.startswith("(?:", i):
                    i += 3
                elif p.startswith("(?P<", i):
                    end = p.find(">", i)
                    if end < 0:
                        raise _refuse(RefusalCode.SYNTAX, "bad group name")
                    i = end + 1
                else:
                    raise _refuse(
                        RefusalCode.UNSAFE_PATTERN,
                        "look-around, conditionals, flags and named references "
                        "are not allowed",
                    )
            else:
                i += 1
            stack.append([False, False])
            costs.append(_PatternWork())
            continue
        elif c == ")":
            if len(stack) == 1:
                raise _refuse(RefusalCode.SYNTAX, "unbalanced ')'")
            just_closed = stack.pop()
            paths, steps = costs.pop().finish()
            stack[-1][0] = stack[-1][0] or just_closed[0]
            stack[-1][1] = stack[-1][1] or just_closed[1]
            i += 1
        elif c == "|":
            stack[-1][1] = True
            costs[-1].branch()
            i += 1
            continue
        else:
            i += 1
        last_group = just_closed
        qlen, var, unb = _quantifier_at(p, i)
        if qlen:
            if var or unb:
                variable += 1
                unbounded += 1 if unb else 0
            if (
                last_group is not None
                and (last_group[0] or last_group[1])
                and (unb or _repeats_more_than_once(p[i : i + qlen]))
            ):
                raise _refuse(
                    RefusalCode.UNSAFE_PATTERN,
                    "a repeated group may not itself repeat or alternate",
                )
            if var or unb:
                stack[-1][0] = True
            paths, steps = _repeat_work(p[i : i + qlen], input_size or 0, paths, steps)
            i += qlen
        costs[-1].append(paths, steps)
    if len(stack) != 1:
        raise _refuse(RefusalCode.SYNTAX, "unbalanced '('")
    if unbounded > MAX_UNBOUNDED_QUANTIFIERS:
        raise _refuse(
            RefusalCode.UNSAFE_PATTERN,
            f"more than {MAX_UNBOUNDED_QUANTIFIERS} unbounded quantifiers",
        )
    if variable > MAX_VARIABLE_QUANTIFIERS:
        raise _refuse(
            RefusalCode.UNSAFE_PATTERN,
            f"more than {MAX_VARIABLE_QUANTIFIERS} variable quantifiers",
        )
    paths, steps = costs[0].finish()
    # A fixed-repeat explosion is independent of input size. Do not even
    # compile it. Input-dependent backtracking is charged before fullmatch.
    if steps > MAX_EVAL_WORK:
        raise _refuse(
            RefusalCode.UNSAFE_PATTERN, "pattern expansion exceeds work bound"
        )
    if ctx is not None:
        ctx.spend(paths * steps)
    try:
        # ASCII: \d, \w and \s mean the same thing on every interpreter, so
        # a receipt doesn't depend on the Unicode database version.
        return re.compile(p, re.ASCII)
    except re.error:
        raise _refuse(RefusalCode.SYNTAX, "invalid regular expression") from None


def _repeats_more_than_once(q: str) -> bool:
    if q[:1] in ("*", "+"):
        return True
    if q[:1] == "?":
        return False
    m = _REPEAT.match(q)
    if not m:
        return False
    if m.group(2) is None:
        return int(m.group(1) or 0) > 1
    if m.group(3) in (None, ""):
        return True
    return int(m.group(3)) > 1


def _regex_input(s: str) -> str:
    if len(s) > MAX_REGEX_INPUT_CHARS:
        raise _refuse(
            RefusalCode.BOUNDS, f"regex input longer than {MAX_REGEX_INPUT_CHARS}"
        )
    return s


# --------------------------------------------------------------------------
# Fuzzy matching
# --------------------------------------------------------------------------


def _normalize(s: str, ctx: _Ctx) -> str:
    # Canonical ordering can be quadratic in combining sequences. Charge raw
    # work before normalization, then bound the expanded intermediate before
    # case folding, splitting or similarity scoring.
    ctx.spend(max(1, len(s)) ** 2)
    normalized = unicodedata.normalize("NFKC", s)
    if len(normalized) > MAX_FUZZY_CHARS:
        raise _refuse(RefusalCode.BOUNDS, "normalized fuzzy text exceeds bound")
    ctx.spend(3 * len(normalized))
    folded = normalized.casefold()
    if len(folded) > MAX_FUZZY_CHARS:
        raise _refuse(RefusalCode.BOUNDS, "folded fuzzy text exceeds bound")
    return " ".join(folded.split())


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# --------------------------------------------------------------------------
# Standard library
# --------------------------------------------------------------------------

S, I, N, B, D, L = (
    ExprType.STRING,
    ExprType.INT,
    ExprType.NUMBER,
    ExprType.BOOL,
    ExprType.DATE,
    ExprType.LIST,
)


@dataclass(frozen=True)
class _Fn:
    args: tuple[ExprType, ...]
    result: ExprType
    impl: Callable[..., Value]
    #: Argument positions that must be literals (patterns, formats, thresholds).
    literal: frozenset[int] = frozenset()


def _substring(s: str, start: int, end: int) -> str:
    if not 0 <= start <= end <= len(s):
        raise _refuse(RefusalCode.INVALID_VALUE, "substring range out of bounds")
    return s[start:end]


def _replace(s: str, old: str, new: str) -> str:
    if not old:
        raise _refuse(RefusalCode.INVALID_VALUE, "replace needs a non-empty target")
    if new and s.count(old) * max(0, len(new) - len(old)) + len(s) > MAX_STRING_CHARS:
        raise _refuse(RefusalCode.BOUNDS, f"string longer than {MAX_STRING_CHARS}")
    return _check_str(s.replace(old, new))


def _pad_left(s: str, width: int, fill: str) -> str:
    if len(fill) != 1:
        raise _refuse(RefusalCode.INVALID_VALUE, "pad_left fill must be one char")
    if not 0 <= width <= MAX_STRING_CHARS:
        raise _refuse(RefusalCode.BOUNDS, "pad_left width out of range")
    return s.rjust(width, fill)


def _split(s: str, sep: str) -> tuple:
    if not sep:
        raise _refuse(RefusalCode.INVALID_VALUE, "split needs a non-empty separator")
    if s.count(sep) + 1 > MAX_LIST_ITEMS:
        raise _refuse(RefusalCode.BOUNDS, f"list longer than {MAX_LIST_ITEMS}")
    return tuple(s.split(sep))


def _join(items: tuple, sep: str) -> str:
    total = sum(len(x) for x in items) + len(sep) * max(0, len(items) - 1)
    if total > MAX_STRING_CHARS:
        raise _refuse(RefusalCode.BOUNDS, f"string longer than {MAX_STRING_CHARS}")
    return sep.join(items)


def _at(items: tuple, i: int) -> str:
    if not 0 <= i < len(items):
        raise _refuse(RefusalCode.INVALID_VALUE, "list index out of range")
    return str(items[i])


def _to_int(s: str) -> int:
    t = s.strip()
    if not re.fullmatch(r"[+-]?[0-9]{1,13}", t):
        raise _refuse(RefusalCode.INVALID_VALUE, "value is not an integer")
    return _check_int(int(t))


def _text(v: Value) -> str:
    return canonical_text(v)


def _matches(s: str, pattern: str, ctx: Optional[_Ctx] = None) -> bool:
    ctx = ctx if ctx is not None else _Ctx(params={})
    s = _regex_input(s)
    result = _check_pattern(pattern, input_size=len(s), ctx=ctx).fullmatch(s)
    ctx.spend(0)
    return result is not None


def _extract(s: str, pattern: str, group: int, ctx: Optional[_Ctx] = None) -> str:
    ctx = ctx if ctx is not None else _Ctx(params={})
    s = _regex_input(s)
    rx = _check_pattern(pattern, input_size=len(s), ctx=ctx)
    if not 0 <= group <= rx.groups:
        raise _refuse(RefusalCode.INVALID_VALUE, "no such regex group")
    m = rx.fullmatch(s)
    ctx.spend(0)
    if m is None or m.group(group) is None:
        raise _refuse(RefusalCode.NO_MATCH, "pattern does not match the value")
    return m.group(group)


_STDLIB: dict[str, _Fn] = {
    "lower": _Fn((S,), S, lambda s: _check_str(s.lower())),
    "upper": _Fn((S,), S, lambda s: _check_str(s.upper())),
    "trim": _Fn((S,), S, lambda s: s.strip()),
    "collapse_spaces": _Fn((S,), S, lambda s: " ".join(s.split())),
    "length": _Fn((S,), I, lambda s: len(s)),
    "replace": _Fn((S, S, S), S, _replace),
    "substring": _Fn((S, I, I), S, _substring),
    "starts_with": _Fn((S, S), B, lambda s, p: s.startswith(p)),
    "ends_with": _Fn((S, S), B, lambda s, p: s.endswith(p)),
    "contains": _Fn((S, S), B, lambda s, p: p in s),
    "pad_left": _Fn((S, I, S), S, _pad_left),
    "to_int": _Fn((S,), I, _to_int),
    "split": _Fn((S, S), L, _split),
    "join": _Fn((L, S), S, _join),
    "at": _Fn((L, I), S, _at),
    "count": _Fn((L,), I, lambda items: len(items)),
    "date": _Fn((S,), D, lambda s: _parse_date(s, "YYYY-MM-DD")),
    "parse_date": _Fn((S, S), D, _parse_date, frozenset({1})),
    "format_date": _Fn((D, S), S, _format_date, frozenset({1})),
    "add_days": _Fn((D, I), D, _add_days),
    "add_months": _Fn((D, I), D, _add_months),
    "days_between": _Fn((D, D), I, lambda a, b: (b - a).days),
    "year": _Fn((D,), I, lambda d: d.year),
    "month": _Fn((D,), I, lambda d: d.month),
    "day": _Fn((D,), I, lambda d: d.day),
    "matches": _Fn((S, S), B, _matches, frozenset({1})),
    "extract": _Fn((S, S, I), S, _extract, frozenset({1, 2})),
    # fuzzy_match, text and if_else are typed specially in the checker.
}
_SPECIAL = {"fuzzy_match", "text", "if_else"}


def stdlib_names() -> list[str]:
    """Every callable name. Anything else is refused as ``unknown_function``."""

    return sorted(set(_STDLIB) | _SPECIAL)


# --------------------------------------------------------------------------
# Type checker
# --------------------------------------------------------------------------


def _type_name(t: ExprType) -> str:
    return t.value


def _accepts(expected: ExprType, got: ExprType) -> bool:
    return expected == got or (expected == N and got == I)


def check(node: Node, names: Optional[set[str]] = None) -> ExprType:
    """Type-check ``node``. Every parameter reference is a string.

    ``names`` is the set of parameters that exist. ``None`` skips the existence
    check (used when a transform is declared, before the run's params exist).
    """

    if isinstance(node, Lit):
        return node.type
    if isinstance(node, Ref):
        if names is not None and node.name not in names:
            raise _refuse(RefusalCode.UNKNOWN_NAME, f"no parameter named {node.name!r}")
        return S
    if isinstance(node, ListLit):
        for item in node.items:
            if check(item, names) != S:
                raise _refuse(RefusalCode.TYPE, "list items must be strings")
        if len(node.items) > MAX_LIST_ITEMS:
            raise _refuse(RefusalCode.BOUNDS, "list literal too long")
        return L
    if isinstance(node, Unary):
        t = check(node.operand, names)
        if node.op == "not":
            if t != B:
                raise _refuse(RefusalCode.TYPE, "'not' needs a bool")
            return B
        if t != I:
            raise _refuse(RefusalCode.TYPE, "unary '-' needs an int")
        return I
    if isinstance(node, Binary):
        lt, rt = check(node.left, names), check(node.right, names)
        op = node.op
        if op in ("and", "or"):
            if lt != B or rt != B:
                raise _refuse(RefusalCode.TYPE, f"'{op}' needs bools")
            return B
        if op in ("==", "!="):
            if lt != rt or lt in (N, L):
                raise _refuse(
                    RefusalCode.TYPE,
                    f"'{op}' needs two values of the same scalar type, got "
                    f"{_type_name(lt)} and {_type_name(rt)}",
                )
            return B
        if op in ("<", "<=", ">", ">="):
            if lt != rt or lt not in (I, D):
                raise _refuse(RefusalCode.TYPE, f"'{op}' compares ints or dates")
            return B
        if op == "+":
            if lt == rt and lt in (S, I):
                return lt
            raise _refuse(
                RefusalCode.TYPE,
                f"'+' joins two strings or adds two ints, got {_type_name(lt)} "
                f"and {_type_name(rt)}",
            )
        if op in ("-", "*"):
            if lt == rt == I:
                return I
            raise _refuse(RefusalCode.TYPE, f"'{op}' needs two ints")
        raise _refuse(RefusalCode.SYNTAX, f"operator {op!r}")  # pragma: no cover
    if isinstance(node, Call):
        return _check_call(node, names)
    raise _refuse(RefusalCode.SYNTAX, "unknown node")  # pragma: no cover


def _arity(node: Call, n: int) -> None:
    if len(node.args) != n:
        raise _refuse(
            RefusalCode.TYPE, f"{node.fn}() takes {n} arguments, got {len(node.args)}"
        )


def _check_call(node: Call, names: Optional[set[str]]) -> ExprType:
    fn = node.fn
    if fn == "text":
        _arity(node, 1)
        t = check(node.args[0], names)
        if t in (L, N):
            raise _refuse(RefusalCode.TYPE, "text() takes a string, int, bool or date")
        return S
    if fn == "if_else":
        _arity(node, 3)
        if check(node.args[0], names) != B:
            raise _refuse(RefusalCode.TYPE, "if_else() needs a bool condition")
        a, b = check(node.args[1], names), check(node.args[2], names)
        if a != b:
            raise _refuse(RefusalCode.TYPE, "if_else() branches must share a type")
        return a
    if fn == "fuzzy_match":
        _arity(node, 3)
        if check(node.args[0], names) != S or check(node.args[1], names) != L:
            raise _refuse(
                RefusalCode.TYPE, "fuzzy_match(value, options, threshold) types"
            )
        thr = node.args[2]
        if not isinstance(thr, Lit) or thr.type not in (I, N):
            raise _refuse(
                RefusalCode.TYPE, "fuzzy_match() threshold must be a number literal"
            )
        if not 0.0 < float(thr.value) <= 1.0:  # type: ignore[arg-type]
            raise _refuse(
                RefusalCode.INVALID_VALUE, "fuzzy_match() threshold must be in (0, 1]"
            )
        return S
    spec = _STDLIB.get(fn)
    if spec is None:
        raise _refuse(RefusalCode.UNKNOWN_FUNCTION, f"{fn}() is not in the library")
    _arity(node, len(spec.args))
    for idx, (want, arg) in enumerate(zip(spec.args, node.args)):
        got = check(arg, names)
        if not _accepts(want, got):
            raise _refuse(
                RefusalCode.TYPE,
                f"{fn}() argument {idx + 1} must be {_type_name(want)}, "
                f"got {_type_name(got)}",
            )
        if idx in spec.literal and not isinstance(arg, Lit):
            raise _refuse(
                RefusalCode.TYPE, f"{fn}() argument {idx + 1} must be a literal"
            )
    # Validate literal patterns and formats up front, before any value exists.
    if fn in ("matches", "extract"):
        _check_pattern(node.args[1].value)  # type: ignore[union-attr,arg-type]
    if fn in ("parse_date", "format_date"):
        _date_format(node.args[1].value)  # type: ignore[union-attr,arg-type]
    return spec.result


def refs(node: Node) -> list[str]:
    """Parameter names read by ``node``, sorted, without repeats."""

    out: set[str] = set()

    def walk(n: Node) -> None:
        if isinstance(n, Ref):
            out.add(n.name)
        elif isinstance(n, (Call, ListLit)):
            for a in n.args if isinstance(n, Call) else n.items:
                walk(a)
        elif isinstance(n, Unary):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return sorted(out)


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------


@dataclass
class _Ctx:
    params: Mapping[str, str]
    matches: list[dict[str, Any]] = field(default_factory=list)
    work: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_EVAL_SECONDS)

    def spend(self, units: int) -> None:
        self.work += units
        if self.work > MAX_EVAL_WORK:
            raise _refuse(RefusalCode.BOUNDS, "evaluation work budget exceeded")
        if time.monotonic() > self.deadline:
            raise _refuse(RefusalCode.BOUNDS, "evaluation time budget exceeded")


def _params(params: Mapping[str, str], ctx: _Ctx) -> dict[str, str]:
    if len(params) > MAX_PARAMS:
        raise _refuse(RefusalCode.BOUNDS, "too many parameters")
    checked: dict[str, str] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise _refuse(RefusalCode.TYPE, "parameter names and values must be text")
        if len(name) > 128:
            raise _refuse(RefusalCode.BOUNDS, "parameter name too long")
        _check_str(value)  # Reject size before allocating a UTF-8 copy.
        ctx.spend(len(name) + len(value) + 1)
        _check_text(name, "parameter name")
        checked[name] = _check_text(value, "parameter")
    return checked


def _value_work(value: Value) -> int:
    if isinstance(value, str):
        return len(value) + 1
    if isinstance(value, tuple):
        return 1 + sum(_value_work(item) for item in value)
    return 1


def _call_work(name: str, args: list[Value]) -> int:
    work = 1 + sum(_value_work(arg) for arg in args)
    if name in {"contains", "replace", "split"}:
        # Include comparison work, not just the output size. Values have
        # already been bounded; saturating admission happens before the call.
        work *= max(1, _value_work(args[1]))
    elif name in {"lower", "upper"}:
        work *= 3  # Unicode full case mappings can expand a scalar.
    return work


def _as_bool(v: Value) -> bool:
    # The checker already proved this; re-check so a checker bug fails closed.
    if not isinstance(v, bool):
        raise _refuse(RefusalCode.TYPE, "expected a bool")
    return v


def _as_int(v: Value) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise _refuse(RefusalCode.TYPE, "expected an int")
    return v


def _compare(op: str, left: Value, right: Value) -> bool:
    if isinstance(left, _dt.date) and isinstance(right, _dt.date):
        lk, rk = left.toordinal(), right.toordinal()
    else:
        lk, rk = _as_int(left), _as_int(right)
    if op == "<":
        return lk < rk
    if op == "<=":
        return lk <= rk
    if op == ">":
        return lk > rk
    return lk >= rk


def _eval(node: Node, ctx: _Ctx) -> Value:
    ctx.spend(1)
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Ref):
        v = ctx.params[node.name]
        if not isinstance(v, str):
            raise _refuse(RefusalCode.TYPE, f"parameter {node.name!r} is not text")
        _check_str(v)
        ctx.spend(len(v))
        return _check_text(v, "parameter")
    if isinstance(node, ListLit):
        return _check_list(tuple(_eval(x, ctx) for x in node.items))
    if isinstance(node, Unary):
        operand = _eval(node.operand, ctx)
        if node.op == "not":
            return not _as_bool(operand)
        return _check_int(-_as_int(operand))
    if isinstance(node, Binary):
        op = node.op
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)  # strict: both sides always evaluate
        ctx.spend(_value_work(left) + _value_work(right))
        if op == "and":
            return _as_bool(left) and _as_bool(right)
        if op == "or":
            return _as_bool(left) or _as_bool(right)
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op in ("<", "<=", ">", ">="):
            return _compare(op, left, right)
        if op == "+" and isinstance(left, str) and isinstance(right, str):
            if len(left) + len(right) > MAX_STRING_CHARS:
                raise _refuse(
                    RefusalCode.BOUNDS, f"string longer than {MAX_STRING_CHARS}"
                )
            return left + right
        li, ri = _as_int(left), _as_int(right)
        if op == "+":
            return _check_int(li + ri)
        if op == "-":
            return _check_int(li - ri)
        if op == "*":
            return _check_int(li * ri)
    if isinstance(node, Call):
        args = [_eval(a, ctx) for a in node.args]
        ctx.spend(_call_work(node.fn, args))
        if node.fn == "text":
            return _text(args[0])
        if node.fn == "if_else":
            return args[1] if _as_bool(args[0]) else args[2]
        if node.fn == "fuzzy_match":
            return _fuzzy(args[0], args[1], float(args[2]), ctx)  # type: ignore[arg-type]
        if node.fn == "matches":
            return _matches(args[0], args[1], ctx)  # type: ignore[arg-type]
        if node.fn == "extract":
            return _extract(args[0], args[1], args[2], ctx)  # type: ignore[arg-type]
        value = _STDLIB[node.fn].impl(*args)
        ctx.spend(0)
        return value
    raise _refuse(RefusalCode.SYNTAX, "unknown node")  # pragma: no cover


def _fuzzy(value: str, options: tuple, threshold: float, ctx: _Ctx) -> str:
    if not options:
        raise _refuse(RefusalCode.NO_MATCH, "fuzzy_match() got no options")
    if len(options) > MAX_FUZZY_OPTIONS:
        raise _refuse(RefusalCode.BOUNDS, f"more than {MAX_FUZZY_OPTIONS} options")
    if len(value) > MAX_FUZZY_CHARS or any(len(o) > MAX_FUZZY_CHARS for o in options):
        raise _refuse(
            RefusalCode.BOUNDS, f"fuzzy_match() text longer than {MAX_FUZZY_CHARS}"
        )
    q = _normalize(value, ctx)
    norms = [_normalize(o, ctx) for o in options]
    # NFKC can expand one code point to many (U+FDFA becomes 18), so the
    # bounds that limit difflib's quadratic work apply to normalized text.
    if len(q) > MAX_FUZZY_CHARS or any(len(o) > MAX_FUZZY_CHARS for o in norms):
        raise _refuse(
            RefusalCode.BOUNDS,
            f"fuzzy_match() normalized text longer than {MAX_FUZZY_CHARS}",
        )
    work = sum(max(1, len(q)) * max(1, len(o)) for o in norms)
    if work > MAX_FUZZY_WORK:
        raise _refuse(RefusalCode.BOUNDS, "fuzzy_match() work bound exceeded")
    ctx.spend(work)
    scored = []
    for idx, option in enumerate(norms):
        scored.append((_similarity(q, option), idx))
        ctx.spend(0)
    best = max(s for s, _ in scored)
    # An exact match (after case and space folding) wins outright; otherwise
    # exactly one distinct option may clear the threshold. Two or more is a
    # guess, and a guess is refused.
    if best == 1.0:
        winners = [idx for s, idx in scored if s == 1.0]
    else:
        winners = [idx for s, idx in scored if s >= threshold]
    losers = [s for s, idx in scored if idx not in winners]
    runner_up = max(losers) if losers else None
    if best < threshold:
        raise _refuse(
            RefusalCode.BELOW_THRESHOLD,
            f"best match scored {best:.3f}, below the threshold {threshold}",
        )
    if len({options[i] for i in winners}) > 1:
        raise _refuse(
            RefusalCode.AMBIGUOUS,
            f"{len(winners)} options clear the threshold; refusing to guess",
        )
    ctx.matches.append(
        {
            "function": "fuzzy_match",
            "score": round(best, 6),
            "threshold": threshold,
            "runner_up": None if runner_up is None else round(runner_up, 6),
            "options": len(options),
            "option_index": winners[0],
        }
    )
    return str(options[winners[0]])


def canonical_text(v: Value) -> str:
    """The one text form a computed value takes in params and receipts."""

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, str):
        return v
    raise _refuse(RefusalCode.TYPE, "a transform must produce one scalar value")


@dataclass(frozen=True)
class Evaluation:
    value: Value
    type: ExprType
    inputs: tuple[str, ...]
    matches: tuple[dict[str, Any], ...]


def compile_expression(source: str, names: Optional[set[str]] = None) -> Node:
    """Parse and type-check, or refuse. Nothing runs."""

    node = parse(source)
    check(node, names)
    return node


def evaluate(source: str, params: Mapping[str, str]) -> Evaluation:
    """Evaluate ``source`` against ``params`` (all text), or refuse."""

    ctx = _Ctx(params={})
    ctx.params = _params(params, ctx)
    return _evaluate(source, ctx)


def _evaluate(source: str, ctx: _Ctx) -> Evaluation:
    names = set(ctx.params)
    node = parse(source)
    ctx.spend(len(source) + 1)
    typ = check(node, names)
    start = len(ctx.matches)
    value = _eval(node, ctx)
    ctx.spend(0)
    return Evaluation(value, typ, tuple(refs(node)), tuple(ctx.matches[start:]))


# --------------------------------------------------------------------------
# Operator set identity and three-valued predicates
# --------------------------------------------------------------------------

_SPECIAL_SIGNATURES: dict[str, dict[str, Any]] = {
    "fuzzy_match": {"args": ["string", "list", "number"], "result": "string",
                    "literal_args": [2]},
    "if_else": {"args": ["bool", "T", "T"], "result": "T", "literal_args": []},
    "text": {"args": ["string|int|bool|date"], "result": "string",
             "literal_args": []},
}  # fmt: skip

_OPERATOR_SIGNATURES: list[dict[str, Any]] = [
    {"op": "+", "args": ["string", "string"], "result": "string"},
    {"op": "+", "args": ["int", "int"], "result": "int"},
    {"op": "-", "args": ["int", "int"], "result": "int"},
    {"op": "*", "args": ["int", "int"], "result": "int"},
    {"op": "neg", "args": ["int"], "result": "int"},
    {"op": "==", "args": ["T", "T"], "result": "bool"},
    {"op": "!=", "args": ["T", "T"], "result": "bool"},
    {"op": "<", "args": ["int|date", "int|date"], "result": "bool"},
    {"op": "<=", "args": ["int|date", "int|date"], "result": "bool"},
    {"op": ">", "args": ["int|date", "int|date"], "result": "bool"},
    {"op": ">=", "args": ["int|date", "int|date"], "result": "bool"},
    {"op": "and", "args": ["bool", "bool"], "result": "bool"},
    {"op": "or", "args": ["bool", "bool"], "result": "bool"},
    {"op": "not", "args": ["bool"], "result": "bool"},
]


def operator_set() -> dict[str, Any]:
    """The closed operator set and bounds, as canonical data.

    This is what a generated effect model or a transform is checked against.
    Its digest changes whenever a function, signature, operator or bound
    changes.
    """

    functions: dict[str, Any] = {
        name: {
            "args": [t.value for t in fn.args],
            "result": fn.result.value,
            "literal_args": sorted(fn.literal),
        }
        for name, fn in _STDLIB.items()
    }
    functions.update(_SPECIAL_SIGNATURES)
    return {
        "language": LANGUAGE_VERSION,
        "work_model": WORK_MODEL,
        "types": [t.value for t in ExprType],
        "operators": _OPERATOR_SIGNATURES,
        "functions": {k: functions[k] for k in sorted(functions)},
        "bounds": {
            "max_source_chars": MAX_SOURCE_CHARS,
            "max_nodes": MAX_NODES,
            "max_depth": MAX_DEPTH,
            "max_string_chars": MAX_STRING_CHARS,
            "max_list_items": MAX_LIST_ITEMS,
            "max_int_abs": MAX_INT_ABS,
            "max_transforms": MAX_TRANSFORMS,
            "max_pattern_chars": MAX_PATTERN_CHARS,
            "max_regex_input_chars": MAX_REGEX_INPUT_CHARS,
            "max_unbounded_quantifiers": MAX_UNBOUNDED_QUANTIFIERS,
            "max_variable_quantifiers": MAX_VARIABLE_QUANTIFIERS,
            "max_fuzzy_chars": MAX_FUZZY_CHARS,
            "max_fuzzy_options": MAX_FUZZY_OPTIONS,
            "max_repeat_count": MAX_REPEAT_COUNT,
            "max_fuzzy_work": MAX_FUZZY_WORK,
            "max_eval_work": MAX_EVAL_WORK,
            "max_eval_seconds": MAX_EVAL_SECONDS,
            "max_params": MAX_PARAMS,
        },
        # casefold, lower/upper and NFKC depend on the Unicode database, so
        # the same receipt is only reproducible against the same version.
        "unicode_version": unicodedata.unidata_version,
        "regex_flags": ["ASCII"],
        "refusal_codes": [c.value for c in RefusalCode],
    }


def operator_set_sha256() -> str:
    """SHA-256 of the canonical JSON of :func:`operator_set`."""

    blob = json.dumps(operator_set(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Outcome(str, Enum):
    """``true``, ``false`` or ``unknown``. Compare it; never test its truth.

    A plain non-empty string is truthy, so ``if result.outcome:`` would pass
    on ``"false"`` and ``"unknown"``. Truth-testing an Outcome raises instead.
    """

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError(
            "an Outcome has no truth value; compare it to Outcome.TRUE or "
            "use PredicateResult.passed"
        )


@dataclass(frozen=True)
class PredicateResult:
    """A three-valued predicate outcome.

    ``"unknown"`` is never a pass. It carries the refusal code, so a caller
    such as an effect-model evaluator can map it to UNCERTAIN and keep the
    reason, while ``"false"`` is an observed contradiction.
    """

    outcome: Outcome
    refusal: Optional[RefusalCode] = None
    reason: str = ""
    inputs: tuple[str, ...] = ()
    matches: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        # Accept "true"/"false"/"unknown" from callers that build results
        # themselves, but always store the enum, so .passed and the
        # truth-test refusal hold for every result.
        object.__setattr__(self, "outcome", Outcome(self.outcome))

    @property
    def passed(self) -> bool:
        """True only for an observed ``true``; ``false`` and ``unknown`` fail."""

        return self.outcome is Outcome.TRUE

    def __bool__(self) -> bool:
        raise TypeError(
            "a PredicateResult has no truth value; use .passed or compare .outcome"
        )


def evaluate_predicate(source: str, params: Mapping[str, str]) -> PredicateResult:
    """Evaluate a bool expression without raising.

    Any refusal (syntax, unknown name or function, type, bounds, a fuzzy
    match below threshold, a missing regex match) is ``"unknown"``, never
    ``"true"``. An expression whose type is not bool is ``"unknown"`` too.
    """

    inputs: tuple[str, ...] = ()
    try:
        ctx = _Ctx(params={})
        ctx.params = _params(params, ctx)
        node = parse(source)
        inputs = tuple(refs(node))
        typ = check(node, set(params))
        if typ != B:
            raise _refuse(RefusalCode.TYPE, "a predicate must be a bool expression")
        value = _as_bool(_eval(node, ctx))
        ctx.spend(0)
    except ExpressionRefused as exc:
        # Keep which parameters it read: the receipt's evidence for "unknown".
        return PredicateResult(Outcome.UNKNOWN, exc.code, exc.reason, inputs=inputs)
    return PredicateResult(
        Outcome.TRUE if value else Outcome.FALSE,
        inputs=tuple(refs(node)),
        matches=tuple(ctx.matches),
    )


# --------------------------------------------------------------------------
# IR declaration and receipt record
# --------------------------------------------------------------------------


class ParamTransform(BaseModel):
    """Compute ``output_param`` from earlier parameters with ``expression``.

    Validated when declared: an expression outside the language, or one whose
    result is a list or a bare number, fails to load.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_param: str = Field(pattern=_PARAM_NAME)
    expression: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)

    @model_validator(mode="after")
    def _validate_expression(self) -> "ParamTransform":
        try:
            typ = check(parse(self.expression), None)
        except ExpressionRefused as exc:
            raise ValueError(f"expression refused ({exc})") from None
        if typ in (L, N):
            raise ValueError(
                "expression refused (type: a transform must produce a string, "
                "int, bool or date)"
            )
        return self


class TransformMatch(BaseModel):
    """Evidence for one fuzzy match: how close it was, and how close the next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    function: Literal["fuzzy_match"] = "fuzzy_match"
    score: float
    threshold: float
    runner_up: Optional[float] = None
    options: int
    option_index: int


class TransformRecord(BaseModel):
    """One computed value, for the run receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.param-transform-record/v1"] = (
        "openadapt.param-transform-record/v1"
    )
    language: Literal["openadapt.expr/v1"] = "openadapt.expr/v1"
    operator_set_sha256: str
    output_param: str
    expression: str
    expression_sha256: str
    inputs: list[str] = Field(description="Parameter names the expression read")
    input_sha256: dict[str, str] = Field(
        description="SHA-256 of each input value, so the receipt can be checked "
        "without repeating the inputs"
    )
    value: str
    value_type: Literal["string", "int", "bool", "date"]
    matches: list[TransformMatch] = Field(default_factory=list)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_transforms(
    params: Mapping[str, str], transforms: Sequence[ParamTransform]
) -> tuple[dict[str, str], list[TransformRecord]]:
    """Run ``transforms`` in order over a copy of ``params``.

    Each transform sees the run's params plus the outputs of the transforms
    before it. A transform may not overwrite an existing parameter. All or
    nothing: if any transform refuses, :class:`ExpressionRefused` propagates
    and the caller's ``params`` are untouched.
    """

    if len(transforms) > MAX_TRANSFORMS:
        raise _refuse(RefusalCode.BOUNDS, f"more than {MAX_TRANSFORMS} transforms")
    ctx = _Ctx(params={})
    working = _params(params, ctx)
    ctx.params = working
    records: list[TransformRecord] = []
    ops_digest = operator_set_sha256()
    for t in transforms:
        if t.output_param in working:
            raise _refuse(
                RefusalCode.CONFLICT,
                f"transform would overwrite parameter {t.output_param!r}",
            )
        result = _evaluate(t.expression, ctx)
        if result.type in (L, N):
            raise _refuse(RefusalCode.TYPE, "a transform must produce one scalar value")
        text = canonical_text(result.value)
        records.append(
            TransformRecord(
                operator_set_sha256=ops_digest,
                output_param=t.output_param,
                expression=t.expression,
                expression_sha256=_sha256(t.expression),
                inputs=list(result.inputs),
                input_sha256={n: _sha256(working[n]) for n in result.inputs},
                value=text,
                value_type=result.type.value,  # type: ignore[arg-type]
                matches=[TransformMatch(**m) for m in result.matches],
            )
        )
        working[t.output_param] = text
    return working, records


__all__ = [
    "LANGUAGE_VERSION",
    "Evaluation",
    "Outcome",
    "PredicateResult",
    "ExprType",
    "ExpressionRefused",
    "ParamTransform",
    "RefusalCode",
    "TransformMatch",
    "TransformRecord",
    "apply_transforms",
    "canonical_text",
    "check",
    "compile_expression",
    "evaluate",
    "evaluate_predicate",
    "operator_set",
    "operator_set_sha256",
    "parse",
    "refs",
    "stdlib_names",
]
