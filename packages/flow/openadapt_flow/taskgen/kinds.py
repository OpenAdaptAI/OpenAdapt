"""Operation kinds, sampled worlds and scripted UI solutions.

Every kind is defined over a profile's roles, never over an app: the
*person* record (a patient, a borrower), its *child* records (encounters,
disbursements) and a read-only *lookup* record that holds hidden state
(a referral, a statement line).

==================  =============================================  ==============
kind                what the agent does                            contract mode
==================  =============================================  ==============
``create``          add a child record to a person                 create
``update``          change a person's contact fields               update
``update_child``    change a field of one of a person's records    update
``cancel``          cancel or void one of a person's records       update
``merge``           mark a duplicate person as merged into another update
==================  =============================================  ==============

A *reconcile* task is an ``update_child`` whose new value is read from the
lookup record, and *duplicate-detect-then-merge* is a ``merge`` whose
duplicate the agent has to find. Multi-step and multi-app tasks chain these.

A solution is a list of the OpenEnv web actions (``click``, ``fill``,
``select``, ``goto``, ``wait``, ``done``) that reaches the effect through the
screens alone.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from openadapt_flow.taskgen.profile import EntitySpec, Profile
from openadapt_flow.taskgen.workbench import slug

KINDS = ("create", "update", "update_child", "cancel", "merge")


@dataclass
class World:
    """Seed rows for one task, in insertion order, with their ids."""

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def add(self, entity: str, row: dict[str, Any]) -> int:
        bucket = self.rows.setdefault(entity, [])
        bucket.append(row)
        return len(bucket)  # ids restart at 1 per table on reset

    def find(self, entity: str, **match: Any) -> list[tuple[int, dict[str, Any]]]:
        return [
            (i + 1, r)
            for i, r in enumerate(self.rows.get(entity, []))
            if all(r.get(k) == v for k, v in match.items())
        ]

    def get(self, entity: str, rid: int) -> dict[str, Any]:
        return self.rows[entity][rid - 1]

    def setup_rows(self) -> dict[str, list[dict[str, Any]]]:
        """The rows for the workbench's reset: generator-only keys (``_target``)
        dropped, ``_as`` aliases kept."""
        return {
            e: [
                {k: v for k, v in r.items() if k == "_as" or not k.startswith("_")}
                for r in rows
            ]
            for e, rows in self.rows.items()
        }


# -- values -------------------------------------------------------------------


class Sampler:
    """Seeded values drawn from a profile's pools."""

    def __init__(self, profile: Profile, seed: int) -> None:
        self.p = profile
        self.r = random.Random(seed)
        self.used_keys: set[str] = set()
        self.used_names: set[tuple[str, str]] = set()

    def pick(self, pool: str) -> str:
        return self.r.choice(self.p.pools[pool])

    def key(self) -> str:
        while True:
            k = str(self.r.randint(100000, 999999))
            if k not in self.used_keys and lookalike(k) != k:
                self.used_keys.add(k)
                return k

    def name(self) -> tuple[str, str]:
        while True:
            n = (self.pick(self.p.names["first"]), self.pick(self.p.names["last"]))
            if n not in self.used_names:
                self.used_names.add(n)
                return n

    def dob(self) -> str:
        return f"{self.r.randint(1940, 2004)}-{self.r.randint(1, 12):02d}-{self.r.randint(1, 28):02d}"

    def phone(self) -> str:
        return f"555-{self.r.randint(100, 999)}-{self.r.randint(1000, 9999)}"

    def email(self, first: str, last: str) -> str:
        return f"{first.lower()}.{last.lower()}{self.r.randint(1, 99)}@{self.pick('email_domain')}"

    def amount(self) -> str:
        return f"{self.r.randint(5, 400) * 50}.00"

    def value(
        self,
        entity: EntitySpec,
        fname: str,
        *,
        avoid: Any = None,
        person: Optional[dict[str, Any]] = None,
    ) -> str:
        f = entity.field(fname)
        for _ in range(50):
            if f.format == "email":
                n = self.p.names
                first, last = (
                    (person[n["first"]], person[n["last"]]) if person else self.name()
                )
                v = self.email(first, last)
                if v != avoid:
                    return v
                continue
            if f.type == "choice":
                v = self.r.choice(f.choices)
            elif f.type == "number":
                v = self.amount()
            elif f.type == "date":
                v = self.dob()
            elif fname in self.p.pools:
                v = self.pick(fname)
            elif f.format == "phone":
                v = self.phone()
            else:
                v = self.pick(next(iter(self.p.pools)))
            if v != avoid:
                return v
        raise ValueError(f"no second value for {entity.name}.{fname}")


