"""Identity as a vetted template plus a generated per-site mapping.

See ``benchmark/generated_parts/README.md``. Records are made up.
"""

from __future__ import annotations

import sys
import types

import pytest

from openadapt_flow.generate.identity_template import (
    TEMPLATES,
    Template,
    TemplateUnavailable,
    adopt_mapping,
    check_demos,
    generate_mapping,
    get_path,
    perturbation_suite,
    run_suite,
)

PEOPLE = [
    ("Adela", "Okafor", "1990-03-27", "555-0101", "adela@example.invalid", "1 A Road"),
    (
        "Barnaby",
        "Lindqvist",
        "1984-11-04",
        "555-0102",
        "barnaby@example.invalid",
        "2 B Road",
    ),
    (
        "Clarice",
        "Tanaka",
        "1977-05-16",
        "555-0103",
        "clarice@example.invalid",
        "3 C Road",
    ),
    (
        "Desmond",
        "Haddad",
        "1966-08-02",
        "555-0104",
        "desmond@example.invalid",
        "4 D Road",
    ),
]


def _typed(p) -> dict:
    return {
        "fname": p[0],
        "lname": p[1],
        "DOB": p[2],
        "phone_home": p[3],
        "email": p[4],
        "street": p[5],
    }


def _rest(i, p) -> dict:
    m, d, y = p[2][5:7], p[2][8:10], p[2][:4]
    return {
        "pid": str(i),
        "fname": p[0],
        "lname": p[1],
        "DOB": f"{m}/{d}/{y}",
        "pubpid": f"MRN{i}",
        "phone_home": p[3],
        "email": p[4],
        "street": p[5],
        "city": p[5],
    }


def _fhir(i, p) -> dict:
    return {
        "resourceType": "Patient",
        "id": str(i),
        "name": [
            {"use": "official", "family": p[1], "given": [p[0]]},
            {"use": "old", "family": f"Maiden{i}", "given": [p[0]]},
        ],
        "birthDate": p[2],
        "telecom": [
            {"system": "phone", "value": p[3]},
            {"system": "email", "value": p[4]},
        ],
        "address": [{"line": [p[5]]}],
    }


def test_a_rest_mapping_is_grounded_on_the_demonstrations() -> None:
    rows = [_rest(i, p) for i, p in enumerate(PEOPLE)]
    demos = [(_typed(p), rows[i]) for i, p in enumerate(PEOPLE[:3])]
    mapping = generate_mapping(rows, demos)
    assert mapping.record["fname"] == "fname" and mapping.record["mrn"] == "pubpid"
    assert mapping.record["street"] == "street"  # not "city", which holds the same text
    # Dates are stored month-first; the demonstrations decide it.
    assert mapping.dob_format == "MM/DD/YYYY"
    assert mapping.record_slots(rows[0])["dob"] == "1990-03-27"
    assert check_demos(mapping, demos) == {k: 3 for k in mapping.want}


def test_a_fhir_mapping_picks_coded_list_items() -> None:
    rows = [_fhir(i, p) for i, p in enumerate(PEOPLE)]
    demos = [(_typed(p), rows[i]) for i, p in enumerate(PEOPLE[:3])]
    mapping = generate_mapping(rows, demos)
    assert mapping.record["email"] == "telecom[system=email].value"
    assert mapping.record["lname"] == "name[use=official].family"
    assert mapping.record["prior_lname"] == "name[use=old].family"
    assert get_path(rows[0], "telecom[system=phone].value") == "555-0101"


def test_a_field_whose_values_dont_look_right_is_not_mapped() -> None:
    """Grounded but wrong shape: a street is never the email."""

    rows = [
        {k: v for k, v in _rest(i, p).items() if k != "email"}
        for i, p in enumerate(PEOPLE)
    ]
    demos = [({**_typed(p), "email": p[5]}, rows[i]) for i, p in enumerate(PEOPLE[:3])]
    mapping = generate_mapping(rows, demos)
    assert "email" not in mapping.record


def test_the_suite_and_the_adoption_rule() -> None:
    people = [_typed(p) for p in PEOPLE]
    cases = perturbation_suite(
        people, lambda who: who, seed=0, nicknames={"barnaby": ["Barney"]}
    )
    kinds = {c["kind"].split("+")[0] for c in cases}
    assert {
        "distinct",
        "first_typo",
        "names_swapped",
        "first_nickname",
        "only_email_kept",
    } <= kinds

    def exact_only(want, rows):
        return (
            "exact"
            if any(
                r["fname"] == want["fname"] and r["lname"] == want["lname"]
                for r in rows
            )
            else "none"
        )

    result = run_suite(cases, people, exact_only)
    assert result["missed"] > 0 and result["false_stops"] == 0
    better = dict(result, missed=0)
    assert adopt_mapping(result, better).use == "reference"
    assert adopt_mapping(better, result).use == "generated"
    assert adopt_mapping(result, None).use == "generated"


def test_the_template_is_reviewed_code_loaded_by_name(monkeypatch) -> None:
    name = TEMPLATES["patient-identity/1"]
    monkeypatch.setitem(sys.modules, name, None)  # not installed
    with pytest.raises(TemplateUnavailable):
        Template("patient-identity/1")
    with pytest.raises(TemplateUnavailable):
        Template("made-up/9")
    fake = types.SimpleNamespace(
        PatientRecord=lambda **kw: kw,
        TableSource=list,
        check_duplicate=lambda src, want: types.SimpleNamespace(decision="none"),
    )
    monkeypatch.setitem(sys.modules, name, fake)  # type: ignore[arg-type]
    t = Template("patient-identity/1")
    assert t.record({"fname": "A", "prior_lname": "B"})["prior_names"] == (("A", "B"),)
    assert t.decide_records({"fname": "A"}, []) == "none"
