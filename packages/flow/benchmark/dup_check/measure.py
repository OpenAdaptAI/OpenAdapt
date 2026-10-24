"""Screen duplicate check against record duplicate check, offline, on the soak's stream.

    python -m benchmark.dup_check.measure --out DIR

The stream is the production soak's seed-7 workload (#1513): the committed
55-item stream it ran, and a longer seed-7 stream from the same generator.
Planted into each are true duplicates the soak lacks: an existing patient
referred again under a married name, under a hyphenated name, and with day
and month swapped.

The patient table each item meets is the existing charts plus every earlier
item's right outcome (the soak's truth), plus another clerk's chart for
``concurrent_same``. So both checks see the same records, and only the check
differs.

- **screen**: the rule ``no_duplicate_candidate`` applies to OpenEMR's
  possible-duplicate list (``production_arm.with_duplicate_list_check``):
  OpenEMR lists every chart that shares the first name, the last name or the
  date of birth, and the check stops when a listed row carries every word of
  the run's last name or shows its date of birth.
- **record**: ``runtime.checks.duplicate_record.check_duplicate`` over the
  same table with FHIR search semantics; ``exact`` and ``close`` stop.

A *false stop* is a stop on an item whose right outcome is a new chart. A
*missed duplicate* is a proceed on an item whose right outcome is no new
chart because the patient already has one. Dates are converted to ISO first,
as intake does since #1514.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from benchmark.lifecycle import workload as wl
from openadapt_flow.runtime.checks.duplicate_record import (
    PatientRecord,
    TableSource,
    check_duplicate,
)

HERE = Path(__file__).resolve().parent
SOAK = HERE.parent / "lifecycle" / "results" / "production-2026-10-24" / "after"
#: Right outcome "no new chart" because the person already has one.
DUPLICATE_KINDS = {
    "dup_existing",
    "dup_resend",
    "ambiguous_same_person",
    "concurrent_same",
    "planted_married",
    "planted_hyphenated",
    "planted_swapped_dob",
    "heldout_first_typo",
    "heldout_last_typo",
    "heldout_married_dob_typo",
}
MARRIED = ("Okafor", "Lindqvist", "Brennan", "Moreau", "Castellanos", "Whitcombe")


@dataclass
class Item:
    item_id: str
    exception: str
    params: dict[str, str]
    #: The chart the right outcome creates, or None.
    intended: Optional[dict[str, str]]
    #: Charts that appear just before the item (another clerk's).
    before: list[dict[str, str]]


def iso(value: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else (value or "")


def from_ledger(path: Path) -> tuple[list[dict[str, str]], list[Item]]:
    existing, items = [], []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["type"] == "existing_chart":
            existing.append({k: v for k, v in row.items() if k != "type"})
        elif row["type"] == "item":
            t = row["truth"]
            before = [
                i["person"]
                for i in t["inject"]
                if i.get("event") == "other_clerk_creates"
            ]
            items.append(
                Item(
                    row["item_id"], t["exception"], row["params"], t["intended"], before
                )
            )
    return existing, items


def from_generator(
    seed: int, n: int, days: int
) -> tuple[list[dict[str, str]], list[Item]]:
    w = wl.generate(seed, n, days)
    items = []
    for it in w.items:
        t = w.truth[it.item_id]
        before = [
            i["person"] for i in t.inject if i.get("event") == "other_clerk_creates"
        ]
        items.append(Item(it.item_id, t.exception, dict(it.params), t.intended, before))
    return list(w.existing), items


def plant(existing: list[dict[str, str]], items: list[Item], seed: int) -> list[Item]:
    """Insert true duplicates of existing patients at seeded positions."""

    rng = random.Random(seed)
    planted: list[Item] = []
    for n, person in enumerate(existing):
        married = dict(person, lname=MARRIED[n % len(MARRIED)])
        planted.append(Item(f"plant-m-{n}", "planted_married", married, None, []))
        hyph = dict(
            person, lname=f"{person['lname']}-{MARRIED[(n + 1) % len(MARRIED)]}"
        )
        planted.append(Item(f"plant-h-{n}", "planted_hyphenated", hyph, None, []))
        y, m, d = person["DOB"].split("-")
        if int(d) <= 12 and d != m:
            planted.append(
                Item(
                    f"plant-s-{n}",
                    "planted_swapped_dob",
                    dict(person, DOB=f"{y}-{d}-{m}"),
                    None,
                    [],
                )
            )
        # Held out: variants the record check's rules were not written for.
        typo = lambda s: s[:-2] + s[-1] + s[-2] if len(s) > 3 else s + "e"  # noqa: E731
        planted.append(
            Item(
                f"held-f-{n}",
                "heldout_first_typo",
                dict(person, fname=typo(person["fname"])),
                None,
                [],
            )
        )
        planted.append(
            Item(
                f"held-l-{n}",
                "heldout_last_typo",
                dict(person, lname=typo(person["lname"])),
                None,
                [],
            )
        )
        planted.append(
            Item(
                f"held-md-{n}",
                "heldout_married_dob_typo",
                dict(
                    person,
                    lname=MARRIED[(n + 2) % len(MARRIED)],
                    DOB=f"{y}-{m}-{int(d) % 28 + 1:02d}",
                ),
                None,
                [],
            )
        )
    out = list(items)
    for p in planted:
        out.insert(rng.randint(0, len(out)), p)
    return out


def record_of(i: int, person: dict[str, str]) -> PatientRecord:
    return PatientRecord(
        str(i), person["fname"], person["lname"], person.get("DOB", "")
    )


def words(value: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", (value or "").casefold())


def screen_stops(params: dict[str, str], table: list[PatientRecord]) -> bool:
    """The no_duplicate_candidate rule on OpenEMR's possible-duplicate list."""

    lname, dob = params.get("lname", ""), params.get("DOB", "")
    if not words(lname) or not dob:
        return True  # a missing parameter fails the check closed
    fname = params.get("fname", "")
    listed = [
        r
        for r in table
        if words(r.fname) == words(fname)
        or words(r.lname) == words(lname)
        or r.dob == dob
    ]
    for r in listed:
        row = set(words(f"{r.fname} {r.lname} {r.dob}"))
        if all(w in row for w in words(lname)) or r.dob == dob:
            return True
    return False


