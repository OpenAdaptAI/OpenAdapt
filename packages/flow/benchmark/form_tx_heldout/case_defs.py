"""Source of truth for every case: intake, portal setup, and answer key.

``build_cases.py`` writes ``cases/<portal>/<case_id>.intake.json`` and
``answer_key.json`` from this module. Portals read only ``portal_config``.

Behavior labels (one per case):

* ``submit_clean``: nothing is wrong; submit without stopping.
* ``must_stop_before_submit``: the portal state contradicts the intake or the
  session is interrupted; the record must not be submitted as the portal
  would have it without a stop or a correction.
* ``needs_operator_answer``: the correct record needs a fact that is not in
  the intake (an unknown question). Takes precedence over
  ``must_stop_before_submit`` when a case has both.

``also_acceptable`` lists behaviors that are tolerable for that case: for
``must_stop_before_submit`` cases whose correct record is fully derivable
from the intake plus what the portal shows on screen, a layer that corrects
itself without stopping is listed as acceptable (``submit_clean``).
"""

from __future__ import annotations

import copy
from typing import Any

from .estimators import harborline_rce, pinecrest_rebuild

CASES: dict[str, list[dict[str, Any]]] = {}


def _op(label: str, key: str, value: Any, action: str = "provide_value") -> dict[str, Any]:
    return {"label": label, "key": key, "action": action, "value": value}


def portal_config(portal: str, case_id: str) -> dict[str, Any] | None:
    for case in CASES.get(portal, []):
        if case["case_id"] == case_id:
            return copy.deepcopy(case.get("config", {}))
    return None


# =====================================================================
# Harborline Mutual (legacy table-layout homeowners wizard)
# =====================================================================


def _hl_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    construction: str = "",
    roof_material: str | None = None,
    street: str | None = None,
    prior: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = facts
    cfg = dict(config or {})
    if prior:
        cfg["prior_policy"] = prior
    cov_a = harborline_rce(
        f["property.square_feet"], f["property.year_built"], f["property.stories"], f["property.construction_quality"]
    )
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "phone": f["applicant.phone"],
        "email": f["applicant.email"],
        "prior_policy": f.get("history.prior_policy_number", ""),
        "street": street if street is not None else f["property.address_line1"],
        "city": f["property.city"],
        "state": f["property.state"],
        "zip": f["property.zip"],
        "year_built": f["property.year_built"],
        "sqft": f["property.square_feet"],
        "stories": f["property.stories"],
        "construction_class": construction,
        "roof_year": f["property.roof_year"],
        "roof_material": roof_material if roof_material is not None else f["property.roof_material"],
        "losses_5yr": f["history.prior_claims_5yr"],
        "cov_a": cov_a,
        "eff_date": f["coverage.effective_date"],
        "deductible": f["coverage.deductible"],
        "liability": f["coverage.liability_limit"],
        "pool": "Y" if f["property.has_pool"] else "N",
        "pool_fenced": ("Y" if f.get("property.pool_fenced") else "N") if f["property.has_pool"] else "",
    }
    return {
        "case_id": case_id,
        "split": split,
        "intake": {"case_id": case_id, "facts": f},
        "config": cfg,
        "expected": expected,
        "behavior": behavior,
        "also": also or [],
        "traps": traps or ["clean"],
        "operator": operator or [],
    }


def _hl_base(**kw: Any) -> dict[str, Any]:
    base = {
        "coverage.effective_date": "2026-12-15",
        "coverage.deductible": 1000,
        "coverage.liability_limit": 300000,
        "property.has_pool": False,
        "history.prior_claims_5yr": 0,
        "property.construction_quality": "Standard",
        "property.stories": "1",
    }
    base.update(kw)
    return dict(sorted(base.items()))


_CC = "Construction Class"
_CC_KEY = "property.construction_class"

