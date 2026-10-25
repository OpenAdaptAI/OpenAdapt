"""Identity as a vetted template plus a generated, per-site mapping.

Calibrating a matching rule on one site's few dozen records can't learn
which near-matches are the same person (``benchmark/generated_parts``). So
identity is split in two:

1. **The template** is reviewed code, versioned and owned like the safety
   policy: ``patient-identity/1`` is the record check of #1516
   (``openadapt_flow.runtime.checks.duplicate_record``: exact, close,
   prior-name and nickname clauses, fixed thresholds). It is never generated.
2. **The mapping** is generated per site: which of this system's record
   fields feed each template slot (given and family name, date of birth and
   its format, MRN, phone, email, street, a prior last name), and which task
   parameter feeds each input.

The mapping is verified, not calibrated on the live table:

- **against the demonstrations**: each saved record, read through the
  mapping, must give back what the person typed, slot by slot;
- **on a perturbation suite** built from the site's own records and schema:
  generated duplicates (a typo, a nickname, day and month swapped, a changed
  or hyphenated name, new contact details) the template must stop, and the
  site's other records posed as new, which it must let through.

**Adoption**: a generated mapping is used only if template plus mapping does
no worse than template plus the reference mapping (a hand-written source
adapter) on that suite: no more misses, no more false stops. Otherwise the
reference stays, and the review says why.

This module imports only the standard library; the template is loaded by
name when it is used.
"""

from __future__ import annotations

import importlib
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

TEMPLATES: dict[str, str] = {
    "patient-identity/1": "openadapt_flow.runtime.checks.duplicate_record",
}
#: Template record slots and the inputs it reads from a task.
RECORD_SLOTS = (
    "record_id",
    "fname",
    "lname",
    "dob",
    "mrn",
    "phone",
    "street",
    "email",
    "prior_lname",
)
WANT_SLOTS = ("fname", "lname", "DOB", "mrn", "phone_home", "email", "street")
#: Record slot that each input is compared with.
WANT_TO_RECORD = {
    "fname": "fname",
    "lname": "lname",
    "DOB": "dob",
    "mrn": "mrn",
    "phone_home": "phone",
    "email": "email",
    "street": "street",
}
_HINTS = {
    "record_id": ("uuid", "pid", "id", "record_id", "resource_id"),
    "fname": ("fname", "given", "first", "forename"),
    "lname": ("lname", "family", "surname", "last"),
    "dob": ("dob", "birthdate", "birth_date", "date_of_birth", "birth"),
    "mrn": ("mrn", "pubpid", "medical_record", "chart_number", "identifier.value"),
    "phone": ("phone", "tel", "mobile"),
    "email": ("email", "mail"),
    "street": ("street", "address.line", "address", "line"),
    "prior_lname": (
        "prior_lname",
        "birth_lname",
        "maiden",
        "previous_last",
        "[use=old].family",
        "[use=maiden].family",
    ),
}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TemplateUnavailable(RuntimeError):
    """The named template's reviewed code is not installed."""


def _norm(value: Any) -> str:
    return " ".join(re.findall(r"[0-9a-z@.]+", str(value or "").casefold()))


_PREDICATE = re.compile(r"^(?P<key>[^\[]+)\[(?P<field>[^=\]]+)=(?P<value>[^\]]*)\]$")


def get_path(row: Mapping[str, Any], path: str) -> Any:
    """``a.b.c``; a list takes its first item, and ``key[field=value]``
    takes the first item of list ``key`` whose ``field`` is ``value``
    (``telecom[system=email].value``)."""

    if path in row:
        return row[path]
    node: Any = row
    for part in re.split(r"\.(?![^\[]*\])", path):
        m = _PREDICATE.match(part)
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, Mapping):
            return None
        if m:
            items = node.get(m.group("key"))
            if not isinstance(items, list):
                return None
            node = next(
                (
                    i
                    for i in items
                    if isinstance(i, Mapping)
                    and str(i.get(m.group("field"))) == m.group("value")
                ),
                None,
            )
        else:
            node = node.get(part)
    if isinstance(node, list):
        node = node[0] if node else None
    return node


#: List items told apart by these fields get one path per value.
_DISCRIMINATORS = ("system", "use")


