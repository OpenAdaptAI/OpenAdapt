"""Write the intake files and ``answers.json`` from ``case_defs``.

Run from ``packages/flow``: ``python -m benchmark.form_tx_heldout.build_cases``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .case_defs import CASES, PORTAL_META, RULES, intake_vocabulary
from .common import CREDENTIALS

HERE = Path(__file__).resolve().parent


def operator_script(case: dict, portal: str) -> dict:
    script: dict = {}
    for op in case["operator"]:
        entry = {"action": op["action"], "value": op["value"]}
        script[op["label"]] = entry
        script[op["key"]] = entry
    creds = CREDENTIALS[portal]
    script["session_login"] = {"username": creds["username"], "password": creds["password"]}
    return script


def build() -> dict:
    key: dict = {
        "version": 1,
        "behavior_labels": ["submit_clean", "must_stop_before_submit", "needs_operator_answer"],
        "portals": {},
        "cases": {},
        "rule_learning": RULES,
    }
    for portal, cases in CASES.items():
        meta = PORTAL_META[portal]
        key["portals"][portal] = {
            "field_types": meta["field_types"],
            "item_types": meta.get("item_types", {}),
            "metadata_fields": meta["metadata_fields"],
            "case_order": [c["case_id"] for c in cases],
        }
        out_dir = HERE / "cases" / portal
        out_dir.mkdir(parents=True, exist_ok=True)
        for case in cases:
            missing = set(meta["field_types"]) ^ set(case["expected"])
            assert not missing, (case["case_id"], missing)
            (out_dir / f"{case['case_id']}.intake.json").write_text(
                json.dumps(case["intake"], indent=2, sort_keys=False) + "\n"
            )
            key["cases"][case["case_id"]] = {
                "portal": portal,
                "split": case["split"],
                "expected_behavior": case["behavior"],
                "also_acceptable": case["also"],
                "traps": case["traps"],
                "expected_record": case["expected"],
                "operator_script": operator_script(case, portal),
            }
    (HERE / "answers.json").write_text(json.dumps(key, indent=2) + "\n")
    vocab = {p: intake_vocabulary(p) for p in CASES}
    (HERE / "intake_vocabulary.json").write_text(json.dumps(vocab, indent=2) + "\n")
    return key


if __name__ == "__main__":
    k = build()
    print(f"wrote {len(k['cases'])} cases")
