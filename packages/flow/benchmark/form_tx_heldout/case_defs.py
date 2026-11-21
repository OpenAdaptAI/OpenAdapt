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
        traps=["unknown_question", "recurring_rule", "rule_trap", "prefill_agree"],
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


# =====================================================================
# Cobalt Ridge Insurance (single-page auto quote, ARIA widgets)
# =====================================================================


def _cr_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    drivers: list[dict[str, str]] | None = None,
    rideshare: str = "",
    lienholder: str | None = None,
    prior: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = dict(sorted(facts.items()))
    cfg = dict(config or {})
    if prior:
        cfg["prior_policy"] = prior
    if drivers is None:
        drivers = []
        if "driver2.first_name" in f:
            drivers = [{
                "first_name": f["driver2.first_name"], "last_name": f["driver2.last_name"],
                "dob": f["driver2.date_of_birth"], "relationship": f["driver2.relationship"],
            }]
    use = f["vehicle.primary_use"].title()
    own = f["vehicle.ownership"].title()
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "email": f["applicant.email"],
        "license_state": f["applicant.license_state"],
        "marital_status": f["applicant.marital_status"],
        "additional_drivers": drivers,
        "vehicle_year": f["vehicle.year"],
        "make": f["vehicle.make"],
        "model": f["vehicle.model"],
        "use": use,
        "commute_miles": f["vehicle.commute_miles_one_way"] if use == "Commute" else "",
        "rideshare": rideshare,
        "annual_miles": f["vehicle.annual_miles"],
        "garaging_zip": f["garaging.zip"],
        "garaging_city": f["garaging.city"],
        "garaging_state": f["garaging.state"],
        "ownership": own,
        "lienholder": (lienholder if lienholder is not None else f.get("vehicle.lienholder", "")) if own != "Own" else "",
        "bi_limits": f["coverage.bi_limits"],
        "collision_ded": str(f["coverage.collision_deductible"]),
        "comp_ded": str(f["coverage.comprehensive_deductible"]),
    }
    return {
        "case_id": case_id, "split": split, "intake": {"case_id": case_id, "facts": f}, "config": cfg,
        "expected": expected, "behavior": behavior, "also": also or [], "traps": traps or ["clean"],
        "operator": operator or [],
    }


