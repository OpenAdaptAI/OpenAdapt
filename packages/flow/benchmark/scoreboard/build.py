"""Aggregate ``raw/`` into ``scoreboard.json`` and ``SCOREBOARD.md``.

    python -m scoreboard.build

Per task and arm, over the final attempt of each item:

* ``correct``: right record, read back from the system of record, on items
  without an injected fault;
* ``silent_wrong``: the arm reported success and the read-back disagrees;
* ``safe_stops``: stops with no wrong record, split into normal items (a cost)
  and fault items (the right answer);
* ``flagged_wrong``: a wrong record landed and the arm flagged it;
* ``paused``: items the batch did not attempt because it paused for a person;
* model calls, tokens and list-price cost per item;
* time per correct item (median, p90) and items per hour, from timed rows
  only (1-minute host load at or under 12 at the start and end);
* people asked to approve.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402

ARM_ORDER = [
    "agent",
    "first_run",
    "program",
    "program_repair",
    "program_fast",
    "program_api",
    "parallel",
    "code_agent",
]
ARM_LABEL = {
    "agent": "1. Agent alone (Claude Sonnet 5.5 computer use)",
    "first_run": "2. Agent's first run under OpenAdapt (recorded, read back, compiled)",
    "program": "3. Compiled program driving the UI",
    "program_repair": "3r. Compiled program with self-repair (#1265, #1294)",
    "program_fast": "4. Compiled program, throughput fast path",
    "program_api": "5. Compiled program with the API step",
    "parallel": "6. Fast path or API step, N parallel workers",
    "code_agent": "7. Code-as-action authoring agent, first run",
}


def _q(values: list[float], p: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _num(x: Any) -> float:
    return float(x) if x not in (None, "") else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_item: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_item[(r["run_id"], int(r["item"]))].append(r)
    finals = [sorted(v, key=lambda r: int(r["attempt"]))[-1] for v in by_item.values()]
    attempted = [r for r in finals if r["outcome"] != "paused"]
    normal = [r for r in attempted if r["fault"] == "none"]
    faulted = [r for r in attempted if r["fault"] != "none"]
    count = lambda rs, o: sum(1 for r in rs if r["outcome"] == o)  # noqa: E731
    tried = [r for r in rows if r["outcome"] != "paused"]
    tok_known = all(int(r["input_tokens"]) >= 0 for r in tried) if tried else False
    timed_ok = [
        r for r in attempted if r["timed"] == "True" and r["outcome"] == "correct"
    ]
    timed_all = [r for r in tried if r["timed"] == "True"]
    t = [_num(r["item_s"]) for r in timed_ok]
    w = [_num(r["wall_s"]) for r in timed_ok]
    wall_timed = sum(_num(r["wall_s"]) for r in timed_all)
    loads = [_num(r["host_load_1m"]) for r in tried if _num(r["host_load_1m"]) >= 0]
    n = len(attempted)
    return {
        "items": n,
        "attempts": len(tried),
        "runs": sorted({r["run_id"] for r in rows}),
        "normal_items": len(normal),
        "correct": count(normal, "correct"),
        "correct_rate": round(count(normal, "correct") / len(normal), 4)
        if normal
        else None,
        "silent_wrong": count(attempted, "silent_wrong"),
        "safe_stops_normal": count(normal, "safe_stop"),
        "fault_items": len(faulted),
        "fault_items_by_kind": {
            k: {
                o: count([r for r in faulted if r["fault"] == k], o)
                for o in schema.OUTCOMES
                if o != "paused"
            }
            for k in sorted({r["fault"] for r in faulted})
        },
        "planned_items": len(finals),
        "done_correctly_all_items": count(finals, "correct"),
        "flagged_wrong": count(attempted, "flagged_wrong"),
        "paused": count(finals, "paused"),
        "model_calls_per_item": round(sum(_num(r["model_calls"]) for r in tried) / n, 2)
        if n
        else None,
        "tokens_per_item": (
            round(
                sum(
                    _num(r["input_tokens"])
                    + _num(r["output_tokens"])
                    + _num(r["cache_read_tokens"])
                    + _num(r["cache_write_tokens"])
                    for r in tried
                )
                / n
            )
            if n and tok_known
            else None
        ),
        "cost_usd_total": round(sum(_num(r["cost_usd"]) for r in tried), 4),
        "cost_usd_per_item": round(sum(_num(r["cost_usd"]) for r in tried) / n, 4)
        if n
        else None,
        "approvals_asked": int(sum(_num(r["approvals_asked"]) for r in tried)),
        "timed_correct_items": len(timed_ok),
        "item_s_median": round(statistics.median(t), 2) if t else None,
        "item_s_p90": round(_q(t, 0.9), 2) if t else None,
        "wall_s_median": round(statistics.median(w), 2) if w else None,
        "wall_s_p90": round(_q(w, 0.9), 2) if w else None,
        "items_per_hour": round(3600 * len(timed_ok) / wall_timed, 1)
        if wall_timed
        else None,
        "host_load_1m": {"min": min(loads), "max": max(loads)} if loads else None,
    }


def build() -> dict[str, Any]:
    tasks: dict[str, Any] = {}
    for items in sorted(schema.RAW.glob("*/*/*/items.csv")):
        run = items.parent
        arm, task = run.parent.name, run.parent.parent.name
        if arm == "claims":
            continue  # its own table: claims()
        meta = run / "meta.json"
        if meta.is_file() and json.loads(meta.read_text()).get("in_table") is False:
            # Kept as raw data, not in the table (its meta.json says why).
            continue
        rows = schema.read_rows(items)
        tasks.setdefault(task, {}).setdefault(arm, {"rows": [], "meta": []})
        tasks[task][arm]["rows"].extend(rows)
        tasks[task][arm].setdefault("runs", {})[run.name] = rows
        hc = run / "hand_check.json"
        if hc.is_file():
            tasks[task][arm].setdefault("hand_checks", []).extend(
                {"run": run.name, **i} for i in json.loads(hc.read_text())["items"]
            )
        if meta.is_file():
            tasks[task][arm]["meta"].append(
                {"run": run.name, **json.loads(meta.read_text())}
            )
    out: dict[str, Any] = {
        "protocol_version": schema.PROTOCOL_VERSION,
        "timed_load_max": schema.TIMED_LOAD_MAX,
        "spend_usd": json.loads((schema.RAW / "spend.json").read_text())["total_usd"]
        if (schema.RAW / "spend.json").is_file()
        else None,
        "tasks": {},
        "claims": claims(),
    }
    for task, arms in sorted(tasks.items()):
        out["tasks"][task] = {}
        for arm in sorted(
            arms, key=lambda a: ARM_ORDER.index(a) if a in ARM_ORDER else 99
        ):
            data = arms[arm]
            # Rows that are not correct and not paused, against the hand check.
            to_check = {
                (r["run_id"], int(r["item"]), int(r["attempt"]))
                for r in data["rows"]
                if r["outcome"] not in ("correct", "paused")
            }
            checks = data.get("hand_checks", [])
            checked = {(c["run"], c["item"], c["attempt"]) for c in checks}
            out["tasks"][task][arm] = {
                "label": ARM_LABEL.get(arm, arm),
                **summarize(data["rows"]),
                "runs": {name: summarize(rows) for name, rows in data["runs"].items()},
                "run_meta": {m["run"]: m for m in data["meta"]},
                "hand_check": {
                    "rows_not_correct": len(to_check),
                    "checked": len(to_check & checked),
                    "confirmed": sum(
                        1
                        for c in checks
                        if c["confirmed"]
                        and (c["run"], c["item"], c["attempt"]) in to_check
                    ),
                    "items": checks,
                },
                "sources": [
                    {
                        k: m.get(k)
                        for k in (
                            "run",
                            "imported_from",
                            "model",
                            "engine",
                            "profile",
                            "note",
                        )
                        if m.get(k)
                    }
                    for m in data["meta"]
                ],
            }
    return out


def claims() -> dict[str, Any]:
    """The agent's claims versus verify-run, per task and run (``claims.py``)."""

    out: dict[str, Any] = {}
    for items in sorted(schema.RAW.glob("*/claims/*/items.csv")):
        run = items.parent
        meta = run / "meta.json"
        info = json.loads(meta.read_text()) if meta.is_file() else {}
        if info.get("in_table") is False or info.get("complete") is not True:
            continue  # kept out: a plumbing check, or a run still going
        rows = schema.read_rows(items)
        hc_file = run / "hand_check.json"
        hand = json.loads(hc_file.read_text())["items"] if hc_file.is_file() else []
        by_item = {int(h["item"]): h for h in hand}

        def t(r: dict[str, Any]) -> bool:
            return r["claimed_success"] == "True"

        def caught(r: dict[str, Any]) -> bool:
            return r["verify_outcome"] != "verified"

        per_fault: dict[str, Any] = {}
        for r in rows:
            f = per_fault.setdefault(
                r["fault"],
                {
                    "n": 0,
                    "bad_saves": 0,
                    "claimed_success": 0,
                    "bad_claimed_success": 0,
                    "bad_claimed_caught_by_verify_run": 0,
                    "bad_caught_by_verify_run": 0,
                    "clean_saves": 0,
                    "clean_confirmed": 0,
                    "false_alarms": 0,
                    "fault_not_reached": 0,
                    "ground_truth": {},
                    "verify_outcome": {},
                },
            )
            bad = r["ground_truth"] != "correct"
            f["n"] += 1
            if r["fault"] != "ok" and r.get("fault_applied", "x") == "":
                # The agent stopped before any chart was written, so the
                # injected fault never fired: neither a bad save nor a clean one.
                f["fault_not_reached"] += 1
                f["claimed_success"] += t(r)
                continue
            f["claimed_success"] += t(r)
            f["ground_truth"][r["ground_truth"]] = (
                f["ground_truth"].get(r["ground_truth"], 0) + 1
            )
            f["verify_outcome"][r["verify_outcome"]] = (
                f["verify_outcome"].get(r["verify_outcome"], 0) + 1
            )
            if bad:
                f["bad_saves"] += 1
                f["bad_claimed_success"] += t(r)
                f["bad_claimed_caught_by_verify_run"] += t(r) and caught(r)
                f["bad_caught_by_verify_run"] += caught(r)
            else:
                f["clean_saves"] += 1
                f["clean_confirmed"] += not caught(r)
                f["false_alarms"] += caught(r)
        total = {
            k: sum(v[k] for v in per_fault.values())
            for k in next(iter(per_fault.values()), {})
            if k not in ("ground_truth", "verify_outcome")
        }
        claimed = [r for r in rows if t(r)]
        out[f"{run.parent.parent.name}/{run.name}"] = {
            "per_fault": per_fault,
            "total": total,
            "cost_usd": round(sum(float(r["cost_usd"]) for r in rows), 6),
            "hand_check": {
                "claimed_success_items": len(claimed),
                "checked": sum(1 for r in claimed if int(r["item"]) in by_item),
                "agrees_with_row": sum(
                    1
                    for r in claimed
                    if by_item.get(int(r["item"]), {}).get("claimed_success") is True
                ),
                "items": hand,
            },
        }
    return out


