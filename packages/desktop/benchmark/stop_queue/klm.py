"""Seconds per stop, before and after the stop queue: a keystroke-level model.

This is an estimate, not a measurement of a person. It applies the
keystroke-level model (Card, Moran and Newell, 1983; operator times as
Kieras, "Using the Keystroke-Level Model to Estimate Execution Times", 2001)
to the steps a person takes for each of the drill's stops:

* ``before``: Needs you as it is today. Open the item, read why it stopped,
  switch to the app, search for the patient, compare each listed record with
  the item's values, switch back, tick "I looked at the record" when the
  action needs it, and pick the action.
* ``after``: the stop's card. Read the question, scan the marked comparison
  for each possible match, press one key, confirm with Touch ID.

Both arms count only deciding, not entering an item by hand afterwards.
Deciding is the part the queue changes. A person measures the real numbers
with the practice page (see README.md).

Run from ``packages/desktop``::

    uv run python benchmark/stop_queue/klm.py benchmark/stop_queue/drill-2026-10-24.json
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

# Operator times in seconds (Kieras 2001).
K = 0.28  # one keystroke, average skilled typist
P = 1.1  # point with the mouse
BB = 0.2  # click (press and release)
H = 0.4  # move a hand between keyboard and mouse
M = 1.2  # mental act: read, decide, recall, compare one value
# System waits (assumptions, not measured):
APP_SEARCH_WAIT = 1.5  # the app's patient search returns
TOUCH_ID = 1.0  # a Touch ID prompt is confirmed
#: Item fields a person carries from Desktop to the app to compare.
CARRIED_FIELDS = 3


def _before(card: dict) -> tuple[float, list[str]]:
    lname = next(
        (r["value"] for r in card["evidence"]["source"] if r["field"].lower() == "lname"),
        "patient",
    )
    candidates = card["evidence"]["candidates"] or [{"fields": []}]
    compared = max(len(c["fields"]) for c in candidates) or CARRIED_FIELDS
    steps = [
        ("open the item in Needs you", M + P + BB),
        ("read why it stopped", M),
    ]
    if card["kind"] in {"same_record", "confirm_save"}:
        steps += [
            ("remember the item's key values", CARRIED_FIELDS * M),
            ("switch to the app", M + 2 * K),
            ("open the patient search", P + BB + H),
            ("type the last name and search", (len(lname) + 1) * K + APP_SEARCH_WAIT),
        ]
        for _ in candidates:
            steps.append(
                ("open one listed record and compare it", P + BB + APP_SEARCH_WAIT + compared * M)
            )
        steps += [
            ("switch back to Desktop", 2 * K),
            ('tick "I looked at the record"', P + BB),
        ]
    steps.append(("pick the action", M + P + BB))
    return sum(t for _, t in steps), [name for name, _ in steps]


def _after(card: dict) -> tuple[float, list[str]]:
    candidates = [c for c in card["evidence"]["candidates"] if c["fields"]]
    steps = [("read the question", M)]
    if candidates:
        # Each row is already marked Same or Different; scanning the marks
        # of one possible match is one mental act.
        steps += [("scan one marked match", M) for _ in candidates]
    elif card["kind"] == "confirm_save":
        # No read-back values on this card yet: the person still looks the
        # record up in the app, as before, minus the inbox steps.
        before, _ = _before(card)
        steps.append(("look the record up in the app", before - 2 * (M + P + BB) - M))
    else:
        steps.append(("read the reason", M))
    steps += [
        ("decide", M),
        ("press the choice's key", K),
        ("confirm with Touch ID", H + TOUCH_ID),
    ]
    return sum(t for _, t in steps), [name for name, _ in steps]


def estimate(drill: dict) -> dict:
    rows = []
    for stop in drill["stops"]:
        card = stop["card"]
        before, _ = _before(card)
        after, _ = _after(card)
        rows.append(
            {
                "item_id": stop["item_id"],
                "kind": card["kind"],
                "possible_matches": len(card["evidence"]["candidates"]),
                "before_s": round(before, 1),
                "after_s": round(after, 1),
                "soak_charged_s": stop["soak_operator"]["charged_s"],
                "soak_action": stop["soak_operator"]["action"],
            }
        )
    before = [r["before_s"] for r in rows]
    after = [r["after_s"] for r in rows]
    return {
        "schema": "openadapt.stop-queue-klm/v1",
        "method": "keystroke-level model (Card, Moran, Newell 1983; Kieras 2001)",
        "assumptions": {
            "K": K,
            "P": P,
            "BB": BB,
            "H": H,
            "M": M,
            "app_search_wait_s": APP_SEARCH_WAIT,
            "touch_id_s": TOUCH_ID,
            "carried_fields": CARRIED_FIELDS,
        },
        "stops": rows,
        "summary": {
            "stops": len(rows),
            "before_mean_s": round(statistics.mean(before), 1),
            "after_mean_s": round(statistics.mean(after), 1),
            "before_median_s": round(statistics.median(before), 1),
            "after_median_s": round(statistics.median(after), 1),
        },
    }


def main() -> None:
    drill = json.loads(Path(sys.argv[1]).read_text())
    result = estimate(drill)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
