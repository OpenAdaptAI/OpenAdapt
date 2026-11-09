"""The EHR leg against Epic's public FHIR sandbox, read-only.

Two claim units for one of Epic's published sandbox test patients (Camila
Lopez). The EHR leg reads her Patient record from the sandbox with the
reconciler's own SMART backend token; the clearinghouse leg reads a 277
written here. One unit's 277 carries her birth date, the other a planted
wrong one. Four sandbox calls in all (two tokens, two searches): no loop,
no timing, no writes, within the open.epic terms
(``benchmark/commercial_ehr/FINDINGS.md``).

    set -a; source /path/to/.env; set +a
    python benchmark/reconcile/epic_check.py --out DIR
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
KNOWN = {"family": "Lopez", "given": "Camila", "birth_date": "1987-09-12"}

SPEC = {
    "unit": "claim",
    "anchor": "ehr",
    "legs": {
        "ehr": {
            "role": "ehr",
            "environment_id": "epic-open-sandbox-r4",
            "verifier": {
                "kind": "fhir",
                "base_url": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
                "resource_type": "Patient",
                "search_param_exprs": {
                    "family": {"param": "family"},
                    "birthdate": {"param": "birth_date"},
                },
                "smart_backend": {
                    "client_id": "${EPIC_SANDBOX_CLIENT_ID}",
                    "scope": "system/Patient.read",
                    "token_url": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
                    "kid": "${EPIC_SANDBOX_KID}",
                    "alg": "RS384",
                    "private_key_file": "${EPIC_SANDBOX_PRIVATE_KEY}",
                },
                "field_paths": {
                    "id": "id",
                    "family": "name[use=official].family",
                    "given": "name[use=official].given",
                    "birth_date": "birthDate",
                },
            },
            "identity": {
                "family": {"param": "family"},
                "given": {"param": "given"},
                "birth_date": {"param": "birth_date"},
            },
            "provides": {"family": "family", "birth_date": "birth_date"},
        },
        "clearinghouse": {
            "role": "clearinghouse",
            "environment_id": "x12-inbox-epic-check",
            "verifier": {
                "kind": "x12",
                "options": {
                    "root": "${RECON_X12_INBOX}",
                    "glob": "*.x12",
                    "transaction": "277",
                },
            },
            "identity": {"trace": {"param": "submission_id"}},
            "compare": {
                "family": {"param": "ehr.family"},
                "birth_date": {"param": "ehr.birth_date"},
            },
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(FLOW))
    import os

    from openadapt_flow.reconcile import ReconcileSpec, reconcile

    spec = importlib.util.spec_from_file_location(
        "x12_fixtures", HERE / "x12_fixtures.py"
    )
    fx = importlib.util.module_from_spec(spec)
    sys.modules["x12_fixtures"] = fx
    spec.loader.exec_module(fx)  # type: ignore[union-attr]

    inbox = Path(tempfile.mkdtemp(prefix="epic-x12-"))
    units = [
        ("CLM-EPIC-1", KNOWN["birth_date"], []),
        ("CLM-EPIC-2", "1987-09-21", [["mismatch", "clearinghouse", "REFUTED"]]),
    ]
    for i, (sid, born, _) in enumerate(units, start=1):
        status = fx.ClaimStatus(
            sid,
            f"SYNM-EPIC-{i}",
            "99213",
            "2026-10-01",
            family=KNOWN["family"],
            birth_date=born,
        )
        (inbox / f"{sid}.x12").write_text(
            fx.interchange("277", [fx.status_set([status])], control=str(i))
        )
    os.environ["RECON_X12_INBOX"] = str(inbox)
    reconcile_spec = ReconcileSpec.model_validate(SPEC)
    rows = []
    for sid, _, expected in units:
        result = reconcile(reconcile_spec, {**KNOWN, "submission_id": sid}).to_dict()
        got = sorted([b["kind"], b["leg"], b["outcome"]] for b in result["breaks"])
        rows.append(
            {
                "submission_id": sid,
                "outcome": result["outcome"],
                "expected": expected,
                "got": got,
                "caught": got == expected,
                "result": result,
            }
        )
        print(sid, result["outcome"], got)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "epic_check.json").write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
