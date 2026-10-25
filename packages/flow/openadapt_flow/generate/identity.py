"""Generate an identity and duplicate rule, calibrated on the system's own data.

"Is this new record already there?" has no general answer: what counts as the
same patient depends on how a practice's records are entered. So the rule is
generated per system, from two things the system already has:

- **the schema**: which record fields hold a first name, a last name, a date
  of birth, an email, a phone (found from field names and value shapes);
- **a profile of existing records**: the records the read channel returns.

Then it is **calibrated on that data**, never on the test set:

- *false-match rate*: each existing record, posed as a new one, against all
  the others (assumed distinct people). A rule that fires there would stop a
  new patient.
- *miss rate*: each existing record copied with one ordinary data-entry error
  from a generic error model (a typo, swapped letters, day and month swapped,
  a digit wrong in the date, a changed or hyphenated last name, new contact
  details), against the rest.

Candidate rules are conjunctions of field comparisons (equal, one edit apart,
sounds alike, a shared name part, a date one field off or day and month
swapped). The rule set is chosen greedily: the comparisons that catch the
most calibration duplicates while firing on no calibration distinct pair. A
small sample of the closest calls goes to a person to label before the rule
is used, and their labels can only make it stricter or add a comparison.

At run time the rule answers ``exact`` (stop: the record exists), ``close``
(stop for a person, with both records shown), ``none`` (proceed) or
``unknown`` (a field the rule needs is missing: stop for a person). Zero-shot
entity matching is not clinically validated, so nothing here is trusted
until its rates on this system's own records are known and a person has
seen them.
"""

from __future__ import annotations

import itertools
import random
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

ROLES = ("first", "last", "dob", "email", "phone", "street", "prior_last")

