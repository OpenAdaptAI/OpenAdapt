"""Write synthetic X12 278 responses and 277 claim statuses.

Written for these tests from the public segment structure (ISA/GS/ST
envelopes, HL loops, TRN, NM1, HCR, STC, REF, DTP, SV1/SVC). Every member,
provider and payer number is synthetic. No implementation-guide text or
vendor file is copied here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ELEMENT, COMPONENT, TERMINATOR = "*", ":", "~"


def _isa(control: str, date: str, time: str) -> str:
    fields = [
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        "SYNTHCLEARING".ljust(15),
        "ZZ",
        "SYNTHCLINIC".ljust(15),
        date[2:],
        time,
        "^",
        "00501",
        control.rjust(9, "0"),
        "0",
        "T",
        COMPONENT,
    ]
    isa = ELEMENT.join(fields)
    assert len(isa) == 105, len(isa)
    return isa


def interchange(
    kind: str,
    sets: list[list[str]],
    *,
    control: str,
    date: str = "20261102",
    time: str = "1200",
) -> str:
    """One ISA/IEA with one GS/GE holding ``sets`` (each a list of segments
    between ST and SE, without them)."""

    group = "1"
    code = {"278": "HI", "277": "HN"}[kind]
    version = {"278": "005010X217", "277": "005010X212"}[kind]
    out = [
        _isa(control, date, time),
        ELEMENT.join(
            [
                "GS",
                code,
                "SYNTHCLEARING",
                "SYNTHCLINIC",
                date,
                time,
                group,
                "X",
                version,
            ]
        ),
    ]
    for n, body in enumerate(sets, start=1):
        st = f"{n:04d}"
        segs = [ELEMENT.join(["ST", kind, st, version])] + body
        segs.append(ELEMENT.join(["SE", str(len(segs) + 1), st]))
        out.extend(segs)
    out.append(ELEMENT.join(["GE", str(len(sets)), group]))
    out.append(ELEMENT.join(["IEA", "1", control.rjust(9, "0")]))
    return TERMINATOR.join(out) + TERMINATOR + "\n"


def _d8(iso: str) -> str:
    return iso.replace("-", "")


@dataclass
class Review:
    """One 278 response: a payer's review of one service."""

    trace: str
    member_id: str
    service_code: str
    service_date: str  # ISO
    decision: str = "A1"  # HCR01: A1 certified, A3 not certified, A4 pended
    auth_number: Optional[str] = None
    payer_id: str = "SYNPAYER01"
    provider_npi: str = "1999999984"


def review_set(r: Review) -> list[str]:
    seg = lambda *e: ELEMENT.join(e)  # noqa: E731
    body = [
        seg("BHT", "0007", "11", r.trace, "20261102", "1200"),
        seg("HL", "1", "", "20", "1"),
        seg("NM1", "X3", "2", "SYNTHETIC PAYER", "", "", "", "", "PI", r.payer_id),
        seg("HL", "2", "1", "21", "1"),
        seg("NM1", "1P", "2", "SYNTHETIC CLINIC", "", "", "", "", "XX", r.provider_npi),
        seg("HL", "3", "2", "22", "1"),
        seg("NM1", "IL", "1", "SYNTH", "MEMBER", "", "", "", "MI", r.member_id),
        seg("HL", "4", "3", "EV", "1"),
        seg("TRN", "2", r.trace, "9SYNTHCLIN"),
        seg("UM", "HS", "I", "2"),
        seg("HCR", r.decision, r.auth_number or ""),
        seg("DTP", "472", "D8", _d8(r.service_date)),
        seg("HL", "5", "4", "SS", "0"),
        seg("SV1", f"HC{COMPONENT}{r.service_code}", "", "UN", "1"),
        seg("HCR", r.decision, r.auth_number or ""),
    ]
    if r.auth_number:
        body.append(seg("REF", "BB", r.auth_number))
    return body


@dataclass
class ClaimStatus:
    """One 277 claim status: what the payer says about one claim."""

    trace: str  # the 837 CLM01 (patient control number)
    member_id: str
    service_code: str
    service_date: str
    category: str = "A1"  # STC01-1: A1 received, F1 finalized/paid
    status: str = "20"
    payer_claim_number: Optional[str] = None
    billed: str = "125.00"
    payer_id: str = "SYNPAYER01"
    provider_npi: str = "1999999984"


def status_set(claims: list[ClaimStatus]) -> list[str]:
    seg = lambda *e: ELEMENT.join(e)  # noqa: E731
    first = claims[0]
    body = [
        seg("BHT", "0010", "08", "SYNTH277", "20261102", "1200", "DG"),
        seg("HL", "1", "", "20", "1"),
        seg("NM1", "PR", "2", "SYNTHETIC PAYER", "", "", "", "", "PI", first.payer_id),
        seg("HL", "2", "1", "21", "1"),
        seg("NM1", "41", "2", "SYNTHCLEARING", "", "", "", "", "46", "SYNTHCH"),
        seg("HL", "3", "2", "19", "1"),
        seg(
            "NM1",
            "1P",
            "2",
            "SYNTHETIC CLINIC",
            "",
            "",
            "",
            "",
            "XX",
            first.provider_npi,
        ),
    ]
    hl = 3
    for c in claims:
        hl += 1
        body += [
            seg("HL", str(hl), "3", "22", "0"),
            seg("NM1", "IL", "1", "SYNTH", "MEMBER", "", "", "", "MI", c.member_id),
            seg("TRN", "2", c.trace),
            seg("STC", f"{c.category}{COMPONENT}{c.status}", "20261102", "", c.billed),
        ]
        if c.payer_claim_number:
            body.append(seg("REF", "1K", c.payer_claim_number))
        body += [
            seg("DTP", "472", "D8", _d8(c.service_date)),
            seg("SVC", f"HC{COMPONENT}{c.service_code}", c.billed, "0"),
        ]
    return body
