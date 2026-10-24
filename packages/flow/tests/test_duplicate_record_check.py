"""The duplicate check with the system of record as its source.

The screen check stops on any listed chart that shares the last name or the
date of birth. This check asks the record channel for the patient and stops
only on a chart that is, or may be, this patient, and on any channel failure.
The FHIR tests run against a real local HTTP server serving OpenEMR-shaped
``Patient`` bundles, page by page.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow.runtime.checks.duplicate_record import (
    FhirPatientSource,
    PatientRecord,
    RestPatientSource,
    SqlPatientSource,
    TableSource,
    check_duplicate,
    classify,
)

WANT = {"fname": "Rosalind", "lname": "Jesswood", "DOB": "1990-04-09"}


def rec(
    fname: str, lname: str, dob: str, rid: str = "1", mrn: str = ""
) -> PatientRecord:
    return PatientRecord(rid, fname, lname, dob, mrn)


@pytest.mark.parametrize(
    ("record", "match"),
    [
        (rec("Rosalind", "Jesswood", "1990-04-09"), "exact"),
        (rec(" ROSALIND ", "jesswood", "1990-04-09"), "exact"),
        (rec("Ana", "Other", "1950-01-01", mrn="P77"), "exact"),  # same MRN
        (rec("Rosalind", "Okafor", "1990-04-09"), "close"),  # married name
        (rec("Rosalind", "Jesswood-Okafor", "1990-04-09"), "close"),  # hyphenated
        (rec("Rosalind", "Jesswood", "1990-09-04"), "close"),  # day and month swapped
        (rec("Rosalind", "Jesswood", "1990-04-10"), "close"),  # a typo in the day
        (rec("Rosalnid", "Jesswood", "1990-04-09"), "close"),  # a typo in the name
        (rec("Rosalind", "Jesswood", ""), "close"),  # the record has no birth date
        (rec("Ana", "Jesswood", "1961-01-01"), None),  # a namesake
        (rec("Ana", "Oakwood", "1990-04-09"), None),  # a shared birth date
        (rec("Rosalind", "Jesswood", "1971-11-23"), None),  # same name, another person
    ],
)
def test_classify(record: PatientRecord, match: Optional[str]) -> None:
    found = classify({**WANT, "mrn": "P77"}, record)
    assert (found.match if found else None) == match


def test_decisions_and_fail_closed_inputs() -> None:
    table = TableSource(
        [
            rec("Ana", "Jesswood", "1961-01-01", "1"),
            rec("Ana", "Oakwood", "1990-04-09", "2"),
        ]
    )
    assert check_duplicate(table, WANT).decision == "none"
    table.records.append(rec("Rosalind", "Jesswood", "1990-04-09", "3"))
    result = check_duplicate(table, WANT)
    assert result.decision == "exact" and result.source == "record"
    assert [c.record.record_id for c in result.candidates] == ["3"]
    # Not an ISO date (intake converts first), or a missing name: stop.
    assert (
        check_duplicate(table, {**WANT, "DOB": "04/09/1990"}).decision == "unreadable"
    )
    assert check_duplicate(table, {**WANT, "lname": " "}).decision == "unreadable"


# -- FHIR over HTTP ---------------------------------------------------------------


def patient(pid: str, given: str, family: str, dob: str) -> dict[str, Any]:
    """Shaped like OpenEMR 8's FHIR Patient (official name, PT identifier)."""

    return {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"use": "official", "family": family, "given": [given]}],
        "birthDate": dob,
        "identifier": [{"type": {"coding": [{"code": "PT"}]}, "value": f"P{pid}"}],
    }


class Server:
    def __init__(
        self, handle: Callable[[str, dict[str, list[str]]], tuple[int, Any]]
    ) -> None:
        outer = self
        self.requests: list[str] = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                outer.requests.append(self.path)
                status, body = handle(parts.path, parse_qs(parts.query))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/fhir"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def serve():
    servers: list[Server] = []

    def make(handle: Callable[[str, dict[str, list[str]]], tuple[int, Any]]) -> Server:
        servers.append(Server(handle))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


PEOPLE = [patient(str(i), "Ana", f"Name{i}", "1990-04-09") for i in range(7)]


