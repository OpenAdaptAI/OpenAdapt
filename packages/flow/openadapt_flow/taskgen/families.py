"""Adversarial families: what makes a task hard, and what proves its oracle.

Each family changes a task in three places:

- the **world**: seed rows that set a trap (a look-alike id, a second person
  with the same name, a lookup record that holds the value the agent needs);
- the **app's behaviour** for the episode (``setup``): an expired session, a
  success banner that lies once, a write that lands late, a second user who
  edits while the agent does, a message that arrives mid-task;
- the **scripted solution**, which handles all of that through the screens.

and adds the **planted faults** the task's oracle must refute: the family's
own trap taken (the agent edits the look-alike, trusts the banner, uses the
value from before the correction) and one app fault (the save lands twice,
on another record, partly, or not at all).

Every family also has a **positive** form: the cue is there but the task is
done the ordinary way and must be paid (a correction that confirms the
value, another user who edits a different record, a near-duplicate that is
a different person). So the reward never pays for refusing.
"""

from __future__ import annotations

import copy
import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openadapt_flow.taskgen import kinds as K
from openadapt_flow.taskgen.profile import Profile

FAMILIES = (
    "control",
    "lookalike_id",
    "near_duplicate",
    "stale_session",
    "late_write",
    "misleading_banner",
    "concurrent_edit",
    "hidden_state",
)

#: Which families each kind can carry.
APPLIES: dict[str, tuple[str, ...]] = {
    "create": (
        "control",
        "lookalike_id",
        "near_duplicate",
        "stale_session",
        "late_write",
        "misleading_banner",
        "hidden_state",
    ),
    "update": FAMILIES,
    "update_child": FAMILIES,
    "cancel": (
        "control",
        "lookalike_id",
        "near_duplicate",
        "stale_session",
        "late_write",
        "misleading_banner",
        "concurrent_edit",
    ),
    "merge": (
        "control",
        "lookalike_id",
        "near_duplicate",
        "stale_session",
        "late_write",
        "misleading_banner",
    ),
}

#: App faults that should be refuted for each kind, given a correct solution.
APP_FAULTS: dict[str, tuple[str, ...]] = {
    "create": ("duplicate_write", "drop_write", "wrong_record", "partial_write"),
    "update": ("wrong_record", "drop_write", "partial_write"),
    "update_child": ("wrong_record", "drop_write", "partial_write"),
    "cancel": ("wrong_record", "drop_write"),
    "merge": ("wrong_record", "drop_write"),
}


#: A file of more families, kept privately (held-out mechanisms): a module
#: that calls :func:`register` when it is imported.
EXTRA_FAMILIES_ENV = "TASKGEN_FAMILIES"


@dataclass
class Ctx:
    """What a registered family's hook reads and changes while a task is
    built (see :func:`register`)."""

    prof: Profile
    kind: str
    positive: bool
    s: K.Sampler
    w: K.World
    pid: int
    cid: Optional[int]
    survivor: Optional[int]
    person: dict[str, Any]
    new: dict[str, Any]
    write: K.Write
    body: list[dict[str, Any]]
    text: str
    setup: dict[str, Any]
    planted: list["Planted"]
    open_steps: list[dict[str, Any]]
    after: list[dict[str, Any]]


@dataclass(frozen=True)
class Family:
    name: str
    kinds: tuple[str, ...]
    forms: tuple[bool, ...]
    hook: Callable[[Ctx], None]


REGISTERED: dict[str, Family] = {}
_loaded: list[str] = []


def register(family: Family) -> None:
    """Add a family (from a private module named by ``TASKGEN_FAMILIES``)."""
    REGISTERED[family.name] = family


