"""gui.repeating.judge (C1 C2 C5): every intake item shows in the instance it
was written into, slot by slot.

The obligations are item-by-slot, fixed from the intake before any instance
was added. A value must equal the item's text exactly. An instance no item
was written into is refuted and unexpected; a missing instance is missing; an
empty intake never passes.
"""

from __future__ import annotations

import copy
from typing import Any

from openadapt_flow.gui.repeating import judge_instances
from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, index, key, register

RUN = "run-1"


def _good() -> dict[str, Any]:
    return {
        "planned": [
            {"item": "0", "slot": "Full name", "expected": "Ann Example"},
            {"item": "0", "slot": "Date of birth", "expected": "1990-01-02"},
            {"item": "1", "slot": "Full name", "expected": "Ben Example"},
            {"item": "1", "slot": "Date of birth", "expected": "1988-03-04"},
        ],
        "bound": {"0": "#p1", "1": "#p2"},
        "seen": [
            {
                "handle": "#p1",
                "slot": "Full name",
                "value": "Ann Example",
                "run_id": RUN,
            },
            {
                "handle": "#p1",
                "slot": "Date of birth",
                "value": "1990-01-02",
                "run_id": RUN,
            },
            {
                "handle": "#p2",
                "slot": "Full name",
                "value": "Ben Example",
                "run_id": RUN,
            },
            {
                "handle": "#p2",
                "slot": "Date of birth",
                "value": "1988-03-04",
                "run_id": RUN,
            },
        ],
        "complete": True,
    }


def _run(ev: dict[str, Any]) -> Any:
    return judge_instances(
        ev["planned"], ev["bound"], ev["seen"], run_id=RUN, complete=ev.get("complete")
    )


def _bad(item: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(item)
    out["value"] = str(out.get("value")) + " (other)"
    return out


def _inflate(ev: dict[str, Any], n: int) -> dict[str, Any]:
    planned = [{"item": str(i), "slot": "v", "expected": f"x{i}"} for i in range(n)]
    bound = {str(i): f"#r{i}" for i in range(n)}
    seen = [
        {
            "handle": f"#r{i}",
            "slot": "v",
            "value": f"x{i}" if i != n - 1 else "no",
            "run_id": RUN,
        }
        for i in range(n)
    ]
    return {**ev, "planned": planned, "bound": bound, "seen": seen}


def _swapped() -> dict[str, Any]:
    ev = _good()
    ev["bound"] = {"0": "#p2", "1": "#p1"}
    return ev


register(
    SiteSpec(
        id="gui.repeating.judge",
        classes="C1 C2 C5",
        target="openadapt_flow.gui.repeating:judge_instances",
        run=_run,
        is_pass=lambda out: out.status is Status.HELD,
        level=lambda out: out.status,
        seeds=lambda: [
            Seed(_good(), True),
            Seed(_swapped(), False, "items in each other's instances"),
            Seed(
                {**_good(), "seen": _good()["seen"][:2]}, False, "an item never added"
            ),
            Seed(
                {
                    **_good(),
                    "seen": _good()["seen"]
                    + [
                        {
                            "handle": "#p3",
                            "slot": "Full name",
                            "value": "",
                            "run_id": RUN,
                        }
                    ],
                },
                False,
                "an instance nobody planned",
            ),
            Seed(
                {**_good(), "bound": {"0": "#p1", "1": "#p1"}},
                False,
                "two items, one instance",
            ),
        ],
        items=key("seen"),
        plan=key("planned"),
        bad_item=_bad,
        inflate=_inflate,
        fields=index(key("seen"), 0),
        values=(
            key("value", "seen", 0),
            key("handle", "seen", 0),
            key("slot", "seen", 0),
            key("expected", "planned", 0),
            key("0", "bound"),
        ),
        subject=key("run_id", "seen", 0),
        partial=lambda ev: [("complete", {**ev, "complete": False})],
    )
)
