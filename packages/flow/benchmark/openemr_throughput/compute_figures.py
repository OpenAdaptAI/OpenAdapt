"""Collect raw run data and derive the throughput figures.

    python -m benchmark.openemr_throughput.compute_figures collect --runs OUT --tags TAG ...
    python -m benchmark.openemr_throughput.compute_figures figures [--check]

``collect`` copies, for each tag, the harness's ``summary.json`` and
``items.json`` (and per-item ``profile.json`` files) from the evidence folder
into ``results/``. ``figures`` derives ``results/figures.json`` from those
files only; ``--check`` fails when ``figures.json`` is stale.

Entries per hour = items saved and read back correctly x 3600 / wall time.
The wall time of a worklist run starts before the first sign-in and ends when
the last worker finishes; the test setup's own database read-back afterwards
is not in it. Every arm is pooled over its rounds: total correct entries over
total wall time.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

ARMS = {
    "serial": "1 worker",
    "n4": "4 workers",
    "n8": "8 workers",
    "n8q50": "8 workers, 50 ms page-quiet window",
    "n4w1": "4 workers, read-back window 1",
}


def collect(runs: Path, tags: list[str]) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "profiles").mkdir(exist_ok=True)
    summaries = []
    item_rows = []
    for tag in tags:
        src = runs / tag
        summary = json.loads((src / "summary.json").read_text())
        summary.pop("gate", None)
        summaries.append(summary)
        for item in json.loads((src / "items.json").read_text()):
            item_rows.append({"tag": tag, **item})
        profiles = {}
        for prof in sorted((src / "pool" / "items").glob("*/profile.json")):
            data = json.loads(prof.read_text())
            profiles[prof.parent.name] = {"buckets": data["buckets"], "steps": data["steps"]}
        if profiles:
            (RESULTS / "profiles" / f"{tag}.json").write_text(json.dumps(profiles, indent=1))
    with open(RESULTS / "runs.jsonl", "w") as fh:
        for s in summaries:
            fh.write(json.dumps(s, sort_keys=True) + "\n")
    cols = ["tag", "idx", "worker", "state", "correct", "new_charts", "readback",
            "duplicate_case", "wall_s", "readback_mode", "claimed_at", "finished_at"]
    with open(RESULTS / "items.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for row in item_rows:
            w.writerow(row)


def collect_profile_main(src_root: Path, tag: str) -> None:
    """Profiles of current main taken with the published method (fresh
    browser context and sign-in per run; see README)."""
    out = {}
    rows = []
    for result in sorted(src_root.glob(f"{tag}/block-*/*/result.json")):
        res = json.loads(result.read_text())
        prof = result.parent / "profile.json"
        if not prof.exists():
            continue
        data = json.loads(prof.read_text())
        key = f"{result.parent.parent.name}/{result.parent.name}"
        out[key] = {"buckets": data["buckets"], "steps": data["steps"]}
        rows.append({"run": key, "correct": res.get("correct"), "readback": res.get("readback"),
                     "outcome": res.get("outcome"), **(res.get("phases") or {}),
                     "load1_mean": (res.get("load") or {}).get("load1_mean")})
    (RESULTS / "profiles").mkdir(parents=True, exist_ok=True)
    (RESULTS / "profiles" / f"{tag}.json").write_text(json.dumps(out, indent=1))
    (RESULTS / f"{tag}.runs.json").write_text(json.dumps(rows, indent=1))


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _arm(tag: str) -> str | None:
    head = tag.split("-")[0]
    return head if head in ARMS else None


def figures() -> dict[str, Any]:
    runs = [json.loads(l) for l in (RESULTS / "runs.jsonl").read_text().splitlines() if l.strip()]
    with open(RESULTS / "items.csv") as fh:
        items = list(csv.DictReader(fh))
    out: dict[str, Any] = {"arms": {}, "profiles": {}, "halt_test": None}
    for arm, label in ARMS.items():
        mine = [r for r in runs if _arm(r["tag"]) == arm]
        if not mine:
            continue
        tags = {r["tag"] for r in mine}
        its = [i for i in items if i["tag"] in tags]
        times = [float(i["wall_s"]) for i in its if i["wall_s"] and i["correct"] == "True"]
        wall = sum(r["wall_s"] for r in mine)
        correct = sum(r["saved_correct"] for r in mine)
        ran = sum(r["items_run"] for r in mine)
        work = []
        for r in mine:
            mine_items = [i for i in its if i["tag"] == r["tag"] and i["claimed_at"]]
            if mine_items:
                work.append(max(float(i["finished_at"]) for i in mine_items)
                            - min(float(i["claimed_at"]) for i in mine_items))
        out["arms"][arm] = {
            "label": label,
            "rounds": len(mine),
            "items_run": ran,
            "saved_correct": correct,
            "not_saved": ran - correct,
            "wrong_or_extra_charts": sum(r["wrong_or_extra_charts"] for r in mine),
            "halted_nothing_saved": sum(r.get("not_saved", 0) for r in mine),
            "stopped_runs": sum(1 for r in mine if r["stop"]),
            "wall_s_total": round(wall, 1),
            "entries_per_hour": round(correct * 3600 / wall, 1) if wall else None,
            "entries_per_hour_excluding_sign_in": round(correct * 3600 / sum(work), 1) if work else None,
            "item_s": {
                "n": len(times),
                "median": round(statistics.median(times), 2) if times else None,
                "p10": round(_q(times, 0.1), 2) if times else None,
                "p90": round(_q(times, 0.9), 2) if times else None,
            },
            "sign_in_s_median": round(statistics.median(
                [v for r in mine for v in r["sign_in_s"].values()]), 2),
            "load1_mean": round(statistics.mean(r["load1_mean"] for r in mine), 1),
            "load1_max": max(r["load1_max"] for r in mine),
            "engine_sha": sorted({r["engine_sha"][:8] for r in mine}),
            "invalid_runs": sum(1 for r in mine if not r["valid"]),
        }
    for prof in sorted((RESULTS / "profiles").glob("*.json")):
        data = json.loads(prof.read_text())
        buckets: dict[str, list[float]] = {}
        for item in data.values():
            for k, v in item["buckets"].items():
                buckets.setdefault(k, []).append(v)
        totals = [sum(item["buckets"].values()) for item in data.values()]
        out["profiles"][prof.stem] = {
            "n": len(data),
            "task_s_median": round(statistics.median(totals), 2) if totals else None,
            "bucket_s_median": {k: round(statistics.median(v), 2) for k, v in
                                sorted(buckets.items(), key=lambda kv: -statistics.median(kv[1]))},
        }
    halt = [r for r in runs if r["tag"].startswith("halt")]
    if halt:
        r = halt[0]
        out["halt_test"] = {
            "workers": r["workers"], "items": r["items"], "halt_at": r["halt_at"],
            "stop": r["stop"], "counts": r["counts"], "items_run": r["items_run"],
            "wrong_or_extra_charts": r["wrong_or_extra_charts"],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--runs", required=True, type=Path)
    c.add_argument("--tags", nargs="+", required=True)
    c.add_argument("--profile-main", default=None, help="tag of the published-method profile")
    c.add_argument("--profile-main-root", type=Path, default=None)
    f = sub.add_parser("figures")
    f.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect(a.runs, a.tags)
        if a.profile_main:
            collect_profile_main(a.profile_main_root or a.runs, a.profile_main)
        return 0
    fig = figures()
    text = json.dumps(fig, indent=1, sort_keys=True) + "\n"
    path = RESULTS / "figures.json"
    if a.check:
        if not path.exists() or path.read_text() != text:
            print("figures.json is stale; run compute_figures figures", file=sys.stderr)
            return 1
        return 0
    path.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
