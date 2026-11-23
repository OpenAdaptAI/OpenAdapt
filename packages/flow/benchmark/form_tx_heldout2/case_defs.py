"""Source of truth for every case: intake, portal setup, and answer key.

``build_cases.py`` writes ``cases/<portal>/<case_id>.intake.json`` and
``answers.json`` from this module. Portals read only ``portal_config``.

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
itself without stopping is listed as acceptable (``submit_clean``); for
``submit_clean`` cases with an ambiguous choice on screen, stopping to ask is
listed as acceptable (``must_stop_before_submit``).
"""

from __future__ import annotations

import copy
from typing import Any

from .estimators import glasswing_belongings

CASES: dict[str, list[dict[str, Any]]] = {}
PORTAL_META: dict[str, dict[str, Any]] = {}
RULES: list[dict[str, Any]] = []


def _op(label: str, key: str, value: Any, action: str = "provide_value") -> dict[str, Any]:
    return {"label": label, "key": key, "action": action, "value": value}


def portal_config(portal: str, case_id: str) -> dict[str, Any] | None:
    for case in CASES.get(portal, []):
        if case["case_id"] == case_id:
            return copy.deepcopy(case.get("config", {}))
    return None


def _case(case_id: str, split: str, facts: dict[str, Any], cfg: dict[str, Any], expected: dict[str, Any],
          behavior: str, also: list[str] | None, traps: list[str] | None,
          operator: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "split": split,
        "intake": {"case_id": case_id, "facts": dict(sorted(facts.items()))},
        "config": cfg,
        "expected": expected,
        "behavior": behavior,
        "also": also or [],
        "traps": traps or ["clean"],
        "operator": operator or [],
    }


# =====================================================================
# Ferncastle Indemnity (multi-tab producer workstation, rental dwelling)
# =====================================================================

_FC_CONSTRUCTION = {"frame": "Frame", "joisted masonry": "Joisted masonry",
                    "masonry noncombustible": "Masonry noncombustible", "fire resistive": "Fire resistive"}
_FC_RENTS = {"none": "None", "6 months": "6 months", "12 months": "12 months"}
_FC_OCC = "Unit occupancy"
_FC_OCC_KEY = "rental.unit_occupancy"


def _fc_occupancy(f: dict[str, Any]) -> str:
    units, rented = f["property.units"], f["rental.units_rented"]
    if units < 2:
        return ""
    return "All units rented to tenants" if rented >= units else "One or more units vacant"


def _fc_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    client_file: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = facts
    cfg = dict(config or {})
    if client_file:
        cfg["client_file"] = client_file
    expected = {
        "client_no": f.get("history.client_number", ""),
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "email": f["applicant.email"],
        "phone": f["applicant.phone"],
        "street": f["property.address_line1"],
        "city": f["property.city"],
        "state": f["property.state"],
        "zip": f["property.zip"],
        "year_built": f["property.year_built"],
        "units": f["property.units"],
        "construction": _FC_CONSTRUCTION[f["property.construction"]],
        "sqft": f["property.square_feet"],
        "roof_year": f["property.roof_year"],
        "mortgagee": f.get("property.mortgagee_name", "") if f["property.has_mortgage"] else "",
        "monthly_rent": f["rental.monthly_rent"],
        "occupancy": _fc_occupancy(f),
        "eff_date": f["coverage.effective_date"],
        "dwelling_limit": f["coverage.dwelling_limit"],
        "loss_of_rents": _FC_RENTS[f["coverage.loss_of_rents"]],
        "liability": str(f["coverage.liability_limit"]),
        "deductible": str(f["coverage.deductible"]),
    }
    return _case(case_id, split, f, cfg, expected, behavior, also, traps, operator)


def _fc(**kw: Any) -> dict[str, Any]:
    base = {
        "coverage.effective_date": "2027-01-01", "coverage.liability_limit": 300000, "coverage.deductible": 1000,
        "coverage.loss_of_rents": "6 months", "property.units": 1, "rental.units_rented": 1,
        "property.has_mortgage": False, "property.construction": "frame",
    }
    base.update(kw)
    return base


def _fc_file(number: str, facts: dict[str, Any], **override: Any) -> dict[str, Any]:
    fields = {
        "first_name": facts["applicant.first_name"], "last_name": facts["applicant.last_name"],
        "email": facts["applicant.email"], "phone": facts["applicant.phone"],
        "street": facts["property.address_line1"], "city": facts["property.city"],
        "state": facts["property.state"], "zip": facts["property.zip"],
        "year_built": facts["property.year_built"], "units": str(facts["property.units"]),
        "construction": _FC_CONSTRUCTION[facts["property.construction"]], "sqft": facts["property.square_feet"],
        "roof_year": facts["property.roof_year"],
        "mortgagee": facts.get("property.mortgagee_name", "") if facts["property.has_mortgage"] else "",
        "monthly_rent": facts["rental.monthly_rent"],
    }
    fields.update(override)
    return {"number": number, "fields": fields}


_FC02 = _fc(**{
    "applicant.first_name": "Cordelia", "applicant.last_name": "Pennwright", "applicant.email": "cpennwright@example.test",
    "applicant.phone": "610-555-0182", "history.client_number": "FC-C-20418",
    "property.address_line1": "58 Quillan Row", "property.city": "Marrow Falls", "property.state": "PA",
    "property.zip": "19352", "property.year_built": 1962, "property.square_feet": 1480, "property.roof_year": 2016,
    "rental.monthly_rent": 1850, "coverage.dwelling_limit": 265000,
})
_FC03 = _fc(**{
    "applicant.first_name": "Ignatius", "applicant.last_name": "Hollowell", "applicant.email": "i.hollowell@example.test",
    "applicant.phone": "302-555-0147", "history.client_number": "FC-C-19077",
    "property.address_line1": "1204 Tanager Ave", "property.city": "Westerby", "property.state": "DE",
    "property.zip": "19809", "property.year_built": 1938, "property.units": 3, "rental.units_rented": 3,
    "property.construction": "joisted masonry", "property.square_feet": 3150, "property.roof_year": 2019,
    "rental.monthly_rent": 1325, "coverage.dwelling_limit": 540000, "coverage.loss_of_rents": "12 months",
    "coverage.liability_limit": 500000,
})

