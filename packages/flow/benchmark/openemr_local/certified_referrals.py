"""Run the certified OpenEMR referral bundle on the landing demo's four referrals.

    OPENEMR_FIXTURE_STATE=... OPENEMR_FIXTURE_LOCK=... OPENEMR_UI_URL=... \\
    OPENEMR_API_URL=... python -m benchmark.openemr_local.certified_referrals \\
        --bundle <certified bundle> --out <dir> --seal-home <dir outside the repo> \\
        --authorized-by "<who authorized, and how>"

Each run is what ``openadapt-flow qualify --run-params`` does for its one
certified run: the fixture is reset (and the existing chart Rosa Delgado,
born 1958-03-27, seeded, so r6 is a possible duplicate), the Standard run
gate is evaluated on the certified bundle under its certification policy, the
governed authorization is built from that gate, and the bundle replays with
durable runs and settled-state detection on. The engine's record check reads
OpenEMR's REST API; a second, direct SQL read is kept beside it. No model is
called. The authorization is the run gate on the certified bundle (no
separate signed admission file); ``authorization.json`` in each run records
who authorized running it.

With ``--hidpi-scale 2``, each run also keeps a 2x copy of every step frame
in ``<run>/hidpi/`` (``hidpi_frames.py``): display copies for a high-density
screen, outside the sealed ``engine/`` directory.

Each run's ``report.json`` is signed with an offline Seal from the key in
``--seal-home`` (the private half stays there and is never committed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

REFS = ("r3-ines-carvalho", "r4-theo-lindqvist", "r5-grace-adeyemi", "r6-rosa-delgado")
EXPECTED = {
    "r3-ines-carvalho": "VERIFIED",
    "r4-theo-lindqvist": "VERIFIED",
    "r5-grace-adeyemi": "VERIFIED",
    "r6-rosa-delgado": "HALTED_BEFORE_EFFECT",
}
#: Where r6 must stop: the duplicate dialog's "No matches were found." check.
DUPLICATE_STOP = "step_039"


def mismatches(row: dict[str, Any], halted_at: str | None) -> list[str]:
    """How one run differs from what the landing demo shows; empty if it doesn't."""

    out = []
    if row["transaction_outcome"] != row["expected"]:
        out.append(f"transaction outcome {row['transaction_outcome']}")
    verified = row["expected"] == "VERIFIED"
    if row["new_charts"] != (1 if verified else 0) or row["changed_charts"]:
        out.append(f"{row['new_charts']} new, {row['changed_charts']} changed charts")
    if not verified and halted_at != DUPLICATE_STOP:
        out.append(f"stopped at {halted_at}, not {DUPLICATE_STOP}")
    return out


def _seed_existing_chart(fixture: Any) -> int:
    from benchmark.openemr_api_tier.common import EXISTING_ROSA, Referral, spec_for

    session = fixture.token_session("actor")
    try:
        return session.post(
            f"{fixture.api_base_url}/apis/default/api/patient",
            json=dict(spec_for(Referral("rosa", EXISTING_ROSA)).fields),
        ).status_code
    finally:
        session.close()


