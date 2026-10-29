"""Summarise the AVC444 campaigns and rank what stops runs, by cause.

Reads each campaign's ``trials.json`` (written by harness.py on vm-client and
fetched by submit_job.sh), and writes ``results.json``: per app and
condition, trials, verified saves, wrong targets, silent wrong saves, false
stops and stops on a look-alike, each with a rate per 100 and a 95% Wilson
interval; then every stop with its cause, and the causes ranked by how many
trials they stopped.

Causes (a stop gets exactly one, from the failing step's own message):

- ``harness``: the lab, not the product (a crash in the harness, or state an
  earlier trial left behind);
- ``locator``: no rung found the target, or a heal was refused;
- ``hover_reresolve``: the target re-resolved a pixel or two away after the
  pointer moved onto it, and the exact-match guard refused the click;
- ``identity_precheck``: the identity check before a click disagreed;
- ``state_check``: a postcondition's recorded screen text did not appear;
- ``layout_reflow``: the session's desktop size changed the page layout, and
  a postcondition still checked the recorded frame's absolute region;
- ``timing_codec``: the screen was still changing (or not stable) where the
  check looked: animation, a caret, or the codec refining a region.

It also copies, for each cause, the frame of one stopped step out of the
kept run archives into ``frames/``.

    python analyze.py <campaign dir> [...] --out <dir>
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import zipfile
from pathlib import Path

CAUSES = [
    ("hover_reresolve", re.compile(r"after remote pointer hover, not the prepared point")),
    ("timing_codec", re.compile(r"screen kept changing|region_stable")),
    ("identity_precheck", re.compile(r"Identity check failed")),
    ("locator", re.compile(r"all resolution rungs failed|heal quarantined|Could not resolve")),
    ("state_check", re.compile(r"text_present|expected screen state not reached")),
]
FIXES = {
    "timing_codec": "Wait for the region to settle (bounded) before the identity and "
                    "stability checks instead of failing on the first changing frame; "
                    "mask a blinking caret; record the codec's refinement interval.",
    "hover_reresolve": "Accept a re-resolve inside the target's own box (or within the "
                       "codec's measured jitter, 1-2 px) instead of exact pixel equality.",
    "identity_precheck": "Exclude run parameters and transient dialog content (duplicate "
                         "lists) from the recorded identity band.",
    "state_check": "Do not compile an auto-selected row's header text into the next "
                   "step's postcondition when the row is chosen by a later click.",
    "layout_reflow": "Bind postcondition regions to the resolved target (offsets from "
                     "it), not the recorded frame's absolute pixels; or halt at the "
                     "first step with an explicit 'layout changed' reason.",
    "locator": "Record after the screen has painted; on a reflowed layout use OCR "
               "landmarks rather than the recorded crop position.",
    "harness": "Lab fix, not product: reset every record a job created before each trial.",
}


def wilson(k: int, n: int) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    z = 1.96
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)]


def rate(k: int, n: int) -> dict:
    return {"count": k, "per_100": round(100 * k / n, 1) if n else None, "ci95": wilson(k, n)}


def cause_of(trial: dict) -> tuple[str, dict]:
    bad = next((s for s in trial["steps"] if not s["ok"]), None)
    if trial.get("error") or bad is None:
        return "harness", {"step_id": None, "detail": trial.get("error")}
    detail = bad.get("detail") or ""
    if trial["condition"].startswith("desktop") and "region_stable" in detail:
        return "layout_reflow", {"step_id": bad["step_id"], "detail": detail}
    for name, pattern in CAUSES:
        if pattern.search(detail):
            return name, {"step_id": bad["step_id"], "detail": detail}
    return "other", {"step_id": bad["step_id"], "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaigns", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--only", nargs="*", default=[],
        help="campaign:condition pairs; a campaign named here contributes only these "
             "conditions (the rest were superseded by a later campaign)",
    )
    args = parser.parse_args()
    frames_dir = args.out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    conditions: dict = {}
    stops: list[dict] = []
    for campaign in args.campaigns:
        trials = json.loads((campaign / "trials.json").read_text())
        app = campaign.name.split("-", 1)[0]  # campaign ids start with the app
        keep = {pair.split(":", 1)[1] for pair in args.only
                if pair.split(":", 1)[0] == campaign.name}
        for t in trials:
            if keep and t["condition"] not in keep:
                continue
            key = f"{app}/{t['condition']}"
            conditions.setdefault(key, []).append(t)
            if t["class"] in ("verified_save",):
                continue
            cause, where = cause_of(t)
            stops.append({"campaign": campaign.name, "app": app, "trial": t["trial"],
                          "condition": t["condition"], "class": t["class"], "cause": cause,
                          **where})

    summary = {}
    for key, ts in sorted(conditions.items()):
        n = len(ts)
        count = collections.Counter(t["class"] for t in ts)
        summary[key] = {
            "trials": n,
            "verified_saves": rate(count["verified_save"], n),
            "wrong_target": rate(count["silent_wrong"] + count["flagged_wrong"], n),
            "silent_wrong_saves": rate(count["silent_wrong"] + count["false_completion"], n),
            "false_stops": rate(count["false_stop"], n),
            "stops_on_lookalike": rate(count["stop_on_lookalike"], n),
            "model_calls": sum(t["model_calls"] for t in ts),
            "median_elapsed_s": sorted(t["elapsed_s"] for t in ts)[n // 2],
        }

    by_cause = collections.Counter(s["cause"] for s in stops if s["class"] == "false_stop")
    ranked = [
        {"cause": c, "false_stops": k, "fix": FIXES.get(c, ""),
         "where": sorted({f"{s['app']}/{s['condition']}:{s['step_id']}" for s in stops
                          if s["cause"] == c})}
        for c, k in by_cause.most_common()
    ]

    # One frame per cause: the failing step's 'before' frame from a kept archive.
    kept: dict[str, str] = {}
    for s in stops:
        if s["cause"] in kept or not s["step_id"]:
            continue
        campaign = next(c for c in args.campaigns if c.name == s["campaign"])
        for archive in sorted(campaign.glob(f"{s['app']}_run_{s['condition']}*.zip")):
            with zipfile.ZipFile(archive) as zf:
                name = f"steps/{s['step_id']}_before.png"
                if name in zf.namelist():
                    dest = frames_dir / f"{s['cause']}_{s['app']}_{s['condition']}_{s['step_id']}.png"
                    dest.write_bytes(zf.read(name))
                    kept[s["cause"]] = dest.name
                    break

    result = {"schema": "openadapt.rdp-avc444-campaigns.v1", "conditions": summary,
              "stops": stops, "ranked_causes": ranked, "cause_frames": kept}
    (args.out / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"conditions": summary, "ranked_causes": ranked, "cause_frames": kept},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
