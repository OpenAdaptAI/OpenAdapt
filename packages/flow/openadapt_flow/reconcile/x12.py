"""X12 277 and 278 files as a system of record: the clearinghouse leg.

A clearinghouse carries a prior authorization as an X12 278 and a claim's
status as a 277. This module reads a folder of those files (the
clearinghouse's inbound mirror, or files a person exported) and flattens
each unit of work in them into one record:

- a 278: one record per patient event (``HL`` code ``EV``), with its trace,
  member, the first service's code, date and review decision (``HCR``), and
  the authorization number;
- a 277: one record per claim status (each ``TRN`` under a subscriber or
  dependent ``HL``), with its trace, member, status category and code, the
  payer's claim number, service date and first service line code.

:class:`X12Verifier` hands those records to the verifier's shared
``judge_records``, so a 277 or 278 is judged with exactly the rules a FHIR
or SQL read is. It is registered as the plugin kind ``x12``::

    verifier:
      kind: x12
      options: {root: /srv/clearinghouse/inbound, glob: "*.x12", transaction: "278"}

Fail closed: a file that isn't X12, an ``ISA`` that isn't 106 characters, a
control number or segment count that doesn't match its trailer, or a
missing root makes the whole read unreadable (INDETERMINATE), never a
partial set. Nothing here writes or calls the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.adapter import (
    VerifierAdapterBase,
    register_verifier_factory,
    resolve_verifier_factory,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectState, EffectVerdict
from openadapt_flow.verification import VerificationTier

#: More files than this reads as unreadable, never as the first ones.
MAX_FILES = 5000
#: A file larger than this reads as unreadable.
MAX_BYTES = 8 * 1024 * 1024
TRANSACTIONS = ("277", "278")


class X12Error(ValueError):
    """The file isn't a complete, well-formed X12 interchange."""


def _date(qualifier: str, value: str) -> tuple[Optional[str], Optional[str]]:
    """A ``DTP`` date as ISO ``YYYY-MM-DD`` (start, end)."""

    def iso(text: str) -> str:
        try:
            if len(text) != 8 or not text.isdigit():
                raise ValueError
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            raise X12Error("a date is not a valid CCYYMMDD") from None

    if qualifier == "D8":
        day = iso(value)
        return day, day
    if qualifier == "RD8":
        start, sep, end = value.partition("-")
        if not sep:
            raise X12Error("a date range has no '-'")
        return iso(start), iso(end)
    raise X12Error("a date format is not D8 or RD8")


def segments(text: str) -> list[list[str]]:
    """The interchange's segments, each split into elements.

    The delimiters come from the ``ISA``: the element separator is its 4th
    character, the component separator its 105th and the segment
    terminator its 106th. Line breaks after a terminator are allowed.
    """

    text = text.lstrip("﻿ \r\n\t")
    if not text.startswith("ISA") or len(text) < 106:
        raise X12Error("the file does not start with a 106-character ISA")
    element, terminator = text[3], text[105]
    if text.find(terminator) != 105:
        raise X12Error("the ISA is not exactly 106 characters")
    if element.isalnum() or terminator.isalnum() or element == terminator:
        raise X12Error("the ISA delimiters are not usable")
    out: list[list[str]] = []
    for raw in text.split(terminator):
        raw = raw.strip("\r\n")
        if not raw.strip():
            continue
        out.append(raw.split(element))
    if len(out[0]) != 17:
        raise X12Error("the ISA does not have 16 elements")
    return out


def _check_envelopes(
    segs: list[list[str]],
) -> list[tuple[str, str, str, list[list[str]]]]:
    """Each transaction set as (group control, ST control, its segments).

    Every trailer must count and name what it closes: ``SE01`` the
    segments from ``ST`` to ``SE``, ``SE02`` the ``ST02``; ``GE`` the
    transaction sets and ``GS06``; ``IEA`` the groups and ``ISA13``.
    """

    isa = segs[0]
    if segs[-1][0] != "IEA":
        raise X12Error("the interchange has no IEA trailer")
    out: list[tuple[str, str, str, list[list[str]]]] = []
    groups = 0
    i = 1
    while i < len(segs) - 1:
        gs = segs[i]
        if gs[0] != "GS" or len(gs) < 7:
            raise X12Error("a functional group does not start with GS")
        groups += 1
        sets = 0
        i += 1
        while i < len(segs) and segs[i][0] == "ST":
            start = i
            st = segs[i]
            while i < len(segs) and segs[i][0] != "SE":
                if segs[i][0] in ("ST", "GE", "IEA") and i != start:
                    raise X12Error("a transaction set has no SE trailer")
                i += 1
            if i >= len(segs):
                raise X12Error("a transaction set has no SE trailer")
            se = segs[i]
            body = segs[start : i + 1]
            if len(st) < 3 or len(se) < 3:
                raise X12Error("an ST or SE is short")
            if se[1] != str(len(body)) or se[2] != st[2]:
                raise X12Error("an SE does not count or name its transaction set")
            out.append((gs[6], st[2], f"{gs[4]}{gs[5][:4]}", body))
            sets += 1
            i += 1
        if i >= len(segs) or segs[i][0] != "GE":
            raise X12Error("a functional group has no GE trailer")
        ge = segs[i]
        if len(ge) < 3 or ge[1] != str(sets) or ge[2] != gs[6]:
            raise X12Error("a GE does not count or name its group")
        i += 1
    iea = segs[-1]
    if len(iea) < 3 or iea[1] != str(groups) or iea[2] != isa[13]:
        raise X12Error("IEA does not count the groups or name the ISA13")
    return out