def paged(page_size: int, people: list[dict[str, Any]], total: Optional[int] = None):
    def handle(path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        page = int(query.get("_page", ["0"])[0])
        if "birthdate" in query:
            match = [p for p in people if p["birthDate"] == query["birthdate"][0]]
        elif "family" in query:
            f = query["family"][0].lower()
            match = [p for p in people if p["name"][0]["family"].lower().startswith(f)]
        else:
            match = []
        chunk = match[page * page_size : (page + 1) * page_size]
        links = [{"relation": "self", "url": "Patient"}]
        if (page + 1) * page_size < len(match):
            links.append(
                {
                    "relation": "next",
                    "url": f"Patient?_page={page + 1}&"
                    + "&".join(f"{k}={v[0]}" for k, v in query.items() if k != "_page"),
                }
            )
        return 200, {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(match) if total is None else total,
            "link": links,
            "entry": [{"resource": r} for r in chunk],
        }

    return handle


def test_fhir_reads_every_page_and_finds_the_chart_on_the_last(serve) -> None:
    people = PEOPLE + [patient("99", "Rosalind", "Jesswood", "1990-04-09")]
    server = serve(paged(3, people))
    result = check_duplicate(FhirPatientSource(server.url), WANT)
    assert result.decision == "exact"
    assert result.candidates[0].record.mrn == "P99"
    assert result.pages >= 3


def test_fhir_no_match_proceeds(serve) -> None:
    server = serve(paged(3, PEOPLE))
    result = check_duplicate(FhirPatientSource(server.url), WANT)
    assert result.decision == "none", result.reason


@pytest.mark.parametrize(
    "handle",
    [
        paged(3, PEOPLE, total=9),  # fewer read than the server reported
        lambda p, q: (401, {"error": "invalid_token"}),
        lambda p, q: (500, {}),
        lambda p, q: (200, {"resourceType": "OperationOutcome"}),
        lambda p, q: (
            200,
            {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Observation"}}],
            },
        ),
        # A next link that never ends.
        lambda p, q: (
            200,
            {
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": "Patient?again=1"}],
            },
        ),
    ],
)
def test_fhir_fails_closed(serve, handle) -> None:
    server = serve(handle)
    assert check_duplicate(FhirPatientSource(server.url), WANT).decision == "unreadable"


def test_fhir_unreachable_fails_closed() -> None:
    source = FhirPatientSource("http://127.0.0.1:9/fhir", timeout_s=0.5)
    assert check_duplicate(source, WANT).decision == "unreadable"


def test_rest_full_page_without_next_link_fails_closed(serve) -> None:
    rows = [
        {"uuid": str(i), "fname": "Ana", "lname": f"N{i}", "DOB": "1990-04-09"}
        for i in range(5)
    ]
    server = serve(lambda p, q: (200, {"data": rows}))
    source = RestPatientSource(server.url, path="/api/patient", page_size=5)
    assert check_duplicate(source, WANT).decision == "unreadable"
    source = RestPatientSource(server.url, path="/api/patient", page_size=50)
    assert check_duplicate(source, WANT).decision == "none"


def test_sql_fallback(tmp_path: Path) -> None:
    db = tmp_path / "openemr.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE patient_data (pid INTEGER, fname TEXT, lname TEXT, "
        "DOB TEXT, pubpid TEXT)"
    )
    con.execute(
        "INSERT INTO patient_data VALUES (4, 'Rosalind', 'Jesswood-Okafor', "
        "'1990-04-09', '4')"
    )
    con.commit()
    con.close()
    result = check_duplicate(SqlPatientSource(lambda: sqlite3.connect(db)), WANT)
    assert result.decision == "close" and result.channel == "sql"


# -- in the replayer --------------------------------------------------------------


def _replayer_check(source: Any, params: dict[str, str]) -> tuple[bool, str]:
    from openadapt_flow.ir import Postcondition, PostconditionKind
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_no_duplicate_candidate import MARKER, _Backend
    from tests.test_replayer import FakeVision

    # The screen would stop: its list shows a namesake.
    backend = _Backend(["5\tJESSWOOD, Ana\t1961-01-01\tFemale"])
    replayer = Replayer(backend, vision=FakeVision(), duplicate_record_source=source)
    replayer._current_params = dict(params)
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text=MARKER,
        candidate_rows="tr.oneresult",
        candidate_frame_path=("#modalframe",),
        candidate_params=("lname", "DOB"),
    )
    passed = replayer._postcondition_passes(pc, b"", None, {})
    assert backend.read == []  # the screen list is not read
    return passed, replayer._describe_postcondition(pc)


def test_the_replayer_asks_the_record_when_a_channel_is_configured() -> None:
    table = TableSource([rec("Ana", "Jesswood", "1961-01-01", "5")])
    assert _replayer_check(table, WANT)[0] is True
    table.records.append(rec("Rosalind", "Okafor", "1990-04-09", "8"))
    passed, why = _replayer_check(table, WANT)
    assert passed is False
    assert "record check via table: close" in why
    assert "close record 8 (same date of birth and first name" in why


def test_the_replayer_fails_closed_when_the_channel_raises() -> None:
    class Broken:
        channel = "fhir"

        def by_birthdate(self, dob: str) -> list[PatientRecord]:
            raise RuntimeError("boom")

    passed, why = _replayer_check(Broken(), WANT)
    assert passed is False and "unreadable" in why
