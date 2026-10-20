"""Qualify, certify and run the OpenEMR patient-registration bundle.

A thin wrapper over ``openadapt-flow qualify``. Everything app-specific is in
``benchmark/openemr_local/verify.yaml`` (read-back, record fields, duplicate
key) and ``benchmark/openemr_local/qualify_hooks.py`` (sign-in, fixture reset,
OAuth session). The same campaign runs without this script::

    openadapt-flow qualify path/to/bundle \
      --verify benchmark/openemr_local/verify.yaml \
      --out runs/openemr-qual-1 --certify clinical-write --run-params patient.json

What this script adds is demo-only: a second oracle that reads
``openemr.patient_data`` directly over SQL on the local fixture database
(``--sql-oracle``, on by default), a real-duplicate check after certification
(``--duplicate-trials``, 3 by default), and flags for the fixture's state, lock
and ports.

The six qualify cases inject faults the engine must notice; none of them is a
business rule the application enforces. The real-duplicate check is one: the
existing chart Rosa Delgado (born 1958-03-27) is seeded, and the certified
bundle runs, as a certified run, on Rosa Delgado born 1958-03-22. OpenEMR's
duplicate dialog lists the existing chart, and the program must halt before
saving: ``HALTED_BEFORE_EFFECT``, with no new or changed chart in SQL, on every
trial. A failure fails the qualification. It never calls a model. Keys are local test keys generated per
campaign, never a production trust root.

Usage (fixture already up; see benchmark/openemr_local/README.md)::

    python -m scripts.openemr_qualification \
      --bundle-src path/to/compiled/bundle --out runs/openemr-qual-1 \
      --fixture-state path/to/state [--lock path/to/environment.lock.json] \
      [--ui-url http://127.0.0.1:9301 --api-url https://127.0.0.1:9300] \
      [--run-params patient.json] [--no-sql-oracle]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_flow.ir import RunReport  # noqa: E402
from openadapt_flow.qualify import (  # noqa: E402
    QualifyConfigError,
    QualifyError,
    QualifyHooks,
    TrialContext,
    load_verify_config,
    qualify,
)

VERIFY_YAML = REPO_ROOT / "benchmark" / "openemr_local" / "verify.yaml"
SQL_FIELDS = (
    "id", "pid", "title", "fname", "lname", "DOB", "sex", "street", "city",
    "state", "postal_code", "phone_home", "email", "country_code",
)  # fmt: skip


def sql_patients(fixture: Any) -> list[dict[str, str]]:
    """Every patient row, read directly from the fixture database."""

    out = fixture._db(
        "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id"
    ).decode()
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
        if line.strip()
    ]


def expected_sql_fields(params: dict[str, Any]) -> dict[str, str]:
    """The patient_data columns the registration must write."""

    names = ("title", "fname", "lname", "DOB", "sex", "street", "city",
             "postal_code", "phone_home", "email", "country_code")  # fmt: skip
    fields = {name: str(params[name]) for name in names}
    fields["state"] = "MA"  # the recorded state selection; see verify.yaml
    return fields


class SqlOracle:
    """Demo-only second oracle: direct SQL, independent of REST and the report."""

    def __init__(self) -> None:
        from benchmark.openemr_local.qualify_hooks import fixture

        self.fixture = fixture()

    def before_trial(self, ctx: TrialContext) -> None:
        ctx.state["sql_before"] = sql_patients(self.fixture)

    def check(self, ctx: TrialContext, report: RunReport) -> tuple[str, dict[str, Any]]:
        before = ctx.state["sql_before"]
        after = sql_patients(self.fixture)
        before_ids = {row["id"] for row in before}
        new_rows = [row for row in after if row["id"] not in before_ids]
        changed = [
            row for row in after if row["id"] in before_ids and row not in before
        ]
        row = new_rows[0] if len(new_rows) == 1 else None
        fields = [
            {"field": name, "match": row is not None and row.get(name) == want}
            for name, want in expected_sql_fields(ctx.params).items()
        ]
        if ctx.oracle_kind == "exact_record":
            passed = (
                len(new_rows) == 1 and not changed and all(f["match"] for f in fields)
            )
            observed = "exact_record" if passed else "record_mismatch"
        else:
            passed = not new_rows and not changed
            observed = "no_mutation" if passed else "unexpected_mutation"
        return "sql-oracle", {
            "schema_version": "openadapt.openemr-qualification-oracle/v1",
            "oracle_kind": ctx.oracle_kind,
            "read_path": "direct SQL on openemr.patient_data",
            "passed": passed,
            "observed": observed,
            "patients_before": len(before),
            "patients_after": len(after),
            "new_charts": len(new_rows),
            "existing_charts_changed": len(changed),
            "fields_matched": sum(f["match"] for f in fields),
            "fields_expected": len(fields),
            "field_check": fields,
            "silent_incorrect_success": bool(not passed and report.success),
        }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bundle-src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fixture-state", type=Path, required=True)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--ui-url")
    parser.add_argument("--api-url")
    parser.add_argument(
        "--run-params", type=Path, help="JSON inputs for a certified run"
    )
    parser.add_argument("--trials", type=int)
    parser.add_argument("--cases", help="comma-separated case ids (default: all six)")
    parser.add_argument("--no-certify", action="store_true")
    parser.add_argument(
        "--no-sql-oracle", action="store_true", help="skip the demo-only SQL check"
    )
    parser.add_argument(
        "--duplicate-trials",
        type=int,
        default=3,
        help="certified runs on a real possible duplicate (0 skips the check)",
    )
    args = parser.parse_args(argv)

    os.environ["OPENEMR_FIXTURE_STATE"] = str(args.fixture_state)
    for name, value in (
        ("OPENEMR_FIXTURE_LOCK", args.lock),
        ("OPENEMR_UI_URL", args.ui_url),
        ("OPENEMR_API_URL", args.api_url),
    ):
        if value:
            os.environ[name] = str(value)

    hooks = QualifyHooks()
    if not args.no_sql_oracle:
        sql = SqlOracle()
        hooks.before_trial = sql.before_trial
        hooks.extra_checks.append(sql.check)
    try:
        config = load_verify_config(VERIFY_YAML)
        result = qualify(
            args.bundle_src,
            config,
            out=args.out,
            cases=args.cases.split(",") if args.cases else None,
            trials=args.trials,
            certify=None if args.no_certify else config.campaign.policy,
            run_params=json.loads(args.run_params.read_text())
            if args.run_params
            else None,
            hooks=hooks,
        )
    except QualifyConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except QualifyError as exc:
        print(f"QUALIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    if not result.passed and result.partial:
        print(
            f"PARTIAL: every case run behaved as required; qualifying needs all six: {args.out}"
        )
        return 0
    if result.certified and args.duplicate_trials > 0:
        check = duplicate_check(result, args.out, args.duplicate_trials)
        if not check["passed"]:
            print(
                "QUALIFICATION FAILED: the certified bundle did not halt on a real "
                f"possible duplicate: {args.out / 'duplicate-check' / 'summary.json'}",
                file=sys.stderr,
            )
            return 1
    print(
        f"{'CERTIFIED' if result.certified else 'QUALIFIED' if result.passed else 'FAILED'}"
        f" under {result.policy or 'no policy'}: {args.out}"
    )
    return 0 if result.passed else 1


def duplicate_check(result: Any, out: Path, trials: int) -> dict[str, Any]:
    """Run the certified bundle ``trials`` times on a real possible duplicate."""

    from benchmark.openemr_local.certified_referrals import (
        certified_env,
        referral_params,
        run_certified,
    )

    key = "r6-rosa-delgado"
    root = out / "duplicate-check"
    env = certified_env(Path(result.bundle_dir))
    rows = []
    for trial in range(1, trials + 1):
        report, sql, seeded = run_certified(
            env,
            key=key,
            params=referral_params(key),
            expected="HALTED_BEFORE_EFFECT",
            item=root / f"trial-{trial}",
            trial=trial,
            policy=result.policy,
        )
        rows.append(
            {
                "trial": trial,
                "execution_outcome": report.execution_outcome,
                "existing_chart_seed_status": seeded,
                "new_charts": sql["new_charts"],
                "changed_charts": sql["changed_charts"],
                "passed": report.execution_outcome == "HALTED_BEFORE_EFFECT"
                and seeded in (200, 201)
                and sql["new_charts"] == 0
                and sql["changed_charts"] == 0,
            }
        )
        print(json.dumps({"duplicate_check": rows[-1]}), flush=True)
    summary = {
        "schema_version": "openadapt.openemr-duplicate-check/v1",
        "existing_chart": "Rosa Delgado, born 1958-03-27 (seeded)",
        "run_params": key,
        "required": "HALTED_BEFORE_EFFECT, no new or changed chart in SQL",
        "trials": rows,
        "passed": all(r["passed"] for r in rows),
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "",
        "## Real-duplicate check",
        "",
        "The existing chart Rosa Delgado (born 1958-03-27) is seeded, and the "
        "certified bundle runs on Rosa Delgado born 1958-03-22. Required: "
        "HALTED_BEFORE_EFFECT and no new or changed chart in SQL.",
        "",
        "| Trial | Outcome | New charts | Changed charts | Pass |",
        "|---|---|---|---|---|",
        *(
            f"| {r['trial']} | {r['execution_outcome']} | {r['new_charts']} | "
            f"{r['changed_charts']} | {'yes' if r['passed'] else 'no'} |"
            for r in rows
        ),
        "",
    ]
    with (out / "QUALIFICATION.md").open("a") as fh:
        fh.write("\n".join(lines))
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
