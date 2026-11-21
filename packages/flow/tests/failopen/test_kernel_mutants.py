"""Each rule of the kernel is load-bearing: break it and a check fails.

A hand-rolled mutant runner (no new dependency): each mutant rewrites one
rule of ``openadapt_flow/verdict.py`` in its source, loads the result as a
fresh module, and runs :func:`battery` on it. Every mutant must be killed;
the unmutated module must survive. A mutant whose source text is no longer
found fails the test, so the runner can't silently stop mutating.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "openadapt_flow" / "verdict.py"

MUTANTS: dict[str, tuple[str, str]] = {
    "drop total>0": ("            self.total > 0\n", "            True\n"),
    # ``passes`` checks both ``len(held) == total`` and ``not missing``, so
    # deleting either one alone is an equivalent mutant. The rule is that an
    # obligation with no result is never counted as held.
    "missing counts as held": (
        '            buckets["missing"].append(item)\n',
        '            buckets["held"].append(item)\n',
    ),
    "ignore unexpected": ("            and not self.unexpected\n", "\n"),
    "ignore mismatched": (
        "        elif any(binding_mismatches(item, r.subject) for r in got):\n",
        "        elif False:\n",
    ),
    "keep last writer": (
        "        elif len({r.status for r in got}) > 1:\n            conflicting.append(item)\n"
        "        else:\n            buckets[got[0].status.value].append(item)\n",
        "        else:\n            buckets[got[-1].status.value].append(item)\n",
    ),
    "truncate at limit": (
        "    for item in obligations.items:\n",
        "    obligations = Obligations(obligations.items[:limit], obligations.source)"
        " if limit is not None else obligations\n    for item in obligations.items:\n",
    ),
    "observe returns a default": (
        "        return Errored(type(exc).__name__, str(exc)[:200])\n",
        "        return Observed(None)\n",
    ),
    "from_code deny-list": (
        "    if code in refuted:\n        return Status.REFUTED\n    return Status.NOT_CHECKED\n",
        "    if code in refuted:\n        return Status.REFUTED\n    return Status.HELD\n",
    ),
}


def _load(source: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through it
    try:
        exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def battery(v: ModuleType) -> None:
    """The kernel's rules as plain asserts, run on a (mutated) module."""

    S, O, R = v.Subject, v.Obligations, v.Result
    a, b = S(key="a"), S(key="b")

    def held(s: object) -> object:
        return R.held(s, v.Observed(1))

    def fix(*items: object) -> object:
        return O.fix(list(items), source="plan")

    assert not v.settle(fix(), []).passes, "empty passes"
    assert not v.settle(fix(a, b), [held(a)]).passes, "missing ignored"
    assert not v.settle(fix(a), [held(a), held(b)]).passes, "unexpected ignored"
    bound = S(key="a", run_id="run-1")
    assert not v.settle(fix(bound), [held(S(key="a", run_id="run-2"))]).passes, (
        "run ignored"
    )
    assert not v.settle(fix(a), [R.refuted(a, v.Observed(0), "x"), held(a)]).passes, (
        "last writer"
    )
    many = [S(key=str(i)) for i in range(5)]
    results = [held(s) for s in many[:-1]] + [R.refuted(many[-1], v.Observed(0), "x")]
    settled = v.settle(fix(*many), results, limit=3)
    assert len(settled.refuted) == 1 and settled.total == 5, "truncated"

    def boom() -> object:
        raise TimeoutError

    assert not isinstance(v.observe(boom), v.Observed), "observe defaulted"
    assert (
        v.from_code("new_code", held=frozenset({"ok"}), refuted=frozenset({"bad"}))
        is v.Status.NOT_CHECKED
    ), "deny-list"


def test_the_kernel_survives_its_battery() -> None:
    battery(_load(SOURCE.read_text(encoding="utf-8"), "_verdict_original"))


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_every_mutant_is_killed(name: str) -> None:
    text = SOURCE.read_text(encoding="utf-8")
    old, new = MUTANTS[name]
    assert text.count(old) == 1, f"mutant {name!r}: its source text is gone"
    mutant = _load(text.replace(old, new), f"_verdict_mutant_{abs(hash(name))}")
    with pytest.raises(AssertionError):
        battery(mutant)