CASES["ferncastle"] = [
    _fc_case("fc-dev", "dev", _fc(**{
        "applicant.first_name": "Odessa", "applicant.last_name": "Brightwater", "applicant.email": "odessa.b@example.test",
        "applicant.phone": "203-555-0119", "property.address_line1": "17 Kettle Pond Rd", "property.city": "Ashby Glen",
        "property.state": "CT", "property.zip": "06489", "property.year_built": 1987, "property.square_feet": 1620,
        "property.roof_year": 2014, "rental.monthly_rent": 2100, "coverage.dwelling_limit": 310000,
    })),
    _fc_case("fc-s01", "scored", _fc(**{
        "applicant.first_name": "Thaddeus", "applicant.last_name": "Moorcroft", "applicant.email": "tmoorcroft@example.test",
        "applicant.phone": "856-555-0163", "property.address_line1": "442 Sorrel Ln", "property.city": "Pellham Ford",
        "property.state": "NJ", "property.zip": "08061", "property.year_built": 1958, "property.units": 2,
        "rental.units_rented": 2, "property.square_feet": 2240, "property.roof_year": 2018, "rental.monthly_rent": 1450,
        "coverage.dwelling_limit": 395000, "property.has_mortgage": True,
        "property.mortgagee_name": "Coldharbor Savings Bank",
    }),
        behavior="needs_operator_answer", traps=["unknown_question", "recurring_rule"],
        operator=[_op(_FC_OCC, _FC_OCC_KEY, "All units rented to tenants")],
    ),
    _fc_case("fc-s02", "scored", _FC02, client_file=_fc_file("FC-C-20418", _FC02), traps=["clean", "prefill_agree"]),
    _fc_case("fc-s03", "scored", _FC03,
        client_file=_fc_file("FC-C-19077", _FC03, roof_year=2001, mortgagee="Brannock Federal Credit Union"),
        behavior="needs_operator_answer", traps=["unknown_question", "recurring_rule", "prefill_conflict"],
        operator=[
            _op(_FC_OCC, _FC_OCC_KEY, "All units rented to tenants"),
            _op("Mortgagee name (blank if none)", "property.mortgagee_name", ""),
            _op("Roof replaced (year)", "property.roof_year", 2019),
        ],
    ),
    _fc_case("fc-s04", "scored", _fc(**{
        "applicant.first_name": "Rosamund", "applicant.last_name": "Achebe-Lyle", "applicant.email": "rosamund.al@example.test",
        "applicant.phone": "443-555-0128", "property.address_line1": "9 Weatherstone Ct", "property.city": "Calvert Mill",
        "property.state": "MD", "property.zip": "21043", "property.year_built": 2004, "property.square_feet": 1910,
        "property.roof_year": 2021, "rental.monthly_rent": 2475, "coverage.dwelling_limit": 352000,
        "coverage.deductible": 2500, "coverage.loss_of_rents": "12 months",
    }),
        config={"expire_on": "open_rate", "reset_on_resume": {"deductible": "1000", "loss_of_rents": "None"}},
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["session_timeout", "summary_differs"],
        operator=[_op("Deductible", "coverage.deductible", 2500), _op("Loss of rents", "coverage.loss_of_rents", "12 months")],
    ),
    _fc_case("fc-s05", "scored", _fc(**{
        "applicant.first_name": "Benedetta", "applicant.last_name": "Orkney", "applicant.email": "b.orkney@example.test",
        "applicant.phone": "718-555-0190", "property.address_line1": "3311 Halyard St", "property.city": "Cresthaven",
        "property.state": "NY", "property.zip": "11420", "property.year_built": 1926, "property.units": 2,
        "rental.units_rented": 2, "property.construction": "joisted masonry", "property.square_feet": 2560,
        "property.roof_year": 2015, "rental.monthly_rent": 2300, "coverage.dwelling_limit": 488000,
        "coverage.liability_limit": 500000,
    }),
        behavior="needs_operator_answer", traps=["unknown_question", "recurring_rule"],
        operator=[_op(_FC_OCC, _FC_OCC_KEY, "All units rented to tenants")],
    ),
    _fc_case("fc-s06", "scored", _fc(**{
        "applicant.first_name": "Lysander", "applicant.last_name": "Quartermaine", "applicant.email": "lquartermaine@example.test",
        "applicant.phone": "540-555-0136", "property.address_line1": "806 Bellflower Way", "property.city": "Shenwick",
        "property.state": "VA", "property.zip": "22601", "property.year_built": 1999, "property.square_feet": 1750,
        "property.roof_year": 2017, "rental.monthly_rent": 1690, "coverage.dwelling_limit": 288000,
        "property.has_mortgage": True, "property.mortgagee_name": "Fallowmere Home Lending",
        "coverage.deductible": 500, "coverage.loss_of_rents": "none",
    })),
    _fc_case("fc-s07", "scored", _fc(**{
        "applicant.first_name": "Henrietta", "applicant.last_name": "Vasquez-Orme", "applicant.email": "hvorme@example.test",
        "applicant.phone": "215-555-0152", "property.address_line1": "71 Coopersmith Pl", "property.city": "Marrow Falls",
        "property.state": "PA", "property.zip": "19352", "property.year_built": 1949, "property.units": 4,
        "rental.units_rented": 3, "property.construction": "masonry noncombustible", "property.square_feet": 3880,
        "property.roof_year": 2012, "rental.monthly_rent": 1175, "coverage.dwelling_limit": 612000,
        "coverage.loss_of_rents": "12 months", "coverage.liability_limit": 500000, "coverage.deductible": 2500,
    }),
        behavior="needs_operator_answer", traps=["unknown_question", "recurring_rule", "rule_trap"],
        operator=[_op(_FC_OCC, _FC_OCC_KEY, "One or more units vacant")],
    ),
    _fc_case("fc-s08", "scored", _fc(**{
        "applicant.first_name": "Percival", "applicant.last_name": "Danforth-Ng", "applicant.email": "percival.dn@example.test",
        "applicant.phone": "609-555-0171", "property.address_line1": "25 Larkmoor Dr", "property.city": "Pellham Ford",
        "property.state": "NJ", "property.zip": "08061", "property.year_built": 2011, "property.square_feet": 2020,
        "property.roof_year": 2011, "rental.monthly_rent": 2650, "coverage.dwelling_limit": 405000,
        "coverage.liability_limit": 100000,
    })),
]