def paths_of(rows: Sequence[Mapping[str, Any]], limit: int = 120) -> list[str]:
    """Every scalar path in ``rows``, with ``key[field=value]`` for lists of
    coded items (a FHIR ``telecom`` or ``name``)."""

    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, list):
            coded = [
                d
                for d in _DISCRIMINATORS
                if node and all(isinstance(i, Mapping) and d in i for i in node)
            ]
            if coded and path:
                key = path.rsplit(".", 1)
                for item in node:
                    seg = f"{key[-1]}[{coded[0]}={item[coded[0]]}]"
                    walk(item, f"{key[0]}.{seg}" if len(key) == 2 else seg)
            elif node:
                walk(node[0], path)
            return
        if isinstance(node, Mapping):
            for k in sorted(node):
                walk(node[k], f"{path}.{k}" if path else str(k))
            return
        if path and path not in out:
            out.append(path)

    for row in rows:
        walk(row, "")
    return out


def to_iso(value: Any, order: Optional[str]) -> str:
    text = (
        str(value or "").strip()[:10]
        if _ISO.match(str(value or "").strip()[:10])
        else str(value or "").strip()
    )
    if _ISO.match(text):
        return text
    m = _NUMERIC_DATE.match(text)
    if not m or order not in ("MM/DD/YYYY", "DD/MM/YYYY"):
        return ""
    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
    month, day = (a, b) if order == "MM/DD/YYYY" else (b, a)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{y}-{month:02d}-{day:02d}"


