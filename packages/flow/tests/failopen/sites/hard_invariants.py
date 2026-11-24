"""hard_invariants.judge: a positive control, built on the kernel. "No
change outside the scope" and "no undeclared delete" are "nothing else
changed" claims, so a partial read of either must not hold (M15)."""

from __future__ import annotations

from typing import Any

from openadapt_flow.hard_invariants import judge_hard_invariants
from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, key, register

GOOD: dict[str, Any] = {
    "outside_scope_changes": 0,
    "undeclared_deletes": 0,
    "before_matches": 0,
    "after_matches": 1,
    "history_matches": 1,
    "scope_complete": True,
    "deletes_complete": True,
    "history_complete": True,
    "search_complete": True,
}


def _fields() -> Any:
    from tests.failopen.harness import Lens

    def put(ev: dict[str, Any], value: dict[str, Any]) -> None:
        ev.clear()
        ev.update(value)

    return Lens(lambda ev: ev, put)


register(
    SiteSpec(
        id="hard_invariants.judge",
        classes="C2 C5",
        target="openadapt_flow.hard_invariants:judge_hard_invariants",
        run=lambda ev: judge_hard_invariants(**ev),
        is_pass=lambda out: out.status == "held",
        level=lambda out: Status(out.status),
        seeds=lambda: [
            Seed(dict(GOOD), True),
            Seed({**GOOD, "outside_scope_changes": 2}, False),
            Seed({**GOOD, "history_matches": 2, "after_matches": 2}, False),
            Seed({**GOOD, "history_complete": False}, False),
        ],
        fields=_fields(),
        partial=lambda ev: [
            (flag, {**ev, flag: False}) for flag in GOOD if flag.endswith("_complete")
        ],
        values=tuple(key(name) for name in GOOD),
    )
)