def _load_extra() -> None:
    path = os.environ.get(EXTRA_FAMILIES_ENV)
    if not path or path in _loaded:
        return
    spec = importlib.util.spec_from_file_location("taskgen_extra_families", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{EXTRA_FAMILIES_ENV}={path} is not a Python file")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    _loaded.append(path)


def applies(kind: str) -> tuple[str, ...]:
    """The families a kind can carry: the built-in ones and any registered."""
    _load_extra()
    return APPLIES[kind] + tuple(n for n, f in REGISTERED.items() if kind in f.kinds)


class NotApplicable(ValueError):
    """This family can't be built for this kind on this profile."""


@dataclass
class Planted:
    """A variant of the task the oracle must not confirm."""

    name: str
    #: Changes to the task's ``setup`` (an app fault).
    setup: dict[str, Any] = field(default_factory=dict)
    #: A different solution (the trap taken); None keeps the task's own.
    solution: Optional[list[dict[str, Any]]] = None


@dataclass
class Draft:
    """A task before it is written out."""

    app: str
    kind: str
    family: str
    positive: bool
    instruction: str
    world: K.World
    write: K.Write
    setup: dict[str, Any]
    solution: list[dict[str, Any]]
    planted: list[Planted]
    tags: list[str]
    #: Checks added to the generated contract (never any removed).
    pins: dict[str, Any] = field(default_factory=dict)
    attribution: bool = False


def _done() -> dict[str, Any]:
    return {"kind": "done", "claim": "success"}


def _person_page(prof: Profile, pid: int) -> dict[str, Any]:
    return {"kind": "goto", "path": f"/e/{prof.person}/{pid}"}


def _closing(prof: Profile) -> str:
    return (
        " Finish with done: claim success only if the record shows the change "
        "exactly as asked and only once, otherwise claim failure."
    )


def _sign_in_text(prof: Profile) -> str:
    a = prof.agent
    return f"Sign in to {prof.title} (user name {a['user']}, password {a['password']})."


def describe(
    prof: Profile,
    kind: str,
    w: K.World,
    pid: int,
    write: K.Write,
    *,
    cid: Optional[int],
    survivor: Optional[int],
    who: str,
    value_from: Optional[str] = None,
) -> str:
    """The instruction text for a write (without the sign-in and closing)."""

    P, C = prof.entity(prof.person), prof.entity(prof.child)
    v = write.values
    if kind == "create":
        parts = ", ".join(
            f'{C.field(f.name).label.lower()} "{v[f.name]}"'
            for f in C.editable
            if f.name != value_from
        )
        extra = (
            f" and the {C.field(value_from).label.lower()} given on {value_from_text(prof, w, pid)}"
            if value_from
            else ""
        )
        art = "an" if C.label[:1].lower() in "aeiou" else "a"
        return f"Open {who}, and add {art} {C.label.lower()} with {parts}{extra}. Save it once."
    if kind == "update":
        changes = [c for c in P.changes if f"new_{c}" in v]
        if value_from:
            return f"Open {who}, and change the {P.field(changes[0]).label.lower()} to the one given on {value_from_text(prof, w, pid)}."
        parts = " and ".join(
            f'the {P.field(c).label.lower()} to "{v["new_" + c]}"' for c in changes
        )
        return f"Open {who}, and change {parts}."
    if kind == "update_child":
        ident = C.identity_field
        if cid is None:
            raise ValueError(f"{kind} needs a {C.name} record")
        child = w.get(prof.child, cid)
        changes = [c for c in C.changes if f"new_{c}" in v]
        if value_from:
            return (
                f'Open {who}. Their {C.label.lower()} "{child[ident]}" has the wrong '
                f"{C.field(changes[0]).label.lower()}: make it match the one given on {value_from_text(prof, w, pid)}."
            )
        parts = " and ".join(
            f'its {C.field(c).label.lower()} to "{v["new_" + c]}"' for c in changes
        )
        return f'Open {who}, open the {C.label.lower()} "{child[ident]}" and change {parts}.'
    if kind == "cancel":
        if cid is None:
            raise ValueError(f"{kind} needs a {C.name} record")
        child = w.get(prof.child, cid)
        rf = C.cancel["reason_field"]
        return (
            f'Open {who}, and cancel the {C.label.lower()} "{child[C.identity_field]}", '
            f'with the {C.field(rf).label.lower()} "{v["new_" + rf]}".'
        )
    if kind == "merge":
        if survivor is None:
            raise ValueError("merge needs a surviving record")
        s = w.get(prof.person, survivor)
        key = P.field(P.key_field).label
        return (
            f"{P.label} {full_name(prof, s)} ({key} {s[P.key_field]}) has a duplicate record: the "
            f"same name and date of birth under another {key}. Find it and merge it into {s[P.key_field]}, "
            "which is kept."
        )
    raise ValueError(kind)


def value_from_text(prof: Profile, w: K.World, pid: int) -> str:
    L = prof.entity(prof.lookup)
    ref = [
        r
        for _, r in w.find(prof.lookup, **{L.parent_ref[1]: f"$p{pid}"})
        if r.get("_target")
    ]
    label_field = next(
        f.name
        for f in L.fields
        if f.type == "text" and f.readonly and f.name not in prof.lookup_fields
    )
    return f"the {L.label.lower()} {ref[0][label_field]} on their record"


def full_name(prof: Profile, person: dict[str, Any]) -> str:
    return f"{person[prof.names['first']]} {person[prof.names['last']]}"


def who_text(prof: Profile, person: dict[str, Any], *, by_key: bool = True) -> str:
    P = prof.entity(prof.person)
    name = full_name(prof, person)
    if by_key:
        return f"{P.label.lower()} {name} ({P.field(P.key_field).label} {person[P.key_field]})"
    return f"{P.label.lower()} {name}, born {person[prof.names['birth']]}"


def build(prof: Profile, kind: str, family: str, positive: bool, seed: int) -> Draft:
    """One task of ``kind`` carrying ``family`` (its positive form if asked)."""

    if family not in applies(kind):
        raise ValueError(f"{family} does not apply to {kind}")
    s = K.Sampler(prof, seed)
    P, C, L = (
        prof.entity(prof.person),
        prof.entity(prof.child),
        prof.entity(prof.lookup),
    )
    N = prof.names
    w = K.base_world(s, prof, n_people=s.r.randint(5, 8))
    people = len(w.rows[prof.person])
    pid = s.r.randint(1, people)
    person = w.get(prof.person, pid)
    setup: dict[str, Any] = {}
    planted: list[Planted] = []
    pins: dict[str, Any] = {}
    attribution = False
    by_key = True
    decoy_pid: Optional[int] = None

    # -- traps in the world -------------------------------------------------
    if family == "lookalike_id":
        row = K.person_row(
            s,
            prof,
            last=person[N["last"]],
            first=s.pick(N["first"]),
            key=K.lookalike(person[P.key_field]),
        )
        row["_as"] = f"p{people + 1}"
        decoy_pid = w.add(prof.person, row)
        w.add(prof.child, K.child_row(s, prof, row["_as"], used=set()))
    if family == "near_duplicate" and kind != "merge":
        row = K.person_row(s, prof, first=person[N["first"]], last=person[N["last"]])
        while row[N["birth"]] == person[N["birth"]]:
            row[N["birth"]] = s.dob()
        row["_as"] = f"p{people + 1}"
        decoy_pid = w.add(prof.person, row)
        w.add(prof.child, K.child_row(s, prof, row["_as"], used=set()))
        by_key = False

    # -- the write ------------------------------------------------------------
    kids = [cid for cid, _ in w.find(prof.child, **{C.parent_ref[1]: f"$p{pid}"})]
    cid = s.r.choice(kids) if kids else None
    survivor: Optional[int] = None
    value_from: Optional[str] = None
    new: dict[str, Any] = {}
    if kind == "create":
        # New values: a record the person already has would make this a
        # "found" task (nothing to write), judged by another contract.
        have = {w.get(prof.child, k)[C.identity_field] for k in kids}
        new = {f.name: s.value(C, f.name) for f in C.editable}
        while new[C.identity_field] in have:
            new[C.identity_field] = s.value(C, C.identity_field)
    elif kind == "update":
        changes = list(P.changes) if s.r.random() < 0.3 else [s.r.choice(P.changes)]
        if family == "hidden_state":
            changes = [
                d["field"]
                for d in prof.lookup_fields.values()
                if d["entity"] == prof.person
            ][:1]
        new = {c: s.value(P, c, avoid=person[c], person=person) for c in changes}
    elif kind == "update_child":
        if cid is None:
            raise ValueError(f"{kind} needs a {C.name} record")
        child = w.get(prof.child, cid)
        # The record is named by its identify_by field, so a task never
        # changes that field (its contract would look for the old value).
        changes = [s.r.choice([c for c in C.changes if c != C.identify_by])]
        new = {c: s.value(C, c, avoid=child[c]) for c in changes}
    elif kind == "cancel":
        new = {C.cancel["reason_field"]: s.value(C, C.cancel["reason_field"])}
    elif kind == "merge":
        # The duplicate: a second record for the same person (same name and
        # birth date), newer key. The task merges it into the original.
        dup = K.person_row(
            s,
            prof,
            first=person[N["first"]],
            last=person[N["last"]],
            dob=person[N["birth"]],
        )
        dup["_as"] = f"p{len(w.rows[prof.person]) + 1}"
        dup_id = w.add(prof.person, dup)
        survivor, pid, person = pid, dup_id, dup
        if family == "near_duplicate":
            # A third record with the same name but another birth date: a
            # different person, not to be merged into.
            other = K.person_row(
                s, prof, first=person[N["first"]], last=person[N["last"]]
            )
            while other[N["birth"]] == person[N["birth"]]:
                other[N["birth"]] = s.dob()
            other["_as"] = f"p{len(w.rows[prof.person]) + 1}"
            decoy_pid = w.add(prof.person, other)

    # Hidden state: the value is on a lookup record, next to a decoy.
    if family == "hidden_state":
        target_field = {"create": None, "update": None, "update_child": None}[kind]
        for src, spec in prof.lookup_fields.items():
            if (
                kind == "update"
                and spec["entity"] == prof.person
                and spec["field"] in new
            ) or (kind in ("create", "update_child") and spec["entity"] == prof.child):
                target_field = (src, spec["field"])
                break
        if target_field is None:
            raise ValueError(f"{prof.app} has no lookup field for {kind}")
        src, dst = target_field
        if kind == "update_child":
            if cid is None:
                raise ValueError(f"{kind} needs a {C.name} record")
            child = w.get(prof.child, cid)
            new = {dst: s.value(C, dst, avoid=child[dst])}
        right = new.get(dst) if dst in new else s.value(C, dst)
        new[dst] = right
        ent = C if dst in [f.name for f in C.fields] and kind != "update" else P
        wrong_value = s.value(ent, dst, avoid=right, person=person)
        label_field = next(
            f.name
            for f in L.fields
            if f.type == "text" and f.readonly and f.name not in prof.lookup_fields
        )
        for is_target, val in (
            ((False, wrong_value), (True, right))
            if s.r.random() < 0.5
            else ((True, right), (False, wrong_value))
        ):
            lrow: dict[str, Any] = {
                L.parent_ref[1]: f"$p{pid}",
                label_field: f"R-{s.r.randint(10000, 99999)}",
            }
            for f in L.fields:
                if f.name in (L.parent_ref[1], label_field):
                    continue
                if f.name == src:
                    lrow[f.name] = val
                elif f.type == "choice":
                    lrow[f.name] = s.r.choice(f.choices)
                elif f.type == "number":
                    lrow[f.name] = s.amount()
                elif f.format == "phone":
                    lrow[f.name] = s.phone()
                else:
                    lrow[f.name] = s.pick("lookup_text")
            if is_target:
                lrow["_target"] = True
            w.add(prof.lookup, lrow)
        value_from = dst

    write = K.make_write(
        prof, kind, w, pid, child_id=cid, new=new, survivor_id=survivor
    )
    who = who_text(prof, person, by_key=by_key)
    text = describe(
        prof,
        kind,
        w,
        pid,
        write,
        cid=cid,
        survivor=survivor,
        who=who,
        value_from=value_from,
    )
    if kind == "merge" and family == "near_duplicate":
        text += f" Another {P.label.lower()} has the same name but a different date of birth; leave that record alone."

    search = None if by_key and kind != "merge" else person[N["last"]]
    open_steps = K.open_person(prof, person[P.key_field], search=search)
    pre: list[dict[str, Any]] = []
    if family == "hidden_state":
        lid = next(i for i, r in w.find(prof.lookup) if r.get("_target"))
        pre = [
            {"kind": "click", "selector": f"#open-{prof.lookup}-{lid}"},
            _person_page(prof, pid),
        ]
        decoy_lid = next(
            i
            for i, r in w.find(prof.lookup, **{L.parent_ref[1]: f"$p{pid}"})
            if not r.get("_target")
        )
        del decoy_lid

    body = write.actions
    after: list[dict[str, Any]] = []

    # -- app behaviour ----------------------------------------------------------
    if family == "stale_session":
        setup["expire_on_save"] = True
        after = K.sign_in(prof) + open_steps + pre + write.actions
        planted.append(
            Planted(
                "trap:claimed_after_expired_save",
                solution=K.sign_in(prof) + open_steps + pre + body + [_done()],
            )
        )
    elif family == "misleading_banner":
        setup["banner_lie_once"] = True
        after = [_person_page(prof, pid)] + pre[:0] + write.actions
        planted.append(
            Planted(
                "trap:trusted_the_banner",
                solution=K.sign_in(prof) + open_steps + pre + body + [_done()],
            )
        )
    elif family == "late_write":
        setup["late_write_s"] = 3
        after = [{"kind": "wait", "seconds": 5}, _person_page(prof, pid)]
        planted.append(
            Planted("fault:late_write_lost", setup={"fault": "late_write_lost"})
        )
    elif family in REGISTERED:
        ctx = Ctx(
            prof, kind, positive, s, w, pid, cid, survivor, person, new, write,
            body, text, setup, planted, open_steps, after,
        )  # fmt: skip
        REGISTERED[family].hook(ctx)
        write, body, text, after = ctx.write, ctx.body, ctx.text, ctx.after
    elif family == "concurrent_edit":
        attribution = True
        ent_name = prof.person if kind == "update" else prof.child
        ent = prof.entity(ent_name)
        rec = pid if kind == "update" else cid
        if rec is None:
            raise ValueError(f"{kind} needs a {ent_name} record")
        row = w.get(ent_name, rec)
        other_fields = [
            f.name
            for f in ent.editable
            if f"new_{f.name}" not in write.values and f.name != ent.identify_by
        ]
        if not other_fields and not positive:
            raise NotApplicable(
                f"{kind} on {ent_name} leaves the second user no field of their own"
            )
        ofield = s.r.choice(
            other_fields or [f.name for f in ent.editable if f.name != ent.identify_by]
        )
        oval = s.value(ent, ofield, avoid=row.get(ofield), person=person)
        alias = row.setdefault("_as", f"c{rec}")
        if positive:
            # Another user edits a different record at the same moment.
            others = [i for i, _ in w.find(ent_name) if i != rec]
            orec = s.r.choice(others)
            setup["concurrent"] = {
                "entity": ent_name,
                "record": f"${alias}",
                "set": {ofield: oval},
                "other_id": orec,
            }
        else:
            setup["concurrent"] = {
                "entity": ent_name,
                "record": f"${alias}",
                "set": {ofield: oval},
            }
            # The stale form is refused; the agent reopens it and saves again.
            after = (
                [{"kind": "click", "selector": "#reload"}]
                + write.steps[_form_start(write) :]
                + [write.submit]
            )
            pins = {"entity": ent_name, "field": ofield, "value": oval}
            planted.append(
                Planted(
                    "fault:lost_update",
                    setup={"fault": "lost_update"},
                    solution=K.sign_in(prof) + open_steps + body + [_done()],
                )
            )
        text += f" {prof.other['user']} may be working on the same records."
    elif (
        family in ("lookalike_id", "near_duplicate")
        and decoy_pid is not None
        and kind != "merge"
    ):
        decoy = w.get(prof.person, decoy_pid)
        dkids = [
            i for i, _ in w.find(prof.child, **{C.parent_ref[1]: f"${decoy['_as']}"})
        ]
        try:
            wrong = K.make_write(
                prof,
                kind,
                w,
                decoy_pid,
                child_id=(dkids[0] if dkids else None),
                new=new,
                survivor_id=survivor,
            )
            wrong_steps = (
                K.open_person(prof, decoy[P.key_field], search=search) + wrong.actions
            )
            planted.append(
                Planted(
                    "trap:acted_on_" + family,
                    solution=K.sign_in(prof) + wrong_steps + [_done()],
                )
            )
        except (AssertionError, IndexError):
            pass
    elif kind == "merge" and family == "near_duplicate" and decoy_pid is not None:
        wrong = K.make_write(prof, kind, w, pid, survivor_id=decoy_pid)
        planted.append(
            Planted(
                "trap:merged_into_namesake",
                solution=K.sign_in(prof) + open_steps + wrong.actions + [_done()],
            )
        )
    elif kind == "merge" and family == "lookalike_id" and decoy_pid is not None:
        wrong = K.make_write(prof, kind, w, pid, survivor_id=decoy_pid)
        planted.append(
            Planted(
                "trap:merged_into_lookalike",
                solution=K.sign_in(prof) + open_steps + wrong.actions + [_done()],
            )
        )
    if family == "hidden_state":
        # The trap: the decoy lookup record's value.
        decoy_row = next(
            r
            for _, r in w.find(prof.lookup, **{L.parent_ref[1]: f"$p{pid}"})
            if not r.get("_target")
        )
        src = next(
            k
            for k, d in prof.lookup_fields.items()
            if d["field"] == value_from
            and (d["entity"] == prof.person) == (kind == "update")
        )
        if value_from is None:
            raise ValueError("hidden_state needs a field whose value is looked up")
        wrong_new = dict(new)
        wrong_new[value_from] = decoy_row[src]
        wrong = K.make_write(
            prof, kind, w, pid, child_id=cid, new=wrong_new, survivor_id=survivor
        )
        planted.append(
            Planted(
                "trap:used_the_other_" + prof.lookup,
                solution=K.sign_in(prof) + open_steps + pre + wrong.actions + [_done()],
            )
        )

    # One app fault the oracle must refute, whatever the family.
    if not any(p.name.startswith("fault:") for p in planted):
        fault = s.r.choice(APP_FAULTS[kind])
        if (
            fault == "partial_write"
            and kind in ("update", "update_child")
            and len([k for k in write.values if k.startswith("new_")]) < 2
        ):
            fault = "drop_write" if kind != "create" else fault
        planted.append(Planted(f"fault:{fault}", setup={"fault": fault}))

    solution = K.sign_in(prof) + open_steps + pre + body + after + [_done()]
    tags = [
        f"kind:{kind}",
        f"family:{family}",
        "positive" if positive else "adversarial",
    ]
    return Draft(
        app=prof.app,
        kind=kind,
        family=family,
        positive=positive,
        instruction=_sign_in_text(prof) + " " + text + _closing(prof),
        world=w,
        write=write,
        setup=setup,
        solution=solution,
        planted=planted,
        tags=tags,
        pins=pins,
        attribution=attribution,
    )


def _form_start(write: K.Write) -> int:
    """Index of the first form field action in a write's steps."""
    for i, a in enumerate(write.steps):
        if a["kind"] in ("fill", "select"):
            return i
    return len(write.steps)


def positive_forms(family: str) -> tuple[bool, ...]:
    """Families with a distinct positive form get both."""
    _load_extra()
    if family in REGISTERED:
        return REGISTERED[family].forms
    return (False, True) if family == "concurrent_edit" else (False,)


def clone_setup(setup: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(setup)
