"""The effect-location record (``openadapt.effect-location/v1``) and a reading.

An :class:`EffectLocation` is data a pack stores for one or more intended
values. It is compiled from a demonstration, never written into the engine:

- ``path`` and ``start``: the demonstration steps that reach the place where
  the value shows, and the condition the re-observation must start from
  (:class:`Start`).
- ``locator``: how to find it there (a label, a row key or a region).
- ``expected``: the value each key should show, in the codec's canonical form.
- ``codec``: how displayed text maps to the value (:class:`Codec`). A
  ``template`` codec reads one displayed text as several keys, such as one
  summary row "Name (DOB)"; a ``split_date`` codec reads several displayed
  parts as one key.
- ``expected_count``: how many times the value should show (usually 1).
- ``baseline``: optionally, what the location showed before the action.

A :class:`Reading` is what an adapter (a browser, an accessibility tree, a
terminal screen, an export) saw when it went back to the location. The
adapter states the start condition it achieved and must bind the reading to
the run; the evaluator judges only what the reading says.

Kinds are kept as strings so a pack written for a newer engine loads. An
unknown kind is judged ``not_checked``, never guessed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    "CODEC_KINDS",
    "LOCATOR_KINDS",
    "SCHEMA",
    "Codec",
    "EffectLocation",
    "Locator",
    "Reading",
    "Start",
]

SCHEMA = "openadapt.effect-location/v1"


class Start(str, Enum):
    """Where the re-observation starts from, weakest first."""

    #: The screen the action was taken on, read again in place.
    SAME_SCREEN = "same_screen"
    #: A new navigation to the location in the acting session.
    FRESH_NAVIGATION = "fresh_navigation"
    #: A separately signed-in, read-only session.
    NEW_SESSION = "new_session"
    #: Another channel than the acting user interface: an export, an API,
    #: a message the system sent.
    OTHER_CHANNEL = "other_channel"


#: The order of :class:`Start`, weakest first.
START_ORDER: tuple[Start, ...] = tuple(Start)

LOCATOR_KINDS = frozenset({"label", "row_key", "region"})
CODEC_KINDS = frozenset(
    {
        "text",
        "fold",
        "number",
        "money",
        "unit",
        "date",
        "bool",
        "enum",
        "split_date",
        "template",
    }
)
#: Codecs that read one displayed text as one key.
SCALAR_KINDS = CODEC_KINDS - {"split_date", "template"}


def _strs(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a list of strings")
    out = tuple(value)
    if not all(isinstance(v, str) for v in out):
        raise ValueError(f"{name} must be a list of strings")
    return out


def _only(data: Mapping[str, Any], allowed: set[str], what: str) -> None:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an object")
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"{what} has unknown fields {sorted(extra)}")


@dataclass(frozen=True)
class Locator:
    """How to find the location on the screen or channel it is read from.

    ``kind`` is ``label`` (a label or heading next to the value), ``row_key``
    (a table or list row whose ``column`` cell equals ``value``) or ``region``
    (a named area). ``value`` and ``section`` are patterns or texts from the
    demonstration; the adapter interprets them, the evaluator does not.
    """

    kind: str
    value: str
    section: str = ""
    column: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "section": self.section,
            "column": self.column,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Locator":
        _only(data, {"kind", "value", "section", "column"}, "locator")
        out = cls(
            kind=data["kind"],
            value=data["value"],
            section=data.get("section", ""),
            column=data.get("column", ""),
        )
        for name in ("kind", "value", "section", "column"):
            if not isinstance(getattr(out, name), str):
                raise ValueError(f"locator {name} must be a string")
        return out


@dataclass(frozen=True)
class Part:
    """One named group of a ``template`` codec: the key it shows and its codec."""

    group: str
    key: str
    codec: "Codec"


@dataclass(frozen=True)
class Codec:
    """How displayed text maps to a value. Every word and symbol is data.

    ``kind`` and its parameters:

    - ``text``: whitespace collapsed, compared exactly. ``fold``: also
      case-folded.
    - ``number``: a decimal number. ``money``: an amount, compared in minor
      units (``decimals`` places). ``unit``: a number followed by one of
      ``units``. ``affixes`` are the symbols allowed before or after the
      number (a currency sign); ``group_marks`` and ``decimal_mark`` are the
      separators.
    - ``date``: an ISO date, or a numeric date in ``date_order`` (``YMD``,
      ``MDY`` or ``DMY``), or with a month spelled as in ``month_names``.
    - ``bool``: one of ``true_tokens`` or ``false_tokens`` (case-folded).
    - ``enum``: the displayed text, case-folded, looked up in ``table``.
    - ``split_date``: the parts ``year``, ``month`` and ``day`` read
      separately (a month by number or by ``month_names``).
    - ``template``: ``pattern`` must match the whole displayed text; each of
      ``parts`` reads one named group as one key.

    With no tokens, names, affixes or table given, only the bare form is
    accepted: a pack that needs more says so.
    """

    kind: str
    date_order: str = ""
    month_names: tuple[tuple[str, ...], ...] = ()
    true_tokens: tuple[str, ...] = ()
    false_tokens: tuple[str, ...] = ()
    table: tuple[tuple[str, str], ...] = ()
    units: tuple[str, ...] = ()
    affixes: tuple[str, ...] = ()
    group_marks: tuple[str, ...] = (",",)
    decimal_mark: str = "."
    decimals: int = 2
    pattern: str = ""
    parts: tuple[Part, ...] = ()

    def keys(self) -> tuple[str, ...]:
        """The keys a ``template`` codec shows (empty for other kinds)."""

        return tuple(p.key for p in self.parts)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        defaults = Codec(kind=self.kind)
        for name in (
            "date_order",
            "true_tokens",
            "false_tokens",
            "units",
            "affixes",
            "group_marks",
            "decimal_mark",
            "decimals",
            "pattern",
        ):
            value = getattr(self, name)
            if value != getattr(defaults, name):
                out[name] = list(value) if isinstance(value, tuple) else value
        if self.month_names:
            out["month_names"] = [list(m) for m in self.month_names]
        if self.table:
            out["table"] = {shown: value for shown, value in self.table}
        if self.parts:
            out["parts"] = [
                {"group": p.group, "key": p.key, "codec": p.codec.to_dict()}
                for p in self.parts
            ]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Codec":
        names = {
            "kind",
            "date_order",
            "month_names",
            "true_tokens",
            "false_tokens",
            "table",
            "units",
            "affixes",
            "group_marks",
            "decimal_mark",
            "decimals",
            "pattern",
            "parts",
        }
        _only(data, names, "codec")
        kind = data["kind"]
        if not isinstance(kind, str):
            raise ValueError("codec kind must be a string")
        kw: dict[str, Any] = {"kind": kind}
        for name in ("true_tokens", "false_tokens", "units", "affixes", "group_marks"):
            if name in data:
                kw[name] = _strs(data[name], name)
        for name in ("date_order", "decimal_mark", "pattern"):
            if name in data:
                if not isinstance(data[name], str):
                    raise ValueError(f"codec {name} must be a string")
                kw[name] = data[name]
        if "decimals" in data:
            d = data["decimals"]
            if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6:
                raise ValueError("codec decimals must be an integer from 0 to 6")
            kw["decimals"] = d
        if "month_names" in data:
            months = data["month_names"]
            if isinstance(months, str) or not isinstance(months, Sequence):
                raise ValueError("month_names must be twelve lists of names")
            kw["month_names"] = tuple(_strs(m, "month_names") for m in months)
            if len(kw["month_names"]) != 12:
                raise ValueError("month_names must be twelve lists of names")
        if "table" in data:
            table = data["table"]
            if not isinstance(table, Mapping) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in table.items()
            ):
                raise ValueError("codec table must map strings to strings")
            kw["table"] = tuple(table.items())
        if "parts" in data:
            parts = data["parts"]
            if isinstance(parts, str) or not isinstance(parts, Sequence):
                raise ValueError("codec parts must be a list")
            built = []
            for p in parts:
                _only(p, {"group", "key", "codec"}, "codec part")
                if not isinstance(p["group"], str) or not isinstance(p["key"], str):
                    raise ValueError("codec part group and key must be strings")
                built.append(Part(p["group"], p["key"], cls.from_dict(p["codec"])))
            kw["parts"] = tuple(built)
        return cls(**kw)


@dataclass(frozen=True)
class EffectLocation:
    """Where one or more intended values show after the action. See the module."""

    id: str
    keys: tuple[str, ...]
    path: tuple[str, ...]
    start: str
    locator: Locator
    codec: Codec
    expected: Mapping[str, Any] = field(default_factory=dict)
    expected_count: int = 1
    baseline: Optional[Mapping[str, Any]] = None
    #: Where the record came from (a demonstration id), for people.
    source: str = ""

    def with_expected(self, values: Mapping[str, Any]) -> "EffectLocation":
        """A copy expecting ``values`` for its keys (other keys are ignored)."""

        return EffectLocation(
            id=self.id,
            keys=self.keys,
            path=self.path,
            start=self.start,
            locator=self.locator,
            codec=self.codec,
            expected={k: values[k] for k in self.keys if k in values},
            expected_count=self.expected_count,
            baseline=self.baseline,
            source=self.source,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": SCHEMA,
            "id": self.id,
            "keys": list(self.keys),
            "path": list(self.path),
            "start": self.start,
            "locator": self.locator.to_dict(),
            "codec": self.codec.to_dict(),
            "expected": dict(self.expected),
            "expected_count": self.expected_count,
        }
        if self.baseline is not None:
            out["baseline"] = dict(self.baseline)
        if self.source:
            out["source"] = self.source
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EffectLocation":
        names = {
            "schema",
            "id",
            "keys",
            "path",
            "start",
            "locator",
            "codec",
            "expected",
            "expected_count",
            "baseline",
            "source",
        }
        _only(data, names, "effect location")
        if data.get("schema") != SCHEMA:
            raise ValueError(f"effect location schema must be {SCHEMA!r}")
        # Required, not defaulted: a record that doesn't say how many times
        # the value shows can't tell one match from the only match.
        if "expected_count" not in data:
            raise ValueError("effect location needs expected_count")
        count = data["expected_count"]
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("expected_count must be an integer")
        expected = data.get("expected", {})
        baseline = data.get("baseline")
        if not isinstance(expected, Mapping) or (
            baseline is not None and not isinstance(baseline, Mapping)
        ):
            raise ValueError("expected and baseline must be objects")
        for name in ("id", "start", "source"):
            if not isinstance(data.get(name, ""), str):
                raise ValueError(f"effect location {name} must be a string")
        return cls(
            id=data["id"],
            keys=_strs(data["keys"], "keys"),
            path=_strs(data.get("path", ()), "path"),
            start=data["start"],
            locator=Locator.from_dict(data["locator"]),
            codec=Codec.from_dict(data["codec"]),
            expected=dict(expected),
            expected_count=count,
            baseline=None if baseline is None else dict(baseline),
            source=data.get("source", ""),
        )


@dataclass(frozen=True)
class Reading:
    """What an adapter saw at one location, bound to one run.

    ``matches`` holds one entry per place the locator matched: a mapping of
    part name to displayed text (``text`` for a single text; ``year``,
    ``month`` and ``day`` for a split date). ``complete`` is false when the
    adapter could not read the whole located area (a truncated table, a
    capped page), so a match can't be shown to be the only one. ``error`` is
    set when the read failed. ``start`` is the start condition the adapter
    achieved and can show; ``basis`` names who produced the evidence for an
    ``other_channel`` read (``system_api``, ``activity_log``, ``db_frame`` or
    ``owned_connection``).
    """

    location_id: str
    run_id: str
    start: str
    matches: tuple[Mapping[str, Any], ...]
    complete: bool
    error: Optional[str]
    basis: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "run_id": self.run_id,
            "start": self.start,
            "matches": [dict(m) for m in self.matches],
            "complete": self.complete,
            "error": self.error,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Reading":
        """Every field is required, ``error`` and ``basis`` as null when unset:
        a reading that leaves out whether it was complete or failed is not
        evidence."""

        names = {
            "location_id",
            "run_id",
            "start",
            "matches",
            "complete",
            "error",
            "basis",
        }
        _only(data, names, "reading")
        missing = names - set(data)
        if missing:
            raise ValueError(f"reading is missing {sorted(missing)}")
        matches = data["matches"]
        if isinstance(matches, (str, Mapping)) or not isinstance(matches, Sequence):
            raise ValueError("reading matches must be a list")
        if not all(isinstance(m, Mapping) for m in matches):
            raise ValueError("each reading match must be an object")
        return cls(
            location_id=data["location_id"],
            run_id=data["run_id"],
            start=data["start"],
            matches=tuple(dict(m) for m in matches),
            complete=data["complete"],
            error=data["error"],
            basis=data["basis"],
        )
