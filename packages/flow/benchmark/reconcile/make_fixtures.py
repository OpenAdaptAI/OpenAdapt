"""Write the pre-registered fixture set: ``fixtures.json`` and its sha256.

Each unit is one prior authorization or claim, with synthetic values, the
fault planted in it (or none), and the breaks the reconciler must report:
``[kind, leg, outcome]``. Run once before the measured run and commit both
files; ``run.py`` refuses a fixture set whose sha256 doesn't match.

    python benchmark/reconcile/make_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Synthetic CPT/HCPCS codes the units draw from, and their look-alikes.
CODES = ["97110", "72148", "E0601", "97530", "73721", "G0283", "70553", "95810"]
NEAR = {
    "97110": "97112",
    "72148": "72149",
    "E0601": "E0606",
    "97530": "97535",
    "73721": "73722",
    "G0283": "G0281",
    "70553": "70552",
    "95810": "95811",
    "99213": "99214",
    "99214": "99215",
    "93000": "93005",
    "80053": "80048",
}
CLAIM_CODES = ["99213", "99214", "93000", "80053"]

#: (category, planted fault, count). Order is the run order.
PA_PLAN = [
    ("clean", None, 10),
    ("lookalike", "lookalike", 2),
    ("in_flight", "in_flight", 3),
    ("fault", "drop_clearinghouse", 2),
    ("fault", "drop_payer", 3),
    ("fault", "code_payer", 2),
    ("fault", "code_clearinghouse", 2),
    ("fault", "date_payer", 2),
    ("fault", "member_clearinghouse", 2),
    ("fault", "missed_writeback", 3),
    ("fault", "wrong_writeback", 1),
    ("fault", "duplicate_payer", 2),
    ("fault", "duplicate_clearinghouse", 1),
]
CLAIM_PLAN = [
    ("clean", None, 5),
    ("fault", "drop_clearinghouse", 2),
    ("fault", "code_clearinghouse", 1),
    ("fault", "date_clearinghouse", 1),
    ("fault", "member_clearinghouse", 1),
    ("fault", "duplicate_clearinghouse", 2),
    # Review round 5: the 277 says the claim was rejected, returned, or
    # something that says neither.
    ("fault", "rejected_clearinghouse", 1),
    ("fault", "returned_clearinghouse", 1),
    ("fault", "unknown_status_clearinghouse", 1),
]

R, U = "REFUTED", "UNCERTAIN"

#: What each planted fault must produce. The payer's decision legs depend on
#: the payer having the request, so a drop there leaves them UNCERTAIN.
EXPECTED = {
    None: [],
    "lookalike": [],
    "in_flight": [["not_received", "clearinghouse", U], ["not_received", "payer", U]],
    "drop_clearinghouse": [["not_received", "clearinghouse", R]],
    "drop_payer": [["not_received", "clearinghouse", R], ["not_received", "payer", R]],
    "code_payer": [["mismatch", "payer", R]],
    "code_clearinghouse": [["mismatch", "clearinghouse", R]],
    "date_payer": [["mismatch", "payer", R]],
    "date_clearinghouse": [["mismatch", "clearinghouse", R]],
    "member_clearinghouse": [["mismatch", "clearinghouse", R]],
    "missed_writeback": [["not_written_back", "writeback", R]],
    "wrong_writeback": [["not_written_back", "writeback", R]],
    "duplicate_payer": [["duplicate", "payer", R]],
    "duplicate_clearinghouse": [["duplicate", "clearinghouse", R]],
    "rejected_clearinghouse": [["not_accepted", "clearinghouse", R]],
    "returned_clearinghouse": [["not_accepted", "clearinghouse", R]],
    "unknown_status_clearinghouse": [["not_accepted", "clearinghouse", U]],
}

#: The 277 status (STC01-1 category, STC01-2 code) each claim fault writes.
STATUS = {
    "rejected_clearinghouse": ("A7", "21"),
    "returned_clearinghouse": ("A3", "21"),
    "unknown_status_clearinghouse": ("E0", "0"),
}


def _day(n: int) -> str:
    return f"2026-12-{1 + n % 28:02d}"


def build() -> dict:
    units = []
    n = 0
    for unit, plan in (("prior_authorization", PA_PLAN), ("claim", CLAIM_PLAN)):
        for category, fault, count in plan:
            for _ in range(count):
                n += 1
                codes = CODES if unit == "prior_authorization" else CLAIM_CODES
                code = codes[n % len(codes)]
                units.append(
                    {
                        "n": n,
                        "unit": unit,
                        "category": category,
                        "fault": fault,
                        "submission_id": f"{'PA' if unit == 'prior_authorization' else 'CLM'}"
                        f"-SYN-{n:03d}",
                        "member_id": f"SYNM{n:05d}",
                        "family": f"Synthetic{n:03d}",
                        "service_code": code,
                        "near_code": NEAR[code],
                        "service_date": _day(n),
                        "expected": EXPECTED[fault],
                    }
                )
    return {
        "schema_version": "openadapt.reconcile-fixtures/v1",
        "note": "Synthetic units only. Pre-registered: expected breaks are fixed "
        "before the run. An expected break is caught when the reconciler reports "
        "that kind on that leg with that outcome; any other REFUTED break is false.",
        "evaluated_at_offset_s": 129600,
        "units": units,
    }


def main() -> None:
    body = json.dumps(build(), indent=1, sort_keys=True) + "\n"
    (HERE / "fixtures.json").write_text(body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    (HERE / "fixtures.json.sha256").write_text(f"{digest}  fixtures.json\n")
    print(digest)


if __name__ == "__main__":
    main()
