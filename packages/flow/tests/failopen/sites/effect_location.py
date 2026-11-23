"""effect_location.evaluate_all: every planned value shown where expected (C1, C2, C5).

The plan (the run's intended values) is the obligation set; the readings are
the results. A dropped reading, an empty plan, a failed read, a reading from
another run, a partial read, a missing field and a close-but-wrong displayed
text must all keep the run from passing. The seeds cover a plain label, a
composite summary row (one text, two values), a split date and an amount.
"""

from __future__ import annotations

import copy
from typing import Any

from openadapt_flow.verdict import Status
from tests.failopen.harness import Seed, SiteSpec, index, key, register

from openadapt_flow.effect_location import EffectLocation, Reading, evaluate_all

RUN = "run-1"


def _loc(
    lid: str, keys: list[str], codec: dict[str, Any], start: str = "same_screen"
) -> dict[str, Any]:
    return {
        "schema": "openadapt.effect-location/v1",
        "id": lid,
        "keys": keys,
        "path": ["s1"],
        "start": start,
        "locator": {"kind": "label", "value": lid},
        "codec": codec,
        "expected": {},
        "expected_count": 1,
    }


def _read(
    lid: str, *matches: dict[str, Any], start: str = "same_screen"
) -> dict[str, Any]:
    return {
        "location_id": lid,
        "run_id": RUN,
        "start": start,
        "matches": list(matches),
        "complete": True,
        "error": None,
        "basis": None,
    }


ROW = {
    "kind": "template",
    "pattern": r"(?P<n>.+) \((?P<d>[^)]*)\)",
    "parts": [
        {"group": "n", "key": "t_name", "codec": {"kind": "text"}},
        {"group": "d", "key": "t_dob", "codec": {"kind": "date", "date_order": "MDY"}},
    ],
}


def _good(start: str = "same_screen") -> dict[str, Any]:
    return {
        "run_id": RUN,
        "plan": [
            ["name", "Ada Byron"],
            ["t_name", "Ada Byron"],
            ["t_dob", "1990-01-31"],
            ["dep", "2024-03-07"],
            ["cost", 125000],
        ],
        "locations": [
            _loc("name", ["name"], {"kind": "text"}, start),
            _loc("row", ["t_name", "t_dob"], ROW, start),
            _loc("dep", ["dep"], {"kind": "split_date"}, start),
            _loc("cost", ["cost"], {"kind": "money", "affixes": ["$"]}, start),
        ],
        "readings": [
            _read("name", {"text": "Ada Byron"}, start=start),
            _read("row", {"text": "Ada Byron (01/31/1990)"}, start=start),
            _read("dep", {"year": "2024", "month": "3", "day": "7"}, start=start),
            _read("cost", {"text": "$1,250.00"}, start=start),
        ],
    }


def _run(ev: dict[str, Any]) -> Any:
    return evaluate_all(
        [tuple(p) for p in ev["plan"]],
        [EffectLocation.from_dict(d) for d in ev["locations"]],
        [Reading.from_dict(d) for d in ev["readings"]],
        run_id=ev["run_id"],
    )


def _bad(reading: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(reading)
    for m in out["matches"]:
        for part in m:
            m[part] = "9" + str(m[part]) if part == "day" else "x" + str(m[part])
    return out


def _inflate(_ev: Any, n: int) -> dict[str, Any]:
    plan = [[f"k{i}", f"v{i}"] for i in range(n)]
    locs = [_loc(f"k{i}", [f"k{i}"], {"kind": "text"}) for i in range(n)]
    reads = [_read(f"k{i}", {"text": f"v{i}"}) for i in range(n - 1)]
    reads.append(_read(f"k{n - 1}", {"text": "wrong"}))
    return {"run_id": RUN, "plan": plan, "locations": locs, "readings": reads}


def _raise(ev: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    out = copy.deepcopy(ev)
    # The adapter saw the value, then the read raised: the matches it kept
    # are not evidence.
    out["readings"][0]["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _partial(ev: dict[str, Any]) -> list[tuple[str, Any]]:
    out = []
    for i in range(len(ev["readings"])):
        p = copy.deepcopy(ev)
        p["readings"][i]["complete"] = False
        out.append((f"reading {i} complete", p))
    return out


def _seed(change: Any) -> dict[str, Any]:
    ev = _good()
    change(ev)
    return ev


register(
    SiteSpec(
        id="effect_location.evaluate_all",
        classes="C1 C2 C5",
        target="openadapt_flow.effect_location.evaluator:evaluate_all",
        run=_run,
        is_pass=lambda out: out.passes,
        level=lambda out: Status(out.status),
        seeds=lambda: [
            Seed(_good(), True, "label, summary row, split date, amount"),
            Seed(_good("new_session"), True, "the same from a new session"),
            Seed(
                _seed(
                    lambda ev: ev["readings"][3]["matches"][0].update(text="$250.00")
                ),
                False,
                "a different amount",
            ),
            Seed(
                _seed(
                    lambda ev: ev["readings"][1]["matches"][0].update(
                        text="Ada Byron (01/30/1990)"
                    )
                ),
                False,
                "the row shows another date",
            ),
            Seed(
                _seed(lambda ev: ev["readings"][0].update(matches=[])),
                False,
                "not found",
            ),
            Seed(
                _seed(
                    lambda ev: ev["readings"][0]["matches"].append(
                        {"text": "Ada Byron"}
                    )
                ),
                False,
                "found twice, expected once",
            ),
            Seed(
                _seed(lambda ev: ev["locations"][0].update(start="new_session")),
                False,
                "start condition not met",
            ),
        ],
        items=key("readings"),
        plan=key("plan"),
        bad_item=_bad,
        inflate=_inflate,
        fields=index(key("readings"), 0),
        values=(
            key("text", "readings", 0, "matches", 0),
            key("text", "readings", 1, "matches", 0),
            key("day", "readings", 2, "matches", 0),
            key("text", "readings", 3, "matches", 0),
            key("complete", "readings", 0),
            key("start", "readings", 0),
            key("expected_count", "locations", 0),
            key("kind", "locations", 0, "codec"),
        ),
        probe_raise=_raise,
        subject=key("run_id", "readings", 0),
        partial=_partial,
        notes={
            "partial": "a reading whose area was not read in full can't show the "
            "value is the only one there, so it never holds (M15)",
        },
    )
)