@dataclass
class IdentityMapping:
    """How one site's records and one task's parameters feed a template."""

    template: str
    #: Template record slot -> the site's record field (a dotted path).
    record: dict[str, str]
    #: Template input -> the task parameter.
    want: dict[str, str]
    #: The site's order for numeric dates of birth, when they aren't ISO.
    dob_format: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def record_slots(self, row: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for slot in RECORD_SLOTS:
            path = self.record.get(slot)
            value = get_path(row, path) if path else None
            out[slot] = "" if value is None else str(value)
        out["dob"] = to_iso(out["dob"], self.dob_format)
        return out

    def want_of(self, params: Mapping[str, Any]) -> dict[str, str]:
        return {
            slot: str(params.get(param, "") or "")
            for slot, param in self.want.items()
            if param
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.identity-mapping/v1",
            "template": self.template,
            "record": dict(self.record),
            "want": dict(self.want),
            "dob_format": self.dob_format,
            "notes": list(self.notes),
        }


class Template:
    """A loaded template: ``decide(want, table_rows) -> exact|close|none|unreadable``."""

    def __init__(self, template_id: str = "patient-identity/1") -> None:
        if template_id not in TEMPLATES:
            raise TemplateUnavailable(f"no template {template_id!r}")
        try:
            self.module = importlib.import_module(TEMPLATES[template_id])
        except ImportError as exc:
            raise TemplateUnavailable(
                f"template {template_id} needs {TEMPLATES[template_id]}"
            ) from exc
        self.id = template_id

    def record(self, slots: Mapping[str, Any]) -> Any:
        prior = (
            ((str(slots.get("fname") or ""), str(slots["prior_lname"])),)
            if slots.get("prior_lname")
            else ()
        )
        return self.module.PatientRecord(
            record_id=str(slots.get("record_id") or ""),
            fname=str(slots.get("fname") or ""),
            lname=str(slots.get("lname") or ""),
            dob=str(slots.get("dob") or ""),
            mrn=str(slots.get("mrn") or ""),
            phone=str(slots.get("phone") or ""),
            street=str(slots.get("street") or ""),
            email=str(slots.get("email") or ""),
            prior_names=prior,
        )

    def decide_records(self, want: Mapping[str, str], records: Sequence[Any]) -> str:
        source = self.module.TableSource(list(records))
        return str(self.module.check_duplicate(source, dict(want)).decision)


# -- generation ---------------------------------------------------------------------


def _fields(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def _shape_ok(slot: str, values: Sequence[str]) -> bool:
    values = [v for v in values if v]
    if not values:
        return False
    if slot == "email":
        return sum(bool(_EMAIL.match(v)) for v in values) >= 0.8 * len(values)
    if slot == "dob":
        return sum(
            bool(_ISO.match(v[:10]) or _NUMERIC_DATE.match(v)) for v in values
        ) >= 0.8 * len(values)
    if slot == "phone":
        return sum(len(re.sub(r"\D", "", v)) >= 7 for v in values) >= 0.8 * len(values)
    return True


def generate_mapping(
    rows: Sequence[Mapping[str, Any]],
    demos: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    template: str = "patient-identity/1",
    bound: Optional[Mapping[str, str]] = None,
) -> IdentityMapping:
    """Map the site's fields and the task's parameters onto ``template``.

    ``rows``: records the read channel returns (flattened). ``demos``: (typed
    params, the record that demonstration saved). ``bound``: the contract's
    record field -> parameter bindings, when known.
    """

    fields = paths_of(rows)
    record: dict[str, str] = {}
    grounded: dict[str, str] = {}
    notes: list[str] = []
    # Grounded first: a slot the person typed a value for gets the field whose
    # value is that typed value in every demonstration (the hinted one first).
    for slot in ("fname", "lname", "dob", "phone", "email", "street"):
        want_slot = next(w for w, r in WANT_TO_RECORD.items() if r == slot)
        for param in list(demos[0][0]) if demos else []:
            matching = [
                f
                for f in fields
                if f not in record.values()
                and all(
                    _same(want_slot, typed.get(param), get_path(saved, f))
                    for typed, saved in demos
                )
                # The values must look like this slot's (an email has an @),
                # and the field or the parameter must be named like it: a
                # typed first name also fills an old name's given name.
                and _shape_ok(slot, [str(get_path(r, f) or "") for r in rows])
                and (_named(slot, f) or _named(slot, param))
            ]
            if matching:
                matching.sort(
                    key=lambda f: (
                        not any(h in f.lower() for h in _HINTS[slot]),
                        "use=old" in f or "use=maiden" in f,
                        len(f),
                        f,
                    )
                )
                record[slot] = matching[0]
                grounded[want_slot] = param
                break
    for slot in RECORD_SLOTS:
        if slot in record:
            continue
        lowered = {f: f.lower() for f in fields if f not in record.values()}
        hits = [
            f
            for f, low in lowered.items()
            if any(
                low == h or low.endswith("." + h) or low.split(".")[-1] == h or h in low
                for h in _HINTS[slot]
            )
        ]
        if slot == "record_id":
            hits = [f for f in hits if f.lower().split(".")[-1] in _HINTS["record_id"]]
        hits = [
            f
            for f in hits
            if _shape_ok(slot, [str(get_path(r, f) or "") for r in rows])
        ]
        if hits:
            # The most specific name first: an exact hint, then the shortest.
            hits.sort(
                key=lambda f: (f.lower().split(".")[-1] not in _HINTS[slot], len(f), f)
            )
            record[slot] = hits[0]
    # Numeric dates: the order that makes every demonstration's date read back.
    dob_format = None
    if "dob" in record:
        raw = [str(get_path(r, record["dob"]) or "") for _p, r in demos]
        if any(_NUMERIC_DATE.match(v) for v in raw):
            for order in ("MM/DD/YYYY", "DD/MM/YYYY"):
                dob_param = grounded.get("DOB") or _param_for(
                    "DOB", demos, record, bound
                )
                if dob_param and all(
                    to_iso(v, order) == str(p.get(dob_param, ""))
                    for v, (p, _r) in zip(raw, demos)
                ):
                    dob_format = order
                    break
            if dob_format is None:
                notes.append(
                    "the site's numeric dates of birth could not be read in one order"
                )
    want: dict[str, str] = {}
    for slot in WANT_SLOTS:
        chosen = grounded.get(slot) or _param_for(slot, demos, record, bound)
        if chosen:
            want[slot] = chosen
    mapping = IdentityMapping(template, record, want, dob_format, notes)
    for slot in ("fname", "lname", "DOB"):
        if slot not in want or WANT_TO_RECORD[slot] not in record:
            notes.append(f"no mapping for {slot}: every new record goes to a person")
    return mapping


def _named(slot: str, name: str) -> bool:
    low = name.lower()
    hints = _HINTS[slot] + ({"dob": ("date",), "phone": ("phone",)}.get(slot, ()))
    if slot == "fname":
        return any(h in low for h in hints) and "family" not in low
    if slot == "lname":
        return any(h in low for h in hints) and "given" not in low
    return any(h in low for h in hints)


def _same(want_slot: str, typed: Any, stored: Any) -> bool:
    a, b = str(typed or "").strip(), str(stored or "").strip()
    if not a or not b:
        return False
    if want_slot == "DOB":
        return to_iso(b, None) == a or (
            bool(_NUMERIC_DATE.match(b))
            and a in {to_iso(b, "MM/DD/YYYY"), to_iso(b, "DD/MM/YYYY")}
        )
    return _norm(a) == _norm(b)


def _param_for(
    slot: str,
    demos: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    record: Mapping[str, str],
    bound: Optional[Mapping[str, str]],
) -> Optional[str]:
    """The task parameter whose typed value is this slot's value in every
    demonstration's saved record."""

    path = record.get(WANT_TO_RECORD[slot])
    if not path:
        return None
    if bound and path in bound:
        return bound[path]
    params = list(demos[0][0]) if demos else []
    for param in params:
        ok = True
        for typed, saved in demos:
            value = str(get_path(saved, path) or "")
            if slot == "DOB":
                value = value[:10]
            if _norm(typed.get(param)) != _norm(value) or not _norm(value):
                ok = False
                break
        if ok:
            return param
    return None


# -- verification -------------------------------------------------------------------


@dataclass
class MappingCheck:
    demos_agree: dict[str, int]
    demos_total: int
    suite: dict[str, Any]
    reference_suite: Optional[dict[str, Any]] = None

    @property
    def demos_ok(self) -> bool:
        return all(n == self.demos_total for n in self.demos_agree.values())


def check_demos(
    mapping: IdentityMapping,
    demos: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, int]:
    """Per input: how many demonstrations read back what was typed."""

    agree = {slot: 0 for slot in mapping.want}
    for typed, saved in demos:
        slots = mapping.record_slots(saved)
        want = mapping.want_of(typed)
        for slot in mapping.want:
            if _norm(want.get(slot)) == _norm(slots.get(WANT_TO_RECORD[slot])):
                agree[slot] += 1
    return agree


ERRORS = (
    "first_typo",
    "last_typo",
    "both_names_typo",
    "day_month_swapped",
    "dob_digit",
    "last_changed",
    "last_hyphenated",
    "first_nickname",
    "first_initial",
    "names_swapped",
    # One case per slot that only that slot can catch: everything the
    # template could otherwise match on is changed. A referral under the prior
    # last name with a mistyped day and new contact details; a new last name
    # and a mistyped day with only the email, or only the phone, kept.
    "prior_last_name",
    "only_email_kept",
    "only_phone_kept",
)

#: Kinds whose contact details are set by the kind itself.
_OWN_CONTACT = {
    "prior_last_name": "changed",
    "only_email_kept": "kept",
    "only_phone_kept": "kept",
}


def perturbation_suite(
    rows: Sequence[Mapping[str, Any]],
    reader: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    seed: int = 0,
    nicknames: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[dict[str, Any]]:
    """Duplicates and distinct people, built from the site's own records.

    ``reader`` turns one site record into the person it holds (the inputs a
    referral for that person would carry), independently of any mapping
    under test: it is how the suite knows who each record is.
    """

    rng = random.Random(seed)
    people = [dict(reader(r)) for r in rows]
    surnames = sorted({p.get("lname", "") for p in people if p.get("lname")})
    cases: list[dict[str, Any]] = []
    for index, person in enumerate(people):
        if not (person.get("fname") and person.get("lname") and person.get("DOB")):
            continue
        cases.append(
            {"kind": "distinct", "want": person, "exclude": index, "duplicate": False}
        )
        for kind in ERRORS:
            copy = _perturb(person, kind, surnames, rng, nicknames or {})
            if copy is None:
                continue
            for contact in ("kept", "changed"):
                if kind in _OWN_CONTACT and contact != _OWN_CONTACT[kind]:
                    continue
                want = dict(copy)
                if contact == "changed" and kind not in _OWN_CONTACT:
                    for key in ("email", "phone_home", "street"):
                        if want.get(key):
                            want[key] = f"changed{rng.randrange(10**6)}"
                cases.append(
                    {
                        "kind": f"{kind}+contact_{contact}",
                        "want": want,
                        "exclude": None,
                        "duplicate": True,
                    }
                )
    return cases


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 3:
        return word + "e"
    i = rng.randrange(1, len(word) - 1)
    return word[:i] + word[i + 1] + word[i] + word[i + 2 :]


def _perturb(
    person: Mapping[str, str],
    kind: str,
    surnames: Sequence[str],
    rng: random.Random,
    nicknames: Mapping[str, Sequence[str]],
) -> Optional[dict[str, str]]:
    out = dict(person)
    others = [s for s in surnames if s.casefold() != out["lname"].casefold()]
    if kind == "first_typo":
        out["fname"] = _typo(out["fname"], rng)
    elif kind == "last_typo":
        out["lname"] = _typo(out["lname"], rng)
    elif kind == "both_names_typo":
        out["fname"], out["lname"] = _typo(out["fname"], rng), _typo(out["lname"], rng)
    elif kind in ("day_month_swapped", "dob_digit"):
        y, m, d = out["DOB"].split("-")
        if kind == "day_month_swapped":
            if m == d or int(d) > 12:
                return None
            out["DOB"] = f"{y}-{d}-{m}"
        else:
            out["DOB"] = f"{y}-{m}-{int(d) + (1 if int(d) < 28 else -1):02d}"
    elif kind == "last_changed" and others:
        out["lname"] = rng.choice(others)
    elif kind == "last_hyphenated" and others:
        out["lname"] = f"{out['lname']}-{rng.choice(others)}"
    elif kind == "first_nickname":
        options = sorted(nicknames.get(out["fname"].casefold(), ()))
        if not options:
            return None
        out["fname"] = rng.choice(options).title()
    elif kind == "first_initial":
        out["fname"] = out["fname"][0]
    elif kind == "names_swapped":
        out["fname"], out["lname"] = out["lname"], out["fname"]
    elif kind in ("prior_last_name", "only_email_kept", "only_phone_kept"):
        y, m, d = out["DOB"].split("-")
        out["DOB"] = f"{y}-{m}-{int(d) + (1 if int(d) < 28 else -1):02d}"
        if kind == "prior_last_name":
            if not out.get("prior_lname"):
                return None
            out["lname"] = out["prior_lname"]
        else:
            if not others:
                return None
            keep = "email" if kind == "only_email_kept" else "phone_home"
            if not out.get(keep):
                return None
            out["lname"] = rng.choice(others)
            for key in ("email", "phone_home", "street"):
                if key != keep and out.get(key):
                    out[key] = f"changed{rng.randrange(10**6)}"
    else:
        return None
    return out


def run_suite(
    cases: Sequence[Mapping[str, Any]],
    table: Sequence[Any],
    decide: Callable[[Mapping[str, str], Sequence[Any]], str],
) -> dict[str, Any]:
    """Misses and false stops of ``decide`` on the suite, over ``table``."""

    missed: dict[str, int] = {}
    missed_cases: list[int] = []
    false_stops = 0
    duplicates = distinct = 0
    for index, case in enumerate(cases):
        rows = (
            list(table)
            if case["exclude"] is None
            else [r for i, r in enumerate(table) if i != case["exclude"]]
        )
        stop = decide(case["want"], rows) != "none"
        if case["duplicate"]:
            duplicates += 1
            if not stop:
                missed[case["kind"]] = missed.get(case["kind"], 0) + 1
                missed_cases.append(index)
        else:
            distinct += 1
            false_stops += int(stop)
    return {
        "duplicates": duplicates,
        "missed": sum(missed.values()),
        "missed_by_kind": dict(sorted(missed.items())),
        "missed_cases": missed_cases,
        "distinct": distinct,
        "false_stops": false_stops,
    }


@dataclass
class MappingAdoption:
    use: str  # generated | reference
    why: str
    generated: dict[str, Any]
    reference: Optional[dict[str, Any]]


def adopt_mapping(
    generated: dict[str, Any], reference: Optional[dict[str, Any]]
) -> MappingAdoption:
    """Use the generated mapping only if it does no worse on the suite: it
    catches every duplicate the reference catches, and stops no more existing
    records."""

    if reference is None:
        return MappingAdoption(
            "generated", "no reference mapping to compare with", generated, None
        )
    if (
        not set(generated.get("missed_cases", []))
        - set(reference.get("missed_cases", []))
        and generated["false_stops"] <= reference["false_stops"]
    ):
        return MappingAdoption(
            "generated",
            f"on the perturbation suite it misses {generated['missed']} and stops "
            f"{generated['false_stops']} distinct records, the reference "
            f"{reference['missed']} and {reference['false_stops']}",
            generated,
            reference,
        )
    return MappingAdoption(
        "reference",
        f"on the perturbation suite the generated mapping misses {generated['missed']} "
        f"and stops {generated['false_stops']} distinct records, the reference "
        f"{reference['missed']} and {reference['false_stops']}: the reference stays",
        generated,
        reference,
    )