def _code(composite: str, component: str) -> Optional[str]:
    """The code in a service composite such as ``HC:97110``."""

    parts = composite.split(component)
    return parts[1] if len(parts) > 1 and parts[1] else None


def _records(body: list[list[str]], component: str, ref: str) -> list[dict[str, Any]]:
    """Flatten one 277 or 278 transaction set into its units of work."""

    kind = body[0][1]
    bht = next((s for s in body if s[0] == "BHT"), None)
    purpose = bht[2] if bht is not None and len(bht) > 2 else None
    #: HL id -> (parent id, level code), and what each level named.
    tree: dict[str, tuple[str, str]] = {}
    names: dict[str, dict[str, str]] = {}
    records: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    hl = ""
    owner = ""  # the HL that opened ``current``
    person = ""  # the HL of the last subscriber or patient named

    def ancestors(node: str) -> list[str]:
        chain = []
        while node and node in tree and node not in chain:
            chain.append(node)
            node = tree[node][0]
        return chain

    def inherited(name: str) -> Optional[str]:
        for node in ancestors(hl):
            if name in names.get(node, {}):
                return names[node][name]
        return None

    def open_record() -> dict[str, Any]:
        rec: dict[str, Any] = {
            "id": f"{ref}.{len(records) + 1}",
            "transaction": kind,
            "purpose": purpose,
            "level": tree.get(hl, ("", ""))[1],
            "trace": None,
            "trace_originator": None,
            "member_id": inherited("member_id"),
            "patient_id": inherited("patient_id"),
            "payer_id": inherited("payer_id"),
            "provider_id": inherited("provider_id"),
            # The patient's (a dependent's, else the subscriber's) last name
            # and birth date.
            "family": inherited("family"),
            "birth_date": inherited("birth_date"),
            "service_code": None,
            "service_date": None,
            "service_date_end": None,
            "service_count": 0,
            "decision": None,
            "auth_number": None,
            "status_category": None,
            "status_code": None,
            "status_date": None,
            "payer_claim_number": None,
        }
        records.append(rec)
        return rec

    for seg in body[1:-1]:
        tag = seg[0]
        el = seg + [""] * 12
        if tag == "HL":
            hl, parent, level = el[1], el[2], el[3]
            if not hl or hl in tree:
                raise X12Error("an HL id is missing or repeated")
            tree[hl] = (parent, level)
            # A unit's own segments: a 277 claim's end at the next HL; a 278
            # event keeps its service levels (SS) and nothing else.
            if current is not None and not (
                kind == "278" and level == "SS" and owner in ancestors(hl)
            ):
                current = None
            if kind == "278" and level == "EV":
                current, owner = open_record(), hl
            continue
        if tag == "NM1":
            entity, number = el[1], el[9]
            slot = {
                "IL": "member_id",
                "QC": "patient_id",
                "PR": "payer_id",
                "X3": "payer_id",
                "1P": "provider_id",
                "SJ": "provider_id",
                "FA": "provider_id",
                "85": "provider_id",
            }.get(entity)
            found = {slot: number} if slot and number else {}
            if entity in ("IL", "QC"):
                person = hl
                if el[3]:
                    found["family"] = el[3]
            for key, value in found.items():
                names.setdefault(hl, {})[key] = value
                if current is not None and current.get(key) is None:
                    current[key] = value
            continue
        if tag == "DMG" and el[1] == "D8" and person:
            born = _date("D8", el[2])[0]
            names.setdefault(person, {})["birth_date"] = born
            if current is not None and current.get("birth_date") is None:
                current["birth_date"] = born
            continue
        if tag == "TRN":
            if kind == "277":
                if el[1] != "2" or tree.get(hl, ("", ""))[1] not in ("22", "23"):
                    continue
                current, owner = open_record(), hl
            elif hl != owner:
                continue  # a 278 service level's own trace is not the event's
            # The referenced trace (TRN01 2) is the submitter's; it wins over
            # the payer's own (TRN01 1).
            if current is not None and (current["trace"] is None or el[1] == "2"):
                current["trace"] = el[2] or None
                current["trace_originator"] = el[3] or None
            continue
        if current is None:
            continue
        if tag == "STC" and current["status_category"] is None:
            cat, _, rest = el[1].partition(component)
            current["status_category"] = cat or None
            current["status_code"] = rest.split(component)[0] or None
            if el[2]:
                current["status_date"] = _date("D8", el[2])[0]
        elif tag == "REF":
            if el[1] == "1K" and current["payer_claim_number"] is None:
                current["payer_claim_number"] = el[2] or None
            elif el[1] in ("BB", "NT") and current["auth_number"] is None:
                current["auth_number"] = el[2] or None
        elif tag == "DTP" and el[1] == "472" and current["service_date"] is None:
            current["service_date"], current["service_date_end"] = _date(el[2], el[3])
        elif tag in ("SV1", "SV2", "SV3", "SVC"):
            current["service_count"] += 1
            if current["service_code"] is None:
                current["service_code"] = _code(el[1], component)
        elif tag == "HCR" and current["decision"] is None:
            current["decision"] = el[1] or None
            if el[2] and current["auth_number"] is None:
                current["auth_number"] = el[2]
    return records


