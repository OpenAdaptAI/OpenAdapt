"""Referral intake cases against a real, synthetic OpenEMR receiver.

The sender is a synthetic referring clinic: a SQLite outbox that creates
referrals with generated, synthetic patients. Each referral goes to the
receiver (a second, separate OpenEMR in Docker, see README.md) through a
delivery channel that plays the receiving clinic's intake. The channel
writes through OpenEMR's standard REST API (``/apis/default/api/patient``)
and can, on purpose, get something wrong.

OpenAdapt reads the receiver back through its FHIR API, with a separate
read-only token, by the profile in
``tests/fixtures/handoffs/fhir-patient-intake.yaml``. It never reads the
delivery call's own answer.

Cases (each repeated ``repeat`` times with a fresh patient):

| Case | Delivery | Expected |
|---|---|---|
| ``intended`` | as sent | ``recorded_as_intended`` |
| ``benign_format`` | names in capitals with extra spaces | ``recorded_as_intended`` (no false alarm) |
| ``wrong_date`` | birth date one day later | ``recorded_differently`` [birth_date] |
| ``swapped_date`` | day and month swapped | ``recorded_differently`` [birth_date] |
| ``wrong_patient_field`` | another family name | ``recorded_differently`` [family_name] |
| ``wrong_sex`` | the other sex | ``recorded_differently`` [sex] |
| ``never_arrived`` | dropped | ``sent``, reason ``not_found``, follow-up due |
| ``duplicate`` | delivered twice | ``sent``, reason ``ambiguous_key`` |
| ``unreachable`` | as sent; read at a closed port | ``sent``, reason ``receiver_unreachable`` |

Synthetic data only. Run inside the lab (``remote-run.sh``), with the
``HANDOFF_RECEIVER_*`` variables from the receiver's setup.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.handoffs import intake
from openadapt_flow.handoffs.ledger import Handoff, HandoffLedger
from openadapt_flow.handoffs.line import to_line
from openadapt_flow.handoffs.readers import FhirSearchProfile, FhirSearchReader

PROFILE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "handoffs"
    / "fhir-patient-intake.yaml"
)
GIVEN = ["Avery", "Rowan", "Sage", "Quinn", "Emery", "Harper", "Reese", "Tatum"]
FAMILY = ["Quill", "Marlow", "Thorne", "Vance", "Ashby", "Corwin", "Hollis", "Pryor"]

EXPECTED: dict[str, tuple[str, Optional[str], list[str]]] = {
    "intended": ("recorded_as_intended", None, []),
    "benign_format": ("recorded_as_intended", None, []),
    "wrong_date": ("recorded_differently", None, ["birth_date"]),
    "swapped_date": ("recorded_differently", None, ["birth_date"]),
    "wrong_patient_field": ("recorded_differently", None, ["family_name"]),
    "wrong_sex": ("recorded_differently", None, ["sex"]),
    "never_arrived": ("sent", "not_found", []),
    "duplicate": ("sent", "ambiguous_key", []),
    "unreachable": ("sent", "receiver_unreachable", []),
}


class Sender:
    """The synthetic referring clinic: an outbox of referrals."""

    def __init__(self, path: Path, seed: int) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "create table if not exists referral (id text primary key, given text,"
            " family text, birth_date text, sex text, reason text)"
        )
        self.rng = random.Random(seed)

    def create(self, *, unswappable: bool = False) -> dict[str, str]:
        day = self.rng.randint(13, 28) if unswappable else self.rng.randint(1, 12)
        month = self.rng.randint(1, 12)
        if not unswappable:
            while month == day:
                month = self.rng.randint(1, 12)
        row = {
            "id": f"REF-{secrets.token_hex(6).upper()}",
            "given": self.rng.choice(GIVEN),
            "family": self.rng.choice(FAMILY) + secrets.token_hex(2),
            "birth_date": date(self.rng.randint(1940, 2015), month, day).isoformat(),
            "sex": self.rng.choice(["female", "male"]),
            "reason": "synthetic referral",
        }
        self.db.execute(
            "insert into referral values (:id, :given, :family, :birth_date, :sex,"
            " :reason)",
            row,
        )
        self.db.commit()
        return row


def _post_patient(base: str, token: str, row: dict[str, str]) -> None:
    import httpx

    body = {
        "fname": row["given"],
        "lname": row["family"],
        "DOB": row["birth_date"],
        "sex": row["sex"].capitalize() if row["sex"] in ("female", "male") else row["sex"],
        "pubpid": row["id"],
        "street": "",
        "postal_code": "",
        "city": "",
        "state": "",
        "country_code": "",
        "phone_contact": "",
        "race": "",
        "ethnicity": "",
    }
    with httpx.Client(verify=_verify(), timeout=30) as client:
        response = client.post(
            f"{base}/patient", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    if response.status_code >= 300 or response.json().get("validationErrors"):
        raise RuntimeError(f"delivery failed: HTTP {response.status_code}")


def _verify() -> bool:
    return os.environ.get("HANDOFF_RECEIVER_VERIFY_TLS", "1") != "0"


def deliver(case: str, row: dict[str, str], base: str, token: str) -> None:
    """The receiving clinic's intake, getting it wrong on purpose."""

    sent = dict(row)
    born = date.fromisoformat(row["birth_date"])
    if case == "never_arrived":
        return
    if case == "benign_format":
        sent["given"] = f"  {row['given'].upper()} "
        sent["family"] = row["family"].upper()
    elif case == "wrong_date":
        sent["birth_date"] = (born + timedelta(days=1)).isoformat()
    elif case == "swapped_date":
        sent["birth_date"] = date(born.year, born.day, born.month).isoformat()
    elif case == "wrong_patient_field":
        sent["family"] = row["family"] + "s"
    elif case == "wrong_sex":
        sent["sex"] = "male" if row["sex"] == "female" else "female"
    _post_patient(base, token, sent)
    if case == "duplicate":
        _post_patient(base, token, sent)


