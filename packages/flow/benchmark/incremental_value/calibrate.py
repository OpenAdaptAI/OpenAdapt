"""Calibrate B's contract on clean work only: 3 ordinary referrals outside the
stream (arm A's agent saves them), plus the harness's own legitimate
clinician2 writes. Prints what moved, by table and column, and the audit
rows each write left. Reads no fault case.

    python -m incremental_value.calibrate --out DIR
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lifecycle.workload import generate

from . import faults, workload
from .oracle import DbOracle, judge, root_sql
from .run import Fixture, Recorder, env_for

LOG_COLS = "id, date, event, category, user, patient_id, success, LEFT(comments, 160)"


def _new_charts(oracle: DbOracle, person: dict, intended: dict) -> list[dict[str, str]]:
    """The person's chart as the save left it, minus the referral's fields."""

    from .oracle import NEW_CHART_VOLATILE, _is_person

    rows = [r for r in oracle.rows("patient_data").values() if _is_person(r, person)]
    skip = set(intended) | NEW_CHART_VOLATILE
    return [{c: v for c, v in r.items() if c not in skip} for r in rows[:1]]


def _write_form_defaults(path: Path, charts: list[dict[str, str]], clean_n: int) -> None:
    if len(charts) < 3 or len(charts) != clean_n:
        raise SystemExit(f"form defaults need 3 or more clean saves with one chart each; got {len(charts)} of {clean_n}")
    cols = sorted(set().union(*charts))
    same = {c: charts[0].get(c, "") for c in cols if all(ch.get(c, "") == charts[0].get(c, "") for ch in charts)}
    varies = [c for c in cols if c not in same]
    path.write_text(json.dumps({"saves": len(charts), "columns": same, "varies": varies}, indent=1, sort_keys=True) + "\n")
    print(f"form defaults: {len(same)} columns fixed, {len(varies)} vary: {varies}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skip-agent", action="store_true")
    ap.add_argument("--arm", default="A")
    ap.add_argument("--first", type=int, default=0)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--skip-legit", action="store_true")
    ap.add_argument("--record-form-defaults", type=Path,
                    help="write the new chart's columns the referral does not name, from these clean saves")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    fx = Fixture()
    app = fx.app
    app.point_hooks_here()
    app.reset()
    stream = workload.build([], None)
    targets = fx.seed(stream)
    oracle = DbOracle(root_sql(app.db_container))
    oracle.start()
    last_log = int(app.sql("SELECT MAX(id) FROM openemr.log;").strip() or 0)
    report = {"targets": targets, "steps": []}
    charts: list[dict[str, str]] = []

    def step(name: str, intended=None, person=None, legit=None) -> None:
        nonlocal last_log
        d = oracle.advance()
        moved = {}
        for t, td in d.items():
            cols = set()
            for b, aft in td.changed:
                cols |= {c for c in aft if b.get(c) != aft.get(c)}
            nonempty_added = sorted({c for r in td.added for c, v in r.items() if v not in ("", "NULL", "0", "0000-00-00", "0000-00-00 00:00:00")})
            moved[t] = {"added": len(td.added), "removed": len(td.removed), "changed": len(td.changed),
                        "changed_cols": sorted(cols), "added_nonempty_cols": nonempty_added if t not in ("log",) else []}
        logs = app.sql(f"SELECT {LOG_COLS} FROM openemr.log WHERE id > {last_log} ORDER BY id;")
        last_log = int(app.sql("SELECT MAX(id) FROM openemr.log;").strip() or 0)
        entry = {"step": name, "moved": moved, "log": logs.splitlines()[:80]}
        if person is not None:
            entry["oracle"] = judge(oracle, d, intended=intended, person=person, legit=legit or faults.Legit())
        report["steps"].append(entry)
        print(json.dumps(entry, indent=1)[:6000], flush=True)

    step("after seed and oracle start (should be empty)")
    if not a.skip_agent:
        from .run import make_arm

        arm = make_arm(a.arm)
        rec = Recorder()
        env = env_for(app, a.out / a.arm)
        arm.setup(env, rec)
        step("agent sign-in")
        wl = generate(9999, 40, 1)
        clean = [w for w in wl.items if wl.truth[w.item_id].exception == "clean"][a.first : a.first + a.n]
        for w in clean:
            t = wl.truth[w.item_id]
            item = workload.Item(0, f"cal-{w.seq}", "ordinary", "calibration", dict(w.params), w.source_text, dict(t.person), t.intended)
            ctx = faults.Ctx(item, app, oracle, targets, fx.api_for)
            arm.begin(ctx, faults.Hooks())
            res = arm.run_item(w, env, rec)
            print("claimed", res.claimed, res.product_status, res.wall_s, res.cost_usd, json.dumps(res.detail.get("verifier"), default=str)[:1500], flush=True)
            step(f"agent save {w.item_id}", intended=t.intended, person=t.person)
            charts.extend(_new_charts(oracle, t.person, t.intended))
        arm.close()
        if a.record_form_defaults:
            _write_form_defaults(a.record_form_defaults, charts, clean_n=len(clean))
    # The harness's own legitimate writes, to see their audit rows.
    if a.skip_legit:
        (a.out / "calibration.json").write_text(json.dumps(report, indent=1, default=str))
        return 0
    item = workload.Item(0, "cal-legit", "perturbation", "x", {}, "", {"fname": "-", "lname": "-", "email": "-"}, None, variant=0)
    for name, fn in (("clinician2 phone edit", faults._clinician2_edit_phone), ("clinician2 allergy", faults._clinician2_add_allergy),
                     ("clinician2 chart", faults._clinician2_create_chart), ("bookkeeping burst", faults._bookkeeping_burst)):
        ctx = faults.Ctx(item, app, oracle, targets, fx.api_for)
        try:
            fn(ctx, 1)
        except Exception as exc:  # noqa: BLE001 - recorded
            print(name, "failed:", exc, flush=True)
        step(name, intended=None, person=item.person, legit=ctx.legit)
    (a.out / "calibration.json").write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
