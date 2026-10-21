"""Fresh agent runs on OpenEMR, judged by three engines, per fault class.

    python -m verification_coverage.openemr_run --run-id <id> --per-fault 5

The rerun of OpenAdaptAI/OpenAdapt#1365's OpenEMR fault experiment that
``docs/VERIFICATION_COVERAGE.md`` asks for: fresh computer-use agent runs
(Claude Sonnet 5.5, the scoreboard's agent), not rebuilt database states.
Faults are injected only in the fixture's database, by the same triggers and
poller as ``scoreboard/claims_openemr.py``. Each item is judged by three
``verify-run`` engines, each from its own checkout and with its own baseline
taken before the agent starts:

- ``e1365``: the engine #1365 measured with (collateral check finds a removed
  record only);
- ``main``: main before this work (#1375's per-effect collateral change);
- ``after``: frame conditions with OpenEMR's audit log for attribution, and
  the round trip against the referral document.

Fault classes: the seven of #1365 plus three.

=========================  ====================================================
``ok``                     control: a clean save
``partial``                a trigger blanks the new chart's home phone
``wrong_value``            a trigger changes the new chart's postal code
``duplicate``              the poller copies the new chart into a second chart
``no_persist``             the poller deletes the new chart 3 s after it lands
``collateral_loss``        the poller deletes another, pre-existing chart
``collateral_edit``        the poller changes another chart's home phone
``collateral_edit_bot``    the same, logged in OpenEMR's audit log under the
                           bot's own user (``openadapt_actor``)
``concurrent_edit_other``  the same, logged under another user: a legitimate
                           concurrent edit, so the save must still verify
``misread_source``         the referral says 02139; the agent is told 02193
                           (a misread before the save) and saves it faithfully
=========================  ====================================================

The audit-log rows the poller writes for the two logged edits are shaped
like the ``patient-record-update`` rows OpenEMR writes for a UI edit. The
fixture's OpenEMR is unchanged; everything injected is in its database.
Synthetic data only.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))

from scoreboard import schema  # noqa: E402
from scoreboard.claims_openemr import (  # noqa: E402
    TOKEN_ENV,
    TRIGGERS,
    VERIFY_YAML,
    Poller,
    _install_trigger,
    _rows,
    _token_env,
    ground_truth,
    reset_and_seed,
)
from scoreboard.run import (  # noqa: E402
    ITEM_TIMEOUT_S,
    SPEND_CAP_USD,
    _agent_item,
    _ledger_view,
    add_spend,
    reported_success,
    spent,
)
from scoreboard.tasks import openemr  # noqa: E402

FAULTS = (
    "ok",
    "partial",
    "wrong_value",
    "duplicate",
    "no_persist",
    "collateral_loss",
    "collateral_edit",
    "collateral_edit_bot",
    "concurrent_edit_other",
    "misread_source",
)
#: Faults after which the save is still correct (a false alarm if refused).
CLEAN = ("ok", "concurrent_edit_other")
ENGINES = ("e1365", "main", "after")
BOT_USER = "openadapt_actor"
OTHER_USER = "clinician2"
AUDIT_USER = "oa_audit_reader"
AUDIT_ENV = "VC_AUDIT_PASSWORD"
MISREAD = ("postal_code", "02193")

FIELDS = (
    "item",
    "fault",
    "claimed_success",
    "ground_truth",
    *(f"{e}_outcome" for e in ENGINES),
    *(f"{e}_checked" for e in ENGINES),
    "presave_check",
    "fault_applied",
    "model_calls",
    "cost_usd",
    "host_load_1m",
    "final_text",
)

#: Each referral field, the label the referral document prints, its kind.
DOC_FIELDS = (
    ("title", "Title", None),
    ("fname", "First name", None),
    ("lname", "Last name", None),
    ("DOB", "Date of birth", "date"),
    ("sex", "Sex", None),
    ("street", "Street", None),
    ("city", "City", None),
    ("state_label", "State", None),
    ("postal_code", "Postal code", None),
    ("phone_home", "Home phone", None),
    ("email", "Email", None),
    ("country_code", "Country", None),
)
#: Parameters the record contract binds (state is a literal there).
BOUND = tuple(name for name, _l, _k in DOC_FIELDS if name != "state_label")


def frame_yaml(db_container: str) -> str:
    """The frame for the ``after`` engine: OpenEMR's audit log attributes."""

    query = (
        "SELECT id, user, patient_id FROM log WHERE event LIKE 'patient-record-%' "
        "AND event <> 'patient-record-select' AND patient_id > 0"
    )
    return (
        "frame:\n"
        "  identity_field: pid\n"
        "  attribution:\n"
        "    kind: audit_read\n"
        "    actor_field: user\n"
        "    record_field: patient_id\n"
        f"    self: [{BOT_USER}]\n"
        "    read:\n"
        "      kind: sql\n"
        "      sql_driver: verification_coverage.exec_mariadb\n"
        "      sql_connect_args:\n"
        f"        container: {db_container}\n"
        f"        user: {AUDIT_USER}\n"
        "        database: openemr\n"
        f"      sql_password_env: {AUDIT_ENV}\n"
        f'      sql_query: "{query}"\n'
    )