CASES["cobalt"] = [
    _cr_case("cr-dev", "dev", {
        "applicant.first_name": "Hollis", "applicant.last_name": "Varga", "applicant.date_of_birth": "1988-06-12",
        "applicant.email": "hollis.varga@example.test", "applicant.license_state": "OR",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2021, "vehicle.make": "Marlowe", "vehicle.model": "Brook", "vehicle.primary_use": "pleasure",
        "vehicle.annual_miles": 9000, "garaging.zip": "97520", "garaging.city": "Cedar Hollow", "garaging.state": "OR",
        "vehicle.ownership": "own", "coverage.bi_limits": "100/300", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 250,
    }),
    _cr_case("cr-s01", "scored", {
        "applicant.first_name": "Ines", "applicant.last_name": "Calloway", "applicant.date_of_birth": "1995-01-17",
        "applicant.email": "ines.calloway@example.test", "applicant.license_state": "WA",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2019, "vehicle.make": "Norvik", "vehicle.model": "Aster", "vehicle.primary_use": "commute",
        "vehicle.commute_miles_one_way": 14, "vehicle.annual_miles": 12000, "garaging.zip": "98052",
        "garaging.city": "Lakemont", "garaging.state": "WA", "vehicle.ownership": "finance",
        "vehicle.lienholder": "Northgate Credit Union", "coverage.bi_limits": "50/100",
        "coverage.collision_deductible": 1000, "coverage.comprehensive_deductible": 500,
    }),
    _cr_case("cr-s02", "scored", {
        "applicant.first_name": "Callum", "applicant.last_name": "Ashdown", "applicant.date_of_birth": "1977-03-04",
        "applicant.email": "callum.ashdown@example.test", "applicant.license_state": "OR",
        "applicant.marital_status": "Divorced", "household.driver_count": 1,
        "vehicle.year": 2017, "vehicle.make": "Halden", "vehicle.model": "Skerry", "vehicle.primary_use": "pleasure",
        "vehicle.annual_miles": 7500, "garaging.zip": "97520", "garaging.city": "Cedar Hollow", "garaging.state": "OR",
        "vehicle.ownership": "own", "coverage.bi_limits": "100/300", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 500,
    },
        prior={"number": "CR-5520187", "email": "callum.ashdown@example.test", "dob": "1977-03-04",
               "drivers": [{"first_name": "Corin", "last_name": "Ashdown", "dob": "1979-08-15", "relationship": "Spouse"}]},
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict"],
        operator=[_op("Driver 2", "driver2", None, action="remove")],
    ),
    _cr_case("cr-s03", "scored", {
        "applicant.first_name": "Dario", "applicant.last_name": "Pellegrin", "applicant.date_of_birth": "1983-09-25",
        "applicant.email": "dario.pellegrin@example.test", "applicant.license_state": "AZ",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2022, "vehicle.make": "Kestrel", "vehicle.model": "Corsa", "vehicle.primary_use": "pleasure",
        "vehicle.annual_miles": 10000, "garaging.zip": "85142", "garaging.city": "Copper Mesa", "garaging.state": "AZ",
        "vehicle.ownership": "own", "coverage.bi_limits": "100/300", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 500,
    },
        behavior="submit_clean", also=["must_stop_before_submit"], traps=["typeahead_ambiguous"],
    ),
    _cr_case("cr-s04", "scored", {
        "applicant.first_name": "Maren", "applicant.last_name": "Lindqvist", "applicant.date_of_birth": "1986-05-30",
        "applicant.email": "maren.lindqvist@example.test", "applicant.license_state": "ID",
        "applicant.marital_status": "Married", "household.driver_count": 2,
        "driver2.first_name": "Tobias", "driver2.last_name": "Lindqvist", "driver2.date_of_birth": "1984-02-28",
        "driver2.relationship": "Spouse",
        "vehicle.year": 2020, "vehicle.make": "Solenne", "vehicle.model": "Lune", "vehicle.primary_use": "commute",
        "vehicle.commute_miles_one_way": 8, "vehicle.annual_miles": 11000, "garaging.zip": "83814",
        "garaging.city": "Brindle Falls", "garaging.state": "ID", "vehicle.ownership": "own",
        "coverage.bi_limits": "250/500", "coverage.collision_deductible": 500, "coverage.comprehensive_deductible": 500,
    },
        prior={"number": "CR-5498120", "email": "maren.lindqvist@example.test", "dob": "1986-05-30",
               "drivers": [{"first_name": "Tobias", "last_name": "Lindqvist", "dob": "1984-02-28", "relationship": "Spouse"}]},
        traps=["clean", "prefill_agree"],
    ),
    _cr_case("cr-s05", "scored", {
        "applicant.first_name": "Yusuf", "applicant.last_name": "Brennan", "applicant.date_of_birth": "1991-12-03",
        "applicant.email": "yusuf.brennan@example.test", "applicant.license_state": "NV",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2018, "vehicle.make": "Marlowe", "vehicle.model": "Avenue LX", "vehicle.primary_use": "commute",
        "vehicle.commute_miles_one_way": 22, "vehicle.annual_miles": 15000, "garaging.zip": "89701",
        "garaging.city": "Juniper Flats", "garaging.state": "NV", "vehicle.ownership": "own",
        "coverage.bi_limits": "100/300", "coverage.collision_deductible": 1000, "coverage.comprehensive_deductible": 500,
    },
        config={"expire_on": "put_step_4", "reset_on_resume": {"use": "Pleasure", "commute_miles": ""}},
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["session_timeout", "summary_differs"],
    ),
    _cr_case("cr-s06", "scored", {
        "applicant.first_name": "Thandiwe", "applicant.last_name": "Corrigan", "applicant.date_of_birth": "1980-07-08",
        "applicant.email": "thandiwe.corrigan@example.test", "applicant.license_state": "TN",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2023, "vehicle.make": "Halden", "vehicle.model": "Fjord Touring", "vehicle.primary_use": "business",
        "vehicle.annual_miles": 18000, "garaging.zip": "37062", "garaging.city": "Fairview", "garaging.state": "TN",
        "vehicle.ownership": "own", "coverage.bi_limits": "100/300", "coverage.collision_deductible": 1000,
        "coverage.comprehensive_deductible": 1000,
    },
        rideshare="No",
        behavior="needs_operator_answer", traps=["typeahead_ambiguous", "unknown_question"],
        operator=[_op("Used for rideshare or delivery?", "vehicle.rideshare_or_delivery", "No")],
    ),
    _cr_case("cr-s07", "scored", {
        "applicant.first_name": "Esme", "applicant.last_name": "Halloran", "applicant.date_of_birth": "1983-04-21",
        "applicant.email": "esme.halloran@example.test", "applicant.license_state": "WA",
        "applicant.marital_status": "Married", "household.driver_count": 2,
        "driver2.first_name": "Pavel", "driver2.last_name": "Halloran", "driver2.date_of_birth": "1981-10-09",
        "driver2.relationship": "Spouse",
        "vehicle.year": 2016, "vehicle.make": "Kestrel", "vehicle.model": "Ridgeback", "vehicle.primary_use": "pleasure",
        "vehicle.annual_miles": 6000, "garaging.zip": "98368", "garaging.city": "Port Alder", "garaging.state": "WA",
        "vehicle.ownership": "own", "coverage.bi_limits": "50/100", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 250,
    }),
    _cr_case("cr-s08", "scored", {
        "applicant.first_name": "Arlo", "applicant.last_name": "Fenwick", "applicant.date_of_birth": "1998-02-14",
        "applicant.email": "arlo.fenwick@example.test", "applicant.license_state": "IL",
        "applicant.marital_status": "Single", "household.driver_count": 1,
        "vehicle.year": 2025, "vehicle.make": "Norvik", "vehicle.model": "Polar Trail", "vehicle.primary_use": "pleasure",
        "vehicle.annual_miles": 8000, "garaging.zip": "62704", "garaging.city": "Springfield", "garaging.state": "IL",
        "vehicle.ownership": "lease", "coverage.bi_limits": "100/300", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 500,
    },
        lienholder="Prairie Lane Auto Leasing",
        behavior="needs_operator_answer", traps=["unknown_question", "typeahead_ambiguous"],
        operator=[_op("Lienholder or lessor name", "vehicle.lienholder", "Prairie Lane Auto Leasing")],
    ),
]


