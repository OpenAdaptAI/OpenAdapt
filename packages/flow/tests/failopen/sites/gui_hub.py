"""gui.hub.judge (C1 C2): every planned section's card shows done on a fresh
read of the hub, with no dialog left open.

The obligations are the sections the run planned before opening any. A card
that shows not done is refuted; one that was not found or not read is not
checked; a section nobody planned is unexpected; an empty plan never passes.
"""

from __future__ import annotations

import copy
from typing import Any

from openadapt_flow.gui.hub import judge_hub
from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, index, key, register

RUN = "run-1"


def _card(sid: str, done: Any = True, found: Any = True) -> dict[str, Any]:
    return {"section_id": sid, "found": found, "done": done, "run_id": RUN}


def _good() -> dict[str, Any]:
    return {
        "planned": ["allergies", "medications"],
        "cards": [_card("allergies"), _card("medications")],
        "complete": True,
        "open_dialogs": 0,
    }


def _run(ev: dict[str, Any]) -> Any:
    return judge_hub(
        ev["planned"],
        ev["cards"],
        run_id=RUN,
        complete=ev.get("complete"),
        open_dialogs=ev.get("open_dialogs"),
    )


def _bad(item: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(item)
    out["done"] = False
    return out


def _inflate(ev: dict[str, Any], n: int) -> dict[str, Any]:
    planned = [f"s{i}" for i in range(n)]
    cards = [_card(f"s{i}", done=i != n - 1) for i in range(n)]
    return {**ev, "planned": planned, "cards": cards}


register(
    SiteSpec(
        id="gui.hub.judge",
        classes="C1 C2",
        target="openadapt_flow.gui.hub:judge_hub",
        run=_run,
        is_pass=lambda out: out.status is Status.HELD,
        level=lambda out: out.status,
        seeds=lambda: [
            Seed(_good(), True),
            Seed(
                {
                    **_good(),
                    "cards": [_card("allergies"), _card("medications", done=False)],
                },
                False,
                "one card not done",
            ),
            Seed(
                {**_good(), "cards": [_card("allergies")]},
                False,
                "a planned section with no card",
            ),
            Seed({**_good(), "open_dialogs": 1}, False, "a dialog left open"),
            Seed(
                {**_good(), "cards": _good()["cards"] + [_card("vitals")]},
                False,
                "a section nobody planned",
            ),
        ],
        items=key("cards"),
        plan=key("planned"),
        bad_item=_bad,
        inflate=_inflate,
        fields=index(key("cards"), 0),
        values=(
            key("done", "cards", 0),
            key("found", "cards", 0),
            key("section_id", "cards", 0),
            key("open_dialogs"),
        ),
        subject=key("run_id", "cards", 0),
        partial=lambda ev: [("complete", {**ev, "complete": False})],
    )
)