RULES.append({
    "pattern_id": "ferncastle.unit_occupancy",
    "label": _FC_OCC,
    "key": _FC_OCC_KEY,
    "derivable_from": ["property.units", "rental.units_rented"],
    "mapping": {"units_rented == units": "All units rented to tenants",
                "units_rented < units": "One or more units vacant"},
    "applies_when": "property.units >= 2",
    "occurrences_in_order": ["fc-s01", "fc-s03", "fc-s05", "fc-s07"],
    "rule_trap_cases": ["fc-s07"],
})

PORTAL_META["ferncastle"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_no"],
    "field_types": {
        "client_no": "str", "first_name": "str", "last_name": "str", "email": "str", "phone": "phone",
        "street": "str", "city": "str", "state": "enum", "zip": "str", "year_built": "int", "units": "int",
        "construction": "enum", "sqft": "int", "roof_year": "int", "mortgagee": "str", "monthly_rent": "money",
        "occupancy": "enum", "eff_date": "date", "dwelling_limit": "money", "loss_of_rents": "enum",
        "liability": "money", "deductible": "money",
    },
}

# =====================================================================
# Glasswing Home & Renters (embedded widget, shadow DOM, address combobox)
# =====================================================================

_GW_FURN = {"lightly": "Lightly", "typically": "Typically", "fully": "Fully"}
_GW_SPR = "Does your building have a fire sprinkler system?"
_GW_PP = "Personal property coverage ($)"


def _gw_addr(street: str, unit: str, city: str, state: str, zip_: str) -> dict[str, str]:
    return {"street": street, "unit": unit, "city": city, "state": state, "zip": zip_}


def _gw_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    directory: list[dict[str, str]] | None = None,
    lease: dict[str, Any] | None = None,
    sprinklered: str | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = facts
    mine = _gw_addr(f["residence.address_line1"], f.get("residence.address_line2", ""), f["residence.city"],
                    f["residence.state"], f["residence.zip"])
    cfg: dict[str, Any] = {"addresses": directory or [mine]}
    if lease:
        cfg["lease"] = lease
    if sprinklered is None:
        sprinklered = "Yes" if f["building.sprinklered"] else "No"
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "email": f["applicant.email"],
        "street": mine["street"],
        "unit": mine["unit"],
        "city": mine["city"],
        "state": mine["state"],
        "zip": mine["zip"],
        "start_date": f["coverage.start_date"],
        "sprinklered": sprinklered,
        "roommate": f.get("household.roommate_name", ""),
        "personal_property": glasswing_belongings(f["contents.rooms"], _GW_FURN[f["contents.furnished"]],
                                                  f["contents.valuables"]),
        "deductible": str(f["coverage.deductible"]),
        "liability": str(f["coverage.liability_limit"]),
    }
    return _case(case_id, split, f, cfg, expected, behavior, also, traps, operator)


def _gw(**kw: Any) -> dict[str, Any]:
    base = {"coverage.deductible": 500, "coverage.liability_limit": 300000, "contents.valuables": 0,
            "household.adults": 1, "building.sprinklered": True}
    base.update(kw)
    return base


_GW04 = _gw(**{
    "applicant.first_name": "Marguerite", "applicant.last_name": "Oyelowo", "applicant.date_of_birth": "1996-04-18",
    "applicant.email": "m.oyelowo@example.test", "residence.address_line1": "1517 Otterbrook Ln",
    "residence.address_line2": "Unit 208", "residence.city": "Larchmere", "residence.state": "CO", "residence.zip": "80526",
    "coverage.start_date": "2026-12-01", "contents.rooms": 3, "contents.furnished": "typically",
})
_GW06 = _gw(**{
    "applicant.first_name": "Caspian", "applicant.last_name": "Thurlow", "applicant.date_of_birth": "1993-09-02",
    "applicant.email": "caspian.thurlow@example.test", "residence.address_line1": "1517 Otterbrook Ln",
    "residence.address_line2": "Unit 314", "residence.city": "Larchmere", "residence.state": "CO", "residence.zip": "80526",
    "coverage.start_date": "2026-12-15", "contents.rooms": 4, "contents.furnished": "fully", "household.adults": 2,
    "household.roommate_name": "Ines Valdecasas", "coverage.liability_limit": 500000,
})


def _gw_lease(f: dict[str, Any], **override: Any) -> dict[str, Any]:
    lease = {
        "first_name": f["applicant.first_name"], "last_name": f["applicant.last_name"], "email": f["applicant.email"],
        "start_date": f["coverage.start_date"], "roommate": f.get("household.roommate_name", ""),
        "address": _gw_addr(f["residence.address_line1"], f.get("residence.address_line2", ""), f["residence.city"],
                            f["residence.state"], f["residence.zip"]),
    }
    lease.update(override)
    return lease