def lookalike(key: str) -> str:
    """The key with its two middle characters swapped (``104729`` -> ``107429``)."""
    i = len(key) // 2 - 1
    return key[:i] + key[i + 1] + key[i] + key[i + 2 :]


def person_row(
    s: Sampler,
    prof: Profile,
    *,
    first: Optional[str] = None,
    last: Optional[str] = None,
    key: Optional[str] = None,
    dob: Optional[str] = None,
) -> dict[str, Any]:
    P = prof.entity(prof.person)
    fn, ln = (first, last) if first and last else s.name()
    n = prof.names
    row: dict[str, Any] = {
        P.key_field: key or s.key(),
        n["first"]: fn,
        n["last"]: ln,
        n["birth"]: dob or s.dob(),
    }
    # Records carry a history: a person saved a few times before the task.
    row["version"] = s.r.randint(1, 5)
    for c in P.changes:
        row[c] = s.value(P, c, person=row)
    return row


def child_row(
    s: Sampler, prof: Profile, parent_alias: str, *, used: set[str]
) -> dict[str, Any]:
    C = prof.entity(prof.child)
    row: dict[str, Any] = {C.parent_ref[1]: f"${parent_alias}"}
    for f in C.editable:
        v = s.value(C, f.name)
        if f.name == C.identify_by:
            while v in used:
                v = s.value(C, f.name)
            used.add(v)
        row[f.name] = v
    return row


def base_world(s: Sampler, prof: Profile, n_people: int = 6) -> World:
    """A handful of people, each with one to three records."""

    w = World()
    for i in range(n_people):
        row = person_row(s, prof)
        row["_as"] = f"p{i + 1}"
        w.add(prof.person, row)
    for i in range(n_people):
        used: set[str] = set()  # one person's records differ in identify_by
        for _ in range(s.r.randint(1, 3)):
            w.add(prof.child, child_row(s, prof, f"p{i + 1}", used=used))
    return w


# -- solutions ----------------------------------------------------------------


def sign_in(prof: Profile) -> list[dict[str, Any]]:
    a = prof.agent
    return [
        {"kind": "fill", "selector": "#username", "text": a["user"]},
        {"kind": "fill", "selector": "#password", "text": a["password"]},
        {"kind": "click", "selector": "#signin"},
    ]


def open_person(
    prof: Profile, key: str, *, search: Optional[str] = None
) -> list[dict[str, Any]]:
    P = prof.entity(prof.person)
    return [
        {"kind": "click", "selector": f"#nav-{P.name}"},
        {"kind": "fill", "selector": "#search-q", "text": search or key},
        {"kind": "click", "selector": "#search-go"},
        {"kind": "click", "selector": f"#open-{P.name}-{slug(key)}"},
    ]


