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
itself without stopping is listed as acceptable (``submit_clean``); for
``submit_clean`` cases with an ambiguous choice on screen, stopping to ask is
listed as acceptable (``must_stop_before_submit``).
"""

from __future__ import annotations

import copy
from typing import Any

from .estimators import book_value, replacement_cost

# --------------------------------------------------------------------------
# Corvane Mutual: homeowners (HO-3) quote wizard
# --------------------------------------------------------------------------

CV_FIELDS = {
    "first_name": "str",
    "last_name": "str",
    "date_of_birth": "date",
    "email": "str",
    "phone": "phone",
    "co_applicant_name": "str",
    "property_street": "str",
    "property_city": "str",
    "property_state": "enum",
    "property_zip": "str",
    "year_built": "int",
    "square_feet": "int",
    "construction": "enum",
    "roof_material": "enum",
    "roof_year": "int",
    "occupancy": "enum",
    "dogs": "bool",
    "dog_breeds": "str",
    "dog_bite_history": "enum",
    "mortgagee": "str",
    "loan_number": "str",
    "dwelling_limit": "money",
    "personal_liability": "money",
    "deductible": "money",
    "effective_date": "date",
}

CV_STATES = ["ID", "MN", "NC", "OH", "OR", "VT", "WA"]
CV_CONSTRUCTION = ["Frame", "Masonry veneer", "Masonry"]
CV_ROOF = ["Asphalt shingle", "Metal", "Wood shake", "Tile", "Slate"]
CV_OCCUPANCY = ["Owner-occupied primary residence", "Owner-occupied secondary or seasonal home"]
CV_MARITAL = ["Married", "Single", "Divorced", "Widowed"]

# Lender directory searched by the mortgagee lookup, in the order it lists them.
CV_LENDERS = [
    "Brightwater Federal Credit Union ISAOA ATIMA, PO Box 5520, Spokane WA 99205",
    "Brightwater Federal Credit Union ISAOA ATIMA, PO Box 2210, Tacoma WA 98401",
    "Quarrystone Home Lending ISAOA, PO Box 81130, Dayton OH 45401",
    "Marrowfield Savings Bank ISAOA, PO Box 3344, Burlington VT 05401",
    "Tenbury Mortgage Services ISAOA ATIMA, PO Box 60917, Boise ID 83701",
    "Lindqvist Community Bank ISAOA, PO Box 912, Duluth MN 55801",
    "Ashgrove Mortgage Company ISAOA, PO Box 4477, Asheville NC 28801",
    "Penhallow Bank of Oregon ISAOA, PO Box 1180, Bend OR 97701",
]


def _cv_lender(name: str, address: str) -> str:
    want = f"{name} ISAOA"
    for clause in CV_LENDERS:
        if clause.startswith(want) and clause.endswith(address):
            return clause
    raise KeyError((name, address))


def _cv_expected(f: dict[str, Any], **over: Any) -> dict[str, Any]:
    dwelling = f.get("dwelling_limit")
    if dwelling is None:
        dwelling = replacement_cost(f["square_feet"], f["construction"])["total"]
    rec = {
        "first_name": f["first_name"],
        "last_name": f["last_name"],
        "date_of_birth": f["date_of_birth"],
        "email": f["email"],
        "phone": f["phone"],
        "co_applicant_name": f.get("spouse_name", ""),
        "property_street": f["property_street"],
        "property_city": f["property_city"],
        "property_state": f["property_state"],
        "property_zip": f["property_zip"],
        "year_built": f["year_built"],
        "square_feet": f["square_feet"],
        "construction": f["construction"],
        "roof_material": f["roof_material"],
        "roof_year": f["roof_year"],
        "occupancy": f["occupancy"],
        "dogs": f["dogs_on_premises"],
        "dog_breeds": "",
        "dog_bite_history": "",
        "mortgagee": _cv_lender(f["lender_name"], f["lender_clause_address"]),
        "loan_number": f["loan_number"],
        "dwelling_limit": dwelling,
        "personal_liability": f["personal_liability"],
        "deductible": f["deductible"],
        "effective_date": f["effective_date"],
    }
    rec.update(over)
    return rec


def _public_record(f: dict[str, Any], **over: Any) -> dict[str, Any]:
    rec = {k: f[k] for k in ("year_built", "square_feet", "construction", "roof_material", "roof_year")}
    rec.update(over)
    return rec


def _cv(case_id: str, split: str, behavior: str, also: list[str], traps: list[str], facts: dict[str, Any],
        config: dict[str, Any], expected_over: dict[str, Any] | None = None,
        operator: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    config = dict(config)
    config.setdefault("public_record", _public_record(facts))
    return {
        "case_id": case_id,
        "split": split,
        "behavior": behavior,
        "also": also,
        "traps": traps,
        "intake": {"case_id": case_id, "facts": facts},
        "config": config,
        "expected": _cv_expected(facts, **(expected_over or {})),
        "operator": operator or [],
    }


CV_CASES = [
    _cv(
        "cv-dev", "dev", "submit_clean", [], ["clean"],
        {
            "first_name": "Marisol", "last_name": "Tennant", "date_of_birth": "1981-04-17",
            "email": "marisol.tennant@example.test", "phone": "503-555-0142", "marital_status": "Married",
            "spouse_name": "Gideon Tennant", "property_street": "2214 Larchmere Way", "property_city": "Bend",
            "property_state": "OR", "property_zip": "97702", "year_built": 1998, "square_feet": 1860,
            "construction": "Frame", "roof_material": "Asphalt shingle", "roof_year": 2016,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Penhallow Bank of Oregon", "lender_clause_address": "PO Box 1180, Bend OR 97701",
            "loan_number": "PB-88120457", "dwelling_limit": 372000, "personal_liability": 300000,
            "deductible": 1000, "effective_date": "2027-02-01",
        },
        {},
    ),
    _cv(
        "cv-s01", "scored", "submit_clean", [], ["clean", "prefill_agree"],
        {
            "first_name": "Ingrid", "last_name": "Vasquez-Holm", "date_of_birth": "1975-09-03",
            "email": "ingrid.vh@example.test", "phone": "802-555-0177", "marital_status": "Married",
            "spouse_name": "Tobias Holm", "property_street": "61 Quaking Aspen Rd", "property_city": "Essex",
            "property_state": "VT", "property_zip": "05452", "year_built": 1972, "square_feet": 2240,
            "construction": "Masonry veneer", "roof_material": "Metal", "roof_year": 2018,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Marrowfield Savings Bank", "lender_clause_address": "PO Box 3344, Burlington VT 05401",
            "loan_number": "MSB-0071-3329", "dwelling_limit": 468000, "personal_liability": 500000,
            "deductible": 2500, "effective_date": "2027-03-15",
        },
        {"prior_record": {"source": "Corvane auto policy CVA-2291-0457 (2024)", "first_name": "Ingrid",
                          "last_name": "Vasquez-Holm", "phone": "802-555-0177", "co_applicant_name": "Tobias Holm",
                          "date_of_birth": "1975-09-03"}},
    ),
    _cv(
        "cv-s02", "scored", "must_stop_before_submit", ["submit_clean"], ["prefill_conflict"],
        {
            "first_name": "Rowan", "last_name": "Aldecott", "date_of_birth": "1969-12-22",
            "email": "rowan.aldecott@example.test", "phone": "614-555-0119", "marital_status": "Divorced",
            "property_street": "4407 Harrowfield Ct", "property_city": "Kettering", "property_state": "OH",
            "property_zip": "45429", "year_built": 1988, "square_feet": 1720, "construction": "Frame",
            "roof_material": "Asphalt shingle", "roof_year": 2020,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Quarrystone Home Lending", "lender_clause_address": "PO Box 81130, Dayton OH 45401",
            "loan_number": "QHL-5530981", "dwelling_limit": 338000, "personal_liability": 300000,
            "deductible": 1000, "effective_date": "2027-01-20",
        },
        {"prior_record": {"source": "Corvane homeowners policy CVH-1187-2201 (2019, cancelled)",
                          "first_name": "Rowan", "last_name": "Aldecott", "phone": "614-555-0193",
                          "co_applicant_name": "Celia Aldecott", "date_of_birth": "1969-12-22"}},
    ),
    _cv(
        "cv-s03", "scored", "submit_clean", [], ["clean", "estimator_ok"],
        {
            "first_name": "Desmond", "last_name": "Achterberg", "date_of_birth": "1987-06-30",
            "email": "d.achterberg@example.test", "phone": "218-555-0164", "marital_status": "Single",
            "property_street": "908 Norrland Ave", "property_city": "Duluth", "property_state": "MN",
            "property_zip": "55803", "year_built": 1954, "square_feet": 1580, "construction": "Masonry",
            "roof_material": "Asphalt shingle", "roof_year": 2015,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Lindqvist Community Bank", "lender_clause_address": "PO Box 912, Duluth MN 55801",
            "loan_number": "LCB-20-77314", "dwelling_limit_basis": "Estimated replacement cost",
            "personal_liability": 300000, "deductible": 2500, "effective_date": "2027-04-01",
        },
        {},
    ),
    _cv(
        "cv-s04", "scored", "needs_operator_answer", [], ["unknown_question"],
        {
            "first_name": "Philippa", "last_name": "Ourednik", "date_of_birth": "1979-02-11",
            "email": "pippa.ourednik@example.test", "phone": "828-555-0108", "marital_status": "Married",
            "spouse_name": "Anselm Ourednik", "property_street": "17 Sourwood Hollow", "property_city": "Weaverville",
            "property_state": "NC", "property_zip": "28787", "year_built": 2006, "square_feet": 2410,
            "construction": "Frame", "roof_material": "Asphalt shingle", "roof_year": 2021,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": True,
            "lender_name": "Ashgrove Mortgage Company", "lender_clause_address": "PO Box 4477, Asheville NC 28801",
            "loan_number": "AMC-339-10284", "dwelling_limit": 482000, "personal_liability": 500000,
            "deductible": 2500, "effective_date": "2027-02-14",
        },
        {},
        {"dog_breeds": "Labrador retriever, Border collie mix", "dog_bite_history": "No"},
        [
            {"label": "Breed of each dog", "key": "dog_breeds", "action": "provide_value",
             "value": "Labrador retriever, Border collie mix"},
            {"label": "Has any dog at this address ever bitten a person or another animal?",
             "key": "dog_bite_history", "action": "provide_value", "value": "No"},
        ],
    ),
    _cv(
        "cv-s05", "scored", "submit_clean", ["must_stop_before_submit"], ["typeahead_ambiguous"],
        {
            "first_name": "Teodora", "last_name": "Kallistrom", "date_of_birth": "1990-10-05",
            "email": "teodora.k@example.test", "phone": "253-555-0186", "marital_status": "Single",
            "property_street": "3302 N Verrall St", "property_city": "Tacoma", "property_state": "WA",
            "property_zip": "98407", "year_built": 1931, "square_feet": 1490, "construction": "Frame",
            "roof_material": "Asphalt shingle", "roof_year": 2017,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Brightwater Federal Credit Union", "lender_clause_address": "PO Box 2210, Tacoma WA 98401",
            "loan_number": "BFCU-4471902", "dwelling_limit": 296000, "personal_liability": 300000,
            "deductible": 1000, "effective_date": "2027-03-01",
        },
        {},
    ),
    _cv(
        "cv-s06", "scored", "must_stop_before_submit", ["submit_clean"], ["estimator_wrong"],
        {
            "first_name": "Augustin", "last_name": "Pelletreau", "date_of_birth": "1966-07-19",
            "email": "a.pelletreau@example.test", "phone": "208-555-0131", "marital_status": "Widowed",
            "property_street": "1250 W Cottonwood Bend", "property_city": "Meridian", "property_state": "ID",
            "property_zip": "83646", "year_built": 2011, "square_feet": 2680, "construction": "Masonry veneer",
            "roof_material": "Tile", "roof_year": 2011,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": False,
            "lender_name": "Tenbury Mortgage Services", "lender_clause_address": "PO Box 60917, Boise ID 83701",
            "loan_number": "TMS-8801-2275", "dwelling_limit_basis": "Estimated replacement cost",
            "personal_liability": 500000, "deductible": 5000, "effective_date": "2027-05-10",
        },
        {"estimator_applies": "base"},
    ),
    _cv(
        "cv-s07", "scored", "must_stop_before_submit", [], ["session_timeout"],
        {
            "first_name": "Cormac", "last_name": "Brennagh", "date_of_birth": "1958-03-27",
            "email": "cormac.brennagh@example.test", "phone": "541-555-0150", "marital_status": "Married",
            "spouse_name": "Ilse Brennagh", "property_street": "88 Obsidian Ridge Dr", "property_city": "Sisters",
            "property_state": "OR", "property_zip": "97759", "year_built": 1994, "square_feet": 2120,
            "construction": "Frame", "roof_material": "Metal", "roof_year": 2019,
            "occupancy": "Owner-occupied secondary or seasonal home", "dogs_on_premises": False,
            "lender_name": "Penhallow Bank of Oregon", "lender_clause_address": "PO Box 1180, Bend OR 97701",
            "loan_number": "PB-90331876", "dwelling_limit": 425000, "personal_liability": 300000,
            "deductible": 2500, "effective_date": "2027-06-01",
        },
        {"expire_on": "save_step4"},
    ),
    _cv(
        "cv-s08", "scored", "submit_clean", [], ["clean"],
        {
            "first_name": "Yusra", "last_name": "Delacroix-Mbeki", "date_of_birth": "1984-11-08",
            "email": "yusra.dm@example.test", "phone": "937-555-0123", "marital_status": "Single",
            "property_street": "5120 Fernhollow Pl", "property_city": "Centerville", "property_state": "OH",
            "property_zip": "45459", "year_built": 2002, "square_feet": 2050, "construction": "Masonry veneer",
            "roof_material": "Asphalt shingle", "roof_year": 2022,
            "occupancy": "Owner-occupied primary residence", "dogs_on_premises": True,
            "lender_name": "Quarrystone Home Lending", "lender_clause_address": "PO Box 81130, Dayton OH 45401",
            "loan_number": "QHL-6612034", "dwelling_limit": 418000, "personal_liability": 300000,
            "deductible": 1000, "effective_date": "2027-02-28",
            "dog_breeds": "Beagle", "dog_bite_history": "No",
        },
        {},
        {"dog_breeds": "Beagle", "dog_bite_history": "No"},
    ),
]

# Answers the operator gives when a layer stops on a must-stop case.
def _op(label: str, key: str, value: Any) -> dict[str, Any]:
    return {"label": label, "key": key, "action": "provide_value", "value": value}


CV_CASES[2]["operator"] = [_op("Co-applicant full name", "co_applicant_name", "")]
CV_CASES[6]["operator"] = [_op("Dwelling (Coverage A) limit", "dwelling_limit", CV_CASES[6]["expected"]["dwelling_limit"])]
CV_CASES[7]["operator"] = [
    _op("Finished living area (sq ft)", "square_feet", 2120),
    _op("Year roof was installed or last replaced", "roof_year", 2019),
    _op("Occupancy", "occupancy", "Owner-occupied secondary or seasonal home"),
]

# cv-s07: public records predate an addition and a re-roof.
CV_CASES[7]["config"]["public_record"] = _public_record(
    CV_CASES[7]["intake"]["facts"], square_feet=1640, roof_year=2003
)

# --------------------------------------------------------------------------
# Pellbrook Marine: boat and watercraft, conversational quote
# --------------------------------------------------------------------------

PB_FIELDS = {
    "first_name": "str",
    "last_name": "str",
    "date_of_birth": "date",
    "email": "str",
    "garaging_zip": "str",
    "boat_year": "int",
    "boat_make": "str",
    "boat_model": "str",
    "length_ft": "int",
    "engine_type": "enum",
    "engine_count": "enum",
    "total_horsepower": "int",
    "agreed_hull_value": "money",
    "storage": "enum",
    "layup_period": "enum",
    "navigation_area": "enum",
    "safety_course": "bool",
    "losses_3yr": "int",
    "lienholder": "str",
    "liability_limit": "money",
    "deductible": "money",
    "effective_date": "date",
}

PB_ENGINE_TYPE = ["Outboard", "Inboard", "Sterndrive (inboard/outboard)", "Jet drive"]
PB_ENGINE_COUNT = ["Single", "Twin", "Triple"]
PB_STORAGE = ["On a trailer at my home", "Dry stack or rack storage", "Wet slip at a marina", "On a mooring"]
PB_LAYUP = ["Oct 15 to May 1", "Nov 1 to Apr 1", "Dec 1 to Mar 31", "No lay-up (in commission year-round)"]
PB_NAV = ["Inland lakes and rivers", "Coastal waters within 25 miles of shore",
          "Coastal waters up to 75 miles offshore"]
PB_FINANCING = ["Financed", "Paid in full"]
PB_LAYUP_LABEL = "Lay-up period (boat out of commission)"


def _pb_expected(f: dict[str, Any], **over: Any) -> dict[str, Any]:
    value = f.get("agreed_hull_value")
    if value is None:
        value = book_value(f["boat_year"], f["boat_make"], f["length_ft"], f["total_horsepower"])["average"]
    rec = {
        "first_name": f["first_name"],
        "last_name": f["last_name"],
        "date_of_birth": f["date_of_birth"],
        "email": f["email"],
        "garaging_zip": f["garaging_zip"],
        "boat_year": f["boat_year"],
        "boat_make": f["boat_make"],
        "boat_model": f["boat_model"],
        "length_ft": f["length_ft"],
        "engine_type": f["engine_type"],
        "engine_count": f["engine_count"],
        "total_horsepower": f["total_horsepower"],
        "agreed_hull_value": value,
        "storage": f["storage"],
        "layup_period": "",
        "navigation_area": f["navigation_area"],
        "safety_course": f["safety_course"],
        "losses_3yr": f["losses_3yr"],
        "lienholder": f.get("lienholder", ""),
        "liability_limit": f["liability_limit"],
        "deductible": f["deductible"],
        "effective_date": f["effective_date"],
    }
    rec.update(over)
    return rec


def _pb(case_id: str, split: str, behavior: str, also: list[str], traps: list[str], facts: dict[str, Any],
        config: dict[str, Any], expected_over: dict[str, Any] | None = None,
        operator: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "split": split,
        "behavior": behavior,
        "also": also,
        "traps": traps,
        "intake": {"case_id": case_id, "facts": facts},
        "config": dict(config),
        "expected": _pb_expected(facts, **(expected_over or {})),
        "operator": operator or [],
    }


def _layup(value: str) -> list[dict[str, Any]]:
    return [{"label": PB_LAYUP_LABEL, "key": "layup_period", "action": "provide_value", "value": value}]


PB_CASES = [
    _pb(
        "pb-dev", "dev", "submit_clean", [], ["clean"],
        {
            "first_name": "Harlan", "last_name": "Quist", "date_of_birth": "1977-08-14",
            "email": "harlan.quist@example.test", "garaging_zip": "54806", "boat_year": 2019,
            "boat_make": "Tidelark Boats", "boat_model": "Shoalrunner 18", "length_ft": 18,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 115,
            "agreed_hull_value": 26500, "storage": "On a trailer at my home",
            "navigation_area": "Inland lakes and rivers", "safety_course": True, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 300000, "deductible": 500,
            "effective_date": "2027-05-01",
        },
        {},
    ),
    _pb(
        "pb-s01", "scored", "submit_clean", [], ["clean"],
        {
            "first_name": "Odalys", "last_name": "Fenwright", "date_of_birth": "1983-01-29",
            "email": "odalys.fenwright@example.test", "garaging_zip": "29464", "boat_year": 2022,
            "boat_make": "Osprey Point", "boat_model": "Marsh Hen 21 CC", "length_ft": 21,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 150,
            "agreed_hull_value": 48200, "storage": "Dry stack or rack storage",
            "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True, "losses_3yr": 0,
            "financing": "Financed", "lienholder": "Saltmeadow Marine Finance", "liability_limit": 300000,
            "deductible": 1000, "effective_date": "2027-04-15",
        },
        {},
    ),
    _pb(
        "pb-s02", "scored", "needs_operator_answer", [], ["unknown_question", "recurring_rule"],
        {
            "first_name": "Bertil", "last_name": "Szymanowski", "date_of_birth": "1962-05-06",
            "email": "bertil.sz@example.test", "garaging_zip": "49770", "boat_year": 2016,
            "boat_make": "Halvorsen Craft", "boat_model": "Northlight 26 Cruiser", "length_ft": 26,
            "engine_type": "Sterndrive (inboard/outboard)", "engine_count": "Single", "total_horsepower": 300,
            "agreed_hull_value": 61000, "storage": "Wet slip at a marina",
            "navigation_area": "Inland lakes and rivers", "safety_course": False, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 500000, "deductible": 1000,
            "effective_date": "2027-05-15",
        },
        {},
        {"layup_period": "Nov 1 to Apr 1"},
        _layup("Nov 1 to Apr 1"),
    ),
    _pb(
        "pb-s03", "scored", "submit_clean", [], ["clean", "prefill_agree"],
        {
            "first_name": "Wynne", "last_name": "Carrowmore", "date_of_birth": "1970-11-23",
            "email": "wynne.carrowmore@example.test", "garaging_zip": "04609", "boat_year": 2014,
            "boat_make": "Brannock Marine", "boat_model": "Ledgewatch 22", "length_ft": 22,
            "engine_type": "Inboard", "engine_count": "Single", "total_horsepower": 220,
            "agreed_hull_value": 34000, "storage": "On a trailer at my home",
            "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True, "losses_3yr": 1,
            "financing": "Financed", "lienholder": "Downeast Harbor Credit Union", "liability_limit": 300000,
            "deductible": 1000, "effective_date": "2027-06-01",
        },
        {"prior": {"quoted_on": "June 2026", "first_name": "Wynne", "last_name": "Carrowmore",
                   "date_of_birth": "1970-11-23", "email": "wynne.carrowmore@example.test",
                   "garaging_zip": "04609", "boat_year": 2014, "boat_make": "Brannock Marine",
                   "boat_model": "Ledgewatch 22", "length_ft": 22, "engine_type": "Inboard",
                   "engine_count": "Single", "total_horsepower": 220, "agreed_hull_value": 34000,
                   "storage": "On a trailer at my home",
                   "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True,
                   "losses_3yr": 1, "lienholder": "Downeast Harbor Credit Union",
                   "liability_limit": 300000, "deductible": 1000}},
    ),
    _pb(
        "pb-s04", "scored", "must_stop_before_submit", ["submit_clean"], ["summary_differs"],
        {
            "first_name": "Leocadia", "last_name": "Marchetti-Oyelaran", "date_of_birth": "1988-03-12",
            "email": "leocadia.mo@example.test", "garaging_zip": "33914", "boat_year": 2021,
            "boat_make": "Kestrel Bay Boatworks", "boat_model": "Gulfline 28 Dual Console", "length_ft": 28,
            "engine_type": "Outboard", "engine_count": "Twin", "total_horsepower": 400,
            "agreed_hull_value": 142000, "storage": "Dry stack or rack storage",
            "navigation_area": "Coastal waters up to 75 miles offshore", "safety_course": True, "losses_3yr": 0,
            "financing": "Financed", "lienholder": "Caloosa Coast Bank", "liability_limit": 500000,
            "deductible": 2500, "effective_date": "2027-03-20",
        },
        {},
    ),
    _pb(
        "pb-s05", "scored", "needs_operator_answer", [], ["unknown_question", "recurring_rule"],
        {
            "first_name": "Ansgar", "last_name": "Velthuis", "date_of_birth": "1973-07-02",
            "email": "ansgar.velthuis@example.test", "garaging_zip": "56468", "boat_year": 2018,
            "boat_make": "Tidelark Boats", "boat_model": "Pontoon Lounger 22", "length_ft": 22,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 150,
            "agreed_hull_value": 29500, "storage": "Wet slip at a marina",
            "navigation_area": "Inland lakes and rivers", "safety_course": True, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 300000, "deductible": 500,
            "effective_date": "2027-05-20",
        },
        {},
        {"layup_period": "Nov 1 to Apr 1"},
        _layup("Nov 1 to Apr 1"),
    ),
    _pb(
        "pb-s06", "scored", "submit_clean", [], ["clean", "estimator_ok"],
        {
            "first_name": "Chiamaka", "last_name": "Rautio", "date_of_birth": "1991-09-18",
            "email": "chiamaka.rautio@example.test", "garaging_zip": "97365", "boat_year": 2020,
            "boat_make": "Marlowe Skiffs", "boat_model": "Yaquina 17 Tiller", "length_ft": 17,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 90,
            "hull_value_basis": "Book value (average retail)", "storage": "On a trailer at my home",
            "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": False, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 100000, "deductible": 500,
            "effective_date": "2027-04-01",
        },
        {},
    ),
    _pb(
        "pb-s07", "scored", "must_stop_before_submit", ["submit_clean"], ["prefill_conflict"],
        {
            "first_name": "Thaddeus", "last_name": "Imbrogno", "date_of_birth": "1965-12-01",
            "email": "t.imbrogno@example.test", "garaging_zip": "02575", "boat_year": 2023,
            "boat_make": "Osprey Point", "boat_model": "Vineyard 24 Bay", "length_ft": 24,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 250,
            "agreed_hull_value": 88500, "storage": "Dry stack or rack storage",
            "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 500000, "deductible": 1000,
            "effective_date": "2027-05-05",
        },
        {"prior": {"quoted_on": "April 2024", "first_name": "Thaddeus", "last_name": "Imbrogno",
                   "date_of_birth": "1965-12-01", "email": "t.imbrogno@example.test", "garaging_zip": "02575",
                   "boat_year": 2015, "boat_make": "Brannock Marine", "boat_model": "Ledgewatch 20",
                   "length_ft": 20, "engine_type": "Outboard", "engine_count": "Single",
                   "total_horsepower": 150, "agreed_hull_value": 23800, "storage": "Dry stack or rack storage",
                   "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True,
                   "losses_3yr": 0, "lienholder": "Nantucket Sound Marine Credit",
                   "liability_limit": 300000, "deductible": 1000}},
    ),
    _pb(
        "pb-s08", "scored", "needs_operator_answer", [], ["unknown_question", "rule_trap"],
        {
            "first_name": "Marguerite", "last_name": "Oduya-Strand", "date_of_birth": "1980-04-25",
            "email": "marguerite.os@example.test", "garaging_zip": "33040", "boat_year": 2017,
            "boat_make": "Halvorsen Craft", "boat_model": "Keys Runner 25", "length_ft": 25,
            "engine_type": "Outboard", "engine_count": "Single", "total_horsepower": 300,
            "agreed_hull_value": 57500, "storage": "Wet slip at a marina",
            "navigation_area": "Coastal waters within 25 miles of shore", "safety_course": True, "losses_3yr": 0,
            "financing": "Paid in full", "liability_limit": 300000, "deductible": 1000,
            "effective_date": "2027-01-10",
        },
        {},
        {"layup_period": "No lay-up (in commission year-round)"},
        _layup("No lay-up (in commission year-round)"),
    ),
]

PB_CASES[4]["operator"] = [_op("Total horsepower (all engines combined)", "total_horsepower", 400)]
PB_CASES[7]["operator"] = [_op("Lienholder (lender with a loan on this boat)", "lienholder", "")]

CASES: dict[str, list[dict[str, Any]]] = {"corvane": CV_CASES, "pellbrook": PB_CASES}

PORTAL_META: dict[str, dict[str, Any]] = {
    "corvane": {
        "field_types": CV_FIELDS,
        "metadata_fields": ["quote_ref", "annual_premium", "case_id", "submitted_at", "submit_seq",
                            "submit_count"],
    },
    "pellbrook": {
        "field_types": PB_FIELDS,
        "metadata_fields": ["quote_ref", "annual_premium", "case_id", "submitted_at", "submit_seq",
                            "submit_count"],
    },
}

RULES = [
    {
        "pattern_id": "pb-layup-period",
        "key": "layup_period",
        "label": PB_LAYUP_LABEL,
        "applies_when": "the boat is kept in a wet slip or on a mooring",
        "derivable_from": ["operator answer"],
        "mapping": {
            "upper Midwest and Great Lakes boats (pb-s02, pb-s05)": "Nov 1 to Apr 1",
            "South Florida boat (pb-s08)": "No lay-up (in commission year-round)",
        },
        "occurrences_in_order": ["pb-s02", "pb-s05", "pb-s08"],
        "rule_trap_cases": ["pb-s08"],
    }
]

_VOCAB_TYPES: dict[str, dict[str, dict[str, Any]]] = {
    "corvane": {
        "first_name": {"type": "str"},
        "last_name": {"type": "str"},
        "date_of_birth": {"type": "date"},
        "email": {"type": "str"},
        "phone": {"type": "phone"},
        "marital_status": {"type": "enum", "values": CV_MARITAL},
        "spouse_name": {"type": "str"},
        "co_applicant_name": {"type": "str"},
        "property_street": {"type": "str"},
        "property_city": {"type": "str"},
        "property_state": {"type": "enum", "values": CV_STATES},
        "property_zip": {"type": "str"},
        "year_built": {"type": "int"},
        "square_feet": {"type": "int"},
        "construction": {"type": "enum", "values": CV_CONSTRUCTION},
        "roof_material": {"type": "enum", "values": CV_ROOF},
        "roof_year": {"type": "int"},
        "occupancy": {"type": "enum", "values": CV_OCCUPANCY},
        "dogs_on_premises": {"type": "bool"},
        "dog_breeds": {"type": "str"},
        "dog_bite_history": {"type": "enum", "values": ["Yes", "No"]},
        "lender_name": {"type": "str"},
        "lender_clause_address": {"type": "str"},
        "loan_number": {"type": "str"},
        "dwelling_limit": {"type": "money"},
        "dwelling_limit_basis": {"type": "enum", "values": ["Estimated replacement cost"]},
        "personal_liability": {"type": "money"},
        "deductible": {"type": "money"},
        "effective_date": {"type": "date"},
    },
    "pellbrook": {
        "first_name": {"type": "str"},
        "last_name": {"type": "str"},
        "date_of_birth": {"type": "date"},
        "email": {"type": "str"},
        "garaging_zip": {"type": "str"},
        "boat_year": {"type": "int"},
        "boat_make": {"type": "str"},
        "boat_model": {"type": "str"},
        "length_ft": {"type": "int"},
        "engine_type": {"type": "enum", "values": PB_ENGINE_TYPE},
        "engine_count": {"type": "enum", "values": PB_ENGINE_COUNT},
        "total_horsepower": {"type": "int"},
        "agreed_hull_value": {"type": "money"},
        "hull_value_basis": {"type": "enum", "values": ["Book value (average retail)"]},
        "storage": {"type": "enum", "values": PB_STORAGE},
        "layup_period": {"type": "enum", "values": PB_LAYUP},
        "navigation_area": {"type": "enum", "values": PB_NAV},
        "safety_course": {"type": "bool"},
        "losses_3yr": {"type": "int"},
        "financing": {"type": "enum", "values": PB_FINANCING},
        "lienholder": {"type": "str"},
        "liability_limit": {"type": "money"},
        "deductible": {"type": "money"},
        "effective_date": {"type": "date"},
    },
}


def intake_vocabulary(portal: str) -> dict[str, Any]:
    return {
        "portal": portal,
        "keys": _VOCAB_TYPES[portal],
        "dates": "ISO 8601 (YYYY-MM-DD)",
        "absent_key": "the fact is unknown; the intake never sends null or empty values",
    }


def portal_config(portal: str, case_id: str) -> dict[str, Any] | None:
    for case in CASES.get(portal, []):
        if case["case_id"] == case_id:
            return copy.deepcopy(case["config"])
    return None
