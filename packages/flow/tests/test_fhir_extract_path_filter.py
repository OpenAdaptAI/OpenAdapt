"""``extract_path`` reads one element of a FHIR list by a filter.

A Patient keeps its phone and its email in one ``telecom`` list, so the first
element is not always the one a record check means. The values are made up.
"""

from openadapt_flow.runtime.effects.fhir import extract_path

PATIENT = {
    "resourceType": "Patient",
    "name": [{"family": "Okafor", "given": ["Ada", "N"]}],
    "telecom": [
        {"system": "phone", "use": "home", "value": "555-0100"},
        {"system": "email", "value": "ada.okafor@example.invalid"},
    ],
}


def test_a_filter_picks_the_matching_element():
    assert extract_path(PATIENT, "telecom[system=email].value") == (
        "ada.okafor@example.invalid"
    )
    assert extract_path(PATIENT, "telecom[system=phone].value") == "555-0100"


def test_no_matching_element_reads_nothing():
    assert extract_path(PATIENT, "telecom[system=fax].value") is None
    assert extract_path(PATIENT, "telecom[system].value") is None


def test_paths_without_a_filter_are_unchanged():
    assert extract_path(PATIENT, "telecom.value") == "555-0100"
    assert extract_path(PATIENT, "name.family") == "Okafor"
    assert extract_path(PATIENT, "name.given") == "Ada"
    assert extract_path(PATIENT, "missing.path") is None


APPOINTMENT = {
    "resourceType": "Appointment",
    "participant": [
        {"actor": {"reference": "Practitioner/d1"}, "status": "accepted"},
        {"actor": {"reference": "Patient/p1"}, "status": "accepted"},
    ],
}


def test_a_dotted_prefix_filter_picks_by_reference_type():
    path = "participant[actor.reference^=Patient/].actor.reference"
    assert extract_path(APPOINTMENT, path) == "Patient/p1"
    assert (
        extract_path(
            APPOINTMENT, "participant[actor.reference^=Practitioner/].actor.reference"
        )
        == "Practitioner/d1"
    )
    assert extract_path(
        APPOINTMENT, "participant[actor.reference=Patient/p1].status"
    ) == ("accepted")
    assert (
        extract_path(APPOINTMENT, "participant[actor.reference^=Device/].actor") is None
    )


def test_a_filter_on_a_missing_key_matches_nothing():
    # Before, a missing key read as the text "None".
    assert extract_path(PATIENT, "telecom[use=None].value") is None