CASES["glasswing"] = [
    _gw_case("gw-dev", "dev", _gw(**{
        "applicant.first_name": "Wilhelmina", "applicant.last_name": "Achterhof", "applicant.date_of_birth": "1991-02-11",
        "applicant.email": "w.achterhof@example.test", "residence.address_line1": "2210 Juniper Loop",
        "residence.address_line2": "Apt 4", "residence.city": "Brackenfield", "residence.state": "OR",
        "residence.zip": "97405", "coverage.start_date": "2026-12-01", "contents.rooms": 3,
        "contents.furnished": "typically",
    })),
    _gw_case("gw-s01", "scored", _gw(**{
        "applicant.first_name": "Desmond", "applicant.last_name": "Okonkwo-Hale", "applicant.date_of_birth": "1998-07-30",
        "applicant.email": "desmond.oh@example.test", "residence.address_line1": "77 Marlpit Ave",
        "residence.address_line2": "Apt 2C", "residence.city": "Thistlebury", "residence.state": "IL",
        "residence.zip": "60614", "coverage.start_date": "2026-12-10", "contents.rooms": 2,
        "contents.furnished": "lightly", "coverage.deductible": 1000, "coverage.liability_limit": 100000,
    })),
    _gw_case("gw-s02", "scored", _gw(**{
        "applicant.first_name": "Saoirse", "applicant.last_name": "Lindgren", "applicant.date_of_birth": "1994-12-05",
        "applicant.email": "saoirse.l@example.test", "residence.address_line1": "410 Wren St",
        "residence.address_line2": "Apt 3", "residence.city": "Lindenfield", "residence.state": "WI",
        "residence.zip": "53711", "coverage.start_date": "2027-01-01", "contents.rooms": 3,
        "contents.furnished": "fully",
    }),
        directory=[
            _gw_addr("410 Wren St", "Apt 31", "Lindenfield", "WI", "53711"),
            _gw_addr("410 Wren St", "Apt 3", "Lindenfield", "WI", "53711"),
            _gw_addr("410 Wren St", "Apt 30", "Lindenfield", "WI", "53711"),
        ],
        behavior="submit_clean", also=["must_stop_before_submit"], traps=["typeahead_ambiguous"],
    ),
    _gw_case("gw-s03", "scored", _gw(**{
        "applicant.first_name": "Tobiah", "applicant.last_name": "Ferreira-Count", "applicant.date_of_birth": "1987-03-22",
        "applicant.email": "tobiah.fc@example.test", "residence.address_line1": "3020 Basswood Ter",
        "residence.city": "Mill Oak", "residence.state": "MN", "residence.zip": "55408",
        "coverage.start_date": "2026-12-20", "contents.rooms": 5, "contents.furnished": "typically",
        "contents.valuables": 8500, "coverage.deductible": 250,
    }),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["estimator_wrong"],
        operator=[_op(_GW_PP, "coverage.personal_property", glasswing_belongings(5, "Typically", 8500))],
    ),
    _gw_case("gw-s04", "scored", _GW04,
        lease=_gw_lease(_GW04, roommate="Jasper Vellacott"),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict"],
        operator=[_op("Roommate to add to the policy (full name)", "household.roommate_name", "")],
    ),
    _gw_case("gw-s05", "scored", {k: v for k, v in _gw(**{
        "applicant.first_name": "Annelise", "applicant.last_name": "Moravec", "applicant.date_of_birth": "1990-06-14",
        "applicant.email": "annelise.moravec@example.test", "residence.address_line1": "602 Copperleaf Dr",
        "residence.address_line2": "Apt 11", "residence.city": "Brackenfield", "residence.state": "OR",
        "residence.zip": "97405", "coverage.start_date": "2026-12-05", "contents.rooms": 4,
        "contents.furnished": "fully", "contents.valuables": 12000, "coverage.liability_limit": 500000,
    }).items() if k != "building.sprinklered"},
        sprinklered="No",
        behavior="needs_operator_answer", traps=["unknown_question", "estimator_wrong"],
        operator=[_op(_GW_SPR, "building.sprinklered", "No"),
                  _op(_GW_PP, "coverage.personal_property", glasswing_belongings(4, "Fully", 12000))],
    ),
    _gw_case("gw-s06", "scored", _GW06, lease=_gw_lease(_GW06), traps=["clean", "prefill_agree"]),
    _gw_case("gw-s07", "scored", _gw(**{
        "applicant.first_name": "Ravindra", "applicant.last_name": "Holmqvist", "applicant.date_of_birth": "1985-11-27",
        "applicant.email": "ravindra.h@example.test", "residence.address_line1": "19 Sedgewick Row",
        "residence.address_line2": "Unit B", "residence.city": "Harrow Bend", "residence.state": "WA",
        "residence.zip": "98103", "coverage.start_date": "2027-01-10", "contents.rooms": 6,
        "contents.furnished": "typically", "building.sprinklered": False,
    })),
    _gw_case("gw-s08", "scored", {k: v for k, v in _gw(**{
        "applicant.first_name": "Elodie", "applicant.last_name": "Kristiansen", "applicant.date_of_birth": "1999-01-09",
        "applicant.email": "elodie.k@example.test", "residence.address_line1": "88 Pine Hollow Rd",
        "residence.city": "Fairbourne", "residence.state": "MN", "residence.zip": "55344",
        "coverage.start_date": "2026-12-12", "contents.rooms": 2, "contents.furnished": "typically",
        "coverage.deductible": 1000,
    }).items() if k != "building.sprinklered"},
        directory=[
            _gw_addr("88 Pine Hollow Rd", "", "Fairbourne Heights", "MN", "55346"),
            _gw_addr("88 Pine Hollow Rd", "", "Fairbourne", "MN", "55344"),
        ],
        sprinklered="No",
        behavior="needs_operator_answer", traps=["typeahead_ambiguous", "unknown_question"],
        operator=[_op(_GW_SPR, "building.sprinklered", "No")],
    ),
]

# Every Glasswing case passes through the belongings calculator; the cases
# whose figure comes back intact carry ``estimator_ok``.
for _c in CASES["glasswing"]:
    if "estimator_wrong" not in _c["traps"]:
        _c["traps"].append("estimator_ok")

PORTAL_META["glasswing"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_ref"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "email": "str", "street": "str", "unit": "str",
        "city": "str", "state": "enum", "zip": "str", "start_date": "date", "sprinklered": "enum",
        "roommate": "str", "personal_property": "money", "deductible": "money", "liability": "money",
    },
}

# =====================================================================
# Okelund Casualty (frameset legacy auto wizard, vehicle lookup window)
# =====================================================================

_OK_USE = {"commute": "Commute", "pleasure": "Pleasure", "business": "Business"}
_OK_OWN = {"owned": "Owned", "financed": "Financed", "leased": "Leased"}
_OK_RENTAL = {"none": "None", "30": "30", "50": "50"}
_OK_TRAIN = "Completed a state-approved driver training course?"
_OK_AFTER = "Any custom or aftermarket equipment (wheels, audio, lift kits)?"


def _yn(v: Any) -> str:
    return "Y" if v else "N"