# =====================================================================
# Pinecrest Assurance (iframe app, re-render on change, modal estimator)
# =====================================================================

_PC_CONSTRUCTION = {"wood frame": "Wood frame", "brick / masonry": "Brick / masonry", "steel / concrete": "Steel / concrete", "log": "Log"}
_PC_ROOF = {"asphalt shingle": "Asphalt shingle", "metal": "Metal", "tile": "Tile", "slate": "Slate"}
_PC_HEAT = {"gas furnace": "Gas furnace", "electric baseboard": "Electric baseboard", "heat pump": "Heat pump",
            "oil boiler": "Oil boiler", "wood stove": "Wood stove"}
_PC_FINISH = {"builder grade": "Builder grade", "semi-custom": "Semi-custom", "custom": "Custom", "luxury": "Luxury"}


def _pc_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    stove: str = "",
    prior: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = dict(sorted(facts.items()))
    cfg = dict(config or {})
    if prior:
        cfg["prior_policy"] = prior
    construction = _PC_CONSTRUCTION[f["property.construction_type"]]
    expected = {
        "street": f["property.address_line1"],
        "city": f["property.city"],
        "state": f["property.state"],
        "zip": f["property.zip"],
        "year_built": f["property.year_built"],
        "living_area_sqft": f["property.square_feet"],
        "construction": construction,
        "stories": f["property.stories"],
        "roof_year": f["property.roof_year"],
        "roof_material": _PC_ROOF[f["property.roof_material"]],
        "heating": _PC_HEAT[f["property.heating_type"]],
        "stove_installed": stove,
        "prior_claims": f["history.prior_claims_5yr"],
        "burglar_alarm": f["property.burglar_alarm"],
        "dwelling_limit": pinecrest_rebuild(
            f["property.square_feet"], f["property.year_built"], construction,
            _PC_FINISH[f["property.finish_level"]],
        ),
        "deductible": str(f["coverage.deductible"]),
        "effective_date": f["coverage.effective_date"],
    }
    return {
        "case_id": case_id, "split": split, "intake": {"case_id": case_id, "facts": f}, "config": cfg,
        "expected": expected, "behavior": behavior, "also": also or [], "traps": traps or ["clean"],
        "operator": operator or [],
    }


