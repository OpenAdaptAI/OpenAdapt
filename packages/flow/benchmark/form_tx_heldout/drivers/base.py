"""Shared pieces of the two reference fillers.

A *source* decides what to type; a per-portal *driver* knows how to move
through that portal's pages and calls the source for every visible field,
keyed by the field's visible label text.

* ``ReferenceSource`` (careful human): values come from the answer key's
  expected record and operator script; it checks every value it relies on
  from the screen (estimator figure, type-ahead option) is actually shown.
* ``NaiveSource`` (unguarded agent): values come from the intake through a
  label-to-intake-key table; no fact means keep the prefill, or take the
  first option when the control is empty; first type-ahead suggestion;
  estimator figure accepted as handed back; no summary check.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent.parent


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().casefold()


def digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s if s is not None else ""))


def us_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def load_intake(portal: str, case_id: str) -> dict[str, Any]:
    return json.loads((HERE / "cases" / portal / f"{case_id}.intake.json").read_text())["facts"]


def option_matches(want: str, value: str, text: str) -> bool:
    w = norm(want)
    if not w:
        return False
    if w == norm(value) or w == norm(text):
        return True
    dw = digits(want)
    if dw and re.fullmatch(r"[\s$,\d]+", want) and (dw == digits(value) or dw == digits(text)):
        return True
    return False


class DriverError(RuntimeError):
    pass


class Source:
    kind = "base"
    fallback_first = False
    restart_after_relogin = False

    def __init__(self, portal: str, case_id: str, creds: dict[str, str]) -> None:
        self.portal = portal
        self.case_id = case_id
        self.creds = creds
        self.labels_seen: set[str] = set()
        self.events: list[str] = []

    def note(self, label: str) -> None:
        self.labels_seen.add(norm(label))

    def get(self, label: str) -> str | None:
        raise NotImplementedError

    def pick(self, label: str, options: list[str], want: str | None) -> int:
        raise NotImplementedError

    def estimator(self, label: str, displayed: int, current: str) -> str | None:
        return None

    def wants(self, label: str) -> bool:
        """Whether the source has an opinion about ``label`` at all."""
        return self.get(label) is not None


Formatter = Callable[[Any], str]


def fmt_plain(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def fmt_date(v: Any) -> str:
    return us_date(str(v))


class NaiveSource(Source):
    kind = "naive"
    fallback_first = True

    def __init__(self, portal: str, case_id: str, creds: dict[str, str], label_map: dict[str, Any]) -> None:
        super().__init__(portal, case_id, creds)
        self.facts = load_intake(portal, case_id)
        self.label_map = {norm(k): v for k, v in label_map.items()}

    def _lookup(self, label: str) -> Any:
        return self.label_map.get(norm(label))

    def get(self, label: str) -> str | None:
        entry = self._lookup(label)
        if entry is None:
            return None
        key, fmt = entry if isinstance(entry, tuple) else (entry, fmt_plain)
        if key not in self.facts:
            return None
        return fmt(self.facts[key])

    def pick(self, label: str, options: list[str], want: str | None) -> int:
        return 0


class ReferenceSource(Source):
    kind = "reference"
    restart_after_relogin = True

    def __init__(
        self,
        portal: str,
        case_id: str,
        creds: dict[str, str],
        label_map: dict[str, Any],
        expected: dict[str, Any],
        operator: dict[str, Any],
    ) -> None:
        super().__init__(portal, case_id, creds)
        self.facts = load_intake(portal, case_id)
        self.expected = expected
        self.operator = operator
        self.label_map = {norm(k): v for k, v in label_map.items()}

    def get(self, label: str) -> str | None:
        entry = self.label_map.get(norm(label))
        if entry is None:
            return None
        if callable(entry):
            return entry(self)
        field, fmt = entry if isinstance(entry, tuple) else (entry, fmt_plain)
        if field.startswith("intake:"):
            key = field[len("intake:"):]
            return fmt(self.facts[key]) if key in self.facts else None
        return fmt(self.expected[field])

    def pick(self, label: str, options: list[str], want: str | None) -> int:
        for i, o in enumerate(options):
            if want is not None and norm(o) == norm(want):
                return i
        raise DriverError(f"{label}: wanted {want!r} not among {options!r}")

    def check_operator_labels(self) -> list[str]:
        """Operator-script labels that never appeared on screen."""
        missing = []
        for k in self.operator:
            if k == "session_login" or re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)*", k):
                continue
            if norm(k) not in self.labels_seen:
                missing.append(k)
        return missing


def oracle(base: str, path: str, payload: dict | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=data, method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def find_option(src: Source, want: str, opts: list[tuple[str, str]]) -> str | None:
    """Value of the option matching ``want``.

    Exact match on value or text first. The naive filler also accepts the
    first option whose text contains the wanted text (a loose label match).
    """
    hit = next((v for v, t in opts if v and option_matches(want, v, t)), None)
    if hit is None and src.kind == "naive":
        w = norm(want)
        hit = next((v for v, t in opts if v and w and w in norm(t)), None)
    return hit