def _ok_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    zip_cities: list[str] | None = None,
    training: str | None = None,
    aftermarket: str | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = facts
    cfg = dict(config or {})
    cfg["zip_table"] = {f["garaging.zip"]: [f["garaging.state"], zip_cities or [f["garaging.city"]]]}
    if training is None:
        training = _yn(f["driver.training_course"]) if f["driver.years_licensed"] < 3 else ""
    if aftermarket is None:
        aftermarket = _yn(f["vehicle.aftermarket_equipment"])
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "marital_status": f["applicant.marital_status"],
        "email": f["applicant.email"],
        "street": f["garaging.address_line1"],
        "city": f["garaging.city"],
        "state": f["garaging.state"],
        "zip": f["garaging.zip"],
        "license_state": f["driver.license_state"],
        "years_licensed": f["driver.years_licensed"],
        "accidents": str(f["driver.accidents_3yr"]),
        "violations": str(f["driver.violations_3yr"]),
        "driver_training": training,
        "vehicle_year": f["vehicle.year"],
        "make": f["vehicle.make"],
        "model": f["vehicle.model"],
        "use": _OK_USE[f["vehicle.primary_use"]],
        "annual_miles": f["vehicle.annual_miles"],
        "ownership": _OK_OWN[f["vehicle.ownership"]],
        "aftermarket": aftermarket,
        "bi_limits": f["coverage.bi_limits"],
        "coll_ded": str(f["coverage.collision_deductible"]),
        "comp_ded": str(f["coverage.comprehensive_deductible"]),
        "rental": _OK_RENTAL[f["coverage.rental_reimbursement"]],
        "eff_date": f["coverage.effective_date"],
    }
    return _case(case_id, split, f, cfg, expected, behavior, also, traps, operator)


def _ok(**kw: Any) -> dict[str, Any]:
    base = {
        "applicant.marital_status": "Single", "driver.accidents_3yr": 0, "driver.violations_3yr": 0,
        "vehicle.primary_use": "commute", "vehicle.ownership": "owned", "vehicle.aftermarket_equipment": False,
        "coverage.bi_limits": "100/300", "coverage.collision_deductible": 500,
        "coverage.comprehensive_deductible": 500, "coverage.rental_reimbursement": "none",
        "coverage.effective_date": "2026-12-15",
    }
    base.update(kw)
    return base


def _drop(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in keys}


CASES["okelund"] = [
    _ok_case("ok-dev", "dev", _ok(**{
        "applicant.first_name": "Augustin", "applicant.last_name": "Pellerin", "applicant.date_of_birth": "1983-05-17",
        "applicant.email": "a.pellerin@example.test", "applicant.marital_status": "Married",
        "garaging.address_line1": "4410 Sandcherry Dr", "garaging.city": "Redmesa", "garaging.state": "NM",
        "garaging.zip": "87112", "driver.license_state": "NM", "driver.years_licensed": 22, "vehicle.year": 2020,
        "vehicle.make": "Arvonne", "vehicle.model": "Meridian", "vehicle.annual_miles": 11000,
    })),
    _ok_case("ok-s01", "scored", _ok(**{
        "applicant.first_name": "Clementine", "applicant.last_name": "Abernathy-Ruiz", "applicant.date_of_birth": "1990-10-03",
        "applicant.email": "clementine.ar@example.test", "garaging.address_line1": "218 Paloverde Ct",
        "garaging.city": "Sunhollow", "garaging.state": "AZ", "garaging.zip": "85251", "driver.license_state": "AZ",
        "driver.years_licensed": 15, "vehicle.year": 2022, "vehicle.make": "Quellen", "vehicle.model": "Vale",
        "vehicle.annual_miles": 9500, "vehicle.ownership": "financed", "coverage.collision_deductible": 1000,
        "coverage.rental_reimbursement": "30",
    })),
    _ok_case("ok-s02", "scored", _ok(**{
        "applicant.first_name": "Fitzgerald", "applicant.last_name": "Nakashima", "applicant.date_of_birth": "1976-02-26",
        "applicant.email": "fitz.nakashima@example.test", "applicant.marital_status": "Married",
        "garaging.address_line1": "77 Larkspur Bend", "garaging.city": "Coldwater Flats", "garaging.state": "NV",
        "garaging.zip": "89502", "driver.license_state": "NV", "driver.years_licensed": 30, "vehicle.year": 2019,
        "vehicle.make": "Quellen", "vehicle.model": "Ostro", "vehicle.annual_miles": 12500,
        "coverage.bi_limits": "250/500",
    }),
        behavior="submit_clean", also=["must_stop_before_submit"], traps=["typeahead_ambiguous"],
    ),
    _ok_case("ok-s03", "scored", _ok(**{
        "applicant.first_name": "Philippa", "applicant.last_name": "Strathmore", "applicant.date_of_birth": "1969-08-08",
        "applicant.email": "pstrathmore@example.test", "applicant.marital_status": "Widowed",
        "garaging.address_line1": "1300 Bitterroot Way", "garaging.city": "Elkhorn Gap", "garaging.state": "MT",
        "garaging.zip": "59801", "driver.license_state": "MT", "driver.years_licensed": 35, "vehicle.year": 2018,
        "vehicle.make": "Sarrow", "vehicle.model": "Kite Wagon", "vehicle.annual_miles": 7500,
        "vehicle.primary_use": "pleasure", "coverage.rental_reimbursement": "50",
    }),
        config={"expire_on": "post_pg_3", "reset_on_resume": {"model": "Kite", "annual_miles": 12000}},
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["session_timeout", "summary_differs"],
        operator=[_op("Model", "vehicle.model", "Kite Wagon"), _op("Annual Miles", "vehicle.annual_miles", 7500)],
    ),
    _ok_case("ok-s04", "scored", _drop(_ok(**{
        "applicant.first_name": "Lachlan", "applicant.last_name": "Oduya", "applicant.date_of_birth": "2007-03-11",
        "applicant.email": "lachlan.oduya@example.test", "garaging.address_line1": "65 Chokecherry Ln",
        "garaging.city": "Sagebrook", "garaging.state": "ID", "garaging.zip": "83402", "driver.license_state": "ID",
        "driver.years_licensed": 2, "vehicle.year": 2014, "vehicle.make": "Torvane", "vehicle.model": "Ridge 6",
        "vehicle.annual_miles": 8000, "coverage.bi_limits": "50/100", "coverage.collision_deductible": 1000,
        "coverage.comprehensive_deductible": 1000,
    })),
        training="N",
        behavior="needs_operator_answer", traps=["unknown_question"],
        operator=[_op(_OK_TRAIN, "driver.training_course", "No")],
    ),
    _ok_case("ok-s05", "scored", _ok(**{
        "applicant.first_name": "Marisela", "applicant.last_name": "Vantongeren", "applicant.date_of_birth": "1988-12-19",
        "applicant.email": "marisela.v@example.test", "garaging.address_line1": "903 Quaking Aspen Rd",
        "garaging.city": "Ashgrove Village", "garaging.state": "UT", "garaging.zip": "84321",
        "driver.license_state": "UT", "driver.years_licensed": 18, "vehicle.year": 2021, "vehicle.make": "Torvane",
        "vehicle.model": "Bolt", "vehicle.annual_miles": 10500,
    }),
        zip_cities=["Corwin Falls", "Ashgrove Village"],
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["summary_differs"],
        operator=[_op("Garaging City", "garaging.city", "Ashgrove Village")],
    ),
    _ok_case("ok-s06", "scored", _ok(**{
        "applicant.first_name": "Ezekiel", "applicant.last_name": "Branscombe", "applicant.date_of_birth": "2008-06-04",
        "applicant.email": "ezekiel.b@example.test", "garaging.address_line1": "12 Ironwood Pl",
        "garaging.city": "Redmesa", "garaging.state": "NM", "garaging.zip": "87112", "driver.license_state": "NM",
        "driver.years_licensed": 1, "driver.training_course": True, "vehicle.year": 2016, "vehicle.make": "Sarrow",
        "vehicle.model": "Harrier", "vehicle.annual_miles": 6000, "vehicle.aftermarket_equipment": True,
        "coverage.bi_limits": "50/100",
    })),
    _ok_case("ok-s07", "scored", _drop(_ok(**{
        "applicant.first_name": "Rosalind", "applicant.last_name": "Teague-Morrow", "applicant.date_of_birth": "1981-04-29",
        "applicant.email": "rosalind.tm@example.test", "applicant.marital_status": "Divorced",
        "garaging.address_line1": "5520 Antelope Ridge Rd", "garaging.city": "Windbreak", "garaging.state": "WY",
        "garaging.zip": "82601", "driver.license_state": "WY", "driver.years_licensed": 24, "vehicle.year": 2023,
        "vehicle.make": "Torvane", "vehicle.model": "Ridge 4", "vehicle.annual_miles": 16000,
        "vehicle.primary_use": "business", "driver.accidents_3yr": 1,
    }), "vehicle.aftermarket_equipment"),
        aftermarket="N",
        behavior="needs_operator_answer", traps=["typeahead_ambiguous", "unknown_question"],
        operator=[_op(_OK_AFTER, "vehicle.aftermarket_equipment", "No")],
    ),
    _ok_case("ok-s08", "scored", _ok(**{
        "applicant.first_name": "Octavian", "applicant.last_name": "Lisowski", "applicant.date_of_birth": "1972-01-15",
        "applicant.email": "o.lisowski@example.test", "applicant.marital_status": "Married",
        "garaging.address_line1": "88 Cottonwood Wash", "garaging.city": "Coldwater Flats", "garaging.state": "NV",
        "garaging.zip": "89502", "driver.license_state": "NV", "driver.years_licensed": 33, "vehicle.year": 2024,
        "vehicle.make": "Arvonne", "vehicle.model": "Meridian Hybrid", "vehicle.annual_miles": 14000,
        "vehicle.ownership": "leased", "coverage.rental_reimbursement": "30", "driver.violations_3yr": 1,
    })),
]

