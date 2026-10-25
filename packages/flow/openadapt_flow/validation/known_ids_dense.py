"""Before/after of the known-ID cross-check on the dense record-list surface.

Runs the dense sibling-surface study (:mod:`.dense_surface`: rendered list,
real OCR, the replayer's band extraction and 2x retry) and scores every armed
click three ways:

- **no list**: the shipped OCR verdict;
- **same frame**: the native read and the 2x upscale of one frame passed
  through :func:`openadapt_flow.runtime.known_ids.resolve_lookalike_abstain`
  (the earlier design);
- **fresh capture**: the native read of the first frame and the native read
  of a new screenshot (the current design, ``Replayer._known_id_lift``).

Both list variants use the seed's complete synthetic identifier list.

Denominators are stated per number. A false stop is an armed click on the
CORRECT row that does not verify. A false accept is an armed click on the
adjacent SIBLING row that verifies. Five collision classes put the only
difference between target and sibling in a look-alike identifier
(``id_*``); there the sibling's record ID is in the list, so the cross-check must
keep stopping.

Run:
    python -m openadapt_flow.validation.known_ids_dense --out benchmark/known_ids
    python -m openadapt_flow.validation.known_ids_dense --from-json PATH  # rescore
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import IdentityCheck
from openadapt_flow.runtime.known_ids import KnownIdList, resolve_lookalike_abstain

TWIN_CLASS_PREFIX = "id_"


def _check(r: dict[str, Any], expected: str = "") -> IdentityCheck:
    return IdentityCheck(
        status=r["status"],
        abstain_reason=r.get("abstain_reason"),
        coverage=r.get("coverage", 0.0),
        expected=expected,
        observed=r.get("observed", ""),
    )


def _final(checks: list[IdentityCheck]) -> IdentityCheck:
    return max(
        checks,
        key=lambda c: (
            {"unreadable": 0, "abstain": 1, "mismatch": 2, "verified": 3}[c.status],
            c.coverage,
        ),
    )


def _after_same_frame(
    reads: list[dict[str, Any]], ids: KnownIdList, recorded: str
) -> str:
    """Earlier design: native read and 2x upscale of the SAME frame."""
    checks = [_check(r, recorded) for r in reads]
    final = _final(checks)
    if final.status == "verified":
        return "verified"
    lifted = resolve_lookalike_abstain(checks, ids)
    return "verified" if lifted is not None else final.status


def _after_fresh(
    reads: list[dict[str, Any]],
    fresh: Optional[dict[str, Any]],
    ids: KnownIdList,
    recorded: str,
) -> str:
    """Current design: native read of the first frame and a native read of a
    FRESH capture (``Replayer._known_id_lift``)."""
    checks = [_check(r, recorded) for r in reads]
    final = _final(checks)
    if final.status == "verified":
        return "verified"
    if fresh is None:
        return final.status
    lifted = resolve_lookalike_abstain([checks[0], _check(fresh, recorded)], ids)
    return "verified" if lifted is not None else final.status


def score(result: dict[str, Any]) -> dict[str, Any]:
    """Before/after counts with explicit denominators."""
    lists = {
        seed: KnownIdList(
            ids=tuple(mrns), source=f"synthetic seed {seed}", complete=True
        )
        for seed, mrns in result["meta"]["known_ids_by_seed"].items()
    }
    armed = [t for t in result["trials"] if t["armed"]]

    def bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out = {
            "correct_row_clicks": len(rows),
            "sibling_row_clicks": len(rows),
            "false_stops_before": 0,
            "false_stops_after_same_frame": 0,
            "false_stops_after": 0,
            "false_accepts_before": 0,
            "false_accepts_after_same_frame": 0,
            "false_accepts_after": 0,
        }
        for t in rows:
            ids = lists[str(t["seed"])]
            # Both trials check against the target row's recorded band.
            recorded = t.get("context_text") or ""
            out["false_stops_before"] += t["fa_status"] != "verified"
            out["false_stops_after_same_frame"] += (
                _after_same_frame(t["fa_reads"], ids, recorded) != "verified"
            )
            out["false_stops_after"] += (
                _after_fresh(t["fa_reads"], t.get("fa_fresh_read"), ids, recorded)
                != "verified"
            )
            out["false_accepts_before"] += t["acc_status"] == "verified"
            out["false_accepts_after_same_frame"] += (
                _after_same_frame(t["acc_reads"], ids, recorded) == "verified"
            )
            out["false_accepts_after"] += (
                _after_fresh(t["acc_reads"], t.get("acc_fresh_read"), ids, recorded)
                == "verified"
            )
        return out

    by_class: dict[str, list[dict[str, Any]]] = {}
    for t in armed:
        by_class.setdefault(t["collision_class"], []).append(t)
    twin = [t for t in armed if t["collision_class"].startswith(TWIN_CLASS_PREFIX)]
    other = [t for t in armed if not t["collision_class"].startswith(TWIN_CLASS_PREFIX)]
    return {
        "all": bucket(armed),
        "no_lookalike_twin_in_list": bucket(other),
        "lookalike_twin_in_list": bucket(twin),
        "by_collision_class": {k: bucket(v) for k, v in sorted(by_class.items())},
        "unarmed_clicks": sum(1 for t in result["trials"] if not t["armed"]),
        "fresh_reads_taken": sum(
            (t.get("fa_fresh_read") is not None) + (t.get("acc_fresh_read") is not None)
            for t in armed
        ),
    }


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d) if d else 0.0:.2f}%"


def _row(name: str, b: dict[str, Any]) -> str:
    n = b["correct_row_clicks"]
    return (
        f"| {name} | {n} | {b['false_stops_before']} ({_pct(b['false_stops_before'], n)}) "
        f"| {b['false_stops_after_same_frame']} "
        f"({_pct(b['false_stops_after_same_frame'], n)}) "
        f"| {b['false_stops_after']} ({_pct(b['false_stops_after'], n)}) "
        f"| {b['false_accepts_before']} / {b['false_accepts_after_same_frame']} "
        f"/ {b['false_accepts_after']} |"
    )


def render_markdown(scored: dict[str, Any], meta: dict[str, Any]) -> str:
    a = scored["all"]
    n = a["correct_row_clicks"]
    identical = meta.get("fresh_capture_identical") or {}
    same_px = sum(1 for v in identical.values() if v)
    diff = abs(a["false_stops_after_same_frame"] - a["false_stops_after"])
    header = (
        "| group | correct-row clicks | false stops, no list "
        "| false stops, list + same-frame reads | false stops, list + fresh capture "
        "| false accepts (no list / same frame / fresh) |"
    )
    lines = [
        "# Known-ID cross-check: dense record list, before and after",
        "",
        "Generated by `python -m openadapt_flow.validation.known_ids_dense`. "
        "Synthetic data only.",
        "",
        "## What was measured",
        "",
        f"- Surface: the dense record list of `benchmark/dense_surface` "
        f"({meta['n_rows']} rows, seeds {', '.join(str(s) for s in meta['seeds'])}), "
        "rendered in a browser and read by RapidOCR, with the replayer's band "
        "extraction and its 2x retry.",
        f"- Replay conditions: {', '.join(meta['replay_conditions'])}. "
        f"Click configs: {', '.join(meta['click_configs'])}.",
        "- No list: the shipped OCR verdict.",
        "- List + same-frame reads (earlier design): the native read and the "
        "2x upscale of the same frame passed through "
        "`runtime.known_ids.resolve_lookalike_abstain`.",
        "- List + fresh capture (current design): the native read of the first "
        "frame and a native read of a new screenshot of the same screen, as "
        "`Replayer._known_id_lift` does.",
        "- In both list columns every identifier in the seed's table is the "
        "complete known-ID list.",
        "- Denominator for false stops: armed clicks on the correct row. "
        "Denominator for false accepts: armed clicks on the adjacent sibling "
        "row (same count).",
        "",
        "## Headline",
        "",
        f"- False stops: **{a['false_stops_before']} / {n} "
        f"({_pct(a['false_stops_before'], n)}) with no list; "
        f"{a['false_stops_after_same_frame']} / {n} "
        f"({_pct(a['false_stops_after_same_frame'], n)}) with the list and "
        f"same-frame reads; {a['false_stops_after']} / {n} "
        f"({_pct(a['false_stops_after'], n)}) with the list and a fresh capture.**",
        f"- False accepts: {a['false_accepts_before']} / "
        f"{a['false_accepts_after_same_frame']} / {a['false_accepts_after']} "
        f"of {a['sibling_row_clicks']}.",
        f"- Fresh captures read: {scored.get('fresh_reads_taken', 0)} "
        "(only clicks whose first read abstained on a look-alike identifier "
        "take one).",
        "",
        header,
        "| --- | --- | --- | --- | --- | --- |",
        _row("all", a),
        _row("no look-alike twin in list", scored["no_lookalike_twin_in_list"]),
        _row("look-alike twin in list (`id_*`)", scored["lookalike_twin_in_list"]),
        "",
        "## By collision class",
        "",
        header.replace("| group |", "| class |"),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, b in scored["by_collision_class"].items():
        lines.append(_row(f"`{name}`", b))
    lines += [
        "",
        "## How to read this",
        "",
        "In the `id_*` classes the sibling's identifier differs from the "
        "target's only by a look-alike glyph, and both are in the list. OCR "
        "cannot tell them apart, so the run still stops; that stop is correct, "
        "not a false stop in any useful sense. It is counted here so the "
        "denominator stays the full published one.",
        "",
        "The same-frame design accepted a lift only when the native read and "
        "the 2x upscale both abstained on a look-alike identifier. The fresh "
        "capture design needs the native read of two separate screenshots to "
        "agree instead; a screen that changes between captures blocks the "
        f"lift. On this fixture the two designs differ by {diff} of {n} "
        "correct-row clicks. Because the fixture's fresh captures are "
        "pixel-identical (see Limits), that difference comes only from clicks "
        "where the 2x upscale read the band differently from the native read.",
        "",
        "The remaining stops are band mismatches that have nothing to do with "
        "look-alike glyphs (dense-row OCR drops or garbles a name cell).",
        "",
        "## Limits",
        "",
        "- The lift is only as safe as the list is complete. The list must "
        "come from a configured export of the system of record, and an "
        "operator confirms its source and completeness at admission; replay "
        "halts if that confirmation is missing, the export changed, or it is "
        "older than the configured age.",
        "- One fixture, one OCR engine (RapidOCR), synthetic rows. A "
        "different renderer, font or OCR engine moves these numbers.",
        f"- The fixture renders deterministically: {same_px} of "
        f"{len(identical)} fresh renders (one per seed and replay condition) "
        "were pixel-identical to the first. "
        "On those, the second read repeats the first. What the fresh capture "
        "guards against (a caret, a partial repaint or a screen that changed "
        "between captures) does not occur in this fixture, so these numbers "
        "measure the availability cost of the design, not its protection.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="benchmark/known_ids")
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--n-rows", type=int, default=40)
    parser.add_argument("--from-json", help="rescore a saved dense_surface.json")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--save-trials", action="store_true", help="also write per-trial reads"
    )
    args = parser.parse_args(argv)

    if args.from_json:
        result = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        from openadapt_flow.validation.dense_surface import run_trials

        result = run_trials(args.seeds, n_rows=args.n_rows, progress=args.progress)
    scored = score(result)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps({"meta": result["meta"], "scored": scored}, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.save_trials:
        (out / "trials.json").write_text(
            json.dumps(result, indent=1) + "\n", encoding="utf-8"
        )
    (out / "KNOWN_IDS.md").write_text(
        render_markdown(scored, result["meta"]), encoding="utf-8"
    )
    a = scored["all"]
    print(
        f"false stops {a['false_stops_before']} / "
        f"{a['false_stops_after_same_frame']} (same frame) / "
        f"{a['false_stops_after']} (fresh capture) of {a['correct_row_clicks']}; "
        f"false accepts {a['false_accepts_before']} / "
        f"{a['false_accepts_after_same_frame']} / {a['false_accepts_after']} "
        f"of {a['sibling_row_clicks']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
