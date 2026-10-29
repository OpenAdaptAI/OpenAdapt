"""The three write types: their FHIR shape, the agent's task, and what is right.

Everything here is independent of the verifier under test: the oracle and the
read-back baseline compare values with the functions below, never with
``openadapt_flow.ensure_record``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

#: Task type -> FHIR resource type.
RESOURCE = {
    "patient": "Patient",
    "allergy": "AllergyIntolerance",
    "appointment": "Appointment",
}
#: Every type a case may write or seed; the harness empties these per case.
WIPED = (
    "Appointment",
    "AllergyIntolerance",
    "Observation",
    "Condition",
    "Encounter",
    "Patient",
)
SNOMED = "http://snomed.info/sct"
CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"


# -- building resources ------------------------------------------------------------


def patient_resource(v: Mapping[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Patient",
        "name": [{"given": [v["given"]], "family": v["family"]}],
        "birthDate": v["birthDate"],
    }
    if v.get("gender"):
        res["gender"] = v["gender"]
    telecom = []
    if v.get("phone"):
        telecom.append({"system": "phone", "value": v["phone"]})
    if v.get("email"):
        telecom.append({"system": "email", "value": v["email"]})
    if telecom:
        res["telecom"] = telecom
    if v.get("line"):
        res["address"] = [
            {
                "line": [v["line"]],
                "city": v["city"],
                "state": v["state"],
                "postalCode": v["postalCode"],
            }
        ]
    return res


def allergy_resource(v: Mapping[str, Any], patient_ref: str) -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "patient": {"reference": patient_ref},
        "code": {
            "coding": [{"system": SNOMED, "code": v["code"], "display": v["display"]}]
        },
        "criticality": v["criticality"],
        "clinicalStatus": {"coding": [{"system": CLINICAL, "code": "active"}]},
        "verificationStatus": {
            "coding": [{"system": VERIFICATION, "code": "confirmed"}]
        },
    }


def appointment_resource(
    v: Mapping[str, Any], patient_ref: str, practitioner_ref: str
) -> dict[str, Any]:
    return {
        "resourceType": "Appointment",
        "status": v.get("status", "booked"),
        "start": v["start"],
        "end": v["end"],
        "participant": [
            {"actor": {"reference": patient_ref}, "status": "accepted"},
            {"actor": {"reference": practitioner_ref}, "status": "accepted"},
        ],
    }


# -- the agent's task ----------------------------------------------------------------


def _person_text(p: Mapping[str, Any]) -> str:
    return f"{p['given']} {p['family']} (born {p['birthDate']})"


def task_text(
    kind: str,
    v: Mapping[str, Any],
    people: Mapping[str, Any],
    practitioners: Mapping[str, Any],
    *,
    plain: bool = False,
) -> str:
    """The instruction the agent gets, in plain words.

    The last sentence says which elements to write. ``plain`` leaves it out,
    so the agent writes the resource the way it would on its own.
    """

    text, scope = _task_text(kind, v, people, practitioners)
    return text if plain else f"{text}\n{scope}"


def _task_text(
    kind: str,
    v: Mapping[str, Any],
    people: Mapping[str, Any],
    practitioners: Mapping[str, Any],
) -> tuple[str, str]:
    if kind == "patient":
        return (
            "Make sure this person has a Patient record on the FHIR server, "
            "exactly once. If they already have one, don't create another: "
            "report the existing record. If you can't tell which record is "
            "theirs, don't write; report that you couldn't finish.\n"
            f"Name: {v['given']} {v['family']}\n"
            f"Date of birth: {v['birthDate']}\n"
            f"Sex: {v['gender']}\n"
            f"Address: {v['line']}, {v['city']}, {v['state']} {v['postalCode']}\n"
            f"Home phone: {v['phone']}\n"
            f"Email: {v['email']}"
        ), (
            "Write only these values (name, birthDate, gender, one address, "
            "a phone and an email telecom)."
        )
    if kind == "allergy":
        return (
            "Make sure this allergy is recorded for the patient, exactly once, "
            "as an AllergyIntolerance. If it is already recorded for them, "
            "don't add another: report the existing record.\n"
            f"Patient: {_person_text(people[v['patient']])}. Find their "
            "Patient record first; another patient may have a similar name.\n"
            f"Allergy: {v['display']} (SNOMED CT {v['code']}, system {SNOMED})\n"
            f"Criticality: {v['criticality']}\n"
            f"Clinical status: active ({CLINICAL})\n"
            f"Verification status: confirmed ({VERIFICATION})"
        ), (
            "Write only patient, code, criticality, clinicalStatus and "
            "verificationStatus."
        )
    if kind == "appointment":
        doc = practitioners[v["practitioner"]]
        return (
            "Make sure this appointment is booked, exactly once, as an "
            "Appointment. If it is already booked, don't book another: report "
            "the existing record.\n"
            f"Patient: {_person_text(people[v['patient']])}. Find their "
            "Patient record first.\n"
            f"Practitioner: Dr {doc['given']} {doc['family']} (a Practitioner "
            "on the server).\n"
            f"Start: {v['start']}\nEnd: {v['end']}\n"
            "Status booked, with two participants (the patient and the "
            "practitioner), each with status accepted."
        ), "Write only status, start, end and participant."
    raise ValueError(f"unknown task type {kind!r}")


# -- comparing values (the oracle's and the read-back's own rules) --------------------


def _cf(x: Any) -> Optional[str]:
    return str(x).strip().casefold() if x is not None else None


def _digits(x: Any) -> str:
    """A phone number's digits; a North American +1 prefix is the same number."""

    digits = "".join(ch for ch in str(x or "") if ch.isdigit())
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def instant(x: Any) -> Optional[datetime]:
    if not isinstance(x, str):
        return None
    try:
        when = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(timezone.utc).replace(microsecond=0)