def run_certified(
    env: dict[str, Any],
    *,
    key: str,
    params: dict[str, str],
    expected: str,
    item: Path,
    trial: int,
    policy: str,
) -> tuple[Any, dict[str, Any], int]:
    """One run of the certified bundle on ``params``, as qualify's certified run.

    The fixture is reset and the existing chart seeded; the Standard run gate
    is evaluated on the bundle under ``policy`` and the governed authorization
    built from it. Returns the run report, the direct SQL read-back, and the
    HTTP status of the seed.
    """

    from openadapt_flow.qualify.campaign import TrialContext, _run
    from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
    from scripts.openemr_qualification import sql_patients

    workflow, fx = env["workflow"], env["fixture"]
    item.mkdir(parents=True)
    env["hooks"].reset()
    seeded = _seed_existing_chart(fx)
    before = sql_patients(fx)
    ctx = TrialContext(
        case_id="certified-referral",
        expected=expected,
        oracle_kind="exact_record" if expected == "VERIFIED" else "no_mutation",
        trial=trial,
        trial_dir=item,
        run_dir=item / "engine",
        run_id=f"openemr-referral-{key}-{int(time.time())}",
        params=params,
        certified_run=True,
        state={"policy": policy},
    )

    def authorize(_verifier: Any, gate_kwargs: dict[str, Any]) -> Any:
        gate = evaluate_run_gate(workflow, **gate_kwargs)
        (item / "run-gate.json").write_text(
            json.dumps(gate.model_dump(mode="json"), indent=2) + "\n"
        )
        if not gate.passed:
            raise SystemExit(f"run gate refused {key}:\n{gate.render()}")
        return build_runtime_authorization(workflow, gate, params=params)

    hidpi_scale = env.get("hidpi_scale")
    copies = item / "hidpi-captures"
    if hidpi_scale:
        os.environ["OPENEMR_HIDPI_FRAMES_DIR"] = str(copies)
        os.environ["OPENEMR_HIDPI_SCALE"] = str(hidpi_scale)
    marks: dict[str, float] = {"start": time.time()}
    setup = type("Setup", (), {"observer": None})()
    report, _ = _run(
        workflow=workflow,
        bundle_dir=env["bundle"],
        config=env["config"],
        hooks=env["hooks"],
        ctx=ctx,
        setup=setup,  # type: ignore[arg-type]
        authorize=authorize,
        fault_driver=None,
        marks=marks,
        lookup=env["lookup"],
    )
    if hidpi_scale:
        from benchmark.openemr_local.hidpi_frames import pair

        os.environ.pop("OPENEMR_HIDPI_FRAMES_DIR", None)
        pair(item / "engine", copies, item / "hidpi", scale=hidpi_scale)
        shutil.rmtree(copies)
    time.sleep(1.0)
    after = sql_patients(fx)
    before_ids = {r["id"] for r in before}
    new_rows = [r for r in after if r["id"] not in before_ids]
    changed = [r for r in after if r["id"] in before_ids and r not in before]
    sql = {
        "new_charts": len(new_rows),
        "changed_charts": len(changed),
        "new_chart_fields": new_rows[0] if len(new_rows) == 1 else None,
    }
    (item / "sql-readback.json").write_text(json.dumps(sql, indent=2) + "\n")
    steps = [
        {
            "step_id": r.step_id,
            "ok": r.ok,
            "elapsed_ms": r.elapsed_ms,
            "timings": r.timings,
        }
        for r in report.results
    ]
    (item / "result.json").write_text(
        json.dumps(
            {
                "ref": key,
                "expected": expected,
                "execution_outcome": report.execution_outcome,
                "transaction_outcome": report.transaction_outcome,
                "execution_profile": report.execution_profile,
                "production_eligible": report.production_eligible,
                "model_calls": report.model_calls,
                "marks_epoch": marks,
                "steps": steps,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    return report, sql, seeded


def certified_env(bundle: Path) -> dict[str, Any]:
    """What every certified run of ``bundle`` shares."""

    from benchmark.openemr_local.qualify_hooks import fixture
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualify.campaign import _default_hooks, lookup_plan
    from openadapt_flow.qualify.config import load_verify_config
    from scripts.openemr_qualification import VERIFY_YAML

    config = load_verify_config(VERIFY_YAML)
    surface, structure = lookup_plan(config)
    return {
        "bundle": bundle,
        "workflow": Workflow.load(bundle),
        "config": config,
        "hooks": _default_hooks(config, None),
        "lookup": {"use_structural": structure, "deployment_surface": surface},
        "fixture": fixture(),
    }


def referral_params(key: str) -> dict[str, str]:
    from benchmark.openemr_api_tier.common import REFERRALS, spec_for

    ref = {r.key: r for r in REFERRALS}[key]
    return {k: str(v) for k, v in spec_for(ref).params().items()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seal-home", required=True, type=Path)
    parser.add_argument("--authorized-by", required=True)
    parser.add_argument("--policy", default="clinical-write")
    parser.add_argument(
        "--hidpi-scale",
        type=int,
        default=0,
        help="also keep a copy of each step frame at this pixel density, in "
        "<run>/hidpi/ (display only; the engine still runs at scale 1)",
    )
    args = parser.parse_args()

    from openadapt_flow.offline_seal import public_key_entry, seal_run

    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    bundle = out / "bundle"
    shutil.copytree(args.bundle, bundle)
    env = certified_env(bundle)
    env["hidpi_scale"] = args.hidpi_scale
    workflow = env["workflow"]
    rows = []
    for n, key in enumerate(REFS, start=1):
        item = out / f"{n}-{key}"
        report, sql, seeded = run_certified(
            env,
            key=key,
            params=referral_params(key),
            expected=EXPECTED[key],
            item=item,
            trial=n,
            policy=args.policy,
        )
        (item / "authorization.json").write_text(
            json.dumps(
                {
                    "authorized_by": args.authorized_by,
                    "how": "Standard run gate on the certified bundle, evaluated "
                    "per run; governed authorization built from that gate",
                    "policy": args.policy,
                    "bundle_content_digest": workflow.manifest.content_digest
                    if workflow.manifest
                    else None,
                    "existing_chart_seed_status": seeded,
                },
                indent=2,
            )
            + "\n"
        )
        seal = seal_run(item / "engine", home=args.seal_home, enqueue=False)
        row = {
            "n": n,
            "ref": key,
            "expected": EXPECTED[key],
            "execution_outcome": report.execution_outcome,
            "transaction_outcome": report.transaction_outcome,
            "new_charts": sql["new_charts"],
            "changed_charts": sql["changed_charts"],
            "seal_key_id": seal.key_id,
            "report_sha256": seal.subject_sha256,
        }
        halted_at = next((r.step_id for r in report.results if not r.ok), None)
        row["halted_at"] = halted_at
        row["mismatches"] = mismatches(row, halted_at)
        rows.append(row)
        print(json.dumps(row), flush=True)
    (out / "seal-key.json").write_text(
        json.dumps([public_key_entry(args.seal_home)], indent=2) + "\n"
    )
    (out / "summary.json").write_text(
        json.dumps(
            {
                "authorized_by": args.authorized_by,
                "bundle_content_digest": workflow.manifest.content_digest
                if workflow.manifest
                else None,
                "runs": rows,
            },
            indent=2,
        )
        + "\n"
    )
    failed = [r["ref"] for r in rows if r["mismatches"]]
    if failed:
        print(f"differs from the expected outcome: {', '.join(failed)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
