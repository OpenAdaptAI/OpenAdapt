"""Shared pieces of the API-tier spike: referrals, fixture, browser, record check.

The eight referrals are the made-up patients of the October referral timing
benchmark (``benchmark/openemr_referral_timing``): seven new patients and r6,
a possible duplicate of the seeded chart Rosa Delgado 1958-03-27 whose correct
outcome is no new chart. Phones use 555-01xx and emails ``example.invalid``.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

FLOW = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

_MA = dict(city="EXAMPLETON", state="MA", state_label="Massachusetts", postal_code="02139")


@dataclass(frozen=True)
class Referral:
    key: str
    kwargs: dict[str, str]
    mismatch: bool = False


REFERRALS: list[Referral] = [
    Referral("r1-nadia-brennan", dict(title="Ms.", fname="Nadia", lname="Brennan", dob="1971-04-19", sex="Female",
                                      street="27 WILLOW BEND ROAD", phone_home="555-0121",
                                      email="nadia.brennan@example.invalid", **_MA)),
    Referral("r2-marcus-feld", dict(title="Mr.", fname="Marcus", lname="Feld", dob="1958-09-02", sex="Male",
                                    street="114 QUARRY HILL DRIVE", phone_home="555-0124",
                                    email="marcus.feld@example.invalid", **_MA)),
    Referral("r3-ines-carvalho", dict(title="Mrs.", fname="Ines", lname="Carvalho", dob="1949-12-11", sex="Female",
                                      street="9 LANTERN COURT", phone_home="555-0131",
                                      email="ines.carvalho@example.invalid", **_MA)),
    Referral("r4-theo-lindqvist", dict(title="Mr.", fname="Theo", lname="Lindqvist", dob="1983-07-26", sex="Male",
                                       street="63 FERRY STREET", phone_home="555-0136",
                                       email="theo.lindqvist@example.invalid", **_MA)),
    Referral("r5-grace-adeyemi", dict(title="Ms.", fname="Grace", lname="Adeyemi", dob="1990-02-08", sex="Female",
                                      street="402 CEDAR ROW", phone_home="555-0145",
                                      email="grace.adeyemi@example.invalid", **_MA)),
    Referral("r6-rosa-delgado", dict(title="Mrs.", fname="Rosa", lname="Delgado", dob="1958-03-22", sex="Female",
                                     street="7 MILL ROAD", phone_home="555-0188",
                                     email="rosa.delgado@example.invalid", **_MA), mismatch=True),
    Referral("r7-arlo-whitcombe", dict(title="Mr.", fname="Arlo", lname="Whitcombe", dob="1966-05-30", sex="Male",
                                       street="88 BEACON TERRACE", phone_home="555-0152",
                                       email="arlo.whitcombe@example.invalid", **_MA)),
    Referral("r8-priya-raman", dict(title="Ms.", fname="Priya", lname="Raman", dob="1975-10-14", sex="Female",
                                    street="15 STATION AVENUE", phone_home="555-0167",
                                    email="priya.raman@example.invalid", **_MA)),
]
EXISTING_ROSA = dict(title="Mrs.", fname="Rosa", lname="Delgado", dob="1958-03-27", sex="Female",
                     street="311 BIRCH COURT", phone_home="555-0119",
                     email="r.delgado.prior@example.invalid", **_MA)


def load_demo() -> Any:
    """The OpenEMR reference harness (sign-in, form frame, Playwright backend)."""
    name = "openemr_local_demo"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FLOW / "scripts/openemr_local_demo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def spec_for(ref: Referral) -> Any:
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec

    return SyntheticPatientSpec(**ref.kwargs)


def q(v: str) -> str:
    assert "'" not in v and "\\" not in v
    return "'" + v + "'"


SQL_FIELDS = ("id", "pid", "title", "fname", "lname", "DOB", "sex", "street", "city", "state",
              "postal_code", "phone_home", "email", "country_code")


def sql_patients(f: SpikeFixture, where: str = "1=1") -> list[dict[str, str]]:
    out = f._db("SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data WHERE " + where + " ORDER BY id").decode()
    return [{k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))} for line in out.splitlines()]


def reset_and_seed(f: SpikeFixture) -> dict[str, Any]:
    """Restore the baseline and seed the existing chart r6 must not duplicate."""
    t = time.time()
    digest = f.reset()
    seed = f.token_session("actor").post(
        f.api_base_url + "/apis/default/api/patient",
        json=dict(spec_for(Referral("rosa", EXISTING_ROSA)).fields),
    ).status_code
    return {"baseline": digest, "seed_status": seed, "reset_seed_s": round(time.time() - t, 2)}


def record_check(f: SpikeFixture, ref: Referral, before_ids: set[str]) -> dict[str, Any]:
    """The test setup's own read-back (direct SQL), never the arm under test.

    Same rule as the referral timing benchmark: a new-patient referral is
    correct iff exactly one new chart exists and it matches the referral on
    all 12 fields; r6 is correct iff no new chart exists.
    """
    spec = spec_for(ref)
    after = sql_patients(f)
    new_rows = [r for r in after if r["id"] not in before_ids]
    target = sql_patients(f, f"lname={q(spec.lname)} AND email={q(spec.email)}")
    row = new_rows[0] if len(new_rows) == 1 else None
    fields = [{"field": k, "want": v, "got": None if row is None else row.get(k),
               "match": row is not None and row.get(k) == v} for k, v in spec.fields.items()]
    nmatch = sum(x["match"] for x in fields)
    if ref.mismatch:
        correct = len(new_rows) == 0
    else:
        correct = len(new_rows) == 1 and len(target) == 1 and nmatch == len(fields)
    return {"new_charts": len(new_rows), "readback": f"{nmatch}/{len(fields)}", "correct": correct,
            "field_check": fields}
