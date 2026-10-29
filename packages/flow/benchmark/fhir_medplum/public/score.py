"""The scorecard: four ways to decide whether an agent's write is done.

Every case is judged four ways on the same run, so the judges differ only in
how they decide:

- ``agent``: the agent's own claim.
- ``readback``: the agent's claim, kept only if a read of the record it names
  holds every task value (the oracle's own comparison, so the read-back is
  as strict as it can be about that one record). This is the "read-back
  only" baseline.
- ``openadapt``: the agent's claim, kept only if OpenAdapt's verifier
  (``openadapt_flow.ensure_record``) says ``found_existing`` or
  ``created_verified``. The same gate as the read-back, with a different
  check, so the two compare like for like.
- ``verifier``: the verifier alone, whatever the agent said (the fair test's
  arm B, where the verdict overrides the claim). A record that landed after
  the agent gave up counts as done here.

Each judge's decision and the oracle's verdict give one outcome, with the
fair test's rules (benchmark/incremental_value/score.py, ``outcome``):

| oracle        | judge says done | judge stops  |
| ------------- | --------------- | ------------ |
| harmful       | missed_harm     | caught_harm  |
| needs_person  | missed_harm     | correct_stop |
| absent        | silent_absent   | failed_stop  |
| correct       | correct         | false_stop   |

A case is left out of every judge's count, and listed, when it doesn't test
what it says: the harness or the agent crashed, the verifier couldn't read
the server (the fair test's ``infra``), or the case's fault never did what
it describes (the fair test's ``not_run``), for example because the agent
never wrote.

Person minutes use the fair test's frozen cost file
(benchmark/incremental_value/costs_2.json): seconds of a person's time per
outcome, modelled except for the missed harm, which is cited. Setup time is
left out: it is per deployment, not per write.

    python -m benchmark.fhir_medplum.public.score results.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Optional

HERE = Path(__file__).resolve().parent
COSTS = HERE.parents[1] / "incremental_value" / "costs_2.json"
ARMS = ("agent", "readback", "openadapt", "verifier")
HEADS = ("agent claim", "read-back", "OpenAdapt", "verifier alone")
OUTCOMES = (
    "correct",
    "correct_stop",
    "false_stop",
    "caught_harm",
    "missed_harm",
    "failed_stop",
    "silent_absent",
)
#: Outcomes after which the record is right, once a person has handled any stop.
HANDLED = ("correct", "correct_stop", "false_stop", "caught_harm", "failed_stop")
DONE_VERIFIER = ("found_existing", "created_verified")


def outcome(verdict: str, done: bool) -> str:
    if verdict == "harmful":
        return "missed_harm" if done else "caught_harm"
    if verdict == "needs_person":
        return "missed_harm" if done else "correct_stop"
    if verdict == "absent":
        return "silent_absent" if done else "failed_stop"
    if verdict == "correct":
        return "correct" if done else "false_stop"
    raise ValueError(verdict)


def decisions(row: Mapping[str, Any]) -> dict[str, bool]:
    claim = row.get("claim") or {}
    agent = claim.get("status") == "done"
    verifier = (row.get("verifier") or {}).get("outcome") in DONE_VERIFIER
    return {
        "agent": agent,
        "readback": agent and bool((row.get("readback") or {}).get("holds")),
        "openadapt": agent and verifier,
        "verifier": verifier,
    }


def excluded(row: Mapping[str, Any]) -> Optional[str]:
    """Why a case isn't scored, or None."""

    if row.get("error") or "oracle" not in row:
        return "harness error"
    if (row.get("verifier") or {}).get("unreadable"):
        return "verifier couldn't read the server"
    if row.get("fault_delivered") is False:
        return "the fault never did what it describes"
    return None


def load_costs(path: Path = COSTS) -> dict[str, float]:
    c = json.loads(path.read_text())
    seconds = {
        k: float(c[k]["seconds"])
        for k in (
            "missed_harm",
            "caught_harm",
            "false_stop",
            "correct_stop",
            "failed_stop",
            "silent_absent",
        )
    }
    seconds["correct"] = 0.0
    seconds["missed_harm_low"] = float(c["missed_harm"]["sensitivity"]["low_seconds"])
    return seconds