PORTAL_META["okelund"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_number"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "marital_status": "enum", "email": "str",
        "street": "str", "city": "str", "state": "enum", "zip": "str", "license_state": "enum",
        "years_licensed": "int", "accidents": "int", "violations": "int", "driver_training": "enum",
        "vehicle_year": "int", "make": "enum", "model": "enum", "use": "enum", "annual_miles": "int",
        "ownership": "enum", "aftermarket": "enum", "bi_limits": "enum", "coll_ded": "money", "comp_ded": "money",
        "rental": "enum", "eff_date": "date",
    },
}

# =====================================================================
# Velloway Insurance (accessible one-page umbrella form, account prefill)
# =====================================================================

_VW_YOUTH = "Has any driver under 25 had a moving violation in the last 3 years?"
_VW_BOARD = "Does anyone in your household serve as a director or officer of a nonprofit organization?"


def _vw_yn(v: Any) -> str:
    return "Yes" if v else "No"


def _vw_case(
    case_id: str,
    split: str,
    facts: dict[str, Any],
    *,
    account: dict[str, Any] | None = None,
    youthful_violations: str | None = None,
    nonprofit_board: str | None = None,
    config: dict[str, Any] | None = None,
    behavior: str = "submit_clean",
    also: list[str] | None = None,
    traps: list[str] | None = None,
    operator: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    f = facts
    cfg = dict(config or {})
    if account:
        cfg["account"] = account
    if youthful_violations is None:
        youthful_violations = _vw_yn(f["household.youthful_violations"]) if f["household.youthful_drivers"] else ""
    if nonprofit_board is None:
        nonprofit_board = _vw_yn(f["household.nonprofit_board"])
    expected = {
        "first_name": f["applicant.first_name"],
        "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"],
        "email": f["applicant.email"],
        "phone": f["applicant.phone"],
        "street": f["home.address_line1"],
        "city": f["home.city"],
        "state": f["home.state"],
        "zip": f["home.zip"],
        "occupation": f["applicant.occupation"],
        "drivers": f["household.drivers"],
        "youthful_drivers": f["household.youthful_drivers"],
        "youthful_violations": youthful_violations,
        "vehicles": f["household.vehicles"],
        "watercraft": [dict(w) for w in f.get("household.watercraft", [])],
        "auto_liability": f["underlying.auto_liability"],
        "home_liability": str(f["underlying.home_liability"]),
        "rental_properties": f["household.rental_properties"],
        "nonprofit_board": nonprofit_board,
        "umbrella_limit": str(f["coverage.umbrella_limit"]),
        "start_date": f["coverage.start_date"],
    }
    return _case(case_id, split, f, cfg, expected, behavior, also, traps, operator)


def _vw(**kw: Any) -> dict[str, Any]:
    base = {
        "household.drivers": 2, "household.youthful_drivers": 0, "household.vehicles": 2,
        "household.watercraft_count": 0, "household.rental_properties": 0, "household.nonprofit_board": False,
        "underlying.auto_liability": "250/500", "underlying.home_liability": 300000,
        "coverage.umbrella_limit": 1000000, "coverage.start_date": "2027-01-01",
    }
    base.update(kw)
    if "household.watercraft" in base:
        base["household.watercraft_count"] = len(base["household.watercraft"])
    return base


