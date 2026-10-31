"""The fault gate on an OpenEMR test copy (no model calls).

    python -m verification_coverage.openemr_gate --run-id <id>

Runs ``openadapt_flow.fault_gate.run_gate`` against the fixture: the clean
save is the actor's own ``POST /api/patient``; the faults are privileged SQL
on ``patient_data``; the check reads through OpenEMR's REST API with the
read-only oracle client, exactly as ``verify-run`` does in production. Two
checks are measured:

- ``full``: the patient list, as in #1365 (``/api/patient``);
- ``scoped``: the same contract read through a search for the run's email,
  the kind of read a synthesized check picks. It can't see another chart.

Writes ``results/openemr/<run-id>/gate-<check>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))

from scoreboard.claims_openemr import (  # noqa: E402
    EXISTING,
    TOKEN_ENV,
    VERIFY_YAML,
    _token_env,
    reset_and_seed,
)
from scoreboard.tasks import openemr  # noqa: E402

FIELDS = openemr.SQL_FIELDS


def _sql_value(value: Any) -> str:
    text = "" if value is None else str(value)
    if not re.fullmatch(r"[A-Za-z0-9 ._@:/+-]*", text):
        raise ValueError("unexpected characters in a synthetic value")
    return f"'{text}'"


class OpenEMRCopy:
    """The fixture as a :class:`openadapt_flow.fault_gate.TestCopy`."""

    name = "openemr_fixture"
    key_field = "id"

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)

    def reset(self) -> None:
        reset_and_seed()

    def save(self) -> Any:
        fx = openemr.fixture()
        body = {k: v for k, v in self.values.items() if k != "state_label"}
        body["state"] = "MA"
        session = fx.token_session("actor")
        try:
            status = session.post(
                f"{fx.api_base_url}/apis/default/api/patient", json=body
            ).status_code
        finally:
            session.close()
        if status not in (200, 201):
            raise RuntimeError(f"the clean save returned HTTP {status}")
        rows = [r for r in openemr.patients() if r["email"] == self.values["email"]]
        return rows[-1]["id"]

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in openemr.patients()]

    def update(self, key: Any, changes: Mapping[str, Any]) -> None:
        sets = ", ".join(
            f"{name} = {_sql_value(value)}"
            for name, value in changes.items()
            if name in FIELDS and name != "id"
        )
        openemr.fixture()._db(
            f"UPDATE openemr.patient_data SET {sets} WHERE id = {int(key)}"
        )

    def insert(self, record: Mapping[str, Any]) -> Any:
        db = openemr.fixture()._db
        donor = db("SELECT MIN(id) FROM openemr.patient_data").decode().split()[0]
        sets = ", ".join(
            f"{name} = {_sql_value(value)}"
            for name, value in record.items()
            if name in FIELDS and name != "id"
        )
        db(
            "CREATE TEMPORARY TABLE openemr.vc_row AS SELECT * FROM "
            f"openemr.patient_data WHERE id = {int(donor)}; "
            "UPDATE openemr.vc_row SET id = (SELECT MAX(id) + 1 FROM "
            "openemr.patient_data), pid = (SELECT MAX(pid) + 1 FROM "
            "openemr.patient_data), uuid = UNHEX(REPLACE(UUID(), '-', ''))"
            + (f", {sets}" if sets else "")
            + "; INSERT INTO openemr.patient_data SELECT * FROM openemr.vc_row;"
        )
        return db("SELECT MAX(id) FROM openemr.patient_data").decode().split()[0]

    def delete(self, key: Any) -> None:
        openemr.fixture()._db(f"DELETE FROM openemr.patient_data WHERE id = {int(key)}")

    def verifier_overrides(self) -> dict[str, Any]:
        return {}


def main() -> int:
    import yaml

    from openadapt_flow.fault_gate import run_gate
    from openadapt_flow.verify_run import parse_verify_config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--item", type=int, default=990)
    args = parser.parse_args()
    out = HERE / "results" / "openemr" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    fx = openemr.fixture()
    values = openemr.item_values(args.item)
    params = {k: v for k, v in values.items() if k != "state_label"}
    os.environ.update(_token_env())
    full = yaml.safe_load(VERIFY_YAML.format(api=fx.ui_base_url, token_env=TOKEN_ENV))
    scoped = json.loads(json.dumps(full))
    scoped["verifier"]["records_path"] = "/apis/default/api/patient?email={email}"
    scoped["verifier"]["path_params"] = {"email": {"param": "email"}}
    assert EXISTING["email"] != values["email"]
    summary = {}
    for name, doc in (("full", full), ("scoped", scoped)):
        cfg = parse_verify_config(doc)
        record = run_gate(cfg, params, OpenEMRCopy(values))
        (out / f"gate-{name}.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        summary[name] = {
            "killed": record.killed,
            "applicable": record.applicable,
            "clean_pass": record.clean_pass,
            "survived": [c["fault"] for c in record.cases if c["result"] == "SURVIVED"],
        }
        print(name, summary[name], flush=True)
    fx.reset()
    (out / "gate-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