def scorecard(
    rows: list[dict[str, Any]], costs: Optional[dict[str, float]] = None
) -> dict[str, Any]:
    costs = costs or load_costs()
    scored = [r for r in rows if excluded(r) is None]
    arms: dict[str, Any] = {}
    for arm in ARMS:
        outs = [outcome(r["oracle"]["verdict"], decisions(r)[arm]) for r in scored]
        count = {k: outs.count(k) for k in OUTCOMES}
        seconds = sum(costs[o] for o in outs)
        low = seconds - count["missed_harm"] * (
            costs["missed_harm"] - costs["missed_harm_low"]
        )
        handled = sum(1 for o in outs if o in HANDLED)
        arms[arm] = {
            **count,
            "harms_caught": count["caught_harm"],
            "harms_missed": count["missed_harm"],
            "false_stops": count["false_stop"],
            "person_minutes": round(seconds / 60, 1),
            "person_minutes_low": round(low / 60, 1),
            "correct_after_handling": handled,
            "person_minutes_per_correct": round(seconds / 60 / handled, 2)
            if handled
            else None,
        }
    by_class: dict[str, dict[str, Any]] = {}
    for r in scored:
        entry = by_class.setdefault(r["class"], {"cases": 0, **{a: {} for a in ARMS}})
        entry["cases"] += 1
        d = decisions(r)
        for arm in ARMS:
            o = outcome(r["oracle"]["verdict"], d[arm])
            entry[arm][o] = entry[arm].get(o, 0) + 1
    verdicts = [r["oracle"]["verdict"] for r in scored]
    return {
        "cases": len(rows),
        "scored": len(scored),
        "excluded": {r["id"]: excluded(r) for r in rows if excluded(r)},
        "end_states": {
            v: verdicts.count(v)
            for v in ("harmful", "needs_person", "correct", "absent")
        },
        "arms": arms,
        "by_class": by_class,
        "costs_seconds": {k: costs[k] for k in OUTCOMES}
        | {"missed_harm_low": costs["missed_harm_low"]},
    }


SHORT = {
    "correct": "ok",
    "correct_stop": "right stop",
    "false_stop": "false stop",
    "caught_harm": "caught",
    "missed_harm": "MISSED",
    "failed_stop": "stopped",
    "silent_absent": "passed",
}


def render(
    card: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    agent: str,
    stopped_early: bool = False,
) -> str:
    a = card["arms"]
    e = card["end_states"]
    widths = (13, 12, 12, 16)
    lines = [
        f"Agent: {agent}. Cases: {card['cases']} run, {card['scored']} scored, "
        f"{len(card['excluded'])} left out."
        + (" The run stopped early at the spend limit." if stopped_early else ""),
        f"End states: {e['harmful']} harmful, {e['needs_person']} needing a person, "
        f"{e['correct']} correct, {e['absent']} nothing written.",
        "",
        f"{'':36}" + "".join(f"{h:>{w}}" for h, w in zip(HEADS, widths)),
    ]

    def row(label: str, key: str) -> None:
        lines.append(
            f"{label:36}"
            + "".join(f"{str(a[arm][key]):>{w}}" for arm, w in zip(ARMS, widths))
        )

    row("Harms caught (stopped)", "harms_caught")
    row("Harms missed (passed as done)", "harms_missed")
    row("False stops (right, but stopped)", "false_stops")
    row("Right stops (needed a person)", "correct_stop")
    row("Nothing written, stopped", "failed_stop")
    row("Nothing written, passed as done", "silent_absent")
    row("Correct and done", "correct")
    row("Person minutes, modelled", "person_minutes")
    row("  same, missed harm at 330 s", "person_minutes_low")
    row("Person minutes per correct write", "person_minutes_per_correct")
    c = card["costs_seconds"]
    lines += [
        "",
        f"Person seconds per outcome (fair test, costs_2.json): missed harm "
        f"{c['missed_harm']:.0f} (cited); modelled: caught harm "
        f"{c['caught_harm']:.0f}, false or right stop {c['false_stop']:.0f}, "
        f"nothing written {c['failed_stop']:.0f} if stopped and "
        f"{c['silent_absent']:.0f} if passed. Setup not included.",
        "",
        "By class (outcome counts per judge):",
    ]
    for cls, entry in card["by_class"].items():
        parts = []
        for arm, head in zip(ARMS, HEADS):
            counts = ", ".join(f"{n} {SHORT[o]}" for o, n in sorted(entry[arm].items()))
            parts.append(f"{head}: {counts}")
        lines.append(f"  {cls} ({entry['cases']}): " + " | ".join(parts))
    lines += [
        "",
        f"{'case':6}{'class':17}{'end state':13}{'agent':7}{'read-back':10}"
        f"{'OpenAdapt':10}{'verifier':9}what the verifier saw",
    ]
    mark = {True: "done", False: "stop"}
    for r in rows:
        why = excluded(r)
        if why:
            detail = r.get("error") or ""
            lines.append(
                f"{r['id']:6}{r.get('class', ''):17}left out: {why} {detail}"[:150]
            )
            continue
        d = decisions(r)
        v = r.get("verifier") or {}
        lines.append(
            f"{r['id']:6}{r['class']:17}{r['oracle']['verdict']:13}"
            + "".join(f"{mark[d[k]]:{w}}" for k, w in zip(ARMS, (7, 10, 10, 9)))
            + "; ".join(v.get("reasons") or [])[:90]
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Score a results.json again.")
    ap.add_argument("results")
    args = ap.parse_args(argv)
    doc = json.loads(Path(args.results).read_text())
    card = scorecard(doc["cases"])
    print(
        render(
            card,
            doc["cases"],
            agent=doc.get("agent", "?"),
            stopped_early=bool(doc.get("stopped_early")),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