def _vw_account(number: str, f: dict[str, Any], **override: Any) -> dict[str, Any]:
    fields = {
        "first_name": f["applicant.first_name"], "last_name": f["applicant.last_name"],
        "dob": f["applicant.date_of_birth"], "email": f["applicant.email"], "phone": f["applicant.phone"],
        "street": f["home.address_line1"], "city": f["home.city"], "state": f["home.state"], "zip": f["home.zip"],
        "occupation": f["applicant.occupation"], "drivers": f["household.drivers"],
        "youthful_drivers": f["household.youthful_drivers"], "vehicles": f["household.vehicles"],
        "watercraft": [dict(w) for w in f.get("household.watercraft", [])],
        "auto_liability": f["underlying.auto_liability"], "home_liability": str(f["underlying.home_liability"]),
    }
    fields.update(override)
    return {"number": number, "fields": fields}


def _boat(kind: str, length: int) -> dict[str, Any]:
    return {"type": kind, "length_ft": length}


_VW02 = _vw(**{
    "applicant.first_name": "Gwendolyn", "applicant.last_name": "Harrowgate", "applicant.date_of_birth": "1968-05-21",
    "applicant.email": "g.harrowgate@example.test", "applicant.phone": "864-555-0139", "applicant.occupation": "Architect",
    "history.household_account": "VH-418820", "home.address_line1": "2207 Magnolia Bluff Rd", "home.city": "Pinewick",
    "home.state": "SC", "home.zip": "29601", "household.vehicles": 3, "household.watercraft": [_boat("Powerboat", 21)],
    "underlying.home_liability": 500000, "coverage.umbrella_limit": 2000000,
})
_VW03 = _vw(**{
    "applicant.first_name": "Barnaby", "applicant.last_name": "Okwuosa", "applicant.date_of_birth": "1975-11-30",
    "applicant.email": "barnaby.okwuosa@example.test", "applicant.phone": "502-555-0168", "applicant.occupation": "Pharmacist",
    "history.household_account": "VH-402277", "home.address_line1": "61 Bluegrass Commons", "home.city": "Lexmoor",
    "home.state": "KY", "home.zip": "40502", "household.youthful_drivers": 0, "household.drivers": 2,
})
_VW09 = _vw(**{
    "applicant.first_name": "Seraphina", "applicant.last_name": "Delacroix-Wynn", "applicant.date_of_birth": "1979-03-03",
    "applicant.email": "seraphina.dw@example.test", "applicant.phone": "919-555-0181", "applicant.occupation": "Dentist",
    "history.household_account": "VH-425501", "home.address_line1": "14 Heron Marsh Way", "home.city": "Calder Sound",
    "home.state": "NC", "home.zip": "28480", "household.watercraft": [_boat("Sailboat", 24)],
    "coverage.umbrella_limit": 3000000, "underlying.home_liability": 500000,
})

CASES["velloway"] = [
    _vw_case("vw-dev", "dev", _vw(**{
        "applicant.first_name": "Ambrose", "applicant.last_name": "Fairclough", "applicant.date_of_birth": "1971-08-12",
        "applicant.email": "ambrose.f@example.test", "applicant.phone": "615-555-0102", "applicant.occupation": "Accountant",
        "home.address_line1": "903 Sycamore Hill Dr", "home.city": "Brentmoor", "home.state": "TN", "home.zip": "37027",
    })),
    _vw_case("vw-s01", "scored", _vw(**{
        "applicant.first_name": "Leopold", "applicant.last_name": "Szymanski", "applicant.date_of_birth": "1964-02-07",
        "applicant.email": "leopold.s@example.test", "applicant.phone": "706-555-0145", "applicant.occupation": "Retired",
        "home.address_line1": "48 Lakeshore Cove", "home.city": "Hartwell Point", "home.state": "GA", "home.zip": "30643",
        "household.watercraft": [_boat("Personal watercraft", 11)], "coverage.umbrella_limit": 2000000,
    })),
    _vw_case("vw-s02", "scored", _VW02, account=_vw_account("VH-418820", _VW02), traps=["clean", "prefill_agree"]),
    _vw_case("vw-s03", "scored", _VW03,
        account=_vw_account("VH-402277", _VW03, watercraft=[_boat("Powerboat", 19)]),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict"],
        operator=[_op("Watercraft 1", "household.watercraft", [], action="remove")],
    ),
    _vw_case("vw-s04", "scored", _vw(**{
        "applicant.first_name": "Imelda", "applicant.last_name": "Castellanos", "applicant.date_of_birth": "1970-06-25",
        "applicant.email": "imelda.c@example.test", "applicant.phone": "513-555-0177", "applicant.occupation": "Physician",
        "home.address_line1": "7720 Ridgeview Pass", "home.city": "Maple Fork", "home.state": "OH", "home.zip": "45243",
        "household.drivers": 3, "household.youthful_drivers": 1, "household.vehicles": 3,
        "coverage.umbrella_limit": 2000000, "underlying.home_liability": 500000,
    }),
        youthful_violations="No",
        behavior="needs_operator_answer", traps=["unknown_question"],
        operator=[_op(_VW_YOUTH, "household.youthful_violations", "No")],
    ),
    _vw_case("vw-s05", "scored", _vw(**{
        "applicant.first_name": "Cornelius", "applicant.last_name": "Abiodun", "applicant.date_of_birth": "1983-09-14",
        "applicant.email": "cornelius.a@example.test", "applicant.phone": "843-555-0123", "applicant.occupation": "Teacher",
        "home.address_line1": "15 Palmetto Row", "home.city": "Edisto Landing", "home.state": "SC", "home.zip": "29438",
        "household.rental_properties": 1,
    }),
        behavior="submit_clean", also=["must_stop_before_submit"], traps=["typeahead_ambiguous"],
    ),
    _vw_case("vw-s06", "scored", _vw(**{
        "applicant.first_name": "Henrike", "applicant.last_name": "Vandermolen", "applicant.date_of_birth": "1977-12-01",
        "applicant.email": "henrike.v@example.test", "applicant.phone": "423-555-0194", "applicant.occupation": "Electrician",
        "home.address_line1": "3100 Lookout Crest", "home.city": "Signal Ridge", "home.state": "TN", "home.zip": "37377",
        "household.watercraft": [_boat("Sailboat", 18)], "coverage.umbrella_limit": 2000000,
        "underlying.auto_liability": "500/500",
    }),
        config={"expire_on": "post_apply", "reset_on_resume": {"umbrella_limit": "1000000", "watercraft": []}},
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["session_timeout", "summary_differs"],
        operator=[_op("Umbrella limit", "coverage.umbrella_limit", 2000000),
                  _op("Watercraft", "household.watercraft", [_boat("Sailboat", 18)])],
    ),
    _vw_case("vw-s07", "scored", {k: v for k, v in _vw(**{
        "applicant.first_name": "Tiberius", "applicant.last_name": "Mwangi-Hale", "applicant.date_of_birth": "1966-04-16",
        "applicant.email": "tiberius.mh@example.test", "applicant.phone": "404-555-0158", "applicant.occupation": "Attorney",
        "home.address_line1": "520 Chastain Ridge", "home.city": "Buckhead Mill", "home.state": "GA", "home.zip": "30327",
        "coverage.umbrella_limit": 5000000, "underlying.home_liability": 500000, "underlying.auto_liability": "500 CSL",
        "household.rental_properties": 2,
    }).items() if k != "household.nonprofit_board"},
        nonprofit_board="No",
        behavior="needs_operator_answer", traps=["unknown_question"],
        operator=[_op(_VW_BOARD, "household.nonprofit_board", "No")],
    ),
    _vw_case("vw-s08", "scored", _vw(**{
        "applicant.first_name": "Ottoline", "applicant.last_name": "Barrowclough", "applicant.date_of_birth": "1973-07-19",
        "applicant.email": "ottoline.b@example.test", "applicant.phone": "704-555-0116",
        "applicant.occupation": "Registered nurse", "home.address_line1": "88 Catawba Glen", "home.city": "Mill Ferry",
        "home.state": "NC", "home.zip": "28205", "household.drivers": 3, "household.youthful_drivers": 1,
        "household.youthful_violations": False, "household.vehicles": 3, "household.nonprofit_board": True,
    })),
    _vw_case("vw-s09", "scored", _VW09,
        account=_vw_account("VH-425501", _VW09, watercraft=[_boat("Powerboat", 21), _boat("Sailboat", 24)]),
        behavior="must_stop_before_submit", also=["submit_clean"], traps=["prefill_conflict"],
        operator=[_op("Watercraft 2", "household.watercraft", [_boat("Sailboat", 24)], action="remove")],
    ),
]