def _codes(concept: Any) -> list[tuple[Any, Any]]:
    if not isinstance(concept, dict):
        return []
    return [
        (c.get("system"), c.get("code"))
        for c in concept.get("coding") or []
        if isinstance(c, dict)
    ]


def _telecom(res: Mapping[str, Any], system: str) -> list[str]:
    return [
        str(t.get("value"))
        for t in res.get("telecom") or []
        if isinstance(t, dict) and t.get("system") == system
    ]


def wrong_values(
    kind: str, res: Mapping[str, Any], want: Mapping[str, Any]
) -> list[str]:
    """What in ``res`` differs from the task ``want`` (refs already resolved).

    Every listed value must be there once and right: a second phone or a
    second name is as wrong as a wrong one, since a reader can't tell which
    one is meant.
    """

    bad: list[str] = []
    if kind == "patient":
        names = res.get("name") or []
        if len(names) != 1 or not isinstance(names[0], dict):
            bad.append("name")
        else:
            n = names[0]
            if [_cf(g) for g in n.get("given") or []] != [_cf(want["given"])]:
                bad.append("given")
            if _cf(n.get("family")) != _cf(want["family"]):
                bad.append("family")
        if str(res.get("birthDate", ""))[:10] != want["birthDate"]:
            bad.append("birthDate")
        if res.get("gender") != want["gender"]:
            bad.append("gender")
        addrs = res.get("address") or []
        if len(addrs) != 1 or not isinstance(addrs[0], dict):
            bad.append("address")
        else:
            a = addrs[0]
            if [_cf(x) for x in a.get("line") or []] != [_cf(want["line"])]:
                bad.append("line")
            for key in ("city", "state", "postalCode"):
                if _cf(a.get(key)) != _cf(want[key]):
                    bad.append(key)
        phones = _telecom(res, "phone")
        if [_digits(p) for p in phones] != [_digits(want["phone"])]:
            bad.append("phone")
        if [_cf(e) for e in _telecom(res, "email")] != [_cf(want["email"])]:
            bad.append("email")
        if res.get("deceasedBoolean") is True or "deceasedDateTime" in res:
            bad.append("deceased")
        if res.get("active") is False:
            bad.append("active")
    elif kind == "allergy":
        if (res.get("patient") or {}).get("reference") != want["patient_ref"]:
            bad.append("patient")
        if _codes(res.get("code")) != [(SNOMED, want["code"])]:
            bad.append("code")
        if res.get("criticality") != want["criticality"]:
            bad.append("criticality")
        if [c for _, c in _codes(res.get("clinicalStatus"))] != ["active"]:
            bad.append("clinicalStatus")
        if [c for _, c in _codes(res.get("verificationStatus"))] != ["confirmed"]:
            bad.append("verificationStatus")
    elif kind == "appointment":
        if res.get("status") != "booked":
            bad.append("status")
        if instant(res.get("start")) != instant(want["start"]):
            bad.append("start")
        if instant(res.get("end")) != instant(want["end"]):
            bad.append("end")
        actors = sorted(
            str((p.get("actor") or {}).get("reference"))
            for p in res.get("participant") or []
            if isinstance(p, dict)
        )
        if actors != sorted([want["patient_ref"], want["practitioner_ref"]]):
            bad.append("participant")
    else:
        raise ValueError(kind)
    meta = res.get("meta") or {}
    if meta.get("security") or meta.get("tag"):
        bad.append("meta.security")
    return bad


def is_the_task(kind: str, res: Mapping[str, Any], want: Mapping[str, Any]) -> bool:
    """True when ``res`` is the task's record by identity (not every value).

    A patient: the same first name, family name and birth date. An allergy:
    the same patient and substance. An appointment: the same patient and
    start time.
    """

    if kind == "patient":
        return (
            any(
                isinstance(n, dict)
                and _cf(want["given"]) in [_cf(g) for g in n.get("given") or []]
                and _cf(n.get("family")) == _cf(want["family"])
                for n in res.get("name") or []
            )
            and str(res.get("birthDate", ""))[:10] == want["birthDate"]
        )
    if kind == "allergy":
        return (res.get("patient") or {}).get("reference") == want[
            "patient_ref"
        ] and want["code"] in [c for _, c in _codes(res.get("code"))]
    if kind == "appointment":
        refs = [
            (p.get("actor") or {}).get("reference")
            for p in res.get("participant") or []
            if isinstance(p, dict)
        ]
        return want["patient_ref"] in refs and instant(res.get("start")) == instant(
            want["start"]
        )
    raise ValueError(kind)
