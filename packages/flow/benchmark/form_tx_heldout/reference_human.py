"""Scripted careful human: completes every case to the answer key.

It reads its values from the answer key (expected record plus operator
script), re-checks each earlier step after a session interruption, and
checks that every screen-sourced value it relies on (the estimator figure,
type-ahead options, operator-script labels) is actually shown.

    python -m benchmark.form_tx_heldout.reference_human [portal ...]
"""

from __future__ import annotations

import sys
from typing import Any

from .common import CREDENTIALS
from .drivers.base import ReferenceSource, digits
from .runner import driver_for, emit, run_cases
from .scoring import load_answer_key


class CarefulHuman(ReferenceSource):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.problems: list[str] = []

    def estimator(self, label: str, displayed: int, current: str) -> str | None:
        field = self.label_map.get(label.strip().casefold())
        want = self.expected[field if isinstance(field, str) else field[0]]
        if int(want) != displayed:
            self.problems.append(f"estimator showed {displayed}, answer key has {want}")
        if digits(current) != str(want):
            return str(want)
        return None


def make_source(portal: str, case_id: str) -> CarefulHuman:
    ak = load_answer_key()["cases"][case_id]
    labels = driver_for(portal).REFERENCE_LABELS
    return CarefulHuman(portal, case_id, CREDENTIALS[portal], labels, ak["expected_record"], ak["operator_script"])


def main(argv: list[str]) -> None:
    portals = [a for a in argv if not a.startswith("-")] or None
    emit(run_cases(make_source, portals, include_dev="--dev" in argv))


if __name__ == "__main__":
    main(sys.argv[1:])
