"""The fail-open harness kills planted mutants of the effect-location evaluator.

Each mutant drops one rule (partial reads, the run binding, the expected
count, a read error). The harness must report a failure for each one. Skipped
where the harness (#2476) is not on the branch yet.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

import pytest

harness = pytest.importorskip("tests.failopen.harness")

from openadapt_flow.effect_location import evaluator  # noqa: E402

_JUDGE = evaluator._judge

MUTANTS: dict[str, Callable[..., Any]] = {
    "partial read held": lambda loc, r, run: _JUDGE(
        loc, replace(r, complete=True), run
    ),
    "run binding ignored": lambda loc, r, run: _JUDGE(loc, replace(r, run_id=run), run),
    "only the first match": lambda loc, r, run: _JUDGE(
        loc, replace(r, matches=r.matches[:1]), run
    ),
    "read error ignored": lambda loc, r, run: _JUDGE(loc, replace(r, error=None), run),
}


def _spec() -> Any:
    return harness.load_all()["effect_location.evaluate_all"]


def test_the_real_evaluator_holds() -> None:
    assert harness.run_site(_spec()) == []


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_mutant_is_killed(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluator, "_judge", MUTANTS[name])
    assert harness.run_site(_spec()), f"the harness missed: {name}"