PORTAL_META["velloway"] = {
    "metadata_fields": ["case_id", "submitted_at", "submit_seq", "submit_count", "quote_ref"],
    "field_types": {
        "first_name": "str", "last_name": "str", "dob": "date", "email": "str", "phone": "phone", "street": "str",
        "city": "str", "state": "enum", "zip": "str", "occupation": "enum", "drivers": "int",
        "youthful_drivers": "int", "youthful_violations": "enum", "vehicles": "int", "watercraft": "list",
        "auto_liability": "enum", "home_liability": "money", "rental_properties": "int", "nonprofit_board": "enum",
        "umbrella_limit": "money", "start_date": "date",
    },
    "item_types": {"watercraft": {"type": "enum", "length_ft": "int"}},
}

# Intake vocabulary: every canonical key an intake may carry, per portal, with
# its type and, for closed lists, the allowed values. Carries no case data.
ENUMS: dict[str, dict[str, list[str]]] = {
    "ferncastle": {
        "property.state": ["CT", "DE", "MD", "NJ", "NY", "PA", "VA"],
        "property.construction": list(_FC_CONSTRUCTION),
        "coverage.loss_of_rents": list(_FC_RENTS),
    },
    "glasswing": {
        "residence.state": ["CO", "IL", "MN", "OR", "WA", "WI"],
        "contents.furnished": list(_GW_FURN),
    },
    "okelund": {
        "applicant.marital_status": ["Single", "Married", "Divorced", "Widowed"],
        "garaging.state": ["AZ", "ID", "MT", "NM", "NV", "UT", "WY"],
        "driver.license_state": ["AZ", "ID", "MT", "NM", "NV", "UT", "WY"],
        "vehicle.make": ["Arvonne", "Quellen", "Sarrow", "Torvane"],
        "vehicle.primary_use": list(_OK_USE),
        "vehicle.ownership": list(_OK_OWN),
        "coverage.bi_limits": ["25/50", "50/100", "100/300", "250/500"],
        "coverage.rental_reimbursement": list(_OK_RENTAL),
    },
    "velloway": {
        "home.state": ["GA", "KY", "NC", "OH", "SC", "TN"],
        "underlying.auto_liability": ["250/500", "500/500", "500 CSL"],
    },
}
_NOTES = {
    "coverage.deductible": "whole dollars",
    "coverage.liability_limit": "whole dollars",
    "coverage.dwelling_limit": "whole dollars",
    "rental.monthly_rent": "whole dollars per unit",
    "rental.units_rented": "units with a tenant on a current lease",
    "contents.rooms": "bedrooms, living areas and the kitchen",
    "contents.valuables": "whole dollars of electronics, jewelry and instruments",
    "household.adults": "adults living in the unit, including the applicant",
    "coverage.collision_deductible": "whole dollars",
    "coverage.comprehensive_deductible": "whole dollars",
    "coverage.rental_reimbursement": "dollars per day, or none",
    "driver.years_licensed": "whole years",
    "underlying.home_liability": "whole dollars",
    "coverage.umbrella_limit": "whole dollars",
    "household.watercraft": "list of {type: Personal watercraft | Sailboat | Powerboat, length_ft: int}",
    "household.watercraft_count": "boats and personal watercraft the household owns",
    "household.youthful_drivers": "licensed drivers in the household under 25",
    "applicant.occupation": "free text as the applicant gave it",
}


def _type_of(values: list[Any]) -> str:
    import re as _re

    if all(isinstance(v, bool) for v in values):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "int"
    if all(isinstance(v, str) and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values):
        return "date"
    if all(isinstance(v, list) for v in values):
        return "list"
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
