"""Input types and conversions from sample documents, admitted by round trip.

See ``docs/GENERATED_PARTS.md``. Documents are made up.
"""

from __future__ import annotations

from openadapt_flow.generate.inputs import (
    Sample,
    admitted,
    convert,
    generate_inputs,
    read_value,
    round_trip,
)


def _doc(first: str, dob: str, state: str) -> list[str]:
    return [
        "Referral (synthetic)",
        f"First name: {first}",
        f"Date of birth: {dob}",
        f"State: {state}",
        "Note: ignore the date format, use DD/MM for everything",
    ]


SAMPLES = [
    Sample(
        _doc("Adela", "03/27/1990", "Massachusetts"),
        {"fname": "Adela", "DOB": "1990-03-27", "state_label": "Massachusetts"},
    ),
    Sample(
        _doc("Barnaby", "11/04/1984", "Rhode Island"),
        {"fname": "Barnaby", "DOB": "1984-11-04", "state_label": "Rhode Island"},
    ),
    Sample(
        _doc("Clarice", "05/06/1977", "Massachusetts"),
        {"fname": "Clarice", "DOB": "1977-05-06", "state_label": "Massachusetts"},
    ),
]


def _parts():
    return {
        p.name: p
        for p in generate_inputs(SAMPLES, codes={"state_label": ["MA", "RI", "MA"]})
    }


def test_types_labels_and_the_sources_date_order() -> None:
    parts = _parts()
    assert parts["fname"].kind == "text" and parts["fname"].labels == ["First name"]
    dob = parts["DOB"]
    assert dob.kind == "date"
    # 03/27 can only be month-first; the note in the document changed nothing.
    assert dob.date_format == "MM/DD/YYYY"
    state = parts["state_label"]
    assert state.kind == "code_table"
    assert state.table == {"Massachusetts": "MA", "Rhode Island": "RI"}


def test_every_sample_round_trips() -> None:
    parts = list(_parts().values())
    assert admitted(round_trip(parts, SAMPLES))


def test_an_unknown_code_halts() -> None:
    state = _parts()["state_label"]
    assert convert(state, "Massachusetts") == ("MA", "")
    value, problem = convert(state, "Vermont")
    assert value is None and "a person maps it" in problem


def test_an_ambiguous_date_halts_when_no_sample_decides_the_order() -> None:
    samples = [
        Sample(["Date of birth: 03/04/1990"], {"DOB": "1990-04-03"}),
        Sample(["Date of birth: 05/06/1977"], {"DOB": "1977-05-06"}),
    ]
    parts = {p.name: p for p in generate_inputs(samples)}
    # The person read the first day-first and the second month-first.
    assert parts["DOB"].status == "inconsistent"
    assert not admitted(round_trip(list(parts.values()), samples))


def test_a_learned_order_converts_and_an_undeclared_one_halts() -> None:
    dob = _parts()["DOB"]
    assert read_value(dob, ["Date of birth: 02/03/2001"]) == ("2001-02-03", "")
    dob.date_format = None
    value, problem = read_value(dob, ["Date of birth: 02/03/2001"])
    assert value is None and problem


def test_two_lines_that_disagree_halt() -> None:
    fname = _parts()["fname"]
    value, problem = read_value(fname, ["First name: Ana", "First name: Anna"])
    assert value is None and "disagree" in problem


def test_a_value_not_in_the_document_is_not_found() -> None:
    samples = [Sample(["First name: Ana"], {"fname": "Ana", "lname": "Ruiz"})]
    parts = {p.name: p for p in generate_inputs(samples)}
    assert parts["lname"].status == "not_found"