def _pc_prior(number: str, facts: dict[str, Any], **override: Any) -> dict[str, Any]:
    fields = {
        "street": facts["property.address_line1"], "city": facts["property.city"], "state": facts["property.state"],
        "zip": facts["property.zip"], "year_built": str(facts["property.year_built"]),
        "area": str(facts["property.square_feet"]), "area_unit": "sqft",
        "construction": _PC_CONSTRUCTION[facts["property.construction_type"]], "stories": facts["property.stories"],
        "roof_year": str(facts["property.roof_year"]), "roof_material": _PC_ROOF[facts["property.roof_material"]],
        "heating": _PC_HEAT[facts["property.heating_type"]], "prior_claims": str(facts["history.prior_claims_5yr"]),
        "burglar_alarm": facts["property.burglar_alarm"],
    }
    fields.update(override)
    return {"number": number, "fields": fields}


def _pc(**kw: Any) -> dict[str, Any]:
    base = {"coverage.deductible": 1000, "coverage.effective_date": "2027-01-01", "history.prior_claims_5yr": 0,
            "property.burglar_alarm": False, "property.stories": "1"}
    base.update({k.replace("__", "."): v for k, v in kw.items()})
    return base


_PC03 = _pc(**{
    "property.address_line1": "238 Fernhollow Way", "property.city": "Hazel Bluff", "property.state": "OR",
    "property.zip": "97814", "property.year_built": 1979, "property.square_feet": 2150,
    "property.construction_type": "wood frame", "property.roof_year": 2018, "property.roof_material": "asphalt shingle",
    "property.heating_type": "gas furnace", "property.finish_level": "builder grade",
})
_PC04 = _pc(**{
    "property.address_line1": "17 Larch Hollow Ct", "property.city": "Bayard Crossing", "property.state": "NC",
    "property.zip": "27510", "property.year_built": 2003, "property.square_feet": 2650,
    "property.construction_type": "wood frame", "property.stories": "2", "property.roof_year": 2019,
    "property.roof_material": "asphalt shingle", "property.heating_type": "gas furnace",
    "history.prior_claims_5yr": 1, "property.burglar_alarm": True, "property.finish_level": "semi-custom",
})
_PC08 = _pc(**{
    "property.address_line1": "77 Osprey Landing", "property.city": "Saltmarsh", "property.state": "MA",
    "property.zip": "02536", "property.year_built": 1971, "property.square_feet": 1980,
    "property.construction_type": "wood frame", "property.stories": "2", "property.roof_year": 2017,
    "property.roof_material": "asphalt shingle", "property.heating_type": "oil boiler",
    "property.finish_level": "semi-custom",
})