def parse(text: str, name: str = "file") -> list[dict[str, Any]]:
    """Every 277 and 278 unit of work in one interchange, flattened.

    Raises :class:`X12Error` when the interchange is incomplete or
    malformed. Transaction sets other than 277 and 278 are skipped.
    """

    segs = segments(text)
    component = segs[0][16][:1]
    control = segs[0][13]
    out: list[dict[str, Any]] = []
    for group, st, sent, body in _check_envelopes(segs):
        if body[0][1] not in TRANSACTIONS:
            continue
        # A person finds the file by its control numbers; file names can
        # carry member ids, so they stay out of the reference.
        ref = f"x12:{control}.{group}.{st}"
        for record in _records(body, component, ref):
            record["sent_at"] = sent
            out.append(record)
    return out


class X12Verifier(VerifierAdapterBase):
    """Judge effects against the 277 or 278 units of work in a folder."""

    substrate = "x12"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self, root: str, *, glob: str = "*", transaction: Optional[str] = None
    ) -> None:
        if transaction is not None and transaction not in TRANSACTIONS:
            raise ValueError(f"x12 transaction must be one of {TRANSACTIONS}")
        self.root = Path(root).expanduser()
        self.glob = glob
        self.transaction = transaction
        self.read_problem: Optional[str] = None

    def read(self) -> Optional[list[dict[str, Any]]]:
        """Every record in the folder, or None (with ``read_problem``)."""

        self.read_problem = None
        if not self.root.is_dir():
            self.read_problem = "the X12 folder does not exist"
            return None
        files = sorted(p for p in self.root.glob(self.glob) if p.is_file())
        if len(files) > MAX_FILES:
            self.read_problem = f"more than {MAX_FILES} X12 files"
            return None
        out: list[dict[str, Any]] = []
        for path in files:
            try:
                if path.stat().st_size > MAX_BYTES:
                    raise X12Error("the file is larger than the read limit")
                text = path.read_bytes().decode("utf-8")
                records = parse(text, path.name)
            except (OSError, UnicodeDecodeError, X12Error) as exc:
                digest = hashlib.sha256(str(path.name).encode()).hexdigest()[:12]
                detail = exc if isinstance(exc, X12Error) else type(exc).__name__
                self.read_problem = f"X12 file name-sha256 {digest}: {detail}"
                return None
            out.extend(
                r
                for r in records
                if self.transaction is None or r["transaction"] == self.transaction
            )
        return out

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self.read()
        detail: dict[str, Any] = {"root": str(self.root), "glob": self.glob}
        if records is None:
            detail["read_problem"] = self.read_problem
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail=detail,
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = judge_records(expected, before, self.read(), substrate=self.substrate)
        if self.read_problem:
            verdict = verdict.model_copy(
                update={"reason": f"{verdict.reason} ({self.read_problem})"}
            )
        return verdict


def _factory(cfg: Any, params: Optional[Mapping[str, object]] = None) -> X12Verifier:
    from openadapt_flow.deployment import plugin_options

    options = plugin_options(cfg, params)
    unknown = set(options) - {"root", "glob", "transaction"}
    if unknown or not options.get("root"):
        raise ValueError(
            "effects.kind 'x12' takes options root (required), glob and transaction"
        )
    return X12Verifier(
        str(options["root"]),
        glob=str(options.get("glob", "*")),
        transaction=options.get("transaction"),
    )


def register() -> None:
    """Register the ``x12`` verifier kind (once)."""

    if resolve_verifier_factory("x12") is None:
        register_verifier_factory("x12", _factory)
