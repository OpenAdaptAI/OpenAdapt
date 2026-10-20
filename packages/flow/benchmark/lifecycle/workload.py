"""A seeded worklist stream with an exception mix, and its ground truth.

Every item is a referral a clerk would register as a new patient. Every
exception is drawn from a seeded generator and written to the event ledger
before the run starts, so the operator and the oracle judge from the same
truth and two runs with one seed see the same stream.

The mix is stress-weighted, not measured: about a quarter of items carry an
exception so that a few hundred items exercise every kind several times. Real
intake mixes are not known to us; the report says so.

Item-level exceptions (``EXCEPTION_MIX``):

- ``dup_existing``: the patient already has a chart (same name and date of
  birth). Right outcome: no new chart.
- ``dup_resend``: a referral already in this stream is sent again (a fax
  resend). Right outcome: no second chart.
- ``ambiguous_same_person``: same name as an existing chart, date of birth off
  by a typo. It is that person. Right outcome: no new chart.
- ``ambiguous_new_person``: same name as an existing chart, a different
  person (different date of birth and address). Right outcome: a new chart.
- ``missing_required``: no date of birth. Right outcome: no chart; the
  referral goes back to the sender.
- ``missing_optional``: no home phone. Right outcome: a chart with a blank
  home phone.
- ``malformed_dob``: date of birth written MM/DD/YYYY. Right outcome: a chart
  with the true date, or a stop; never a wrong date.
- ``malformed_case``: names in capitals with stray spaces. Right outcome: a
  chart with the names as the person's name (case-insensitive match, trimmed).
- ``concurrent_same``: another clerk registers the same patient seconds
  before this item runs. Right outcome: exactly one chart for the patient.
- ``concurrent_other``: another clerk edits an unrelated chart while this item
  runs. Right outcome: this item's chart; the other edit is not collateral.
- ``session_expiry``: the arm's sessions are ended (forced logout) just
  before the item. Right outcome: a chart, or a safe stop.
- ``slow_save``: the save takes several seconds longer than usual. Right
  outcome: a chart.
- ``failed_save``: the save fails once (the app returns an error). Right
  outcome: no chart from the failed attempt; a chart after a retry, or a safe
  stop.

Day-level events (``DRIFT_SCHEDULE``) stay on from their day to the end, as a
vendor release would: a theme change, a relabel of the save button, a field
reorder, and an added interstitial notice.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .arm import WorkItem

WORKLOAD_VERSION = "lifecycle-workload/2026-10-20.2"

EXCEPTION_MIX: dict[str, float] = {
    "dup_existing": 0.03,
    "dup_resend": 0.03,
    "ambiguous_same_person": 0.02,
    "ambiguous_new_person": 0.02,
    "missing_required": 0.02,
    "missing_optional": 0.02,
    "malformed_dob": 0.02,
    "malformed_case": 0.02,
    "concurrent_same": 0.015,
    "concurrent_other": 0.02,
    "session_expiry": 0.02,
    "slow_save": 0.02,
    "failed_save": 0.015,
}

#: (day, kind) — each stays on from that day to the end of the run.
DRIFT_SCHEDULE: tuple[tuple[int, str], ...] = (
    (2, "theme"),
    (4, "relabel"),
    (6, "reorder"),
    (8, "interstitial"),
)

# Invented names. Short names are kept on purpose: they occur in real
# worklists and some identity checks treat them differently.
FIRST = (
    "Nadia Marcus Ines Theo Grace Arlo Priya Tomas Wen Esme Kofi Liesel Rafael "
    "Odile Bram Yuki Soren Amara Dmitri Luz Ana Bo Jae Ola Cyrus Mireille "
    "Henrik Talia Oren Signe Mateo Leilani Fergus Ilse Kwame Rosalind Zeno "
    "Petra Anouk Calum Dalia Emeric Farida Gideon Hollis Ida Jasper Kalinda"
).split()
#: 300 invented surnames (20 stems x 15 endings). OpenEMR's duplicate search
#: lists any chart that shares a last name, so the size of this pool sets how
#: often a new patient meets a namesake; a real practice has common surnames
#: too. Short surnames are kept for the same reason as short first names.
_STEMS = (
    "Ash Birch Cald Dun Ell Fair Gal Hart Ivers Jess Kell Lang Mar Nor Oak "
    "Pem Quen Rad Stan Thorn"
).split()
_ENDINGS = (
    "by ford wick ton ley more well wood field brook combe stead mere gate ridge"
).split()
LAST = [a + b for a in _STEMS for b in _ENDINGS][:298] + ["Ng", "Li"]
STREETS = (
    "WILLOW BEND ROAD QUARRY HILL DRIVE LANTERN COURT FERRY STREET CEDAR ROW "
    "BEACON TERRACE STATION AVENUE MILL ROAD ORCHARD LANE HARBOR WAY"
).split()
STREET_NAMES = [
    " ".join(STREETS[i : i + 3]) if i + 3 <= len(STREETS) else STREETS[i]
    for i in range(0, len(STREETS), 3)
]
TITLES = {"Female": ("Ms.", "Mrs."), "Male": ("Mr.",)}


@dataclass
class Truth:
    """What is true about one item, kept by the harness, never shown to arms."""

    item_id: str
    seq: int
    day: int
    exception: str
    #: The chart the item should produce, or None when no new chart is right.
    intended: Optional[dict[str, str]]
    #: The person this item is about (the true values), for the oracle's
    #: "exactly one chart for this person" check.
    person: dict[str, str]
    rationale: str
    #: Item-level injections the harness must apply around this item.
    inject: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Workload:
    seed: int
    days: int
    items: list[WorkItem]
    truth: dict[str, Truth]
    #: Charts that exist before the run (seeded so duplicates have targets).
    existing: list[dict[str, str]]
    drift: list[dict[str, Any]]

    def ledger_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {
                "type": "workload",
                "version": WORKLOAD_VERSION,
                "seed": self.seed,
                "days": self.days,
                "items": len(self.items),
                "exception_mix": EXCEPTION_MIX,
            }
        ]
        rows += [{"type": "existing_chart", **c} for c in self.existing]
        rows += [{"type": "drift_scheduled", **d} for d in self.drift]
        for item in self.items:
            t = self.truth[item.item_id]
            rows.append({"type": "item", **asdict(item), "truth": asdict(t)})
        return rows


def _person(rng: random.Random, used: set[tuple[str, str]], n: int) -> dict[str, str]:
    while True:
        fname, lname = rng.choice(FIRST), rng.choice(LAST)
        if (fname, lname) not in used:
            used.add((fname, lname))
            break
    sex = rng.choice(("Female", "Male"))
    year = rng.randint(1935, 2004)
    dob = f"{year:04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    number = rng.randint(2, 498)
    street = f"{number} {rng.choice(STREET_NAMES)}"
    return {
        "title": rng.choice(TITLES[sex]),
        "fname": fname,
        "lname": lname,
        "DOB": dob,
        "sex": sex,
        "street": street,
        "city": "EXAMPLETON",
        "state_label": "Massachusetts",
        "postal_code": "02139",
        "phone_home": f"555-01{rng.randint(0, 99):02d}",
        "email": f"{fname.lower()}.{lname.lower()}.{n}@example.invalid",
        "country_code": "USA",
    }


def _source_text(params: dict[str, str]) -> str:
    lines = ["REFERRAL - new patient registration requested", ""]
    labels = (
        ("title", "Title"),
        ("fname", "First name"),
        ("lname", "Last name"),
        ("DOB", "Date of birth"),
        ("sex", "Sex"),
        ("street", "Street"),
        ("city", "City"),
        ("state_label", "State"),
        ("postal_code", "ZIP"),
        ("phone_home", "Home phone"),
        ("email", "Email"),
        ("country_code", "Country"),
    )
    for key, label in labels:
        lines.append(f"{label}: {params.get(key, '')}")
    return "\n".join(lines) + "\n"


def _pick(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for kind, p in EXCEPTION_MIX.items():
        acc += p
        if r < acc:
            return kind
    return "clean"


def generate(seed: int, n_items: int, days: int, n_existing: int = 12) -> Workload:
    """The stream for ``seed``: ``n_items`` referrals over ``days`` days."""

    rng = random.Random(seed)
    used: set[tuple[str, str]] = set()
    existing = [_person(rng, used, 9000 + i) for i in range(n_existing)]
    items: list[WorkItem] = []
    truth: dict[str, Truth] = {}
    sent: list[tuple[WorkItem, dict[str, str]]] = []
    per_day = max(1, -(-n_items // days))

    for seq in range(1, n_items + 1):
        item_id = f"ref-{seq:05d}"
        day = (seq - 1) // per_day
        kind = _pick(rng)
        person = _person(rng, used, seq)
        params = dict(person)
        intended: Optional[dict[str, str]] = dict(person)
        inject: list[dict[str, Any]] = []
        why = "a new referral; register it"

        if kind == "dup_existing":
            person = dict(rng.choice(existing))
            params, intended = dict(person), None
            why = "this patient already has a chart"
        elif kind == "dup_resend":
            originals = [s for s in sent if truth[s[0].item_id].intended is not None]
            if originals:
                orig, orig_person = rng.choice(originals)
                person, params, intended = dict(orig_person), dict(orig.params), None
                why = f"a resend of {orig.item_id}"
            else:
                kind = "clean"
        elif kind == "ambiguous_same_person":
            person = dict(rng.choice(existing))
            params, intended = dict(person), None
            y, m, d = person["DOB"].split("-")
            params["DOB"] = f"{y}-{m}-{int(d) % 28 + 1:02d}"
            why = "same person as an existing chart; the date of birth has a typo"
        elif kind == "ambiguous_new_person":
            other = rng.choice(existing)
            person["fname"], person["lname"] = other["fname"], other["lname"]
            person["email"] = (
                f"{other['fname'].lower()}.{other['lname'].lower()}.{seq}@example.invalid"
            )
            params, intended = dict(person), dict(person)
            why = "same name as an existing chart, a different person"
        elif kind == "missing_required":
            params.pop("DOB")
            intended = None
            why = "no date of birth; return the referral to the sender"
        elif kind == "missing_optional":
            params["phone_home"] = ""
            person["phone_home"] = ""
            intended = dict(person)
            why = "no home phone; register without one"
        elif kind == "malformed_dob":
            y, m, d = person["DOB"].split("-")
            params["DOB"] = f"{m}/{d}/{y}"
            why = "date of birth written MM/DD/YYYY; the true date is ISO"
        elif kind == "malformed_case":
            params["fname"] = f" {person['fname'].upper()} "
            params["lname"] = person["lname"].upper() + " "
            why = "names in capitals with stray spaces"
        elif kind == "concurrent_same":
            intended = None
            inject.append(
                {
                    "event": "other_clerk_creates",
                    "person": dict(person),
                    "when": "before_item",
                }
            )
            why = "another clerk registered this patient moments earlier"
        elif kind == "concurrent_other":
            inject.append(
                {"event": "other_clerk_edits_unrelated", "when": "during_item"}
            )
            why = "another clerk edits an unrelated chart meanwhile"
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
            app="openemr",
            params=params,
            source_text=_source_text(params),
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


def dumps(workload: Workload) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in workload.ledger_rows())