CASES["pinecrest"] = [
    _pc_case("pc-dev", "dev", _pc(**{
        "property.address_line1": "61 Gullwing Rd", "property.city": "Quarry Point", "property.state": "WA",
        "property.zip": "98221", "property.year_built": 1997, "property.square_feet": 1900,
        "property.construction_type": "wood frame", "property.stories": "2", "property.roof_year": 2014,
        "property.roof_material": "asphalt shingle", "property.heating_type": "gas furnace",
        "property.burglar_alarm": True, "property.finish_level": "semi-custom",
    })),
    _pc_case("pc-s01", "scored", _pc(**{
        "property.address_line1": "9 Brackish Cove Ln", "property.city": "Saltmarsh", "property.state": "MA",
        "property.zip": "02536", "property.year_built": 1985, "property.square_feet": 2300,
        "property.construction_type": "brick / masonry", "property.roof_year": 2016, "property.roof_material": "slate",
        "property.heating_type": "heat pump", "property.finish_level": "semi-custom",
        "coverage.deductible": 2500, "coverage.effective_date": "2026-12-20",
    })),
    _pc_case("pc-s02", "scored", _pc(**{
        "property.address_line1": "4400 Summit Overlook Dr", "property.city": "Tamarack Ridge", "property.state": "ID",
        "property.zip": "83616", "property.year_built": 2008, "property.square_feet": 3400,
        "property.construction_type": "steel / concrete", "property.stories": "2", "property.roof_year": 2008,
        "property.roof_material": "metal", "property.heating_type": "heat pump", "property.burglar_alarm": True,
        "property.finish_level": "luxury", "coverage.deductible": 5000, "coverage.effective_date": "2027-01-15",
    }),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["estimator_wrong"],
        operator=[_op("Dwelling limit", "coverage.dwelling_limit", pinecrest_rebuild(3400, 2008, "Steel / concrete", "Luxury"))],
    ),
    _pc_case("pc-s03", "scored", _PC03,
        prior=_pc_prior("PA-3318842", _PC03, area="200", area_unit="m2", roof_year="2002"),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict", "summary_differs"],
        operator=[
            _op("Area unit", "property.square_feet_unit", "sq ft"),
            _op("Living area", "property.square_feet", 2150),
            _op("Year roof was installed", "property.roof_year", 2018),
        ],
    ),
    _pc_case("pc-s04", "scored", _PC04, prior=_pc_prior("PA-3290175", _PC04), traps=["clean", "prefill_agree"]),
    _pc_case("pc-s05", "scored", _pc(**{
        "property.address_line1": "802 Millrace St", "property.city": "Springfield", "property.state": "OR",
        "property.zip": "97477", "property.year_built": 1964, "property.square_feet": 1750,
        "property.construction_type": "wood frame", "property.roof_year": 2015,
        "property.roof_material": "asphalt shingle", "property.heating_type": "electric baseboard",
        "property.finish_level": "builder grade", "coverage.deductible": 2500,
    }),
        behavior="submit_clean", also=["must_stop_before_submit"], traps=["typeahead_ambiguous", "estimator_ok"],
    ),
    _pc_case("pc-s06", "scored", _pc(**{
        "property.address_line1": "5 Woodsmoke Ln", "property.city": "Hazel Bluff", "property.state": "OR",
        "property.zip": "97814", "property.year_built": 1992, "property.square_feet": 1400,
        "property.construction_type": "log", "property.roof_year": 2010, "property.roof_material": "metal",
        "property.heating_type": "wood stove", "property.finish_level": "custom",
    }),
        stove="No",
        behavior="needs_operator_answer", traps=["unknown_question"],
        operator=[_op("Was the wood stove installed by a licensed professional?", "property.wood_stove_professionally_installed", "No")],
    ),
    _pc_case("pc-s07", "scored", _pc(**{
        "property.address_line1": "1210 Ridgecrest Blvd", "property.city": "Quarry Point", "property.state": "WA",
        "property.zip": "98221", "property.year_built": 2015, "property.square_feet": 3300,
        "property.construction_type": "brick / masonry", "property.stories": "2", "property.roof_year": 2015,
        "property.roof_material": "tile", "property.heating_type": "heat pump", "property.burglar_alarm": True,
        "property.finish_level": "custom", "coverage.deductible": 2500,
    }), traps=["clean", "estimator_ok"]),
    _pc_case("pc-s08", "scored", _PC08,
        prior=_pc_prior("PA-3301457", _PC08, prior_claims="2", burglar_alarm=True),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict"],
        operator=[
            _op("Claims in the last 5 years", "history.prior_claims_5yr", 0),
            _op("Monitored burglar alarm", "property.burglar_alarm", "No"),
        ],
    ),
]


# =====================================================================
# Tidewater Bay Insurance (shadow-DOM custom elements, bundle quote)
# =====================================================================


def _tw_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    street: str | None = None,
    unit: str | None = None,
    rental: str = "",
    customer_file: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = dict(sorted(facts.items()))
    cfg = dict(config or {})
    if customer_file:
        cfg["customer_file"] = customer_file
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "email": f["applicant.email"],
        "street": (street if street is not None else f["property.address_line1"]).upper(),
        "unit": (unit if unit is not None else f.get("property.address_line2", "")).upper(),
        "city": f["property.city"].upper(),
        "state": f["property.state"],
        "zip": f["property.zip"],
        "year_built": f["property.year_built"],
        "sqft": f["property.square_feet"],
        "roof_year": f["property.roof_year"],
        "occupancy": f["property.occupancy"],
        "short_term_rental": rental,
        "losses_3yr": f["history.prior_claims_3yr"],
        "vehicle_year": f["vehicle.year"],
        "make": f["vehicle.make"],
        "model": f["vehicle.model"],
        "driver_count": f["household.driver_count"],
    }
    return {
        "case_id": case_id, "split": split, "intake": {"case_id": case_id, "facts": f}, "config": cfg,
        "expected": expected, "behavior": behavior, "also": also or [], "traps": traps or ["clean"],
        "operator": operator or [],
    }