def form_fill(entity: EntitySpec, values: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, v in values.items():
        f = entity.field(name)
        if f.type == "choice":
            out.append({"kind": "select", "selector": f"#in-{name}", "value": str(v)})
        else:
            out.append({"kind": "fill", "selector": f"#in-{name}", "text": str(v)})
    return out


@dataclass
class Write:
    """One write a task asks for: the contract kind, its values, and the UI
    steps that make it once a person's record is open."""

    kind: str
    values: dict[str, Any]
    steps: list[dict[str, Any]]
    submit: dict[str, Any]
    #: The page to come back to (a ``goto`` path) when the save has to be redone.
    form_path: str = ""

    @property
    def actions(self) -> list[dict[str, Any]]:
        return self.steps + [self.submit]


def contract_kind(prof: Profile, kind: str) -> str:
    """The verify_contract kind name for an operation kind."""
    return {
        "create": f"create_{prof.child}",
        "update": f"update_{prof.person}",
        "update_child": f"update_{prof.child}",
        "cancel": f"cancel_{prof.child}",
        "merge": f"merge_{prof.person}",
    }[kind]


def make_write(
    prof: Profile,
    kind: str,
    w: World,
    person_id: int,
    *,
    child_id: Optional[int] = None,
    new: Optional[dict[str, Any]] = None,
    survivor_id: Optional[int] = None,
) -> Write:
    """The write ``kind`` on the person ``person_id``. ``new`` holds the
    values the task sets (field -> value)."""

    P, C = prof.entity(prof.person), prof.entity(prof.child)
    person = w.get(prof.person, person_id)
    key = person[P.key_field]
    new = dict(new or {})
    ck = contract_kind(prof, kind)
    if kind == "create":
        values = {P.key_field: key, **new}
        steps = [{"kind": "click", "selector": f"#new-{C.name}"}] + form_fill(C, new)
        return Write(
            ck,
            values,
            steps,
            {"kind": "click", "selector": "#save"},
            f"/e/{C.name}/new?parent={person_id}",
        )
    if kind == "update":
        values = {P.key_field: key, **{f"new_{k}": v for k, v in new.items()}}
        steps = [{"kind": "click", "selector": "#edit"}] + form_fill(P, new)
        return Write(
            ck,
            values,
            steps,
            {"kind": "click", "selector": "#save"},
            f"/e/{P.name}/{person_id}/edit",
        )
    if kind == "merge":
        assert survivor_id is not None
        skey = w.get(prof.person, survivor_id)[P.key_field]
        m = P.merge
        values = {
            P.key_field: key,
            f"new_{m['status_field']}": m["status_value"],
            f"new_{m['into_field']}": skey,
        }
        steps = [
            {"kind": "click", "selector": "#merge"},
            {"kind": "select", "selector": "#in-merge-into", "value": skey},
        ]
        return Write(
            ck,
            values,
            steps,
            {"kind": "click", "selector": "#confirm-merge"},
            f"/e/{P.name}/{person_id}/merge",
        )
    assert child_id is not None
    child = w.get(prof.child, child_id)
    ident = C.identity_field
    open_child = [{"kind": "click", "selector": f"#open-{C.name}-{child_id}"}]
    if kind == "update_child":
        values = {
            P.key_field: key,
            ident: child[ident],
            **{f"new_{k}": v for k, v in new.items()},
        }
        steps = (
            open_child + [{"kind": "click", "selector": "#edit"}] + form_fill(C, new)
        )
        return Write(
            ck,
            values,
            steps,
            {"kind": "click", "selector": "#save"},
            f"/e/{C.name}/{child_id}/edit",
        )
    if kind == "cancel":
        cf = C.cancel
        reason = new[cf["reason_field"]]
        values = {
            P.key_field: key,
            ident: child[ident],
            f"new_{cf['status_field']}": cf["status_value"],
            f"new_{cf['reason_field']}": reason,
        }
        steps = open_child + [
            {"kind": "click", "selector": "#cancel"},
            {"kind": "fill", "selector": f"#in-{cf['reason_field']}", "text": reason},
        ]
        return Write(
            ck,
            values,
            steps,
            {"kind": "click", "selector": "#confirm-cancel"},
            f"/e/{C.name}/{child_id}/cancel",
        )
    raise ValueError(f"unknown kind {kind!r}")


def kinds_yaml(prof: Profile) -> dict[str, Any]:
    """The ``kinds.yaml`` :mod:`~openadapt_flow.generate.verify_contract`
    reads: each contract kind and its parameter names. Examples are added
    from the demonstrations."""

    P, C = prof.entity(prof.person), prof.entity(prof.child)
    return {
        "kinds": [
            {
                "kind": contract_kind(prof, "create"),
                "parameters": [P.key] + [f.name for f in C.editable],
            },
            {
                "kind": contract_kind(prof, "update"),
                "parameters": [P.key] + [f"new_{c}" for c in P.changes],
            },
            {
                "kind": contract_kind(prof, "update_child"),
                "parameters": [P.key, C.identify_by] + [f"new_{c}" for c in C.changes],
            },
            {
                "kind": contract_kind(prof, "cancel"),
                "parameters": [
                    P.key,
                    C.identify_by,
                    f"new_{C.cancel['status_field']}",
                    f"new_{C.cancel['reason_field']}",
                ],
            },
            {
                "kind": contract_kind(prof, "merge"),
                "parameters": [
                    P.key,
                    f"new_{P.merge['status_field']}",
                    f"new_{P.merge['into_field']}",
                ],
            },
        ]
    }
