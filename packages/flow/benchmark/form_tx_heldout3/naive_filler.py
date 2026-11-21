"""Unguarded agent baseline.

Types intake values into fields matched by label text, keeps any prefilled
value it has no intake fact for (and overwrites prefill when it has one),
picks the first type-ahead suggestion, accepts the estimator's handed-back
value, answers unknown questions with the first option (or leaves text
empty), signs in again on a timeout and carries on, and submits without
checking the summary.

    python -m benchmark.form_tx_heldout3.naive_filler [portal ...]
"""

from __future__ import annotations

import sys

from .common import CREDENTIALS
from .drivers.base import NaiveSource
from .portals import PORTALS
from .runner import driver_for, emit, run_cases


def make_source(portal: str, case_id: str) -> NaiveSource:
    return NaiveSource(portal, case_id, CREDENTIALS[portal], driver_for(portal).NAIVE_LABELS)


def main(argv: list[str]) -> None:
    portals = [a for a in argv if not a.startswith("-")] or list(PORTALS)
    for pid in portals:  # one block per portal, printed as soon as it finishes
        emit(run_cases(make_source, [pid], include_dev="--dev" in argv))


if __name__ == "__main__":
    main(sys.argv[1:])
