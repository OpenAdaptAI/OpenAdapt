"""Measure the separate-session UI read-back against a same-screen check and
an API read, on the repository's fault list.

Two harnesses, both localhost-only, zero model calls:

- ``gui_replay`` (``gui_harness.py``): the recorded MockMed triage bundle,
  replayed in a real browser against the fault server
  (``benchmark/fault_model``'s persistence boundary plus two server faults).
- ``api_write`` (``api_harness.py``): the ``benchmark/effect_e2e`` record
  service and its independent SQLite ground truth, plus display probes that
  change only how the record page shows a correct save.

Usage::

    python -m benchmark.ui_readback.run                 # results.json + RESULTS.md
    python -m benchmark.ui_readback.run --gui-n 1 --api-n 1   # quick
    python -m benchmark.ui_readback.run --skip-gui      # API-write harness only
    python -m benchmark.ui_readback.run --render-only   # RESULTS.md from results.json
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

ARM_LABELS = {
    "none": "no record check (screen postconditions only)",
    "same_screen": "same-screen check (tier 4)",
    "ui_readback": "separate-session UI read-back (tier 2)",
    "api_read": "API read (tier 1)",
}


def _uniform(rows: list[dict[str, Any]], key: str) -> Any:
    values = {json.dumps(r[key], sort_keys=True) for r in rows}
    if len(values) == 1:
        return rows[0][key]
    return "MIXED"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per harness and arm: catch counts on the fault list, false stops on
    correct saves, and the display probes."""

    by_cell: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cell[(row["harness"], row["scenario"], row["arm"])].append(row)

    cells: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (harness, scenario, arm), group in by_cell.items():
        cells[f"{harness}/{scenario}"][arm] = {
            "n": len(group),
            "gt_fault": _uniform(group, "gt_fault"),
            "gt_correct": _uniform(group, "gt_correct"),
            "halted": _uniform(group, "halted"),
            "stopped_by": _uniform(group, "stopped_by"),
            "transaction_outcome": _uniform(group, "transaction_outcome"),
            "probe": group[0]["probe"],
            "write_accepted": group[0]["write_accepted"],
            "blurb": group[0]["blurb"],
            "consistent": all(
                _uniform(group, k) != "MIXED" for k in ("gt_fault", "halted")
            ),
        }

    summary: dict[str, Any] = {}
    for harness in sorted({r["harness"] for r in rows}):
        harness_cells = {
            key.split("/", 1)[1]: arms
            for key, arms in cells.items()
            if key.startswith(harness + "/")
        }
        arms = sorted({a for arms in harness_cells.values() for a in arms})
        fault_list = {
            name: arms_
            for name, arms_ in harness_cells.items()
            if not next(iter(arms_.values()))["probe"]
        }

        def silent_without_check(name: str, arms_: dict[str, Any]) -> bool:
            if "none" in arms_:
                return arms_["none"]["halted"] is False
            return bool(next(iter(arms_.values()))["write_accepted"])

        # Whether a scenario produces a wrong result is judged on a reference
        # arm that never stops before the save. A check that stops a run
        # before the save leaves nothing wrong behind, which is a stop, not a
        # correct save and not a caught wrong one.
        reference_arm = "none" if "none" in arms else "same_screen"
        reference = {
            n: a[reference_arm]["gt_correct"] is False
            for n, a in fault_list.items()
            if reference_arm in a
        }
        per_arm: dict[str, Any] = {}
        for arm in arms:
            wrong = [
                n
                for n, is_wrong in reference.items()
                if is_wrong and arm in fault_list[n]
            ]
            caught = [n for n in wrong if fault_list[n][arm]["halted"] is True]
            needs_check = [n for n in wrong if silent_without_check(n, fault_list[n])]
            caught_needing = [n for n in needs_check if n in caught]
            correct = [
                n
                for n, is_wrong in reference.items()
                if not is_wrong and arm in fault_list[n]
            ]
            false_stops = [n for n in correct if fault_list[n][arm]["halted"] is True]
            per_arm[arm] = {
                "wrong_classes": len(wrong),
                "caught": len(caught),
                "missed": sorted(set(wrong) - set(caught)),
                "wrong_classes_silent_without_check": len(needs_check),
                "caught_of_silent": len(caught_needing),
                "missed_of_silent": sorted(set(needs_check) - set(caught_needing)),
                "correct_classes": len(correct),
                "false_stops": sorted(false_stops),
                "false_stops_by_check": sorted(
                    n
                    for n in false_stops
                    if fault_list[n][arm]["stopped_by"] == "check"
                ),
                "caught_outcomes": dict(
                    Counter(
                        str(fault_list[n][arm]["transaction_outcome"]) for n in caught
                    )
                ),
            }
        probes = {
            name: arms_
            for name, arms_ in harness_cells.items()
            if next(iter(arms_.values()))["probe"]
        }
        summary[harness] = {
            "arms": arms,
            "reference_arm": reference_arm,
            "per_arm": per_arm,
            "fault_list": fault_list,
            "probes": probes,
            "all_repeats_consistent": all(
                cell["consistent"]
                for arms_ in harness_cells.values()
                for cell in arms_.values()
            ),
            "median_run_s": {
                arm: _median(
                    [
                        r["elapsed_s"]
                        for r in rows
                        if r["harness"] == harness
                        and r["arm"] == arm
                        and "elapsed_s" in r
                        and r["scenario"] in ("ok", "idempotent")
                    ]
                )
                for arm in arms
            },
        }
    return summary


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


