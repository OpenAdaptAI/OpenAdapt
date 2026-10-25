"""The generated duplicate rule: misses first, within a false-stop budget.

See ``docs/GENERATED_PARTS.md``. Records are made up.
"""

from __future__ import annotations

import random

from openadapt_flow.generate.identity import adopt, generate_rule, infer_roles
from openadapt_flow.generate.name_variants import are_variants, load

FIRST = [
    "Robert",
    "Elizabeth",
    "Ana",
    "Kofi",
    "Mei",
    "Omar",
    "Petra",
    "Lucia",
    "Cal",
    "Asha",
    "Jordan",
    "Sam",
    "Amara",
    "Desmond",
    "Clarice",
    "Barnaby",
    "Ines",
    "Theo",
    "Grace",
    "Priya",
]
LAST = [
    "Rivera",
    "Okafor",
    "Lindqvist",
    "Tanaka",
    "Haddad",
    "Novak",
    "Mensah",
    "Silva",
    "Brennan",
    "Kaur",
    "Moreau",
    "Feld",
    "Carvalho",
    "Adeyemi",
    "Whitcombe",
    "Raman",
]


def _profile(n: int = 60, seed: int = 3) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        out.append(
            {
                "id": i,
                "fname": rng.choice(FIRST),
                "lname": rng.choice(LAST),
                "DOB": f"19{rng.randrange(40, 99)}-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}",
                "email": f"p{i}@example.invalid",
                "phone_home": f"555-{1000 + i}",
                "street": f"{i} Example Road",
            }
        )
    return out


def test_roles_come_from_names_and_value_shapes() -> None:
    profile = _profile()
    roles = infer_roles(sorted(profile[0]), profile)
    assert (
        roles["first"] == "fname" and roles["last"] == "lname" and roles["dob"] == "DOB"
    )
    assert roles["email"] == "email"


def test_a_bigger_budget_never_misses_more() -> None:
    profile = _profile()
    misses = []
    for budget in (0.002, 0.02, 0.2):
        rule = generate_rule(profile, false_stop_budget=budget, expected_records=500)
        misses.append(rule.calibration.duplicates - rule.calibration.caught)
        assert rule.estimated_false_stops_per_record <= budget
    assert misses == sorted(misses, reverse=True)


def test_the_generic_name_variant_table() -> None:
    table = load()
    assert are_variants("Robert", "Bob", table)
    assert are_variants("bob", "robert", table)
    assert not are_variants("Robert", "Robert", table)


def test_a_nickname_is_caught_with_the_variant_table() -> None:
    profile = _profile()
    rule = generate_rule(profile, false_stop_budget=0.05, expected_records=500)
    robert = next(p for p in profile if p["fname"] == "Robert")
    referral = dict(
        robert, fname="Bob", email="new@example.invalid", phone_home="555-0000"
    )
    decision, _why = rule.decide(referral, profile)
    assert decision != "none"
    without = generate_rule(
        profile, false_stop_budget=0.05, expected_records=500, name_variants=False
    )
    assert without.variants is None


def test_adoption_keeps_a_reference_that_misses_less() -> None:
    profile = _profile()
    rule = generate_rule(profile, false_stop_budget=0.002, expected_records=500)

    def stop_everything(new, table):
        return True

    kept = adopt(rule, stop_everything)
    assert kept.use == "reference"
    assert "reference stays" in kept.why

    def stop_nothing(new, table):
        return False

    assert adopt(rule, stop_nothing).use == "generated"
    assert adopt(rule).use == "generated"


def test_labels_only_remove_evidence_a_person_looked_at() -> None:
    profile = _profile()
    # Six look-alike pairs: same first name and date of birth, new last name.
    for i in range(6):
        twin = dict(
            profile[i], id=1000 + i, lname=f"Married{i}", email=f"t{i}@example.invalid"
        )
        profile.append(twin)
    plain = generate_rule(profile, false_stop_budget=0.02, expected_records=500)
    labelled = generate_rule(
        profile,
        false_stop_budget=0.02,
        expected_records=500,
        labeller=lambda item: True,
    )
    assert plain.calibration.labelled
    assert all(entry["label"] is None for entry in plain.calibration.labelled)
    assert labelled.calibration.caught >= plain.calibration.caught


def test_adoption_needs_a_superset_of_the_reference_catches() -> None:
    """Fewer misses is not enough: missing one the reference stops refuses."""

    from types import SimpleNamespace

    from openadapt_flow.generate.identity_template import adopt_mapping

    gen = {"missed": 1, "missed_cases": [7], "false_stops": 0}
    ref = {"missed": 2, "missed_cases": [3, 4], "false_stops": 0}
    assert adopt_mapping(gen, ref).use == "reference"
    assert adopt_mapping({**gen, "missed_cases": [3]}, ref).use == "generated"

    rule = generate_rule(_profile(), false_stop_budget=0.02, expected_records=500)
    scores = iter(
        [
            {"missed": 1, "missed_cases": [7], "duplicates": 10},
            {"missed": 2, "missed_cases": [3, 4], "duplicates": 10},
        ]
    )
    rule.calibration_set = SimpleNamespace(score=lambda decide: next(scores))
    kept = adopt(rule, lambda new, table: True)
    assert kept.use == "reference" and "reference catches" in kept.why
