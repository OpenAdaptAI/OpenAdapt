"""verify_run's three folds over per-effect verdicts (C2: an empty set).

Each factory takes the fold, so the self-test runs the same spec on the
pre-migration copies vendored from main (``selftest/s9_verify_run_folds.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from openadapt_flow import verify_run
from openadapt_flow.runtime.effects.effect import Verdict
from tests.failopen.harness import Seed, SiteSpec, key, register, root_list

C, R, I = "confirmed", "refuted", "indeterminate"


def _verdicts(codes: list[str]) -> list[Any]:
    return [SimpleNamespace(verdict=Verdict(c)) for c in codes]


def aggregate_spec(fn: Callable[..., Any], id: str, kind: str = "site") -> SiteSpec:
    return SiteSpec(
        id=id,
        classes="C2 C3",
        target="openadapt_flow.verify_run:_aggregate",
        kind=kind,
        run=lambda ev: fn(_verdicts(ev)),
        is_pass=lambda out: out == "CONFIRMED",
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


register(aggregate_spec(verify_run._aggregate, "verify_run.aggregate"))
register(observed_spec(verify_run._observed, "verify_run.observed"))
register(readback_spec(verify_run.readback_class, "verify_run.readback_class"))