def referral_text(values: dict[str, str]) -> str:
    lines = ["Referral (synthetic test document)", "Referred to: Example Family Clinic"]
    lines += [f"{label}: {values[name]}" for name, label, _k in DOC_FIELDS]
    return "\n".join(lines) + "\n"


def provenance(doc_sha: str, values: dict[str, str]) -> dict[str, Any]:
    """What a first reader extracted, citing each line of the referral."""

    params = {}
    for offset, (name, label, kind) in enumerate(DOC_FIELDS):
        if name not in BOUND:
            continue
        entry: dict[str, Any] = {
            "value": values[name],
            "source": {
                "kind": "document",
                "sha256": doc_sha,
                "line": 3 + offset,
                "label": label,
            },
            "extractor": "model:simulated-first-reader",
        }
        if kind:
            entry["value_kind"] = kind
        params[name] = entry
    return {"schema_version": "openadapt.param-provenance/v1", "params": params}


class LoggedPoller(Poller):
    """The scoreboard poller, plus edits recorded in OpenEMR's audit log."""

    def __init__(self, fault: str, *args: Any, **kwargs: Any) -> None:
        base = "collateral_edit" if fault.endswith(("_bot", "_other")) else fault
        super().__init__(base, *args, **kwargs)
        self.logged_as = (
            BOT_USER
            if fault == "collateral_edit_bot"
            else OTHER_USER
            if fault == "concurrent_edit_other"
            else None
        )

    def run(self) -> None:
        super().run()
        if self.logged_as and self.applied:
            db = openemr.fixture()._db
            pid = (
                db(f"SELECT pid FROM openemr.patient_data WHERE id = {self.victim}")
                .decode()
                .split()[0]
            )
            db(
                "INSERT INTO openemr.log (date, event, category, user, groupname, "
                "comments, patient_id, success) VALUES (NOW(), "
                "'patient-record-update', 'Patient Demographics', "
                f"'{self.logged_as}', 'Default', '', {int(pid)}, 1)"
            )
            self.applied += f" logged as {self.logged_as}"


def engine_env(engine_root: Path, extra: dict[str, str]) -> dict[str, str]:
    packages = engine_root / "packages"
    paths = [packages / "types", packages / "flow", BENCH]
    return {**os.environ, **extra, "PYTHONPATH": os.pathsep.join(map(str, paths))}


def verify(
    engine_root: Path, env: dict[str, str], params: dict[str, str], *argv: str
) -> subprocess.CompletedProcess[str]:
    """verify-run from ``engine_root``, the item's values on stdin."""

    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _LAUNCH,
            "verify-run",
            "--params",
            "/dev/stdin",
            *argv,
        ],
        input=json.dumps(params),
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=str(engine_root),
    )


#: ``-I`` ignores PYTHONPATH, so put it back by hand: the engine's packages
#: and this benchmark (for the audit-read driver), nothing else of the caller.
_LAUNCH = (
    "import os, runpy, sys; "
    "sys.path[:0] = os.environ['PYTHONPATH'].split(os.pathsep); "
    "sys.argv = ['openadapt-flow'] + sys.argv[1:]; "
    "runpy.run_module('openadapt_flow', run_name='__main__')"
)


