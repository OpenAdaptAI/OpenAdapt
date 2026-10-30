"""Run 3's item stream: 300 items, in one fixed order for both arms.

    stream = build(faultset, seed=2031)

The mix is run 2's at N = 300 (``incremental_value/PREREGISTRATION_2.md``):

- 168 ordinary tasks, a new person or slot each, with no fault;
- 80 blind fault cases, from the private fault set, as written;
- 52 harness perturbations, 13 of each kind below. Each leaves a correct end
  state when the agent does the task right:

  - ``lost_response``: the create lands and the agent gets a 504;
  - ``concurrent_staff``: another clinician writes another patient's record
    just before the agent's first write lands;
  - ``delayed_staff``: another clinician writes another patient's record
    after the agent has finished and before the check reads the server;
  - ``restart``: the agent's connection is cut after its first write of the
    task's type lands, and a fresh agent, with no memory of the first, is
    started on the same task.

Every item is a case in ``openadapt.medplum-public-bench/v1`` form, with
the extra keys run 3's driver reads (``source``, ``restart``, ``other`` with
``at: after_agent``). The order is shuffled with ``seed``; the same seed
gives the same stream.
"""

from __future__ import annotations

import copy
import random
from typing import Any, Mapping

N_ORDINARY = 168
N_PER_PERTURBATION = 13
PERTURBATIONS = ("lost_response", "concurrent_staff", "delayed_staff", "restart")
TYPES = ("patient", "allergy", "appointment")

#: The practitioners and defaults FAULT_BRIEF_3.md promised the fault author.
PRACTITIONERS = {
    "dr_osei": {"given": "Ama", "family": "Osei"},
    "dr_lind": {"given": "Petra", "family": "Lind"},
}
DEFAULTS = {
    "patient": {
        "given": "Lena",
        "family": "Okafor",
        "birthDate": "1988-04-22",
        "gender": "female",
        "line": "14 Birch Lane",
        "city": "Springfield",
        "state": "MA",
        "postalCode": "01103",
        "phone": "413-555-0187",
        "email": "lena.okafor@example.org",
    },
    "allergy": {
        "patient": "rosa",
        "code": "91936005",
        "display": "Allergy to penicillin",
        "criticality": "high",
    },
    "appointment": {
        "patient": "tomasz",
        "practitioner": "dr_osei",
        "start": "2026-11-17T14:00:00Z",
        "end": "2026-11-17T14:30:00Z",
    },
}
PEOPLE = {
    "rosa": {
        "given": "Rosa",
        "family": "Delgado",
        "gender": "female",
        "birthDate": "1979-06-14",
    },
    "tomasz": {
        "given": "Tomasz",
        "family": "Wren",
        "gender": "male",
        "birthDate": "1966-01-30",
    },
}

# Invented values for ordinary items (555 numbers, example.org addresses).
GIVEN = {
    "female": ["Ada", "Bea", "Cleo", "Dina", "Esme", "Faye", "Gia", "Hana", "Iris"],
    "male": ["Abel", "Bram", "Cyrus", "Dov", "Emil", "Finn", "Gus", "Hugo", "Ivo"],
}
FAMILY = [
    "Achebe",
    "Bergstrom",
    "Castellano",
    "Dunmore",
    "Eskildsen",
    "Farouk",
    "Gallardo",
    "Holloway",
    "Ishikawa",
    "Jovanovic",
    "Kowalczyk",
    "Lindqvist",
    "Mbeki",
    "Nakashima",
    "Oyelaran",
    "Petrakis",
]
PLACES = [
    ("Springfield", "MA", "011"),
    ("Hartford", "CT", "061"),
    ("Providence", "RI", "029"),
    ("Burlington", "VT", "054"),
    ("Albany", "NY", "122"),
]
STREETS = ["Birch Lane", "Mill Road", "Cedar Court", "Harbor Street", "Orchard Way"]
ALLERGIES = [
    ("91936005", "Allergy to penicillin"),
    ("300916003", "Latex allergy"),
    ("418689008", "Allergy to grass pollen"),
    ("91935009", "Allergy to peanut"),
    ("294505008", "Allergy to amoxicillin"),
    ("232347008", "Dander (animal) allergy"),
]