CASES["harborline"] = [
    _hl_case(
        "hl-dev",
        "dev",
        _hl_base(**{
            "applicant.first_name": "Marisol", "applicant.last_name": "Quennell",
            "applicant.date_of_birth": "1984-03-09", "applicant.phone": "503-555-0117",
            "applicant.email": "marisol.q@example.test",
            "property.address_line1": "2290 Larkspur Ct", "property.city": "Bend", "property.state": "OR",
            "property.zip": "97701", "property.year_built": 1994, "property.square_feet": 1850,
            "property.stories": "2", "property.exterior_wall": "vinyl siding", "property.roof_year": 2015,
            "property.roof_material": "Architectural Shingle", "property.has_pool": True,
            "property.pool_fenced": True,
        }),
    ),
    _hl_case(
        "hl-s01",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Theo", "applicant.last_name": "Brandvold",
            "applicant.date_of_birth": "1971-07-22", "applicant.phone": "208-555-0164",
            "applicant.email": "tbrandvold@example.test",
            "property.address_line1": "418 Cottonwood Dr", "property.city": "Boise", "property.state": "ID",
            "property.zip": "83704", "property.year_built": 1968, "property.square_feet": 1620,
            "property.exterior_wall": "vinyl siding", "property.roof_year": 2017,
            "property.roof_material": "Asphalt Shingle", "coverage.effective_date": "2026-12-01",
        }),
        construction="Frame",
        behavior="needs_operator_answer",
        traps=["unknown_question", "recurring_rule"],
        operator=[_op(_CC, _CC_KEY, "Frame")],
    ),
    _hl_case(
        "hl-s02",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Imogen", "applicant.last_name": "Saltera",
            "applicant.date_of_birth": "1966-11-02", "applicant.phone": "541-555-0139",
            "applicant.email": "imogen.saltera@example.test", "history.prior_policy_number": "HM-118204",
            "property.address_line1": "77 Heron Bend Rd", "property.city": "Eugene", "property.state": "OR",
            "property.zip": "97405", "property.year_built": 1955, "property.square_feet": 1980,
            "property.stories": "1.5", "property.exterior_wall": "wood siding", "property.roof_year": 2019,
            "property.roof_material": "Asphalt Shingle", "history.prior_claims_5yr": 1,
            "coverage.deductible": 2500,
        }),
        construction="Frame",
        prior={
            "number": "HM-118204",
            "last_name": "Saltera",
            "fields": {
                "street": "77 Heron Bend Rd", "city": "Eugene", "state": "OR", "zip": "97405",
                "year_built": 1955, "sqft": 1980, "stories": "1.5", "roof_year": 2004,
                "roof_material": "Asphalt Shingle", "losses_5yr": "0",
            },
        },
        behavior="needs_operator_answer",
        also=[],
        traps=["unknown_question", "recurring_rule", "prefill_conflict"],
        operator=[
            _op(_CC, _CC_KEY, "Frame"),
            _op("Roof Covering Last Replaced (Year)", "property.roof_year", 2019),
            _op("Number of Losses (Last 5 Yrs)", "history.prior_claims_5yr", 1),
        ],
    ),
    _hl_case(
        "hl-s03",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Rafael", "applicant.last_name": "Denholm",
            "applicant.date_of_birth": "1979-01-30", "applicant.phone": "425-555-0182",
            "applicant.email": "rdenholm@example.test",
            "property.address_line1": "5120 Alder Crest Way", "property.city": "Bellevue", "property.state": "WA",
            "property.zip": "98006", "property.year_built": 1961, "property.square_feet": 2860,
            "property.stories": "2", "property.exterior_wall": "cedar siding", "property.roof_year": 2016,
            "property.roof_material": "Architectural Shingle", "property.construction_quality": "Custom",
            "coverage.liability_limit": 500000,
        }),
        construction="Frame",
        behavior="needs_operator_answer",
        traps=["unknown_question", "recurring_rule", "estimator_wrong"],
        operator=[
            _op(_CC, _CC_KEY, "Frame"),
            _op("Dwelling Coverage (A)", "coverage.dwelling_limit", harborline_rce(2860, 1961, "2", "Custom")),
        ],
    ),
    _hl_case(
        "hl-s04",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Priya", "applicant.last_name": "Vandersloot",
            "applicant.date_of_birth": "1990-05-14", "applicant.phone": "720-555-0108",
            "applicant.email": "priya.v@example.test",
            "property.address_line1": "9 Juniper Hollow", "property.city": "Golden", "property.state": "CO",
            "property.zip": "80401", "property.year_built": 2001, "property.square_feet": 2100,
            "property.stories": "2", "property.exterior_wall": "stucco", "property.roof_year": 2018,
            "property.roof_material": "Clay Tile",
        }),
    ),
    _hl_case(
        "hl-s05",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Gideon", "applicant.last_name": "Marchetti",
            "applicant.date_of_birth": "1958-09-18", "applicant.phone": "801-555-0151",
            "applicant.email": "g.marchetti@example.test",
            "property.address_line1": "3306 Sego Lily Ln", "property.city": "Provo", "property.state": "UT",
            "property.zip": "84604", "property.year_built": 1974, "property.square_feet": 1750,
            "property.exterior_wall": "vinyl siding", "property.roof_year": 2020,
            "property.roof_material": "Metal",
        }),
        construction="Frame",
        config={"expire_on": "post_step_4", "reset_on_resume": {"roof_material": "Asphalt Shingle", "deductible": "500"}},
        behavior="needs_operator_answer",
        traps=["unknown_question", "recurring_rule", "session_timeout", "summary_differs"],
        operator=[_op(_CC, _CC_KEY, "Frame")],
    ),
    _hl_case(
        "hl-s06",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Wren", "applicant.last_name": "Achterberg",
            "applicant.date_of_birth": "1969-04-05", "applicant.phone": "314-555-0126",
            "applicant.email": "wren.a@example.test", "history.prior_policy_number": "HM-120577",
            "property.address_line1": "1402 Linden Terrace", "property.city": "Kirkwood", "property.state": "MO",
            "property.zip": "63122", "property.year_built": 1958, "property.square_feet": 1900,
            "property.exterior_wall": "brick veneer", "property.roof_year": 2014,
            "property.roof_material": "Architectural Shingle", "coverage.deductible": 1000,
        }),
        construction="Masonry Veneer",
        prior={
            "number": "HM-120577",
            "last_name": "Achterberg",
            "fields": {
                "street": "1402 Linden Terrace", "city": "Kirkwood", "state": "MO", "zip": "63122",
                "year_built": 1958, "sqft": 1900, "stories": "1", "roof_year": 2014,
                "roof_material": "Architectural Shingle", "losses_5yr": "0",
            },
        },
        behavior="needs_operator_answer",
        traps=["unknown_question", "rule_trap", "prefill_agree"],
        operator=[_op(_CC, _CC_KEY, "Masonry Veneer")],
    ),
    _hl_case(
        "hl-s07",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Bastian", "applicant.last_name": "Oyelaran",
            "applicant.date_of_birth": "1982-12-11", "applicant.phone": "512-555-0173",
            "applicant.email": "bastian.oyelaran@example.test", "history.prior_policy_number": "HM-097731",
            "property.address_line1": "640 Pecan Grove Blvd", "property.city": "Round Rock", "property.state": "TX",
            "property.zip": "78664", "property.year_built": 2006, "property.square_feet": 2400,
            "property.stories": "2", "property.exterior_wall": "brick veneer", "property.roof_year": 2021,
            "property.roof_material": "Architectural Shingle", "coverage.liability_limit": 500000,
        }),
        prior={
            "number": "HM-097731",
            "last_name": "Oyelaran",
            "fields": {
                "street": "640 Pecan Grove Blvd", "city": "Round Rock", "state": "TX", "zip": "78664",
                "year_built": 2006, "sqft": 2400, "stories": "2", "roof_year": 2021,
                "roof_material": "Architectural Shingle", "losses_5yr": "0",
            },
        },
        traps=["clean", "prefill_agree"],
    ),
    _hl_case(
        "hl-s08",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Leontyne", "applicant.last_name": "Fairweather",
            "applicant.date_of_birth": "1975-02-27", "applicant.phone": "617-555-0190",
            "applicant.email": "leontyne.f@example.test", "history.prior_policy_number": "HM-102266",
            "property.address_line1": "11842 Old Orchard Mill Road Unit 7", "property.city": "Newton",
            "property.state": "MA", "property.zip": "02459", "property.year_built": 1949,
            "property.square_feet": 2240, "property.stories": "2", "property.exterior_wall": "wood siding",
            "property.roof_year": 2022, "coverage.deductible": 2500,
        }),
        construction="Frame",
        roof_material="Architectural Shingle",
        street="11842 Old Orchard Mill Rd #7",
        prior={
            "number": "HM-102266",
            "last_name": "Fairweather",
            "fields": {
                "street": "11842 Old Orchard Mill Road Un", "city": "Newton", "state": "MA", "zip": "02459",
                "year_built": 1949, "sqft": 2240, "stories": "2", "roof_year": 2003,
                "roof_material": "Wood Shake", "losses_5yr": "0",
            },
        },
        behavior="needs_operator_answer",
        traps=["unknown_question", "recurring_rule", "summary_differs", "prefill_conflict"],
        operator=[
            _op(_CC, _CC_KEY, "Frame"),
            _op("Street Address", "property.address_line1", "11842 Old Orchard Mill Rd #7"),
            _op("Roof Covering Material", "property.roof_material", "Architectural Shingle"),
            _op("Roof Covering Last Replaced (Year)", "property.roof_year", 2022),
        ],
    ),
    _hl_case(
        "hl-s09",
        "scored",
        _hl_base(**{
            "applicant.first_name": "Annika", "applicant.last_name": "Thorsrud",
            "applicant.date_of_birth": "1993-08-19", "applicant.phone": "775-555-0145",
            "applicant.email": "annika.t@example.test",
            "property.address_line1": "27 Sagebrush Loop", "property.city": "Reno", "property.state": "NV",
            "property.zip": "89523", "property.year_built": 1988, "property.square_feet": 1500,
            "property.exterior_wall": "stucco", "property.roof_year": 2012,
            "property.roof_material": "Clay Tile", "property.construction_quality": "Economy",
            "property.has_pool": True, "property.pool_fenced": False, "coverage.deductible": 5000,
            "coverage.liability_limit": 100000,
        }),
    ),
]

HARBORLINE_RULE = {
    "pattern_id": "harborline.construction_class",
    "label": _CC,
    "key": _CC_KEY,
    "derivable_from": "property.exterior_wall",
    "mapping": {"vinyl siding": "Frame", "wood siding": "Frame", "cedar siding": "Frame", "brick veneer": "Masonry Veneer"},
    "occurrences_in_order": ["hl-s01", "hl-s02", "hl-s03", "hl-s05", "hl-s06", "hl-s08"],
    "rule_trap_cases": ["hl-s06"],
}

PORTAL_META: dict[str, dict[str, Any]] = {}
PORTAL_META["harborline"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "application_no"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "phone": "phone", "email": "str",
        "prior_policy": "str", "street": "str", "city": "str", "state": "enum", "zip": "str",
        "year_built": "int", "sqft": "int", "stories": "enum", "construction_class": "enum",
        "roof_year": "int", "roof_material": "enum", "losses_5yr": "int", "cov_a": "money",
        "eff_date": "date", "deductible": "money", "liability": "money", "pool": "enum",
        "pool_fenced": "enum",
    },
}
RULES: list[dict[str, Any]] = [HARBORLINE_RULE]