def ensure_audit_reader(password: str) -> str:
    """A SELECT-only MariaDB user for the audit log; returns the container."""

    fx = openemr.fixture()
    fx._db(
        f"CREATE USER IF NOT EXISTS '{AUDIT_USER}'@'%' IDENTIFIED BY '{password}'; "
        f"ALTER USER '{AUDIT_USER}'@'%' IDENTIFIED BY '{password}'; "
        f"GRANT SELECT ON openemr.log TO '{AUDIT_USER}'@'%'; FLUSH PRIVILEGES;"
    )
    out = fx._compose("ps", "-q", "db").stdout.decode().split()
    if not out:
        raise SystemExit("no database container for the fixture")
    return out[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--per-fault", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=0.50)
    parser.add_argument("--faults", default=",".join(FAULTS))
    parser.add_argument("--first-item", type=int, default=941)
    for engine in ENGINES:
        parser.add_argument(f"--engine-{engine}", required=True, type=Path)
    args = parser.parse_args()
    engines = {e: getattr(args, f"engine_{e}") for e in ENGINES}

    out = HERE / "results" / "openemr" / args.run_id
    work = schema.CACHE / "verification_coverage" / "openemr" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    done = (
        {int(r["item"]) for r in csv.DictReader(rows_path.open())}
        if rows_path.is_file()
        else set()
    )
    fx = openemr.fixture()
    base_yaml = VERIFY_YAML.format(api=fx.ui_base_url, token_env=TOKEN_ENV)
    password = secrets.token_hex(16)
    heads = {
        e: subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for e, root in engines.items()
    }
    meta_written = False
    plan = list(
        enumerate(
            (f for f in args.faults.split(",") for _ in range(args.per_fault)),
            start=args.first_item,
        )
    )
    try:
        for i, fault in plan:
            if i in done:
                continue
            if spent() + args.max_cost > SPEND_CAP_USD:
                raise SystemExit("spend cap reached")
            reset_and_seed()
            container = ensure_audit_reader(password)
            if not meta_written:
                after_yaml = base_yaml + frame_yaml("<db container>")
                schema.write_meta(
                    out,
                    task="openemr_referral",
                    arm="verification_coverage",
                    model="claude-sonnet-5-5",
                    engines=heads,
                    verify_yaml=base_yaml,
                    verify_yaml_after=after_yaml,
                    faults=args.faults.split(","),
                    per_fault=args.per_fault,
                    fault_injection=(
                        "fixture database only: the scoreboard triggers and poller "
                        "(claims_openemr.py), plus audit-log rows for the two "
                        "logged edits (openemr_run.py)"
                    ),
                )
                meta_written = True
            truth_values = openemr.item_values(i)
            told = dict(truth_values)
            if fault == "misread_source":
                told[MISREAD[0]] = MISREAD[1]
            before = _rows()
            item_dir = work / f"item-{i:03d}"
            shutil.rmtree(item_dir, ignore_errors=True)
            item_dir.mkdir(parents=True)
            # The referral holds the true values; the provenance, what the
            # first reader extracted (the misread, for misread_source).
            doc = item_dir / "referral.txt"
            doc.write_text(referral_text(truth_values))
            from openadapt_flow.provenance import (
                ProvenanceFile,
                cross_check,
                default_extractor,
                document_sha256,
                read_sources,
            )

            prov_doc = provenance(document_sha256(doc), told)
            (item_dir / "provenance.json").write_text(json.dumps(prov_doc))
            prov = ProvenanceFile.model_validate(prov_doc)
            presave = cross_check(prov, read_sources([doc]), default_extractor(prov))
            params = {k: told[k] for k in BOUND}
            env = {**_token_env(), AUDIT_ENV: password}
            yamls = {
                "e1365": base_yaml,
                "main": base_yaml,
                "after": base_yaml + frame_yaml(container),
            }
            baselines = {}
            for engine, root in engines.items():
                cfg = item_dir / f"verify-{engine}.yaml"
                cfg.write_text(yamls[engine])
                baselines[engine] = item_dir / f"baseline-{engine}.json"
                cap = verify(
                    root,
                    engine_env(root, env),
                    params,
                    "--verify",
                    str(cfg),
                    "--capture-baseline",
                    str(baselines[engine]),
                )
                if cap.returncode != 0:
                    raise SystemExit(f"{engine} baseline failed: {cap.stderr[-800:]}")
            poller = None
            if fault in TRIGGERS:
                _install_trigger(fault, max(int(k) for k in before))
            elif fault not in ("ok", "misread_source"):
                poller = LoggedPoller(fault, before, told["email"])
                poller.start()
            ledger = out / "ledgers" / f"item-{i:03d}.json"
            load0 = schema.load_1m()
            ctx = multiprocessing.get_context("spawn")
            box = ctx.Queue()
            proc = ctx.Process(
                target=_agent_item,
                args=(
                    openemr.NAME,
                    openemr.entry_url(),
                    openemr.instruction(told),
                    str(ledger),
                    args.max_cost,
                    box,
                ),
            )
            proc.start()
            proc.join(ITEM_TIMEOUT_S)
            result: dict[str, Any] = {}
            if proc.is_alive():
                proc.terminate()
                proc.join(10)
                result = {"error": "ItemTimeout"}
            elif not box.empty():
                result = box.get()
            if poller is not None:
                poller.join(timeout=8.0)
                poller.stop.set()
            view = _ledger_view(ledger, result)
            add_spend(
                view.cost_usd, f"verification_coverage openemr {args.run_id} item {i}"
            )
            claimed = reported_success(view, result.get("error"))  # type: ignore[arg-type]
            truth = ground_truth(before, truth_values)
            if fault == "concurrent_edit_other" and truth == "collateral_edit":
                truth = "correct"  # another user's edit; the save itself is right
            record = item_dir / "agent-run.json"
            record.write_text(
                json.dumps(
                    {
                        "executor_kind": "agent",
                        "executor_id": "claude-sonnet-5-5",
                        "claimed_success": claimed,
                    }
                )
            )
            row: dict[str, Any] = {
                "item": i,
                "fault": fault,
                "claimed_success": claimed,
                "ground_truth": truth,
                "presave_check": presave.status,
                "fault_applied": (
                    poller.applied
                    if poller is not None
                    else fault
                    if fault in TRIGGERS or fault == "misread_source"
                    else ""
                ),
                "model_calls": len(view.calls),
                "cost_usd": round(view.cost_usd, 6),
                "host_load_1m": load0,
                "final_text": (result.get("error") or view.final_text)[:400],
            }
            for engine, root in engines.items():
                share = item_dir / f"share-{engine}"
                shutil.rmtree(share, ignore_errors=True)
                extra = (
                    [
                        "--provenance",
                        str(item_dir / "provenance.json"),
                        "--source",
                        str(doc),
                    ]
                    if engine == "after"
                    else []
                )
                ver = verify(
                    root,
                    engine_env(root, {**_token_env(), AUDIT_ENV: password}),
                    params,
                    "--verify",
                    str(item_dir / f"verify-{engine}.yaml"),
                    "--run-record",
                    str(record),
                    "--authorization-ref",
                    f"VERIFY-COVERAGE-{args.run_id}-{i:03d}",
                    "--baseline",
                    str(baselines[engine]),
                    "--out",
                    str(share),
                    *extra,
                )
                receipt_file = share / "effect-receipt.json"
                receipt = (
                    json.loads(receipt_file.read_text())["receipt"]
                    if receipt_file.is_file() and ver.returncode in (0, 1)
                    else {}
                )
                row[f"{engine}_outcome"] = receipt.get("outcome", "refused")
                row[f"{engine}_checked"] = (
                    json.dumps(receipt.get("checked"), sort_keys=True)
                    if receipt.get("checked")
                    else ""
                )
                if not receipt:
                    (item_dir / f"{engine}.stderr").write_text(ver.stderr[-4000:])
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            print(
                f"vc {i:3d} {fault:22s} truth={truth:16s} "
                + " ".join(f"{e}={row[f'{e}_outcome']}" for e in ENGINES)
                + f" presave={presave.status} ${view.cost_usd:.3f}",
                flush=True,
            )
            time.sleep(0.5)
    finally:
        fx.reset()
    meta = json.loads((out / "meta.json").read_text())
    meta["complete"] = True
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