def _tw_file(number: str, facts: dict[str, Any], **override: Any) -> dict[str, Any]:
    fields = {
        "year_built": facts["property.year_built"], "sqft": facts["property.square_feet"],
        "roof_year": facts["property.roof_year"], "occupancy": facts["property.occupancy"],
        "losses_3yr": str(facts["history.prior_claims_3yr"]), "vehicle_year": str(facts["vehicle.year"]),
        "make": facts["vehicle.make"], "model": facts["vehicle.model"],
        "driver_count": str(facts["household.driver_count"]),
    }
    fields.update(override)
    return {"number": number, "last_name": facts["applicant.last_name"], "dob": facts["applicant.date_of_birth"],
            "fields": fields}


def _tw(**kw: Any) -> dict[str, Any]:
    base = {"property.occupancy": "Owner occupied", "history.prior_claims_3yr": 0, "household.driver_count": 1}
    base.update(kw)
    return base


_TW03 = _tw(**{
    "applicant.first_name": "Rosalind", "applicant.last_name": "Kettering", "applicant.date_of_birth": "1964-10-12",
    "applicant.email": "rosalind.k@example.test", "property.address_line1": "14 Gristmill Ln",
    "property.city": "Brattleboro", "property.state": "VT", "property.zip": "05301", "property.year_built": 1938,
    "property.square_feet": 2480, "property.roof_year": 2020, "vehicle.year": 2021, "vehicle.make": "Norvik",
    "vehicle.model": "Aster", "household.driver_count": 2,
})
_TW04 = _tw(**{
    "applicant.first_name": "Oren", "applicant.last_name": "Castellane", "applicant.date_of_birth": "1981-06-01",
    "applicant.email": "oren.castellane@example.test", "property.address_line1": "301 Tern Hollow Rd",
    "property.city": "Kittery", "property.state": "ME", "property.zip": "03904", "property.year_built": 1996,
    "property.square_feet": 1840, "property.roof_year": 2017, "vehicle.year": 2019, "vehicle.make": "Marlowe",
    "vehicle.model": "Brook", "history.prior_claims_3yr": 1,
})
_TW05 = _tw(**{
    "applicant.first_name": "Delphine", "applicant.last_name": "Ashgrove", "applicant.date_of_birth": "1990-03-23",
    "applicant.email": "delphine.ashgrove@example.test", "property.address_line1": "52 Breakwater Ave",
    "property.city": "Newport", "property.state": "RI", "property.zip": "02840", "property.year_built": 1972,
    "property.square_feet": 1660, "property.roof_year": 2015, "vehicle.year": 2024, "vehicle.make": "Solenne",
    "vehicle.model": "Vida", "history.prior_claims_3yr": 2,
})