def _person(rng: random.Random) -> dict[str, str]:
    gender = rng.choice(["female", "male"])
    year, month, day = rng.randint(1940, 2004), rng.randint(1, 12), rng.randint(1, 28)
    return {
        "given": rng.choice(GIVEN[gender]),
        "family": rng.choice(FAMILY),
        "gender": gender,
        "birthDate": f"{year:04d}-{month:02d}-{day:02d}",
    }


def _ordinary(i: int, kind: str, rng: random.Random) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": f"ord_{i:03d}",
        "type": kind,
        "class": "ordinary",
        "expect": "create",
        "source": "ordinary",
    }
    p = _person(rng)
    name = f"ord_{i:03d}_patient"
    if kind == "patient":
        city, state, zip3 = rng.choice(PLACES)
        case["task"] = {
            **p,
            "line": f"{rng.randint(2, 980)} {rng.choice(STREETS)}",
            "city": city,
            "state": state,
            "postalCode": f"{zip3}{rng.randint(0, 99):02d}",
            "phone": f"{rng.randint(201, 989)}-555-01{rng.randint(0, 99):02d}",
            "email": f"{p['given']}.{p['family']}@example.org".lower(),
        }
        return case
    case["people"] = {name: p}
    case["seed"] = [{"patient": {"as": name, "person": name}}]
    if kind == "allergy":
        code, display = rng.choice(ALLERGIES)
        case["task"] = {
            "patient": name,
            "code": code,
            "display": display,
            "criticality": rng.choice(["high", "low"]),
        }
        return case
    day, hour, minutes = rng.randint(1, 28), rng.randint(8, 16), rng.choice([0, 30])
    start = f"2026-12-{day:02d}T{hour:02d}:{minutes:02d}:00Z"
    end_h, end_m = (hour, 30) if minutes == 0 else (hour + 1, 0)
    case["task"] = {
        "patient": name,
        "practitioner": rng.choice(sorted(PRACTITIONERS)),
        "start": start,
        "end": f"2026-12-{day:02d}T{end_h:02d}:{end_m:02d}:00Z",
    }
    return case


def _perturbed(i: int, kind: str, what: str, rng: random.Random) -> dict[str, Any]:
    case = _ordinary(i, kind, rng)
    case.update(
        id=f"per_{what}_{i:02d}", **{"class": f"perturbation_{what}"}, source=what
    )
    other_person = {**_person(rng)}
    other = f"per_{what}_{i:02d}_other"
    case.setdefault("people", {})[other] = other_person
    case.setdefault("seed", []).append({"patient": {"as": other, "person": other}})
    staff = {
        "allergy": {
            "patient": other,
            "code": "300916003",
            "display": "Latex allergy",
            "criticality": "low",
        }
    }
    if what == "lost_response":
        case["faults"] = [{"drop_response": True}]
    elif what == "concurrent_staff":
        case["other"] = [{**staff, "at_write": True}]
    elif what == "delayed_staff":
        case["other"] = [{**staff, "at": "after_agent"}]
    elif what == "restart":
        case["restart"] = True
    return case


def build(faultset: Mapping[str, Any], *, seed: int = 2031) -> dict[str, Any]:
    """The whole stream, as a case set the driver runs in order."""

    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    for i in range(N_ORDINARY):
        items.append(_ordinary(i + 1, TYPES[i % len(TYPES)], rng))
    n = 0
    for what in PERTURBATIONS:
        for j in range(N_PER_PERTURBATION):
            n += 1
            items.append(_perturbed(n, TYPES[n % len(TYPES)], what, rng))
    people = {**PEOPLE, **copy.deepcopy(faultset.get("people") or {})}
    for case in faultset["cases"]:
        blind = copy.deepcopy(case)
        blind["source"] = "blind"
        blind["id"] = f"blind_{case['id']}"
        items.append(blind)
    rng.shuffle(items)
    for k, item in enumerate(items, 1):
        item["item"] = f"iv3-{k:03d}"
        people.update(item.pop("people", {}) or {})
    return {
        "schema": "openadapt.medplum-public-bench/v1",
        "practitioners": copy.deepcopy(PRACTITIONERS),
        "people": people,
        "defaults": copy.deepcopy(DEFAULTS),
        "cases": items,
    }
