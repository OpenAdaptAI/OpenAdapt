"""Score run 3: A and B on the same items, with the frozen cost files.

    python -m benchmark.fhir_medplum.run3.score <out>/rows.jsonl

Outcomes are the fair test's (``public/score.py``, from run 2's scorer).
People seconds come from ``incremental_value/costs_3.json``. It is run 2's
``costs_2.json`` except in two places, both set in PREREGISTRATION_3.md:

- **B's stops are charged from the stop card (modelled).** A caught harm
  costs the card's person seconds at x1 (``card.person_s_x1``). Any other B
  stop costs the larger of the card's seconds and costs_2's charge for that
  outcome, so a card that would undo a correct record is charged, never
  credited. The V1 charge (420 s for a caught harm) is reported as a
  sensitivity.
- **Setup** is B's measured contract time plus, in the primary figure,
  Medplum's one-time read-path qualification. The figure without it is
  reported too.

An item is left out of both arms when the harness failed, the fault set's
op isn't implemented, or the fault never did what it describes. B's
unreadable reads are B's stops, not exclusions.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from benchmark.fhir_medplum.public.score import DONE_VERIFIER, HANDLED, outcome

COSTS = Path(__file__).resolve().parents[2] / "incremental_value" / "costs_3.json"
ARMS = ("A", "B", "C")
#: Each comparison is (baseline, challenger); the challenger must win.
COMPARISONS = (("A", "B"), ("A", "C"), ("B", "C"))
STOPPED = ("caught_harm", "false_stop", "correct_stop", "failed_stop")


def load_costs(path: Path = COSTS) -> dict[str, Any]:
    return json.loads(path.read_text())


def excluded(row: Mapping[str, Any]) -> Optional[str]:
    if row.get("error") or ("oracle" not in row and not row.get("not_run")):
        return "harness error"
    if row.get("not_run"):
        return "not run"
    if row.get("fault_delivered") is False:
        return "the fault never did what it describes"
    return None


def done(row: Mapping[str, Any], arm: str) -> bool:
    """B and C take the verifier's word; they differ in when it lands."""

    if arm == "A":
        return (row.get("claim") or {}).get("status") == "done"
    return (row.get("verifier") or {}).get("outcome") in DONE_VERIFIER


def late(row: Mapping[str, Any]) -> bool:
    """C's stop arrives after the agent moved on: it claimed done."""

    return (row.get("claim") or {}).get("status") == "done"


def arm_outcome(
    row: Mapping[str, Any], arm: str, downstream_after: Optional[float] = None
) -> str:
    """The outcome, with C's late catches read as missed when someone could
    have used the record first (the sensitivity ``downstream_after`` seconds)."""

    out = outcome(row["oracle"]["verdict"], done(row, arm))
    if (
        arm == "C"
        and downstream_after is not None
        and out == "caught_harm"
        and late(row)
        and float(row.get("detect_s") or 0) > downstream_after
    ):
        return "missed_harm"
    return out


def seconds(
    row: Mapping[str, Any],
    arm: str,
    out: str,
    costs: Mapping[str, Any],
    *,
    card: bool = True,
    factor: float = 1.0,
    missed: str = "seconds",
) -> float:
    if out == "correct":
        return 0.0
    if out == "missed_harm":
        m = costs["missed_harm"]
        base = m["seconds"] if missed == "seconds" else m["sensitivity"][missed]
        return float(base) * factor
    base = float(costs[out]["seconds"])
    stopped = out in STOPPED
    if arm == "C" and stopped and late(row):
        # A person reopens an item they had closed: B's charge plus the
        # re-engagement, and a late catch never costs more than the cap.
        now = seconds(row, "B", out, costs, card=card, factor=factor, missed=missed)
        lc = costs["late_catch"]
        s = now + float(lc["reengage_seconds"]) * factor
        if out != "caught_harm":
            return s
        # Between catching it now and 330 s; never cheaper than now.
        return max(now, min(s, float(lc["cap_seconds"]) * factor))
    if arm in ("B", "C") and card and stopped:
        c = (row.get("card") or {}).get("person_s")
        if c is not None:
            s = float(c[str(factor)]) if str(factor) in c else float(c["1.0"]) * factor
            return s if out == "caught_harm" else max(s, base * factor)
    return base * factor


