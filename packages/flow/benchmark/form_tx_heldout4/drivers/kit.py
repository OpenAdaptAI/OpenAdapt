"""Small Playwright helpers shared by the set-4 drivers."""

from __future__ import annotations

import time
from typing import Any

from ..scoring import load_answer_key, normalize
from .base import DriverError, Source, find_option, norm


def fill_text(src: Source, loc: Any, label: str) -> None:
    """Type the source's value for ``label``; no value keeps what is there."""
    src.note(label)
    want = src.get(label)
    if want is None:
        return
    loc.fill(str(want))


def fill_select(src: Source, loc: Any, label: str) -> None:
    """Choose the option for ``label`` in a native ``<select>``."""
    src.note(label)
    opts = loc.evaluate("s => [...s.options].map(o => [o.value, o.text])")
    real = [(v, t) for v, t in opts if v]
    want = src.get(label)
    if want is not None:
        value = find_option(src, want, real)
        if value is None:
            raise DriverError(f"{label}: no option for {want!r} in {[t for _, t in real]!r}")
        loc.select_option(value)
        return
    if src.fallback_first and not loc.input_value() and real:
        loc.select_option(real[0][0])


def choose(src: Source, label: str, options: list[tuple[str, str]], current: str | None) -> str | None:
    """Value to choose for a radio-like group, or ``None`` to leave it."""
    src.note(label)
    want = src.get(label)
    if want is not None:
        value = find_option(src, want, options)
        if value is None:
            raise DriverError(f"{label}: no option for {want!r} in {[t for _, t in options]!r}")
        return value
    if src.fallback_first and not current and options:
        return options[0][0]
    return None


def wait_until(fn: Any, timeout: float = 10.0, step: float = 0.1) -> Any:
    end = time.time() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.time() > end:
            return v
        time.sleep(step)


def field_differs(portal: str, field: str, shown: str, expected: Any) -> bool:
    """Whether a summary value differs from the expected one (scorer normalization)."""
    ftype = load_answer_key()["portals"][portal]["field_types"][field]
    if ftype == "list":
        return False
    shown = "" if shown.strip() in ("—", "-", "") else shown
    return normalize(shown, ftype) != normalize(expected, ftype)


def summary_mismatches(src: Source, rows: dict[str, str], label_to_field: dict[str, str]) -> list[str]:
    """Fields whose shown summary value differs from the answer key (reference only)."""
    if src.kind != "reference":
        return []
    bad = []
    expected = getattr(src, "expected", {})
    lookup = {norm(k): v for k, v in label_to_field.items()}
    for label, shown in rows.items():
        field = lookup.get(norm(label))
        if field and field in expected and field_differs(src.portal, field, shown, expected[field]):
            bad.append(field)
    return bad