@dataclass
class CaseResult:
    case: str
    referral: str
    state: str
    reason: Optional[str]
    differing: list[str]
    line: dict[str, Any]
    ok: bool
    seconds: float


def run_cases(
    work: Path, *, repeat: int = 5, seed: int = 20261121, cases: Optional[list[str]] = None
) -> list[CaseResult]:
    api = os.environ["HANDOFF_RECEIVER_API_BASE"]
    write_token = os.environ["HANDOFF_RECEIVER_WRITE_TOKEN"]
    fhir = os.environ["HANDOFF_RECEIVER_FHIR_BASE"]
    read_token = os.environ["HANDOFF_RECEIVER_READ_TOKEN"]
    profile = FhirSearchProfile.load(PROFILE)
    reader = FhirSearchReader(profile, fhir, token=read_token, verify_tls=_verify())
    closed = FhirSearchReader(
        profile, "https://127.0.0.1:9789/apis/default/fhir", token=read_token,
        verify_tls=False, timeout=5,
    )
    sender = Sender(work / "sender.sqlite", seed)
    ledger = HandoffLedger(work / "handoffs")
    results: list[CaseResult] = []
    for name in cases or list(EXPECTED):
        for _ in range(repeat):
            row = sender.create(unswappable=False)
            started = time.monotonic()
            handoff: Handoff = ledger.send(
                row["id"],
                {
                    "given_name": (row["given"], "text"),
                    "family_name": (row["family"], "text"),
                    "birth_date": (row["birth_date"], "date"),
                    "sex": (row["sex"], "code"),
                },
                counterparty=profile.counterparty,
                system_kind=profile.system_kind,
                follow_up="referrals desk",
                follow_up_hours=24,
                step=0,
            )
            deliver(name, row, api, write_token)
            used = closed if name == "unreachable" else reader
            after = intake.check(ledger, handoff.id, row["id"], used)
            last = after.last
            assert last is not None
            want_state, want_reason, want_fields = EXPECTED[name]
            got = (after.state, last.reason, list(last.differing_fields))
            results.append(
                CaseResult(
                    case=name,
                    referral=row["id"],
                    state=after.state,
                    reason=last.reason,
                    differing=list(last.differing_fields),
                    line=to_line(after).line,
                    ok=got == (want_state, want_reason, want_fields),
                    seconds=round(time.monotonic() - started, 3),
                )
            )
    return results


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    by_case: dict[str, dict[str, Any]] = {}
    for item in results:
        entry = by_case.setdefault(
            item.case,
            {"expected": list(EXPECTED[item.case]), "runs": 0, "as_expected": 0, "got": {}},
        )
        entry["runs"] += 1
        entry["as_expected"] += item.ok
        label = f"{item.state}|{item.reason}|{','.join(item.differing)}"
        entry["got"][label] = entry["got"].get(label, 0) + 1
    return {
        "receiver": "OpenEMR 7.0.3 (docker, synthetic, second instance)",
        "read": "FHIR R4 Patient search, read-only token",
        "delivery": "OpenEMR standard REST API (write token)",
        "cases": by_case,
        "total_runs": len(results),
        "as_expected": sum(r.ok for r in results),
    }


if __name__ == "__main__":  # pragma: no cover - lab entry point
    import argparse
    import tempfile

    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        found = run_cases(Path(tmp), repeat=args.repeat)
    summary = summarize(found)
    text = json.dumps(summary, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