#: The OpenEMR comparison the README quotes: (bar, arm, run).
THREE_BARS = (
    ("Agent alone", "agent", "study-main-0c642bc0"),
    ("Compiled program, screen (API step off)", "program", "api-tier-2026-10-19"),
    ("Compiled program, API step on", "program_api", "api-tier-2026-10-19"),
    (
        "Arm 3 today, worklist (not comparable on wall time)",
        "program",
        "worklist-std-2026-10-19",
    ),
)


def _three_bars_markdown(sb: dict[str, Any]) -> list[str]:
    arms = sb["tasks"].get("openemr_referral", {})
    rows = []
    for bar, arm, run in THREE_BARS:
        r = arms.get(arm, {}).get("runs", {}).get(run)
        m = arms.get(arm, {}).get("run_meta", {}).get(run, {})
        if r is None:
            continue
        engine = str((m.get("engine") or {}).get("git_head", ""))[:8]
        gate = (
            "the study's start gate: 1-minute load under 12 before each run"
            if arm == "agent"
            else "per row: load at or under 12 at the start and the end"
        )
        rows.append(
            f"| {bar} | `{arm}` / `{run}` | {engine} | "
            f"{m.get('profile') or m.get('note') or '-'} | "
            f"{r['correct']} of {r['normal_items']} | {r['timed_correct_items']} | "
            f"{_fmt(r['item_s_median'])} | {_fmt(r['wall_s_median'])} | {gate} |"
        )
    if not rows:
        return []
    return [
        "## OpenEMR: agent, screen program and API step",
        "",
        "One referral, entered by the agent alone, by the compiled program on "
        "the screen, and by the same program with the API step (#1269). The "
        "screen and API rows are the same bundle, interleaved by block on one "
        "fixture copy. Medians are over correct, timed rows (n). Wall seconds "
        "run from a new browser context through sign-in, the work and the "
        "read-back, for the agent and for arm 5 alike; task seconds are the "
        "arm's own work (the agent's first action to its answer; the engine's "
        "time for a program). The possible-duplicate referral (r6) is in both "
        "program rows as a safe stop. The worklist run signs in once per "
        "worker, so its wall time is not comparable.",
        "",
        "| Bar | Arm / run | Engine | Profile | Correct (read back) | n timed | Task s median | Wall s median | Load gate |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ]


def _claims_markdown(cl: dict[str, Any]) -> list[str]:
    if not cl:
        return []
    lines = [
        "## The agent's claims versus the read-back",
        "",
        "Separate from arms 1 to 7 (`claims.py`, `claims_openemr.py`). Claude "
        "Sonnet 5.5 computer use works alone while the save is broken. A bad save "
        "is one where the store, compared before and after, does not hold exactly "
        "the intended record, or another record was lost or changed. "
        "`verify-run` then judges the same write from the system of record, with "
        "a baseline taken before the agent started. Untimed.",
        "",
        "- MockMed: the fault server breaks the write, and the screen shows the "
        "save as done under every fault (the app updates the page before the "
        "server answers). `duplicate_keyed` is a double send the server "
        "de-duplicated on an idempotency key, so a clean save (the run's "
        "meta.json).",
        "- OpenEMR: OpenEMR is unchanged; the faults are injected only in the "
        "fixture's database, by a trigger (`partial`: home phone blanked; "
        "`wrong_value`: postal code changed) or by a poller after the chart is "
        "written (`duplicate`, `no_persist`, `collateral_loss`: another chart "
        "deleted, `collateral_edit`: another chart's phone changed). One existing "
        "chart is seeded before every item. verify-run's collateral check covers "
        "a chart that vanished; a changed chart that is not the target is outside "
        'its contract, which `collateral_edit` measures. "Fault not reached": '
        "the agent stopped before any chart was written.",
        "",
    ]
    for key, c in cl.items():
        lines += [
            f"### {key}",
            "",
            "| Fault | Items | Bad saves | Agent claimed success | Bad saves the agent claimed as success | ...of those, caught by verify-run | Bad saves caught by verify-run | Clean saves | Clean saves verify-run confirmed | False alarms | Fault not reached |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, f in list(c["per_fault"].items()) + [("all", c["total"])]:
            lines.append(
                f"| {name} | {f['n']} | {f['bad_saves']} | {f['claimed_success']} | "
                f"{f['bad_claimed_success']} | {f['bad_claimed_caught_by_verify_run']} | "
                f"{f['bad_caught_by_verify_run']} | {f['clean_saves']} | "
                f"{f['clean_confirmed']} | {f['false_alarms']} | "
                f"{f.get('fault_not_reached', 0)} |"
            )
        hc = c["hand_check"]
        lines += [
            "",
            f"- Hand check of every item where the agent claimed success: "
            f"{hc['checked']} of {hc['claimed_success_items']} checked against the "
            f"ledger; the ledger agrees with the row on {hc['agrees_with_row']} "
            "(evidence in hand_check.json).",
            f"- Model spend: ${c['cost_usd']}.",
            "",
        ]
    return lines


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "-"
    return f"{v}{unit}"


def markdown(sb: dict[str, Any]) -> str:
    lines = [
        "# Scoreboard",
        "",
        f"Generated by `python -m scoreboard.build` from `raw/` (protocol {sb['protocol_version']}, "
        f"see PROTOCOL.md). Do not edit by hand. Times come only from rows whose 1-minute host load "
        f"was at or under {sb['timed_load_max']:g}; a dash means no timed rows yet.",
        "",
    ]
    for task, arms in sb["tasks"].items():
        lines += [
            f"## {task}",
            "",
            "| Arm | Model | Items | Correct (read back) | Done correctly, all items | Silent wrong | Safe stops (normal / fault items) | Flagged wrong | Paused | Model calls / item | Cost / item | Task s, median / p90 | Wall s, median / p90 | Items / hour | Approvals | Host load |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for arm, s in arms.items():
            fault_stops = sum(
                v.get("safe_stop", 0) for v in s["fault_items_by_kind"].values()
            )
            models = (
                ", ".join(sorted({m["model"] for m in s["sources"] if m.get("model")}))
                or "none"
            )
            rate = (
                f" ({s['correct_rate']:.0%})" if s["correct_rate"] is not None else ""
            )
            load = s["host_load_1m"]
            lines.append(
                f"| {s['label']} | {models} | {s['items']} | "
                f"{s['correct']} of {s['normal_items']}{rate} | "
                f"{s['done_correctly_all_items']} of {s['planned_items']} | "
                f"{s['silent_wrong']} | {s['safe_stops_normal']} / {fault_stops} of {s['fault_items']} | "
                f"{s['flagged_wrong']} | {s['paused']} | {_fmt(s['model_calls_per_item'])} | "
                f"{_fmt(s['cost_usd_per_item'] if s['cost_usd_per_item'] is not None else None, '')} | "
                f"{_fmt(s['item_s_median'])} / {_fmt(s['item_s_p90'])}"
                f"{'' if s['item_s_median'] is None else ' (n=' + str(s['timed_correct_items']) + ')'} | "
                f"{_fmt(s['wall_s_median'])} / {_fmt(s['wall_s_p90'])} | "
                f"{_fmt(s['items_per_hour'])} | "
                f"{s['approvals_asked']} | {(str(round(load['min'])) + '-' + str(round(load['max']))) if load else '-'} |"
            )
        lines.append("")
        for arm, s in arms.items():
            if s["fault_items"]:
                lines.append(
                    f"- {task} / {arm}: fault items {json.dumps(s['fault_items_by_kind'])}"
                )
        for arm, s in arms.items():
            worklist = any(
                m.get("wall_s") and m.get("saved_correct") is not None
                for m in s["run_meta"].values()
            )
            if len(s["runs"]) < 2 and not worklist:
                continue
            # Runs of one arm can differ in engine or harness (meta.json);
            # show each so a blended figure is never the only one.
            for name, r in s["runs"].items():
                meta = s["run_meta"].get(name, {})
                engine = str((meta.get("engine") or {}).get("git_head", ""))[:8]
                lines.append(
                    f"- {task} / {arm} / run `{name}` (engine {engine}): "
                    f"{r['items']} rows, {r['correct']} of {r['normal_items']} "
                    f"correct, {r['paused']} paused, task s median "
                    f"{_fmt(r['item_s_median'])} (timed rows only), "
                    f"items/hour {_fmt(r['items_per_hour'])}"
                )
                if meta.get("wall_s") and meta.get("saved_correct") is not None:
                    # The harness's own worklist figure: correct saves over the
                    # run's wall time. For arm 6 the workers load the host
                    # themselves, so it is gated on the start only (Amendment 4).
                    start = ((meta.get("gate") or {}).get("load_at_start") or [None])[0]
                    label = (
                        "self-loaded throughput"
                        if arm == "parallel"
                        else "worklist throughput"
                    )
                    lines.append(
                        f"  - {label}: "
                        f"{meta['saved_correct'] / meta['wall_s'] * 3600:.0f} correct "
                        f"items/hour ({meta['saved_correct']} correct of "
                        f"{meta['items_run']} run in {meta['wall_s']:.0f} s wall, "
                        f"{meta.get('workers', 1)} worker(s)); load at start "
                        f"{'-' if start is None else round(start, 1)}, mean during "
                        f"{round(meta.get('load1_mean') or 0, 1)}"
                    )
        for arm, s in arms.items():
            hc = s["hand_check"]
            if arm == "agent" and hc["rows_not_correct"]:
                lines.append(
                    f"- {task} / {arm}: hand check of rows that are not correct: "
                    f"{hc['checked']} of {hc['rows_not_correct']} checked, "
                    f"{hc['confirmed']} outcomes confirmed"
                    + "".join(
                        f"; item {c['item']} ({c['fault']}): {c['row_outcome']}"
                        + (
                            f", {c['evidence'].rstrip('.')}"
                            if c["row_outcome"] in ("silent_wrong", "flagged_wrong")
                            else ""
                        )
                        for c in hc["items"]
                    )
                    + " (evidence for each in hand_check.json)"
                )
        lines.append("")
    lines += _three_bars_markdown(sb)
    lines += _claims_markdown(sb.get("claims", {}))
    lines.append(
        f"Claude API spend so far: ${sb['spend_usd']}."
        if sb["spend_usd"] is not None
        else ""
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    sb = build()
    (HERE / "scoreboard.json").write_text(json.dumps(sb, indent=2) + "\n")
    (HERE / "SCOREBOARD.md").write_text(markdown(sb))
    print(markdown(sb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
