"""Code as action against the screenshot loop on the local OpenEMR referral task.

The task is the referral benchmark's (``benchmark/openemr_referral_timing``):
register a referred, made-up patient in OpenEMR from 12 structured fields,
without creating a duplicate. The same eight referrals are used, including r6,
whose name matches an existing chart with a different birth date (the agent
must stop, not save).

Both arms run in the same harness: a fresh browser, sign-in and the blank New
Patient form off the clock (as in the referral study), then every action goes
through the compile-agent harness and is recorded by the JIT capture:

* ``code``: ``ClaudeAgent(vision="code")``, one ``run`` tool.
* ``every_turn``: ``ClaudeAgent(vision="every_turn")``, Claude's computer
  toolset with a screenshot after every action (the screenshot loop).

The test setup reads the result back on its own, by SQL on the fixture
database: correct means exactly one new chart whose 12 fields match (or, for
r6, no new chart). For the code arm the recording is also compiled and the
compiled program is checked to carry the locator of every element the agent
acted on.

    OPENEMR_FIXTURE_STATE=... OPENEMR_FIXTURE_LOCK=... PYTHONPATH=. \\
        python benchmark/code_as_action/measure_openemr.py --env-file .env \\
        --reset --referrals r1,r2,r6 --arms code,every_turn

Made-up patients only (example.invalid emails, 555-01xx phones).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent.parent
sys.path.insert(0, str(HERE))
# ``benchmark`` here is a namespace package; an installed regular package of
# the same name would win the import, so bind this one first.
_pkg = types.ModuleType("benchmark")
_pkg.__path__ = [str(FLOW / "benchmark")]  # type: ignore[attr-defined]
sys.modules["benchmark"] = _pkg

import common  # noqa: E402

INSTRUCTION = (
    "Register this referred patient in the EMR. Do not create a duplicate chart. "
    "If the EMR shows a possible existing patient, stop and report instead of saving."
)

#: The referral study's eight made-up referrals (structured fields only).
REFERRALS: dict[str, dict[str, str]] = {
    "r1": dict(
        title="Ms.",
        fname="Nadia",
        lname="Brennan",
        dob="1971-04-19",
        sex="Female",
        street="27 WILLOW BEND ROAD",
        phone_home="555-0121",
        email="nadia.brennan@example.invalid",
    ),
    "r2": dict(
        title="Mr.",
        fname="Marcus",
        lname="Feld",
        dob="1958-09-02",
        sex="Male",
        street="114 QUARRY HILL DRIVE",
        phone_home="555-0124",
        email="marcus.feld@example.invalid",
    ),
    "r3": dict(
        title="Mrs.",
        fname="Ines",
        lname="Carvalho",
        dob="1949-12-11",
        sex="Female",
        street="9 LANTERN COURT",
        phone_home="555-0131",
        email="ines.carvalho@example.invalid",
    ),
    "r4": dict(
        title="Mr.",
        fname="Theo",
        lname="Lindqvist",
        dob="1983-07-26",
        sex="Male",
        street="63 FERRY STREET",
        phone_home="555-0136",
        email="theo.lindqvist@example.invalid",
    ),
    "r5": dict(
        title="Ms.",
        fname="Grace",
        lname="Adeyemi",
        dob="1990-02-08",
        sex="Female",
        street="402 CEDAR ROW",
        phone_home="555-0145",
        email="grace.adeyemi@example.invalid",
    ),
    "r6": dict(
        title="Mrs.",
        fname="Rosa",
        lname="Delgado",
        dob="1958-03-22",
        sex="Female",
        street="7 MILL ROAD",
        phone_home="555-0188",
        email="rosa.delgado@example.invalid",
    ),
    "r7": dict(
        title="Mr.",
        fname="Arlo",
        lname="Whitcombe",
        dob="1966-05-30",
        sex="Male",
        street="88 BEACON TERRACE",
        phone_home="555-0152",
        email="arlo.whitcombe@example.invalid",
    ),
    "r8": dict(
        title="Ms.",
        fname="Priya",
        lname="Raman",
        dob="1975-10-14",
        sex="Female",
        street="15 STATION AVENUE",
        phone_home="555-0167",
        email="priya.raman@example.invalid",
    ),
}
MISMATCH = {"r6"}
EXISTING = dict(
    title="Mrs.",
    fname="Rosa",
    lname="Delgado",
    dob="1958-03-27",
    sex="Female",
    street="311 BIRCH COURT",
    phone_home="555-0119",
    email="r.delgado.prior@example.invalid",
)
COMMON_FIELDS = dict(
    city="EXAMPLETON", state="MA", state_label="Massachusetts", postal_code="02139"
)

SQL_FIELDS = (
    "id",
    "pid",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)
KEYS = [
    "model_calls",
    "prompt_tokens",
    "output_tokens",
    "cost_usd",
    "run_seconds",
    "model_seconds",
    "actions",
    "screenshots_sent",
]


def spec_for(key: str) -> Any:
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec

    return SyntheticPatientSpec(**REFERRALS[key], **COMMON_FIELDS)


def task_prompt(spec: Any) -> str:
    fields = json.dumps(spec.params(), indent=2)
    return (
        "You are operating OpenEMR, an electronic medical record, in a web browser. It is a "
        "local test copy and every patient in it is made up. You are already signed in, and "
        "the New Patient form is open.\n\n"
        f"{INSTRUCTION}\n\n"
        "Referred patient (structured fields):\n"
        f"{fields}\n\n"
        "When you have finished, or if you stop, reply with a short report: what you did, "
        "whether a chart was saved, and anything that did not match."
    )


def _q(value: str) -> str:
    assert "'" not in value and "\\" not in value
    return "'" + value + "'"


def sql_patients(fixture: Any, where: str) -> list[dict[str, str]]:
    out = fixture._db(
        "SELECT "
        + ",".join(SQL_FIELDS)
        + " FROM openemr.patient_data WHERE "
        + where
        + " ORDER BY id"
    ).decode()
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
    ]


def _demo() -> Any:
    spec = importlib.util.spec_from_file_location(
        "openemr_local_demo", FLOW / "scripts" / "openemr_local_demo.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["openemr_local_demo"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def run_one(
    fixture: Any, demo: Any, arm: str, key: str, args: Any, work: Path
) -> dict[str, Any]:
    from openadapt_flow.authoring import AuthoringSession
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.compile_agent import ClaudeAgent
    from openadapt_flow.compile_agent.harness import Harness, SaveGate
    from openadapt_flow.jit.adapters import CaptureComputer
    from openadapt_flow.jit.compile_agent import TaskRequest
    from openadapt_flow.jit.keys import load_customer_model_key

    spec = spec_for(key)
    agent = ClaudeAgent(
        vision=arm,
        model=args.model,
        effort=args.effort,
        max_cost_usd=args.max_run_usd,
        max_turns=args.max_turns,
    )
    before_all = sql_patients(fixture, "1=1")
    backend, close = PlaywrightBackend.launch(fixture.ui_base_url, headless=True)
    error: Optional[str] = None
    ledger: Any = None
    harness: Any = None
    program: Optional[dict[str, Any]] = None
    started = time.time()
    load_before = load_after = common.load_1m()
    wall = 0.0
    try:
        demo._preauthenticate_browser(backend, fixture)
        backend.page.wait_for_function(
            "() => !document.querySelector('.modal-backdrop')", timeout=30_000
        )
        session = AuthoringSession(
            backend, work / "recording", backend_kind="web", app_url=fixture.ui_base_url
        )
        computer = CaptureComputer(session, dialect=agent.dialect, page=backend.page)
        session.start_record()
        values = [str(v) for v in spec.params().values()]
        # A new patient's identity is typed, not printed: the gate checks that
        # the commit happens in the new-patient confirm dialog.
        harness = Harness(
            computer,
            SaveGate(["Create New Patient"]),
            keep=values,
            evidence_dir=work / "gates",
        )
        request = TaskRequest(
            task_id=f"openemr.referral.{key}",
            instruction=task_prompt(spec),
            inputs=spec.params(),
            model_key=load_customer_model_key("anthropic"),
        )
        load_before = common.load_1m()
        started = time.time()
        try:
            ledger = agent.drive(request, harness)
        except Exception as exc:  # noqa: BLE001 - a failed run is a row
            error = f"{type(exc).__name__}: {exc}"[:500]
            ledger = getattr(agent, "last_ledger", None)
        wall = time.time() - started
        load_after = common.load_1m()
        trace = getattr(agent, "last_trace", None)
        if trace is not None and computer.actions:
            try:
                from openadapt_flow.compile_agent.code_action import (
                    check_trace_against_ir,
                )
                from openadapt_flow.ir import Workflow

                session.compile(
                    work / "bundle", name=f"openemr-referral-{key}", distill=True
                )
                program = check_trace_against_ir(
                    trace.to_dict(), Workflow.load(work / "bundle")
                )
            except Exception as exc:  # noqa: BLE001 - reported with the row
                program = {
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
    finally:
        close()
    time.sleep(1.0)
    after_all = sql_patients(fixture, "1=1")
    before_ids = {r["id"] for r in before_all}
    new_rows = [r for r in after_all if r["id"] not in before_ids]
    fields = spec.fields
    match = None
    if len(new_rows) == 1:
        match = {k: new_rows[0].get(k) == v for k, v in fields.items()}
    if key in MISMATCH:
        correct = not new_rows
    else:
        correct = match is not None and all(match.values())
    agent_row = ledger.to_dict() if ledger is not None else {}
    metrics = common.agent_metrics(
        agent_row, harness.stats.to_dict() if harness else {}
    )
    trace = getattr(agent, "last_trace", None)
    return {
        "app": "openemr-referral",
        "arm": arm,
        "referral": key,
        "mismatch_case": key in MISMATCH,
        "model": args.model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "correct": correct,
        "new_charts": len(new_rows),
        "fields_matched": (sum(match.values()) if match else 0),
        "fields_total": len(fields),
        "field_mismatches": sorted(k for k, ok in (match or {}).items() if not ok),
        "error": error,
        "run_seconds": round(wall, 3),
        "host_load_1m": [load_before, load_after],
        "under_load": max(load_before, load_after) >= common.LOAD_LIMIT,
        **metrics,
        "gates": [
            {
                k: v
                for k, v in g.items()
                if k not in ("screenshot", "url", "at", "identity")
            }
            for g in (harness.gate_log if harness else [])
        ],
        "code_action": trace.summary() if trace is not None else None,
        "program": program,
        "agent_summary": agent_row.get("summary"),
        "calls": agent_row.get("calls"),
        "scripts": trace.log if trace is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--arms", default="code,every_turn")
    parser.add_argument("--referrals", default="r1,r2,r3,r4,r5,r6,r7,r8")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reset the fixture and seed the existing chart first",
    )
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--max-run-usd", type=float, default=0.75)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--out", type=Path, default=common.RESULTS / "openemr")
    args = parser.parse_args()
    common.load_key(args.env_file)
    arms = [a for a in args.arms.split(",") if a]
    keys = [k for k in args.referrals.split(",") if k]

    from benchmark.openemr_local import qualify_hooks
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec

    fixture = qualify_hooks.fixture()
    demo = _demo()
    meta: dict[str, Any] = {"started_at": common.now()}
    if args.reset:
        meta["baseline_sha256"] = fixture.reset()
        existing = SyntheticPatientSpec(**EXISTING, **COMMON_FIELDS)
        meta["seeded_existing_chart_status"] = (
            fixture.token_session("actor")
            .post(
                fixture.api_base_url + "/apis/default/api/patient",
                json=dict(existing.fields),
            )
            .status_code
        )
    work_root = Path(tempfile.mkdtemp(prefix="code-action-openemr-"))
    rows_path = args.out / "runs.jsonl"
    try:
        for round_ in range(1, args.rounds + 1):
            for i, key in enumerate(keys):
                # Alternate which arm goes first, so drift hits both.
                order = arms if (i + round_) % 2 == 0 else list(reversed(arms))
                for arm in order:
                    if key not in MISMATCH and sql_patients(
                        fixture, f"email={_q(REFERRALS[key]['email'])}"
                    ):
                        # The other arm already registered this referral; a
                        # second save would be a duplicate. Remove nothing:
                        # skip it and say so.
                        print(f"{key} {arm}: skipped (chart exists)", flush=True)
                        continue
                    stopped = common.may_start(args.max_run_usd)
                    if stopped:
                        print(f"stopping: {stopped}", flush=True)
                        raise StopIteration
                    work = work_root / f"{round_}-{key}-{arm}"
                    work.mkdir(parents=True)
                    row = run_one(fixture, demo, arm, key, args, work)
                    row["round"] = round_
                    total = common.add_spend(
                        f"openemr {arm} {key}", float(row["cost_usd"] or 0)
                    )
                    common.append(rows_path, row)
                    print(
                        f"{key} {arm}: correct={row['correct']} new={row['new_charts']} "
                        f"fields={row['fields_matched']}/{row['fields_total']} "
                        f"{row['model_calls']} calls {row['actions']} actions "
                        f"{row['prompt_tokens']} prompt tokens ${float(row['cost_usd'] or 0):.4f} "
                        f"{row['run_seconds']} s load {row['host_load_1m']} err={row['error']} "
                        f"(total ${total:.2f})",
                        flush=True,
                    )
                    shutil.rmtree(work, ignore_errors=True)
    except StopIteration:
        pass
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    every = [json.loads(line) for line in rows_path.read_text().splitlines() if line]
    summary = {
        "arms": common.summarize(every, arms, KEYS),
        "environment": {
            "model": args.model,
            "effort": args.effort,
            "app": "OpenEMR local fixture (pinned image), New Patient form, 1280x800, headless",
            "engine_commit": common.commit(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "measured_at": common.now(),
            "load_limit_1m": common.LOAD_LIMIT,
            "spend_total_usd": common.spent(),
            **meta,
        },
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary["arms"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
