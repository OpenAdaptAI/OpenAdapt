"""The seeded claims-intake stream for openIMIS (``run.py --app openimis``).

Each item is one health-facility claim, as a facility's claim form would send
it: the insuree number, a claim number (at most 8 characters, openIMIS's
limit) and an explanation. The diagnosis (A000) and the service (A1) are the
ones the taught program enters on every claim. The right outcome of every
item is in its truth; the oracle reads ``tblClaim`` directly.

Exceptions and their right outcomes:

- ``dup_existing``: the claim number already exists in openIMIS. No new claim.
- ``dup_resend``: a claim this run already sent arrives again. No new claim.
- ``missing_required``: no insuree number. Return it to the facility.
- ``unknown_insuree``: the insuree number matches no insuree. No claim.
- ``concurrent_same``: another clerk enters the same claim first. No new claim.
- ``concurrent_other``, ``session_expiry``, ``slow_save``, ``failed_save``: as
  in the referral workload; the claim is still right to enter.

A run of at most 40 items gets each exception at least once, in a seeded
order, with the rest clean. A longer run draws from ``EXCEPTION_MIX``. The
mix is stress-weighted, not a measured claims distribution. All values are
invented.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from .arm import WorkItem
from .workload import Truth, Workload

WORKLOAD_VERSION = "lifecycle-claims-workload/2026-10-24.1"

#: The fixture's insurees with coverage in force (benchmark/openimis_claims).
ACTIVE_INSUREES = ("999000001", "999000003")
UNKNOWN_INSUREE = "999999999"
HEALTH_FACILITY = "VIHOS001"
#: tblClaim.ClaimStatus for "Entered".
ENTERED = "2"

EXCEPTION_MIX: dict[str, float] = {
    "dup_existing": 0.04,
    "dup_resend": 0.04,
    "missing_required": 0.03,
    "unknown_insuree": 0.03,
    "concurrent_same": 0.02,
    "concurrent_other": 0.02,
    "session_expiry": 0.02,
    "slow_save": 0.02,
    "failed_save": 0.02,
}

#: (day, kind): each stays on from that day to the end of the run.
DRIFT_SCHEDULE: tuple[tuple[int, str], ...] = ((1, "theme"),)

_REASONS = (
    "Outpatient consultation, fever and headache",
    "Follow-up visit after treatment",
    "Consultation for persistent cough",
    "Routine consultation, referred by the village clinic",
    "Consultation for abdominal pain",
)


def _claim(rng: random.Random, seq: int) -> dict[str, str]:
    return {
        "insurance_no": rng.choice(ACTIVE_INSUREES),
        "claim_no": f"SI{seq:06d}",
        "explanation": f"{rng.choice(_REASONS)} (ref {seq})",
    }


def _intended(claim: dict[str, str]) -> dict[str, str]:
    return {**claim, "hf_code": HEALTH_FACILITY, "claim_status": ENTERED}


def source_text(params: dict[str, str]) -> str:
    """The claim form a facility sends, as text."""

    return "\n".join(
        [
            "HEALTH FACILITY CLAIM",
            f"Facility: {HEALTH_FACILITY}",
            f"Claim no.: {params.get('claim_no', '')}",
            f"Insuree no.: {params.get('insurance_no', '')}",
            "Main diagnosis: A000",
            "Service: A1 General Consultation",
            f"Explanation: {params.get('explanation', '')}",
        ]
    )


def _schedule(rng: random.Random, n_items: int) -> list[str]:
    if n_items <= 40:
        kinds = list(EXCEPTION_MIX)
        # dup_resend needs an earlier claim to resend.
        kinds.remove("dup_resend")
        rng.shuffle(kinds)
        slots = ["clean"] * n_items
        positions = rng.sample(range(1, n_items), min(len(kinds), n_items - 1))
        for pos, kind in zip(positions, kinds):
            slots[pos] = kind
        later = [i for i in range(2, n_items) if slots[i] == "clean"]
        if later:
            slots[rng.choice(later)] = "dup_resend"
        return slots
    out = []
    for _ in range(n_items):
        r, acc = rng.random(), 0.0
        kind = "clean"
        for k, p in EXCEPTION_MIX.items():
            acc += p
            if r < acc:
                kind = k
                break
        out.append(kind)
    return out


def generate(
    seed: int, n_items: int, days: int, n_existing: int = 6, app: str = "openimis"
) -> Workload:
    """The stream for ``seed``: ``n_items`` claims over ``days`` days."""

    rng = random.Random(seed)
    existing = [_intended(_claim(rng, 900000 + i)) for i in range(min(n_existing, 6))]
    items: list[WorkItem] = []
    truth: dict[str, Truth] = {}
    sent: list[tuple[WorkItem, dict[str, str]]] = []
    per_day = max(1, -(-n_items // days))

    for seq, kind in enumerate(_schedule(rng, n_items), start=1):
        item_id = f"clm-{seq:05d}"
        day = (seq - 1) // per_day
        claim = _claim(rng, seq)
        params = dict(claim)
        person = _intended(claim)
        intended: Optional[dict[str, str]] = dict(person)
        inject: list[dict[str, Any]] = []
        why = "a new claim; enter it"

        if kind == "dup_existing":
            person = dict(rng.choice(existing))
            params = {k: person[k] for k in ("insurance_no", "claim_no", "explanation")}
            intended = None
            why = "this claim number is already in openIMIS"
        elif kind == "dup_resend":
            originals = [s for s in sent if truth[s[0].item_id].intended is not None]
            if originals:
                orig, orig_person = rng.choice(originals)
                person, params, intended = dict(orig_person), dict(orig.params), None
                why = f"a resend of {orig.item_id}"
            else:
                kind = "clean"
        elif kind == "missing_required":
            params["insurance_no"] = ""
            intended = None
            why = "no insuree number; return the claim to the facility"
        elif kind == "unknown_insuree":
            params["insurance_no"] = UNKNOWN_INSUREE
            person = {**person, "insurance_no": UNKNOWN_INSUREE}
            intended = None
            why = "the insuree number matches no insuree; return the claim"
        elif kind == "concurrent_same":
            intended = None
            inject.append(
                {
                    "event": "other_clerk_creates",
                    "person": dict(person),
                    "when": "before_item",
                }
            )
            why = "another clerk entered this claim moments earlier"
        elif kind == "concurrent_other":
            inject.append(
                {"event": "other_clerk_edits_unrelated", "when": "during_item"}
            )
            why = "another clerk edits an unrelated claim meanwhile"
        elif kind == "session_expiry":
            inject.append({"event": "force_logout", "when": "before_item"})
            why = "sessions ended just before the item"
        elif kind == "slow_save":
            inject.append(
                {
                    "event": "slow_save",
                    "seconds": rng.choice((4, 6, 8)),
                    "when": "during_item",
                }
            )
            why = "the save is slow"
        elif kind == "failed_save":
            inject.append({"event": "failed_save", "times": 1, "when": "during_item"})
            why = "the save fails once"

        item = WorkItem(
            seq=seq,
            item_id=item_id,
            day=day,
            app=app,
            params=params,
            source_text=source_text(params),
        )
        items.append(item)
        truth[item_id] = Truth(
            item_id=item_id,
            seq=seq,
            day=day,
            exception=kind,
            intended=intended,
            person=person,
            rationale=why,
            inject=inject,
        )
        sent.append((item, person))

    drift = [
        {"day": d, "kind": k, "first_seq": d * per_day + 1}
        for d, k in DRIFT_SCHEDULE
        if d < days
    ]
    return Workload(
        seed=seed, days=days, items=items, truth=truth, existing=existing, drift=drift
    )