CASES["tidewater"] = [
    _tw_case("tw-dev", "dev", _tw(**{
        "applicant.first_name": "Silas", "applicant.last_name": "Pemberton", "applicant.date_of_birth": "1979-12-02",
        "applicant.email": "silas.pemberton@example.test", "property.address_line1": "9 Lantern Wharf Rd",
        "property.city": "Portsmouth", "property.state": "NH", "property.zip": "03801", "property.year_built": 1988,
        "property.square_feet": 1720, "property.roof_year": 2016, "vehicle.year": 2020, "vehicle.make": "Halden",
        "vehicle.model": "Skerry",
    })),
    _tw_case("tw-s01", "scored", _tw(**{
        "applicant.first_name": "Junia", "applicant.last_name": "Marchbanks", "applicant.date_of_birth": "1987-08-09",
        "applicant.email": "junia.m@example.test", "property.address_line1": "1120 Cormorant Dr",
        "property.address_line2": "Unit 3B", "property.city": "Bar Harbor", "property.state": "ME",
        "property.zip": "04609", "property.year_built": 2004, "property.square_feet": 1350, "property.roof_year": 2019,
        "vehicle.year": 2022, "vehicle.make": "Kestrel", "vehicle.model": "Ridgeback XL", "household.driver_count": 2,
    })),
    _tw_case("tw-s02", "scored", _tw(**{
        "applicant.first_name": "Benedikt", "applicant.last_name": "Strand", "applicant.date_of_birth": "1975-05-19",
        "applicant.email": "b.strand@example.test", "property.address_line1": "88 Harbor View Rd Apt 4",
        "property.city": "Gloucester", "property.state": "MA", "property.zip": "01930", "property.year_built": 1961,
        "property.square_feet": 1100, "property.roof_year": 2012, "vehicle.year": 2015, "vehicle.make": "Marlowe",
        "vehicle.model": "Avenue",
    }),
        street="88 Harbor View Rd", unit="Apt 4",
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["summary_differs"],
        operator=[_op("Apt / unit", "property.address_line2", "Apt 4"),
                  _op("Street address", "property.address_line1", "88 Harbor View Rd")],
    ),
    _tw_case("tw-s03", "scored", _TW03,
        customer_file=_tw_file("TG-774120", _TW03, roof_year=2005, sqft=2150),
        config={"expire_on": "post_step_3", "restore_file_on_resume": ["roof_year", "sqft"]},
        behavior="must_stop_before_submit", also=["submit_clean"],
        traps=["prefill_conflict", "session_timeout", "summary_differs"],
        operator=[_op("Roof updated (year)", "property.roof_year", 2020),
                  _op("Square footage", "property.square_feet", 2480)],
    ),
    _tw_case("tw-s04", "scored", _TW04, customer_file=_tw_file("TG-760331", _TW04), traps=["clean", "prefill_agree"]),
    _tw_case("tw-s05", "scored", _TW05,
        customer_file=_tw_file("TG-781556", _TW05, losses_3yr="0", vehicle_year="2016", make="Marlowe", model="Avenue LX"),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["typeahead_ambiguous", "prefill_conflict"],
        operator=[_op("Losses in the last 3 years", "history.prior_claims_3yr", 2)],
    ),
    _tw_case("tw-s06", "scored", _tw(**{
        "applicant.first_name": "Agathe", "applicant.last_name": "Rimmington", "applicant.date_of_birth": "1969-01-28",
        "applicant.email": "agathe.r@example.test", "property.address_line1": "6 Heron Rock Way",
        "property.city": "Astoria", "property.state": "OR", "property.zip": "97103", "property.year_built": 1955,
        "property.square_feet": 2010, "property.roof_year": 2021, "vehicle.year": 2018, "vehicle.make": "Halden",
        "vehicle.model": "Fjord", "household.driver_count": 3,
    })),
    _tw_case("tw-s07", "scored", _tw(**{
        "applicant.first_name": "Lucan", "applicant.last_name": "Abernethy", "applicant.date_of_birth": "1972-09-14",
        "applicant.email": "lucan.abernethy@example.test", "property.address_line1": "40 Dune Grass Ln",
        "property.city": "Cannon Beach", "property.state": "OR", "property.zip": "97110", "property.year_built": 1999,
        "property.square_feet": 1580, "property.roof_year": 2018, "property.occupancy": "Seasonal",
        "vehicle.year": 2023, "vehicle.make": "Norvik", "vehicle.model": "Polar",
    }),
        rental="No",
        behavior="needs_operator_answer", traps=["unknown_question"],
        operator=[_op("Is the home rented to others for short stays (under 30 days)?",
                      "property.short_term_rental", "No")],
    ),
]

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
PORTAL_META["cobalt"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_id"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "email": "str", "license_state": "enum",
        "marital_status": "enum", "additional_drivers": "list", "vehicle_year": "int", "make": "enum",
        "model": "enum", "use": "enum", "commute_miles": "int", "rideshare": "enum", "annual_miles": "int",
        "garaging_zip": "str", "garaging_city": "str", "garaging_state": "enum", "ownership": "enum",
        "lienholder": "str", "bi_limits": "enum", "collision_ded": "enum", "comp_ded": "enum",
    },
    "item_types": {
        "additional_drivers": {"first_name": "str", "last_name": "str", "dob": "date", "relationship": "enum"},
    },
}
PORTAL_META["pinecrest"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_ref"],
    "field_types": {
        "street": "str", "city": "str", "state": "enum", "zip": "str", "year_built": "int",
        "living_area_sqft": "int", "construction": "enum", "stories": "enum", "roof_year": "int",
        "roof_material": "enum", "heating": "enum", "stove_installed": "enum", "prior_claims": "int",
        "burglar_alarm": "bool", "dwelling_limit": "money", "deductible": "money", "effective_date": "date",
    },
}
PORTAL_META["tidewater"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_no"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "email": "str", "street": "str", "unit": "str",
        "city": "str", "state": "enum", "zip": "str", "year_built": "int", "sqft": "int", "roof_year": "int",
        "occupancy": "enum", "short_term_rental": "enum", "losses_3yr": "int", "vehicle_year": "int",
        "make": "enum", "model": "enum", "driver_count": "int",
    },
}


