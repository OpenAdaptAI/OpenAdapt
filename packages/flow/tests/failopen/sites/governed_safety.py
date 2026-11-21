"""scripts/ci/check_windows_governed_safety.check (C5): '' and NULL are
different values.

On main a held phase whose target note went from NULL to '' (or back) reports
no problem: ``written`` keeps rows whose note is truthy, so a write of '' is
no write. Fixed on #2258 by diffing every row against the seed; the post-fix
copy is ``selftest/s8_governed_note.py``.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any, Callable

from tests.failopen.harness import Seed, SiteSpec, key, register

ROOT = Path(__file__).resolve().parents[5]


def _rows(note1: Any, note2: Any = None) -> list[dict[str, Any]]:
    return [
        {"id": 1, "first": "Ada", "last": "Ray", "dob": "1990-01-01", "note": note1},
        {"id": 2, "first": "Bo", "last": "Lee", "dob": "1991-02-02", "note": note2},
    ]


def _held(seed_note: Any, held_note: Any) -> dict[str, Any]:
    return {
        "target_id": 1,
        "expected_note": "follow up",
        "phases": ["held"],
        "seed_rows": _rows(seed_note),
        "held": {"rows": _rows(held_note), "outcome": "HALTED_BEFORE_EFFECT"},
    }


def _approved() -> dict[str, Any]:
    return {
        "target_id": 1,
        "expected_note": "follow up",
        "phases": ["approved"],
        "seed_rows": _rows(None),
        "approved": {
            "rows": _rows("follow up"),
            "outcome": "VERIFIED",
            "approval_decision": "approved",
        },
    }


def governed_spec(check: Callable[[Any], list[str]], id: str, kind: str) -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C5",
        target="scripts/ci/check_windows_governed_safety.py:check",
        kind=kind,
        run=lambda ev: check(copy.deepcopy(ev)),
        is_pass=lambda out: out == [],
        seeds=lambda: [
            Seed(_held(None, None), True, "held, untouched"),
            Seed(_approved(), True, "approved write of the expected note"),
            Seed(_held("", None), False, "held, '' became NULL"),
        ],
        values=(key("note", "held", "rows", 0), key("note", "approved", "rows", 0)),
        equivalent=frozenset({"absent"}),
        notes={"absent": "rows are full-row dumps; a missing column reads as NULL"},
    )


def load_check(path: Path) -> Callable[[Any], list[str]]:
    spec = importlib.util.spec_from_file_location(f"_vendored_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check  # type: ignore[no-any-return]


register(
    governed_spec(
        load_check(ROOT / "scripts/ci/check_windows_governed_safety.py"),
        "governed_safety.check",
        "owned",
    ),
    xfail="#2258: a held target note changed between '' and NULL reads as no write",
    known=[
        "M10 P2: seed 0: value 0 empty for null: still passes",
        "seed P0: seed 2 held, '' became NULL: pass=True",
    ],
)