_GROUND_TRUTH = {
    "gui_replay": (
        "Ground truth: the fault server's store, read in process before and after "
        "each replay and judged by `benchmark/effect_e2e/ground_truth.judge`. The "
        "API arm reads that same store over HTTP, so on this harness its result "
        "is close to definitional."
    ),
    "api_write": (
        "Ground truth: a read-only connection to the SQLite file that bypasses the "
        "service, judged by its own classifier (`benchmark/effect_e2e`). Writes "
        "go through the replayer's API tier, as in `effect_e2e`; the record page "
        "is served from the same database."
    ),
}


def _verdict_word(cell: dict[str, Any]) -> str:
    if cell["gt_correct"] is True and cell["halted"] is False:
        return "passed"
    if cell["gt_correct"] is True:
        return f"stopped ({cell['stopped_by']})"
    if cell["halted"] is True:
        return "caught"
    if cell["halted"] is False:
        return "**MISSED**"
    return str(cell["halted"])


def render_markdown(results: dict[str, Any]) -> str:
    s = results["summary"]
    lines = [
        "# Separate-session UI read-back: measured catch rates and false stops",
        "",
        f"Generated {results['generated_at'][:10]} by `python -m benchmark.ui_readback.run`.",
        f"Platform: {results['platform']}. Model calls: 0. Localhost only.",
        "",
        "Every arm judges the same two effect contracts on the save (exactly one new",
        "Triage encounter for the patient with no earlier row lost, and its note equal",
        "to the input). Arms differ only in where the check reads. Ground truth never",
        "comes from the arm being scored.",
        "",
        "These are counts over a small, hand-written, deterministic fault list, not",
        "rates you should expect in production. Repeats agree"
        + (
            " in every cell."
            if all(h["all_repeats_consistent"] for h in s.values())
            else "; see MIXED cells."
        ),
        "",
    ]
    for harness, title, n_key in (
        ("gui_replay", "GUI replay (MockMed, real browser)", "gui_n"),
        (
            "api_write",
            "API write (effect_e2e record service, SQLite ground truth)",
            "api_n",
        ),
    ):
        if harness not in s:
            continue
        h = s[harness]
        lines += [
            f"## {title}",
            "",
            f"Repeats per cell: {results[n_key]}. {_GROUND_TRUTH[harness]}",
            "",
        ]
        lines += [
            "| arm | wrong results caught | caught where the run alone showed success "
            "| correct saves the check stopped | correct saves the run stopped |",
            "|---|---|---|---|---|",
        ]
        for arm in [
            a
            for a in ("none", "same_screen", "ui_readback", "api_read")
            if a in h["arms"]
        ]:
            p = h["per_arm"][arm]
            lines.append(
                f"| {ARM_LABELS[arm]} | {p['caught']} of {p['wrong_classes']} | "
                f"{p['caught_of_silent']} of {p['wrong_classes_silent_without_check']} | "
                f"{len(p['false_stops_by_check'])} of {p['correct_classes']} | "
                f"{len(p['false_stops']) - len(p['false_stops_by_check'])} of "
                f"{p['correct_classes']} |"
            )
        lines += [
            "",
            "| scenario | ground truth | "
            + " | ".join(
                ARM_LABELS[a].split(" (")[0]
                for a in ("none", "same_screen", "ui_readback", "api_read")
                if a in h["arms"]
            )
            + " |",
            "|---|---|"
            + "---|"
            * len(
                [
                    a
                    for a in ("none", "same_screen", "ui_readback", "api_read")
                    if a in h["arms"]
                ]
            ),
        ]
        for name, arms_ in h["fault_list"].items():
            ref = next(iter(arms_.values()))
            gt = arms_[h["reference_arm"]]["gt_fault"]
            cells = " | ".join(
                _verdict_word(arms_[a])
                for a in ("none", "same_screen", "ui_readback", "api_read")
                if a in arms_
            )
            lines.append(f"| `{name}`: {ref['blurb']} | {gt} | {cells} |")
        lines.append("")
        timed = {
            a: h["median_run_s"][a]
            for a in ("none", "same_screen", "ui_readback", "api_read")
            if h["median_run_s"].get(a) is not None
        }
        if timed:
            lines += [
                "Median time per replay on correct saves: "
                + ", ".join(
                    f"{ARM_LABELS[a].split(' (')[0]} {t:.2f} s"
                    for a, t in timed.items()
                )
                + ".",
                "",
            ]
        if h["probes"]:
            lines += [
                "### Display probes (correct saves; only the record page's display changes)",
                "",
                "| probe | "
                + " | ".join(
                    ARM_LABELS[a].split(" (")[0]
                    for a in ("same_screen", "ui_readback", "api_read")
                    if a in h["arms"]
                )
                + " | when the read-back stopped |",
                "|---|"
                + "---|"
                * (
                    len(
                        [
                            a
                            for a in ("same_screen", "ui_readback", "api_read")
                            if a in h["arms"]
                        ]
                    )
                    + 1
                ),
            ]
            for name, arms_ in h["probes"].items():
                ref = next(iter(arms_.values()))
                cells = []
                for a in ("same_screen", "ui_readback", "api_read"):
                    if a not in arms_:
                        continue
                    c = arms_[a]
                    cells.append(
                        "passed"
                        if c["halted"] is False
                        else f"stopped ({c['stopped_by']})"
                    )
                ui = arms_.get("ui_readback")
                when = "-"
                if ui and ui["halted"] is True:
                    when = (
                        "before the save (nothing written)"
                        if ui["gt_correct"] is False and ui["gt_fault"] == "absent"
                        else "after the save"
                    )
                lines.append(
                    f"| `{name}`: {ref['blurb']} | "
                    + " | ".join(cells)
                    + f" | {when} |"
                )
            lines.append("")
    lines += [
        "## Reproduce",
        "",
        "```",
        "python -m benchmark.ui_readback.run",
        "```",
        "",
        "Needs the `browser` extra and Chromium (`python -m playwright install chromium`).",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    from benchmark.ui_readback import api_harness, gui_harness

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui-n", type=int, default=3, help="repeats per GUI cell")
    parser.add_argument("--api-n", type=int, default=5, help="repeats per API cell")
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--out", default=str(HERE))
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="re-render RESULTS.md from the results.json in --out; run nothing",
    )
    args = parser.parse_args(argv)

    if args.render_only:
        out = Path(args.out)
        results = json.loads((out / "results.json").read_text())
        results["summary"] = summarize(results["runs"])
        (out / "results.json").write_text(
            json.dumps(results, indent=2, default=str) + "\n"
        )
        (out / "RESULTS.md").write_text(render_markdown(results))
        print(f"re-rendered {out / 'RESULTS.md'}")
        return 0

    rows: list[dict[str, Any]] = []
    if not args.skip_gui:
        rows += gui_harness.run(args.gui_n)
    rows += api_harness.run(args.api_n)
    results = {
        "instrument": "separate-session UI read-back vs same-screen check vs API read",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "model_calls": 0,
        "gui_n": 0 if args.skip_gui else args.gui_n,
        "api_n": args.api_n,
        "summary": summarize(rows),
        "runs": rows,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
    (out / "RESULTS.md").write_text(render_markdown(results))
    for harness, h in results["summary"].items():
        for arm, p in h["per_arm"].items():
            print(
                f"{harness:10s} {arm:12s} caught {p['caught']}/{p['wrong_classes']} "
                f"(silent-without-check {p['caught_of_silent']}/"
                f"{p['wrong_classes_silent_without_check']}) "
                f"false stops {len(p['false_stops'])}/{p['correct_classes']} "
                f"(by the check {len(p['false_stops_by_check'])})"
            )
    print(f"wrote {out / 'results.json'} and {out / 'RESULTS.md'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