def measure(existing: list[dict[str, str]], items: list[Item]) -> dict[str, Any]:
    table = [record_of(i, p) for i, p in enumerate(existing)]
    rows = []
    for item in items:
        for person in item.before:
            table.append(record_of(len(table), person))
        params = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in item.params.items()
        }
        params["DOB"] = iso(params.get("DOB", ""))
        is_dup = item.exception in DUPLICATE_KINDS
        new_chart = item.intended is not None
        screen = screen_stops(params, table)
        result = check_duplicate(TableSource(table), params)
        rows.append(
            {
                "item": item.item_id,
                "exception": item.exception,
                "right": "new chart"
                if new_chart
                else ("no chart: duplicate" if is_dup else "no chart"),
                "screen": "stop" if screen else "proceed",
                "record": result.decision,
                "record_why": [c.why for c in result.candidates],
            }
        )
        if item.intended is not None:
            table.append(record_of(len(table), item.intended))
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"items": len(rows)}
    new = [r for r in rows if r["right"] == "new chart"]
    dups = [r for r in rows if r["right"] == "no chart: duplicate"]
    out["new_chart_items"], out["true_duplicates"] = len(new), len(dups)
    for check, stops in (
        ("screen", lambda r: r["screen"] == "stop"),
        ("record", lambda r: r["record"] != "none"),
    ):
        false_stops = [r for r in new if stops(r)]
        missed = [r for r in dups if not stops(r)]
        out[check] = {
            "false_stops": len(false_stops),
            "false_stops_per_100_items": round(100 * len(false_stops) / len(rows), 1),
            "false_stops_by_exception": _count(false_stops),
            "missed_duplicates": len(missed),
            "missed_by_exception": _count(missed),
            "caught_duplicates": len(dups) - len(missed),
        }
    out["record"]["decisions"] = _count(rows, key="record")
    return out


def _count(rows: list[dict[str, Any]], key: str = "exception") -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return dict(sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--long-items", type=int, default=500)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    runs = {}
    existing, items = from_ledger(SOAK / "workload.jsonl")
    runs["soak-seed7-55"] = measure(existing, plant(existing, items, 7))
    existing, items = from_generator(7, args.long_items, 10)
    runs[f"seed7-{args.long_items}"] = measure(existing, plant(existing, items, 7))
    for name, run in runs.items():
        (args.out / f"{name}.json").write_text(
            json.dumps(run, indent=2, sort_keys=True) + "\n"
        )
        print(name, json.dumps(run["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