def arm_summary(
    rows: list[dict[str, Any]],
    arm: str,
    costs: Mapping[str, Any],
    *,
    card: bool = True,
    factor: float = 1.0,
    missed: str = "seconds",
    with_qualification: bool = True,
    amortize_over: Optional[int] = None,
    downstream_after: Optional[float] = None,
) -> dict[str, Any]:
    counted = [(r, arm_outcome(r, arm, downstream_after)) for r in rows]
    n = len(counted)
    count: dict[str, int] = {}
    for _, o in counted:
        count[o] = count.get(o, 0) + 1
    people_s = sum(
        seconds(r, arm, o, costs, card=card, factor=factor, missed=missed)
        for r, o in counted
    )
    setup = costs["setup_min"][arm]
    if "contract" in setup and setup["contract"] is None:
        raise ValueError(f"{arm}'s contract minutes aren't filled in yet")
    setup_min = float(setup["agent"]) + float(setup.get("contract") or 0.0)
    if with_qualification:
        setup_min += float(setup.get("read_path_qualification", 0.0))
    if amortize_over:
        setup_min = setup_min * n / amortize_over
    correct_after = sum(1 for _, o in counted if o in HANDLED)
    total_min = setup_min + people_s / 60
    cards = [r.get("card") or {} for r, o in counted if arm != "A" and r.get("card")]
    # B waits for the check before the next item; A and C don't.
    walls = sorted(
        float(r.get("agent_s", 0)) + (float(r.get("verify_s", 0)) if arm == "B" else 0)
        for r, _ in counted
    )
    detect = sorted(
        float(r["detect_s"])
        for r, o in counted
        if arm == "C" and o in STOPPED and late(r) and r.get("detect_s") is not None
    )
    dups = sum(
        1
        for r, _ in counted
        if any(
            "not one" in x or "now match" in x for x in r["oracle"].get("reasons", [])
        )
    )
    return {
        "items": n,
        "correct_completions": count.get("correct", 0) + count.get("correct_stop", 0),
        "missed_harmful_changes": count.get("missed_harm", 0),
        "caught_harmful_changes": count.get("caught_harm", 0),
        "false_stops": count.get("false_stop", 0),
        "correct_stops": count.get("correct_stop", 0),
        "failed_stops": count.get("failed_stop", 0),
        "silent_absent": count.get("silent_absent", 0),
        "correct_after_handling": correct_after,
        "people_min_items": round(people_s / 60, 1),
        "setup_min": round(setup_min, 2),
        "people_min_per_correct_txn": round(total_min / correct_after, 3)
        if correct_after
        else None,
        "duplicates": dups,
        "wall_s_median": walls[len(walls) // 2] if walls else None,
        "wall_s_p90": walls[min(len(walls) - 1, int(len(walls) * 0.9))]
        if walls
        else None,
        "model_usd_per_item": round(
            sum(float(r.get("cost_usd", 0)) for r, _ in counted) / n, 4
        )
        if n
        else None,
        "late_stops": len(detect) if arm == "C" else None,
        "detect_s_median": detect[len(detect) // 2] if detect else None,
        "detect_s_p90": detect[min(len(detect) - 1, int(len(detect) * 0.9))]
        if detect
        else None,
        "cards_by_category": _tally(c.get("category") for c in cards),
        "card_failed_read_backs": sum(1 for c in cards if c.get("action_ok") is False),
    }


def _tally(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def verdict(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    n = max(a["items"], b["items"])
    allowance = math.ceil(n / 100)
    pa, pb = a["people_min_per_correct_txn"], b["people_min_per_correct_txn"]
    c1 = b["missed_harmful_changes"] < a["missed_harmful_changes"]
    c2 = b["false_stops"] <= a["false_stops"] + allowance
    c3 = pa is not None and pb is not None and pb < pa
    return {
        "1_fewer_missed_harms": c1,
        "2_false_stops_within": c2,
        "false_stop_allowance": allowance,
        "3_fewer_people_min_per_correct_txn": c3,
        "wins": c1 and c2 and c3,
    }


def score(all_rows: list[dict[str, Any]], costs: Mapping[str, Any]) -> dict[str, Any]:
    left_out = {r["item"]: excluded(r) for r in all_rows if excluded(r)}
    rows = [r for r in all_rows if r["item"] not in left_out]

    def both(**kw: Any) -> dict[str, Any]:
        s = {arm: arm_summary(rows, arm, costs, **kw) for arm in ARMS}
        verdicts = {f"{n} vs {b}": verdict(s[b], s[n]) for b, n in COMPARISONS}
        b, c = s["B"], s["C"]
        no_worse = {
            "missed_le": c["missed_harmful_changes"] <= b["missed_harmful_changes"],
            "false_stops_within": c["false_stops"]
            <= b["false_stops"] + math.ceil(max(b["items"], c["items"]) / 100),
            "fewer_people_min": (c["people_min_per_correct_txn"] or 0)
            < (b["people_min_per_correct_txn"] or 0),
        }
        verdicts["C no worse than B (secondary)"] = {
            **no_worse,
            "holds": all(no_worse.values()),
        }
        return {**s, "verdicts": verdicts}

    primary = both()
    sensitivity = {
        "B_and_C_caught_harm_at_V1_420s": both(card=False),
        "setup_without_read_path_qualification": both(with_qualification=False),
        "x0.5": both(factor=0.5),
        "x2": both(factor=2.0),
        "missed_harm_low": both(missed="low_seconds"),
        "missed_harm_high": both(missed="high_seconds"),
        "setup_amortized_1000": both(amortize_over=costs["amortized_items_secondary"]),
        **{
            f"C_late_catch_used_after_{t:g}s": both(downstream_after=t)
            for t in costs["late_catch"]["downstream_after_seconds"]
        },
    }
    by_source: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        for arm in ARMS:
            o = arm_outcome(r, arm)
            src = by_source.setdefault(r["source"], {}).setdefault(arm, {})
            src[o] = src.get(o, 0) + 1
    return {
        "items_scored": len(rows),
        "left_out": left_out,
        "primary": primary,
        "sensitivity": sensitivity,
        "by_source": by_source,
        "author_vs_oracle": _tally(
            f"{r.get('author_expect')}->{r['oracle']['verdict']}"
            for r in rows
            if r["source"] == "blind"
        ),
        "model_usd": round(sum(float(r.get("cost_usd", 0.0)) for r in all_rows), 2),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rows")
    ap.add_argument("--costs", default=str(COSTS))
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rows = [json.loads(x) for x in Path(args.rows).read_text().splitlines() if x]
    doc = score(rows, load_costs(Path(args.costs)))
    text = json.dumps(doc, indent=2)
    if args.json:
        Path(args.json).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
