"""verify_run's three folds over per-effect verdicts (C2: an empty set).

Each factory takes the fold, so the self-test runs the same spec on the
pre-migration copies vendored from main (``selftest/s9_verify_run_folds.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from openadapt_flow import verify_run
from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, key, register, root_list

C, R, I = "confirmed", "refuted", "indeterminate"

#: verify_run's labels by level (P6).
#: Both folds rank an unread effect above a refuted one (C7): an effect that
#: wasn't read may have landed, so the run says "check the record", never a
#: plain "refuted".
_UNREAD_FIRST = (Status.NOT_CHECKED, Status.REFUTED)
_PRECEDENCE_NOTE = (
    "an unread effect outranks a refuted one: when an effect may have landed "
    "unseen, reconciliation beats an assumed refutation, so the run is "
    "INDETERMINATE (check the record), not REFUTED, which would invite a "
    "retry over a write that may be in the store (C7; FAIL_CLOSED.md P6)"
)

_LEVEL = {
    "CONFIRMED": Status.HELD,
    "REFUTED": Status.REFUTED,
    "INDETERMINATE": Status.NOT_CHECKED,
    "present": Status.HELD,
    "absent": Status.REFUTED,
    "conflicting": Status.REFUTED,
    "unknown": Status.NOT_CHECKED,
}


def _verdicts(codes: list[str]) -> list[Any]:
    return [SimpleNamespace(verdict=Verdict(c)) for c in codes]


def aggregate_spec(fn: Callable[..., Any], id: str, kind: str = "site") -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C2",
        target="openadapt_flow.verify_run:_aggregate",
        kind=kind,
        run=lambda ev: fn(_verdicts(ev)),
        is_pass=lambda out: out == "CONFIRMED",
        level=lambda out: _LEVEL.get(out, Status.NOT_CHECKED),
        precedence=_UNREAD_FIRST,
        notes={"precedence": _PRECEDENCE_NOTE},
        seeds=lambda: [
            Seed([C], True),
            Seed([C, C, C], True),
            Seed([C, R], False),
            Seed([C, I], False),
            Seed([R, I], False),
        ],
        items=root_list(),
        bad_item=lambda _c: R,
        inflate=lambda _ev, n: [C] * (n - 1) + [R],
    )


def _observed(codes: list[str]) -> list[Any]:
    return [SimpleNamespace(observed_effect=c) for c in codes]


def observed_spec(fn: Callable[..., Any], id: str, kind: str = "site") -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C2",
        target="openadapt_flow.verify_run:_observed",
        kind=kind,
        run=lambda ev: fn(_observed(ev)),
        is_pass=lambda out: out == "present",
        level=lambda out: _LEVEL.get(out, Status.NOT_CHECKED),
        precedence=_UNREAD_FIRST,
        notes={"precedence": _PRECEDENCE_NOTE},
        seeds=lambda: [
            Seed(["present"], True),
            Seed(["present", "present"], True),
            Seed(["present", "absent"], False),
            Seed(["unknown"], False),
        ],
        items=root_list(),
        bad_item=lambda _c: "absent",
        inflate=lambda _ev, n: ["present"] * (n - 1) + ["unknown"],
    )


class _Tier:
    def __init__(self, sor: bool) -> None:
        self.sor = sor

    def reads_system_of_record(self) -> bool:
        return self.sor


def _readback(fn: Callable[..., Any], ev: dict[str, Any]) -> Any:
    verdicts = [SimpleNamespace(unavailable=False, stale=False) for _ in ev["effects"]]
    tiers = [None if t is None else _Tier(t == "sor") for t in ev["tiers"]]
    return fn(verdicts, tiers)


def readback_spec(fn: Callable[..., Any], id: str, kind: str = "site") -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C1 C2",
        target="openadapt_flow.verify_run:readback_class",
        kind=kind,
        run=lambda ev: _readback(fn, ev),
        is_pass=lambda out: out == "independent_readback",
        seeds=lambda: [
            Seed({"effects": ["e1"], "tiers": ["sor"]}, True),
            Seed({"effects": ["e1", "e2"], "tiers": ["sor", "sor"]}, True),
            Seed({"effects": ["e1", "e2"], "tiers": ["sor", "ui"]}, False),
            Seed({"effects": ["e1"], "tiers": [None]}, False),
        ],
        # One tier per effect: the effects are the plan, the tiers the results.
        items=key("tiers"),
        plan=key("effects"),
        bad_item=lambda _t: None,
        inflate=lambda _ev, n: {
            "effects": [f"e{i}" for i in range(n)],
            "tiers": ["sor"] * (n - 1) + ["ui"],
        },
    )


def _unplanned(fn: Callable[..., Any]) -> Callable[..., Any]:
    """A fold that takes the plan, called the way the S9 copies are: one
    planned effect per verdict, so only the empty case is in question."""

    return lambda verdicts: fn([str(i) for i in range(len(verdicts))], verdicts)


def planned_spec(
    fn: Callable[..., Any],
    id: str,
    make: Callable[[list[Any]], list[Any]],
    base: Callable[[Callable[..., Any], str], SiteSpec],
    last_bad: tuple[Any, Any],
) -> SiteSpec:
    """The live fold, with the plan (``cfg.effects``) apart from the
    verdicts: dropping a verdict keeps the plan (M1), and a verdict past the
    plan answers nothing."""

    spec = base(_unplanned(fn), id)
    seeds = spec.seeds
    good, bad = last_bad

    def plan_seeds() -> list[Seed]:
        return [
            Seed(
                {"plan": [f"{i}:e" for i in range(len(s.evidence))], "got": s.evidence},
                s.correct_pass,
                s.note,
            )
            for s in seeds()
        ]

    spec.run = lambda ev: fn(ev["plan"], make(ev["got"]))
    spec.seeds = plan_seeds
    spec.items = key("got")
    spec.plan = key("plan")
    spec.inflate = lambda _ev, n: {
        "plan": [f"{i}:e" for i in range(n)],
        "got": [good] * (n - 1) + [bad],
    }
    return spec


register(
    planned_spec(
        verify_run._aggregate, "verify_run.aggregate", _verdicts, aggregate_spec, (C, R)
    )
)
register(
    planned_spec(
        verify_run._observed,
        "verify_run.observed",
        _observed,
        observed_spec,
        ("present", "unknown"),
    )
)
register(readback_spec(verify_run.readback_class, "verify_run.readback_class"))