_NAME_HINTS = {
    "first": ("fname", "first", "given", "forename"),
    "last": ("lname", "last", "family", "surname"),
    "dob": ("dob", "birth", "birthdate", "date_of_birth"),
    "email": ("email", "mail"),
    "phone": ("phone", "tel", "mobile"),
    "street": ("street", "address", "line"),
    "prior_last": (
        "prior_lname",
        "prior_last",
        "maiden",
        "previous_last",
        "birth_name",
    ),
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^[\d\s().+-]{7,}$")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", "" if value is None else str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[0-9a-z]+", text.casefold()))


def infer_roles(
    record_fields: Sequence[str], profile: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """Role -> record field, from field names, confirmed by value shapes."""

    out: dict[str, str] = {}
    lowered = {f: f.lower().replace("-", "_").replace(".", "_") for f in record_fields}
    for role in ("prior_last", *[r for r in ROLES if r != "prior_last"]):
        hits = [
            f
            for f, low in lowered.items()
            if any(h in low for h in _NAME_HINTS[role]) and f not in out.values()
        ]
        if not hits:
            continue
        best = sorted(hits, key=lambda f: (len(f), f))[0]
        values = [str(r.get(best) or "") for r in profile if r.get(best)]
        shape = {"dob": _DATE, "email": _EMAIL, "phone": _PHONE}.get(role)
        if (
            shape
            and values
            and sum(bool(shape.match(v)) for v in values) < 0.8 * len(values)
        ):
            continue
        out[role] = best
    return out


# -- comparisons ----------------------------------------------------------------


def _edit1(a: str, b: str) -> bool:
    """At most one insert, delete, substitute or adjacent swap."""

    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _soundex(word: str) -> str:
    codes = {
        **dict.fromkeys("bfpv", "1"),
        **dict.fromkeys("cgjkqsxz", "2"),
        **dict.fromkeys("dt", "3"),
        "l": "4",
        **dict.fromkeys("mn", "5"),
        "r": "6",
    }
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return ""
    out, last = word[0].upper(), codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def _date_parts(value: str) -> Optional[tuple[str, str, str]]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def compare(role: str, a: Any, b: Any) -> set[str]:
    """Which comparisons hold between two values of one role."""

    x, y = norm(a), norm(b)
    if not x or not y:
        return {"missing"}
    out: set[str] = set()
    if x == y:
        out.add("eq")
    if role in ("first", "last"):
        if _edit1(x.replace(" ", ""), y.replace(" ", "")):
            out.add("edit1")
        if role == "first" and (len(x) == 1 or len(y) == 1) and x[0] == y[0]:
            out.add("initial")
        if _soundex(x) == _soundex(y):
            out.add("sound")
        if set(x.split()) & set(y.split()):
            out.add("share")
    elif role == "dob":
        pa, pb = _date_parts(str(a)), _date_parts(str(b))
        if pa and pb:
            same = sum(i == j for i, j in zip(pa, pb))
            if same >= 2:
                out.add("near")
            if pa[0] == pb[0] and pa[1] == pb[2] and pa[2] == pb[1]:
                out.add("swap")
    return out


@dataclass(frozen=True)
class Clause:
    """All of these (role, comparison) pairs hold."""

    terms: tuple[tuple[str, str], ...]

    def holds(self, comps: Mapping[str, set[str]]) -> bool:
        return all(c in comps.get(role, set()) for role, c in self.terms)

    def text(self) -> str:
        words = {
            "eq": "the same",
            "edit1": "one letter apart",
            "sound": "sounding alike",
            "share": "sharing a name part",
            "near": "one part off",
            "swap": "with day and month swapped",
            "initial": "matching by initial",
        }
        roles = {"names_swapped": "first and last names", "prior_last": "the last name"}
        parts = []
        for role, c in self.terms:
            if role == "names_swapped":
                parts.append("first and last names swapped")
            elif role == "prior_last":
                parts.append("the last name the same as a prior last name")
            else:
                parts.append(f"{roles.get(role, role)} {words[c]}")
        return " and ".join(parts)


EXACT = Clause((("first", "eq"), ("last", "eq"), ("dob", "eq")))


def comparisons(
    roles: Mapping[str, str],
    new: Mapping[str, Any],
    record: Mapping[str, Any],
    new_roles: Optional[Mapping[str, str]] = None,
) -> dict[str, set[str]]:
    src = new_roles or roles
    out = {
        role: compare(role, new.get(src[role]), record.get(roles[role]))
        for role in roles
        if role in src and role != "prior_last"
    }
    if "first" in roles and "last" in roles and "first" in src and "last" in src:
        swapped = compare(
            "first", new.get(src["first"]), record.get(roles["last"])
        ) & compare("last", new.get(src["last"]), record.get(roles["first"]))
        out["names_swapped"] = {"eq"} if "eq" in swapped else set()
    if "prior_last" in roles and "last" in src:
        out["prior_last"] = (
            {"eq"}
            if "eq"
            in compare("last", new.get(src["last"]), record.get(roles["prior_last"]))
            else set()
        )
    return out


def candidate_clauses(roles: Sequence[str]) -> list[Clause]:
    options: dict[str, tuple[str, ...]] = {
        "first": ("eq", "edit1", "sound", "initial"),
        "names_swapped": ("eq",),
        "last": ("eq", "edit1", "sound", "share"),
        "dob": ("eq", "near", "swap"),
        "email": ("eq",),
        "phone": ("eq",),
        "street": ("eq",),
        "prior_last": ("eq",),
    }
    present = [r for r in roles if r in options]
    if "first" in roles and "last" in roles:
        present.append("names_swapped")
    out: list[Clause] = []
    for size in (2, 3):
        for combo in itertools.combinations(present, size):
            for picks in itertools.product(*(options[r] for r in combo)):
                out.append(Clause(tuple(zip(combo, picks))))
    return out


# -- the generic error model ---------------------------------------------------------


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 3:
        return word + "e"
    i = rng.randrange(1, len(word) - 1)
    kind = rng.choice(("swap", "drop", "sub"))
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    if kind == "drop":
        return word[:i] + word[i + 1 :]
    letters = "aeiourstnl"
    return word[:i] + rng.choice([c for c in letters if c != word[i]]) + word[i + 1 :]


#: A generic data-entry error model (the standard record-linkage error kinds:
#: typographical, transposed, date-part, changed or compound surname, initial,
#: swapped name fields). Each is applied with the contact details kept and,
#: separately, changed: a person registered again usually brings new ones.
ERRORS = (
    "first_typo",
    "last_typo",
    "day_month_swapped",
    "dob_digit",
    "last_changed",
    "last_hyphenated",
    "last_second_surname",
    "first_initial",
    "names_swapped",
    "contact_changed",
)


def perturb(
    person: Mapping[str, Any],
    kind: str,
    roles: Mapping[str, str],
    surnames: Sequence[str],
    rng: random.Random,
) -> Optional[dict[str, Any]]:
    """``person`` with one ordinary data-entry error, or None if it can't apply."""

    out = dict(person)
    f, l, d = roles.get("first"), roles.get("last"), roles.get("dob")
    if kind == "first_typo" and f:
        out[f] = _typo(str(out.get(f) or ""), rng)
    elif kind == "last_typo" and l:
        out[l] = _typo(str(out.get(l) or ""), rng)
    elif kind in ("day_month_swapped", "dob_digit") and d:
        parts = _date_parts(str(out.get(d) or ""))
        if not parts:
            return None
        y, m, dd = parts
        if kind == "day_month_swapped":
            if m == dd or int(dd) > 12:
                return None
            out[d] = f"{y}-{dd}-{m}"
        else:
            day = int(dd) + (1 if int(dd) < 28 else -1)
            out[d] = f"{y}-{m}-{day:02d}"
    elif kind == "last_changed" and l:
        others = [s for s in surnames if norm(s) != norm(out.get(l))]
        if not others:
            return None
        out[l] = rng.choice(others)
    elif kind == "last_hyphenated" and l:
        others = [s for s in surnames if norm(s) != norm(out.get(l))]
        if not others:
            return None
        out[l] = f"{out.get(l)}-{rng.choice(others)}"
    elif kind == "last_second_surname" and l:
        others = [s for s in surnames if norm(s) != norm(out.get(l))]
        if not others:
            return None
        out[l] = f"{out.get(l)} {rng.choice(others)}"
    elif kind == "first_initial" and f:
        first = str(out.get(f) or "")
        if not first:
            return None
        out[f] = first[0]
    elif kind == "names_swapped" and f and l:
        out[f], out[l] = out.get(l), out.get(f)
    elif kind == "contact_changed":
        pass
    else:
        return None
    return out


def change_contact(
    person: Mapping[str, Any], roles: Mapping[str, str], rng: random.Random
) -> dict[str, Any]:
    out = dict(person)
    for role in ("email", "phone", "street"):
        if roles.get(role):
            out[roles[role]] = f"changed {rng.randrange(10**6)}"
    return out


# -- calibration -----------------------------------------------------------------


@dataclass
class Calibration:
    distinct_pairs: int
    false_matches: int
    duplicates: int
    caught: int
    caught_by_error: dict[str, tuple[int, int]]
    labelled: list[dict[str, Any]] = field(default_factory=list)

    def rates(self) -> dict[str, Any]:
        return {
            "distinct_queries": self.distinct_pairs,
            "false_match_rate": round(
                self.false_matches / max(1, self.distinct_pairs), 4
            ),
            "calibration_duplicates": self.duplicates,
            "miss_rate": round(1 - self.caught / max(1, self.duplicates), 4),
            "miss_by_error": {
                k: round(1 - c / max(1, n), 3)
                for k, (c, n) in sorted(self.caught_by_error.items())
            },
        }


@dataclass
class DuplicateRule:
    roles: dict[str, str]
    clauses: list[Clause]
    calibration: Optional[Calibration] = None
    notes: list[str] = field(default_factory=list)
    estimated_false_stops_per_record: float = 0.0

    def decide(
        self,
        new: Mapping[str, Any],
        table: Sequence[Mapping[str, Any]],
        *,
        new_roles: Optional[Mapping[str, str]] = None,
    ) -> tuple[str, list[str]]:
        """``exact`` | ``close`` | ``none`` | ``unknown``, and why."""

        src = new_roles or self.roles
        for role in ("first", "last", "dob"):
            if role in self.roles and not norm(new.get(src.get(role, ""), "")):
                return "unknown", [f"the new record has no {role}"]
        why: list[str] = []
        decision = "none"
        for record in table:
            comps = comparisons(self.roles, new, record, src)
            if EXACT.holds(comps):
                return "exact", [EXACT.text()]
            for clause in self.clauses:
                if clause.holds(comps):
                    decision = "close"
                    why.append(clause.text())
                    break
        return decision, why

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.duplicate-rule/v1",
            "roles": dict(self.roles),
            "exact": [list(t) for t in EXACT.terms],
            "close": [[list(t) for t in c.terms] for c in self.clauses],
            "calibration": self.calibration.rates() if self.calibration else None,
            "estimated_false_stops_per_record": self.estimated_false_stops_per_record,
            "notes": list(self.notes),
        }

    def describe(self) -> list[str]:
        lines = [f"Stop: the record exists when {EXACT.text()}."]
        lines += [f"Stop for a person when {c.text()}." for c in self.clauses]
        lines.append(
            "Stop for a person when the new record lacks a name or a date of birth."
        )
        return lines


def generate_rule(
    profile: Sequence[Mapping[str, Any]],
    *,
    roles: Optional[Mapping[str, str]] = None,
    seed: int = 0,
    max_false_match: float = 0.0,
    false_stop_budget: float = 0.005,
    expected_records: Optional[int] = None,
    max_clauses: int = 12,
    label_sample: int = 20,
    labeller: Any = None,
) -> DuplicateRule:
    """Generate and calibrate a duplicate rule on ``profile`` alone.

    ``labeller(pair) -> bool`` stands for the person who labels the closest
    calls (True: the same person). Without one the sample is listed for a
    person and the rule is used as calibrated.

    A clause is admitted only when it fires on no calibration distinct pair
    (``max_false_match``) and its estimated false stops per new record stay
    within ``false_stop_budget``: the product of each comparison's chance on
    a distinct pair (estimated per comparison, Fellegi-Sunter style, with
    add-one smoothing) times ``expected_records``, the size of the table a
    new record meets (default: the profile). A small profile can't show a
    rare coincidence directly; per-comparison rates can.
    """

    fields = sorted({k for r in profile for k in r})
    roles = dict(roles or infer_roles(fields, profile))
    notes: list[str] = []
    for needed in ("first", "last", "dob"):
        if needed not in roles:
            notes.append(
                f"no field holds the {needed}; every new record goes to a person"
            )
    rng = random.Random(seed)
    surnames = sorted(
        {str(r.get(roles.get("last", ""), "")) for r in profile if roles.get("last")}
        - {""}
    )
    clauses = candidate_clauses(list(roles))

    pairs: list[tuple[int, int]] = []
    negatives: list[dict[str, set[str]]] = []
    for i, person in enumerate(profile):
        for j, other in enumerate(profile):
            if i != j:
                pairs.append((i, j))
                negatives.append(comparisons(roles, person, other))
    positives: list[tuple[str, dict[str, set[str]]]] = []
    for person in profile:
        for kind in ERRORS:
            copy = perturb(person, kind, roles, surnames, rng)
            if copy is None:
                continue
            for contact in ("kept", "changed"):
                if kind == "contact_changed" and contact == "kept":
                    continue
                variant = (
                    change_contact(copy, roles, rng) if contact == "changed" else copy
                )
                positives.append(
                    (f"{kind}+contact_{contact}", comparisons(roles, variant, person))
                )

    table_size = max(expected_records or 0, len(profile))
    same_person: set[frozenset[int]] = set()

    def select() -> tuple[
        list[Clause], set[int], float, list[tuple[Clause, set[int], list[int]]]
    ]:
        live = [
            k for k, (i, j) in enumerate(pairs) if frozenset((i, j)) not in same_person
        ]
        allowed = max_false_match * max(1, len(live))
        u_prob: dict[tuple[str, str], float] = {}

        def chance(term: tuple[str, str]) -> float:
            if term not in u_prob:
                role, c = term
                fires = sum(c in negatives[k].get(role, set()) for k in live)
                u_prob[term] = (fires + 1) / (len(live) + 2)
            return u_prob[term]

        scored = []
        blocked: list[tuple[Clause, set[int], list[int]]] = []
        for clause in clauses:
            firing = [k for k in live if clause.holds(negatives[k])]
            hits = {
                i
                for i, (_k, p) in enumerate(positives)
                if clause.holds(p) or EXACT.holds(p)
            }
            if len(firing) > allowed:
                blocked.append((clause, hits, firing))
                continue
            estimate = float(table_size)
            for term in clause.terms:
                estimate *= chance(term)
            if estimate > false_stop_budget:
                continue
            scored.append((clause, hits, len(firing), estimate))
        chosen: list[Clause] = []
        covered = {i for i, (_k, p) in enumerate(positives) if EXACT.holds(p)}
        spent = 0.0
        while len(chosen) < max_clauses:
            # The most new calibration duplicates per estimated false stop,
            # among the clauses the remaining budget can still pay for.
            affordable = [
                t
                for t in scored
                if t[3] <= false_stop_budget - spent and t[1] - covered
            ]
            best = max(
                affordable,
                key=lambda t: (len(t[1] - covered) / (t[3] + 1e-9), -len(t[0].terms)),
                default=None,
            )
            if best is None:
                break
            chosen.append(best[0])
            covered |= best[1]
            spent += best[3]
            scored.remove(best)
        return chosen, covered, spent, blocked

    chosen, covered, spent, blocked = select()
    # The closest calls a person labels: pairs of existing records that alone
    # keep a clause out (records the system holds as two people that the
    # clause calls one), largest coverage gain first. A label of "the same
    # person" means the system already holds a duplicate: that pair no longer
    # counts against the clause. Labels can only add a clause the budget
    # still affords.
    sample: list[dict[str, Any]] = []
    seen: set[frozenset[int]] = set()
    for clause, hits, firing in sorted(blocked, key=lambda t: -len(t[1] - covered)):
        if not (hits - covered) or len(firing) > 2 * max(1, label_sample):
            continue
        for k in firing:
            key = frozenset(pairs[k])
            if key in seen:
                continue
            seen.add(key)
            i, j = pairs[k]
            sample.append(
                {
                    "pair": "existing records",
                    "records": (dict(profile[i]), dict(profile[j])),
                    "_pair": (i, j),
                    "clause": clause.text(),
                }
            )
        if len(sample) >= label_sample:
            break
    sample = sample[:label_sample]
    if labeller is not None and sample:
        for item in sample:
            same = bool(labeller(item))
            item["label"] = "same person" if same else "different people"
            if same:
                same_person.add(frozenset(item["_pair"]))
        if same_person:
            chosen, covered, spent, blocked = select()
    for item in sample:
        item.pop("records", None)
        item.pop("_pair", None)
    negatives = [
        n for k, n in enumerate(negatives) if frozenset(pairs[k]) not in same_person
    ]

    rule = DuplicateRule(roles, chosen, notes=notes)
    rule.estimated_false_stops_per_record = round(spent, 5)
    by_kind: dict[str, list[int]] = {}
    for i, (kind, _p) in enumerate(positives):
        by_kind.setdefault(kind, []).append(i)
    false_matches = sum(
        any(c.holds(n) for c in chosen) or EXACT.holds(n) for n in negatives
    )
    rule.calibration = Calibration(
        distinct_pairs=len(negatives),
        false_matches=false_matches,
        duplicates=len(positives),
        caught=len(covered),
        caught_by_error={
            k: (len(set(v) & covered), len(v)) for k, v in by_kind.items()
        },
    )
    rule.calibration.labelled = sample
    return rule