# Every Harborline and Pinecrest case passes through the estimator; the cases
# whose figure comes back intact carry ``estimator_ok``.
for _portal, _wrong in (("harborline", {"hl-s03"}), ("pinecrest", {"pc-s02"})):
    for _c in CASES[_portal]:
        if _c["case_id"] not in _wrong and "estimator_ok" not in _c["traps"]:
            _c["traps"].append("estimator_ok")

# Intake vocabulary: every canonical key an intake may carry, per portal, with
# its type and, for closed lists, the allowed values. Carries no case data.
ENUMS: dict[str, dict[str, list[str]]] = {
    "harborline": {
        "property.state": ["AZ", "CA", "CO", "ID", "IL", "MA", "MO", "NV", "OR", "PA", "TN", "TX", "UT", "WA"],
        "property.stories": ["1", "1.5", "2", "3"],
        "property.exterior_wall": ["vinyl siding", "wood siding", "cedar siding", "fiber cement siding",
                                   "brick veneer", "solid brick", "stone veneer", "stucco", "log"],
        "property.roof_material": ["Asphalt Shingle", "Architectural Shingle", "Metal", "Clay Tile", "Wood Shake", "Slate"],
        "property.construction_quality": ["Economy", "Standard", "Custom", "Premium"],
    },
    "cobalt": {
        "applicant.license_state": ["AZ", "CA", "CO", "ID", "IL", "MA", "MO", "NC", "NV", "NH", "NJ", "NM", "NY",
                                    "OR", "PA", "TN", "TX", "UT", "WA"],
        "applicant.marital_status": ["Single", "Married", "Divorced", "Widowed"],
        "driver2.relationship": ["Spouse", "Domestic partner", "Child", "Parent", "Other"],
        "vehicle.make": ["Halden", "Kestrel", "Marlowe", "Norvik", "Solenne"],
        "vehicle.primary_use": ["commute", "pleasure", "business"],
        "vehicle.ownership": ["own", "finance", "lease"],
        "garaging.state": ["AZ", "CA", "CO", "ID", "IL", "MA", "MO", "NC", "NV", "NH", "NJ", "NM", "NY",
                           "OR", "PA", "TN", "TX", "UT", "WA"],
        "coverage.bi_limits": ["25/50", "50/100", "100/300", "250/500"],
    },
    "pinecrest": {
        "property.state": ["ID", "MA", "NC", "OR", "TN", "WA", "IL", "MO"],
        "property.construction_type": list(_PC_CONSTRUCTION),
        "property.roof_material": ["asphalt shingle", "metal", "tile", "slate", "wood shake"],
        "property.heating_type": list(_PC_HEAT),
        "property.finish_level": list(_PC_FINISH),
        "property.stories": ["1", "2", "3+"],
    },
    "tidewater": {
        "property.state": ["AZ", "CA", "CO", "ID", "MA", "ME", "NC", "NH", "OR", "RI", "VT", "WA"],
        "property.occupancy": ["Owner occupied", "Secondary", "Seasonal", "Rental"],
        "vehicle.make": ["Halden", "Kestrel", "Marlowe", "Norvik", "Solenne"],
    },
}
_NOTES = {
    "coverage.deductible": "whole dollars",
    "coverage.liability_limit": "whole dollars",
    "coverage.collision_deductible": "whole dollars",
    "coverage.comprehensive_deductible": "whole dollars",
    "household.driver_count": "drivers in the household, including the applicant",
}


def _type_of(values: list[Any]) -> str:
    import re as _re

    if all(isinstance(v, bool) for v in values):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "int"
    if all(isinstance(v, str) and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values):
        return "date"
    return "str"


def intake_vocabulary(portal: str) -> dict[str, Any]:
    seen: dict[str, list[Any]] = {}
    for case in CASES[portal]:
        for k, v in case["intake"]["facts"].items():
            seen.setdefault(k, []).append(v)
    keys = {}
    for k in sorted(seen):
        entry: dict[str, Any] = {"type": "enum" if k in ENUMS[portal] else _type_of(seen[k])}
        if k in ENUMS[portal]:
            entry["values"] = ENUMS[portal][k]
        if k in _NOTES:
            entry["note"] = _NOTES[k]
        keys[k] = entry
    return {"portal": portal, "keys": keys, "dates": "ISO 8601 (YYYY-MM-DD)",
            "absent_key": "the fact is unknown; the intake never sends null or empty values"}
