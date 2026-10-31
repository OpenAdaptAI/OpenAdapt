"""Propose ``openadapt.verify/v1`` contracts from a SQL schema and demonstrations.

    python -m openadapt_flow.generate.verify_contract --help

A person setting up the verifier for an app used to write every contract by
hand, with 10 or 11 decisions per app. This generator writes them from:

- the store's schema (``information_schema`` on MySQL, MariaDB or
  PostgreSQL), read through the read-only account the contracts will use;
- a short description of each kind of write: its parameter names and one or
  two example tasks (the ``kinds.yaml`` a setup agent is given);
- one demonstration write per example, which it snapshots before and after
  to see which tables and columns the kind touches.

For each kind it proposes one contract per field set the kind's tasks can
use (an update that may change the phone, the email or both gets a contract
for each), reading the whole database so an extra record anywhere is seen,
and a manifest that picks the contract for a task's values
(:func:`choose`). Whatever it can't decide alone becomes one numbered
question in ``QUESTIONS.md``; the goal is at most three per app. Answers go
in ``answers.yaml`` and the generator is run again.

It never emits a contract that reads something that doesn't exist: every
contract is loaded and read once against the live store (a dry read) before
it is written out, and a contract that needs a database view gets it created
(``--create-views``, through an admin connection) or is refused with the
reason. See ``docs/GENERATE_CONTRACTS.md``. Nothing here calls a model.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from openadapt_flow.generate import sql_store as store
from openadapt_flow.generate.demo_analysis import (
    ColumnRole,
    Demo,
    Hop,
    Location,
    _hop_cost,
    _rank,
    best,
    column_roles,
    is_identifier,
    is_link_column,
    link_columns,
    locate,
    pick_anchor,
)
from openadapt_flow.generate.sql_store import RowChange, Schema

MANIFEST_SCHEMA = "openadapt.contract-manifest/v1"
#: Most contracts one kind gets (all subsets of up to six optional fields).
MAX_VARIANTS = 64
#: Longest list a list parameter gets a contract for.
MAX_LIST = 10
#: The recheck window proposed when the person hasn't answered.
DEFAULT_RECHECK_S = 300


class Unpinned(ValueError):
    """A column of a new row that no rule checks, in one field set only (seen
    in one demonstration, or two explanations the demonstrations can't tell
    apart), and that the answer doesn't name under ``unchecked``: that field
    set gets no contract."""


class GenerateError(RuntimeError):
    """The generator can't go on; the message says why."""


# -- inputs -------------------------------------------------------------------


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GenerateError(f"cannot read {path}: {exc}") from exc


def load_read(path: Path) -> dict[str, Any]:
    """``read.yaml``: the read-only connection, as in a ``kind: sql``
    verifier (``sql_driver``, ``sql_connect_args``, ``sql_password_env``)."""

    data = load_yaml(path)
    if not isinstance(data, dict) or not data.get("sql_driver"):
        raise GenerateError(f"{path}: needs sql_driver and sql_connect_args")
    if "password" in dict(data.get("sql_connect_args") or {}):
        raise GenerateError(
            f"{path}: put the password in an environment variable named by "
            "sql_password_env, not in the file"
        )
    return {
        k: data[k]
        for k in ("sql_driver", "sql_connect_args", "sql_password_env")
        if k in data
    }


@dataclass
class KindSpec:
    kind: str
    parameters: list[str]
    description: str = ""
    examples: list[dict[str, Any]] = field(default_factory=list)


def load_kinds(path: Path) -> list[KindSpec]:
    data = load_yaml(path) or {}
    kinds = data.get("kinds") if isinstance(data, dict) else None
    if not isinstance(kinds, list) or not kinds:
        raise GenerateError(f"{path}: needs a list under kinds:")
    out = []
    for k in kinds:
        if not isinstance(k, dict) or not k.get("kind") or not k.get("parameters"):
            raise GenerateError(f"{path}: each kind needs kind: and parameters:")
        out.append(
            KindSpec(
                str(k["kind"]),
                [str(p) for p in k["parameters"]],
                str(k.get("description") or ""),
                [
                    {
                        "instruction": e.get("instruction", ""),
                        "values": dict(e.get("values") or {}),
                    }
                    for e in k.get("examples") or []
                ],
            )
        )
    return out


def load_demos(demo_dir: Path) -> list[Demo]:
    demos = []
    for f in sorted(Path(demo_dir).glob("*.demo.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        after = store.load_snapshot(Path(demo_dir) / d["after_snapshot"])
        demos.append(
            Demo(
                kind=d["kind"],
                values=dict(d["values"]),
                diff=store.Diff.from_json(d["diff"]),
                after=after,
                started_at=d.get("started_at", ""),
                finished_at=d.get("finished_at", ""),
                name=f.name,
            )
        )
    return demos


# -- questions ------------------------------------------------------------------


@dataclass
class Question:
    key: str
    text: str
    proposal: Any
    options: list[str] = field(default_factory=list)


def _answer(answers: Mapping[str, Any], key: str) -> Any:
    return (answers or {}).get(key)


# -- the plan for one kind --------------------------------------------------------


@dataclass
class ChildGroup:
    """Rows a write adds beside the main row that hold a parameter's value."""

    table: str
    link: Hop  # child hop from the anchor
    params: dict[str, Location]  # param -> location relative to the child row
    roles: dict[str, ColumnRole]
    count: int = 1
    #: List parameter whose items these rows are, if any.
    list_param: Optional[str] = None
    changed_columns: list[str] = field(default_factory=list)
    #: Other parameters this row also points to (a contact's account link
    #: row also holds the title): with one of them given and this one not,
    #: the row is still expected, with this value empty.
    co_owners: list[str] = field(default_factory=list)
    #: The app adds this row with an empty value when the task leaves the
    #: parameter out (Redmine's custom field values): expected then too.
    empty_when_null: bool = False
    #: The parameter whose value the rows hold (``children`` may hold a
    #: second group for it: a call's lead is its parent and an attendee).
    param: str = ""
    #: An update replaced earlier rows here (removed or changed rows linked
    #: to the record), leaving ``after_count`` of them.
    replaced: bool = False
    after_count: int = 1


@dataclass
class SideSpec:
    table: str
    count: int
    roles: dict[str, ColumnRole]
    #: Used only when this parameter is not in the field set (the table holds
    #: that parameter's row otherwise).
    unless: Optional[str] = None
    #: Added rows of other tables these rows hold the key of (for ownership).
    owner: Optional[str] = None


@dataclass
class KindPlan:
    kind: str
    mode: str = ""  # create | update
    anchor: str = ""
    params: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    identity: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    locations: dict[str, Location] = field(default_factory=dict)
    #: dispatch value -> (value param -> location)
    dispatch: Optional[str] = None
    dispatch_locations: dict[str, dict[str, Location]] = field(default_factory=dict)
    children: dict[str, ChildGroup] = field(default_factory=dict)  # by param
    lists: dict[str, ChildGroup] = field(default_factory=dict)
    added_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    sides: list[SideSpec] = field(default_factory=list)
    logs: set[str] = field(default_factory=set)
    bookkeeping: dict[str, set[str]] = field(default_factory=dict)
    anchor_changed: set[str] = field(default_factory=set)
    demos: list[Demo] = field(default_factory=list)
    anchors: list[RowChange] = field(default_factory=list)
    unresolved: dict[str, list[Location]] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    removed: dict[str, int] = field(default_factory=dict)
    #: dispatch value -> group name -> rows that hold the value param.
    dispatch_children: dict[str, dict[str, "ChildGroup"]] = field(default_factory=dict)
    #: unresolved key -> (parameter, dispatch value or None).
    unresolved_meta: dict[str, tuple[str, Optional[str]]] = field(default_factory=dict)
    #: Parameters placed by name, not by a demonstration.
    by_name: list[str] = field(default_factory=list)
    #: Parameters placed where another kind's demonstrations found them.
    shared: list[str] = field(default_factory=list)
    #: Existing rows (not the target) the demonstrations changed with no
    #: task value: each the same row every time (by key) or the row a
    #: located value names (an account behind the contact's key), with the
    #: columns that moved and what kind of change each was.
    touched: list[dict[str, Any]] = field(default_factory=list)
    #: The target's own columns that moved with no task value, by kind.
    anchor_extra: dict[str, str] = field(default_factory=dict)
    #: Changed rows nothing identifies: a write that changes them stops.
    unexcused: dict[str, set[str]] = field(default_factory=dict)
    #: Tables whose added rows differ between demonstrations: logs only if a
    #: person says so.
    log_candidates: set[str] = field(default_factory=set)
    #: Where the target's versions are read (``frame.versions``): a version
    #: column that steps by one per save, or an audit table naming the
    #: record and the user who saved it. None when the demonstrations show
    #: neither.
    versions: Optional[dict[str, Any]] = None
    #: More places a value is in: rows the write added beside the record
    #: (a call's lead is its parent and also an attendee row).
    extra: dict[str, list[Location]] = field(default_factory=dict)
    #: The acting user's ids (``--actor-param``), for the roles of new rows.
    actor_values: dict[str, str] = field(default_factory=dict)


def _scalars(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in values.items()
        if v is not None and not isinstance(v, (list, dict))
    }


def _intersect(per_demo: list[list[Location]]) -> list[Location]:
    if not per_demo:
        return []
    common = {loc.signature(): loc for loc in per_demo[0]}
    for locs in per_demo[1:]:
        sigs = {loc.signature() for loc in locs}
        common = {s: loc for s, loc in common.items() if s in sigs}
    return list(common.values())


def _as_change(table: str, row: Mapping[str, Optional[str]]) -> RowChange:
    return RowChange(table, "", "added", None, dict(row))


def analyze(
    schema: Schema,
    spec: KindSpec,
    demos: Sequence[Demo],
    actor_values: Mapping[str, str],
) -> KindPlan:
    plan = KindPlan(spec.kind, params=list(spec.parameters))
    plan.actor_values = dict(actor_values)
    mine = [d for d in demos if d.kind == spec.kind and d.diff.changes]
    if not mine:
        plan.refused.append(
            "no demonstration write of this kind (record one with "
            "`verify_contract record`)"
        )
        return plan
    examples = [e["values"] for e in spec.examples] + [d.values for d in mine]
    plan.optional = sorted(
        p for p in spec.parameters if any(v.get(p) is None for v in examples)
    )
    for d in mine:
        anchor, mode, score = pick_anchor(schema, d, spec.parameters)
        if anchor is None or score == 0:
            plan.refused.append(
                f"{d.name}: no row the write touched holds any of the task's values"
            )
            return plan
        if plan.anchor and (anchor.table != plan.anchor or mode != plan.mode):
            plan.refused.append(
                f"{d.name}: the main record is {anchor.table} ({mode}), but other "
                f"demonstrations write {plan.anchor} ({plan.mode})"
            )
            return plan
        plan.anchor, plan.mode = anchor.table, mode
        plan.demos.append(d)
        plan.anchors.append(anchor)
    lists = [
        p
        for p in spec.parameters
        if any(isinstance(d.values.get(p), list) for d in plan.demos)
    ]
    links = link_columns(schema, list(zip(plan.demos, plan.anchors)))
    scalars = [p for p in spec.parameters if p not in lists]

    # Where each value is, in every demonstration that gives it.
    found: dict[str, list[list[Location]]] = {p: [] for p in scalars}
    roles_of: dict[str, set[str]] = {p: set() for p in scalars}
    for d, a in zip(plan.demos, plan.anchors):
        for p, v in _scalars(d.values).items():
            if p not in found:
                continue
            if plan.mode == "update":
                change = locate(schema, d, a, v, changed_only=True, links=links)
                ident = [
                    loc
                    for loc in locate(schema, d, a, v, before=True)
                    if not loc.hops or loc.hops[0].kind == "fk"
                ]
                if ident and (not change or _rank(ident[0]) < _rank(change[0])):
                    found[p].append(ident)
                    roles_of[p].add("identity")
                else:
                    found[p].append(change)
                    roles_of[p].add("change" if change else "missing")
            else:
                found[p].append(locate(schema, d, a, v, links=links))
    # A parameter whose value names a field (``field: status``).
    for p in scalars:
        values = [d.values.get(p) for d in plan.demos if d.values.get(p) is not None]
        if (
            len(values) >= 2
            and all(is_identifier(v) for v in values)
            and len(set(values)) == len(values)
            and all(not locs for locs in found[p])
            and plan.dispatch is None
        ):
            plan.dispatch = p
    for p in scalars:
        if p == plan.dispatch:
            continue
        if plan.dispatch is not None and "identity" not in roles_of[p]:
            by_value: dict[str, list[list[Location]]] = {}
            for d, locs in zip(
                [d for d in plan.demos if d.values.get(p) is not None], found[p]
            ):
                by_value.setdefault(str(d.values.get(plan.dispatch)), []).append(locs)
            if len(by_value) > 1:
                ok = True
                for dv, per in by_value.items():
                    loc, tied = _pick(p, _intersect(per), plan.demos, schema)
                    if loc is None:
                        key = f"{p} (when {plan.dispatch} is {dv})"
                        plan.unresolved[key] = tied
                        plan.unresolved_meta[key] = (p, dv)
                        ok = False
                    else:
                        plan.dispatch_locations.setdefault(dv, {})[p] = loc
                if ok:
                    plan.changes.append(p)
                continue
        all_found = _intersect(found[p])
        loc, tied = _pick(p, all_found, plan.demos, schema)
        if (
            loc is not None
            and _by_value_only(loc)
            and any(x != loc and not _by_value_only(x) for x in all_found)
        ):
            # The best place reaches the value through a key that only equal
            # values support (no declared foreign key, no column named after
            # its table), and a place whose every key is supported fits every
            # demonstration too: the demonstrations can't tell a real join
            # from ids that happened to be equal. A person picks.
            loc, tied = None, sorted(all_found, key=_rank)
        if loc is not None:
            plan.extra[p] = [
                x
                for x in all_found
                if x.hops
                and x.hops[0].kind == "child"
                and x != loc
                # in another table than the main place, through keys named
                # after their tables (a call's lead attendee row)
                and (not loc.hops or x.hops[0].table != loc.hops[0].table)
                and all(_hop_cost(h) <= 1 for h in x.hops[1:])
            ]
        if loc is None:
            if not found[p]:
                named = _by_name(schema, plan, p)
                if named is not None:
                    plan.locations[p] = named
                    plan.by_name.append(p)
                    if plan.mode == "update":
                        plan.changes.append(p)
                continue  # never given: no contract uses it
            plan.unresolved[p] = tied
            plan.unresolved_meta[p] = (p, None)
            continue
        plan.locations[p] = loc
        if plan.mode == "update":
            (plan.identity if "identity" in roles_of[p] else plan.changes).append(p)
    # A column one value is already in is not another value's place.
    progress = True
    while progress and plan.unresolved:
        progress = False
        claimed = {
            (loc.hops[0].column if loc.hops else c)
            for loc in plan.locations.values()
            for c in loc.columns
        }
        for name, tied in list(plan.unresolved.items()):
            # A place one key away through a column another value already
            # holds is that value's row, not this one's. A longer path through
            # it (a customer behind the project) still counts.
            left = [
                loc
                for loc in tied
                if len(loc.hops) > 1
                or (loc.hops[0].column if loc.hops else loc.columns[0]) not in claimed
            ]
            if len(left) == 1 and name in plan.params:
                plan.locations[name] = left[0]
                plan.unresolved.pop(name)
                if plan.mode == "update" and name not in plan.identity:
                    plan.changes.append(name)
                progress = True
    if plan.mode == "create":
        # A create task may leave out any value whose place can be empty: a
        # nullable column, a nullable key column, or a row beside the record.
        anchor_t = schema.tables[plan.anchor]
        for p, loc in plan.locations.items():
            col = None
            if not loc.hops and len(loc.columns) == 1:
                col = anchor_t.column(loc.columns[0])
            elif loc.hops and loc.hops[0].kind == "fk":
                col = anchor_t.column(loc.hops[0].column)
            if (col is not None and col.nullable) or (
                loc.hops and loc.hops[0].kind == "child"
            ):
                if p not in plan.optional:
                    plan.optional.append(p)
        plan.optional.sort()
    if plan.mode == "update" and not plan.identity:
        plan.refused.append(
            "no parameter names the record before the write, so the update's "
            "target can't be told apart from other records"
        )
    _children(schema, plan, lists)
    _sides(schema, plan, actor_values, demos)
    return plan


def _by_value_only(loc: Location) -> bool:
    """Does the place go through a hop that only a value match supports (no
    declared foreign key, no column named after the table)?"""

    return any(_hop_cost(h) >= 2 for h in loc.hops)


def _share_locations(
    schema: Schema, plans: Sequence[KindPlan], kinds: Sequence[KindSpec]
) -> None:
    """An update parameter no example of its kind gives (a priority), placed
    where another kind's demonstrations found the parameter of the same name
    on the same main table (``issues.priority_id -> enumerations.name``).
    Placed this way, it replaces a placement by name."""

    for plan in plans:
        if plan.mode != "update" or not plan.anchor:
            continue
        for name in plan.params:
            if name in plan.identity or name == plan.dispatch:
                continue
            if name in plan.locations and name not in plan.by_name:
                continue
            for other in plans:
                if other is plan or other.anchor != plan.anchor:
                    continue
                loc = other.locations.get(name)
                if loc is None or name in other.by_name:
                    continue
                if loc.hops and loc.hops[0].kind != "fk":
                    continue
                plan.locations[name] = loc
                if name in plan.by_name:
                    plan.by_name.remove(name)
                plan.shared.append(name)
                if name not in plan.changes:
                    plan.changes.append(name)
                break


def _pick(
    param: str,
    found: Sequence[Location],
    demos: Sequence[Demo] = (),
    schema: Optional[Schema] = None,
) -> tuple[Optional[Location], list[Location]]:
    """The best location. Among tied ones: the one whose column is named
    like the parameter (``stage``, not ``last_stage``); else drop a column
    that is only a copy of another tied one, lower-cased or digits only, in
    every row of its table (EspoCRM's ``email_address.lower``,
    ``phone_number.numeric``)."""

    loc, tied = best(found)
    if loc is not None or not tied:
        return loc, tied
    name = re.sub(r"^(new|set)_", "", param)
    named = [t for t in tied if t.columns and t.columns[-1] == name]
    if len(named) == 1:
        return named[0], []
    keep = [t for t in tied if not _derived_copy(t, tied, demos)]
    if len(keep) == 1:
        return keep[0], []
    same = _identical_columns(keep, demos, schema)
    if same is not None:
        return same, []
    return None, tied


def _identical_columns(
    tied: Sequence[Location], demos: Sequence[Demo], schema: Optional[Schema] = None
) -> Optional[Location]:
    """Tied columns of one row that hold the same value in every row of
    their table can't be told apart by any data: keep the one the table
    defines first (the source; a copy is added after it). Either compares
    exactly, so a wrong pick stops a write, never passes one."""

    if not demos or len(tied) < 2:
        return None
    first = tied[0]
    if any(
        t.hops != first.hops or len(t.columns) != 1 or t.rule != first.rule
        for t in tied
    ):
        return None
    table = first.hops[-1].table if first.hops else None
    rows = demos[0].after.get(table) if table else None
    if rows is None or rows.count_only is not None or not rows.rows:
        return None
    cols = [t.columns[0] for t in tied]
    if not all(len({r.get(c) for c in cols}) == 1 for r in rows.rows.values()):
        return None
    if schema is None or table not in schema.tables:
        return None
    order = schema.tables[table].column_names
    return min(
        tied, key=lambda t: order.index(t.columns[0]) if t.columns[0] in order else 999
    )


def _derived_copy(
    loc: Location, tied: Sequence[Location], demos: Sequence[Demo]
) -> bool:
    if len(loc.columns) != 1 or not loc.hops or not demos:
        return False
    table = loc.hops[-1].table
    rows = demos[0].after.get(table)
    if rows is None or rows.count_only is not None or not rows.rows:
        return False

    def digits(v: str) -> str:
        return "".join(ch for ch in v if ch.isdigit())

    for other in tied:
        if other is loc or other.hops != loc.hops or len(other.columns) != 1:
            continue
        a, b = other.columns[0], loc.columns[0]
        for fn in (str.lower, digits):
            if all(
                r.get(b) == (fn(str(r[a])) if r.get(a) is not None else None)
                for r in rows.rows.values()
            ) and any(r.get(a) != r.get(b) for r in rows.rows.values()):
                return True
    return False


def _all_locations(plan: KindPlan) -> list[tuple[str, Location]]:
    out = list(plan.locations.items())
    for locs in plan.dispatch_locations.values():
        out.extend(locs.items())
    for g in [*plan.children.values(), *plan.lists.values()]:
        out.extend(g.params.items())
    return out


def _confirm_phones(plan: KindPlan, confirmed: set[str]) -> None:
    """A phone rule a person didn't confirm compares the text exactly."""

    def fix(name: str, loc: Location) -> Location:
        if loc.rule == "phone" and name not in confirmed:
            return Location(loc.hops, loc.columns, "exact")
        return loc

    plan.locations = {k: fix(k, v) for k, v in plan.locations.items()}
    plan.dispatch_locations = {
        dv: {k: fix(k, v) for k, v in locs.items()}
        for dv, locs in plan.dispatch_locations.items()
    }
    for g in [*plan.children.values(), *plan.lists.values()]:
        g.params = {k: fix(k, v) for k, v in g.params.items()}


def _resolve(plan: KindPlan, key: str, loc: Location) -> None:
    """Place an unresolved value (answered, or settled by a tie-break)."""

    param, dv = plan.unresolved_meta.get(key, (key, None))
    plan.unresolved.pop(key, None)
    if dv is not None:
        plan.dispatch_locations.setdefault(dv, {})[param] = loc
        if param not in plan.changes:
            plan.changes.append(param)
        return
    plan.locations[param] = loc
    if plan.mode == "update" and param not in plan.identity + plan.changes:
        plan.changes.append(param)


def _tail(plan: KindPlan, loc: Location) -> tuple[str, tuple[str, ...]]:
    return (loc.hops[-1].table if loc.hops else plan.anchor, loc.columns)


def _break_ties_across(plans: Sequence[KindPlan]) -> None:
    """A value tied between columns of one table (an email's ``name`` and
    ``lower``) goes where another kind's demonstrations put the same kind
    of value, if exactly one tied place is one of those."""

    for plan in plans:
        for key, tied in list(plan.unresolved.items()):
            if not tied:
                continue
            seen = {
                _tail(o, loc)
                for o in plans
                if o is not plan
                for loc in [
                    *o.locations.values(),
                    *[x for d in o.dispatch_locations.values() for x in d.values()],
                ]
            }
            hits = [t for t in tied if _tail(plan, t) in seen]
            if len(hits) == 1:
                _resolve(plan, key, hits[0])


def _by_name(schema: Schema, plan: KindPlan, param: str) -> Optional[Location]:
    """An update parameter no demonstration gives (``new_zip``), placed at
    the main table's column of the same name after a leading ``new_`` or
    ``set_`` (``zip``), when one exists. Compared exactly by the column's
    type. Reported as placed by name; a demonstration that gives it replaces
    this."""

    if not plan.anchor:
        return None
    name = re.sub(r"^(new|set)_", "", param)
    col = schema.tables[plan.anchor].column(name)
    if col is None or name in schema.tables[plan.anchor].primary_key:
        return None
    rule = {
        "number": "number",
        "bool": "number",
        "date": "date",
        "datetime": "datetime",
    }.get(col.kind, "exact")
    if col.kind not in ("text", "number", "bool", "date", "datetime"):
        return None
    return Location((), (name,), rule)


def _children(
    schema: Schema, plan: KindPlan, lists: Sequence[str], only_missing: bool = False
) -> None:
    """Group child-row locations, and find each list parameter's rows."""

    todo: list[tuple[str, str, Location, Optional[str]]] = []
    for p, loc in plan.locations.items():
        todo.append((p, p, loc, None))
        for i, x in enumerate(plan.extra.get(p, [])):
            todo.append((f"{p}#{i + 2}", p, x, None))
    for value, locs in plan.dispatch_locations.items():
        for p, loc in locs.items():
            todo.append((f"{p}@{value}", p, loc, value))
    for gname, p, loc, dv in todo:
        if not loc.hops or loc.hops[0].kind != "child":
            continue
        if only_missing and (
            gname in plan.children or gname in plan.dispatch_children.get(dv or "", {})
        ):
            continue
        rel = Location(loc.hops[1:], loc.columns, loc.rule)
        rows = []
        for d, a in zip(plan.demos, plan.anchors):
            if d.values.get(p) is None:
                continue
            if dv is not None and str(d.values.get(plan.dispatch or "")) != dv:
                continue
            for ch in d.diff.of(loc.hops[0].table, "added"):
                if ch.after is None:
                    continue
                key = (a.after or {}).get(loc.hops[0].ref_column)
                if ch.after.get(loc.hops[0].column) != key:
                    continue
                if locate(
                    schema,
                    d,
                    _as_change(ch.table, ch.after),
                    d.values[p],
                    max_hops=len(rel.hops),
                ):
                    rows.append((d, ch.after))
                    break
        explained = {loc.hops[0].column, *rel.columns}
        explained |= {h.column for h in rel.hops[:1]}
        co: list[str] = []
        for q in plan.params:
            if q == p or not rows:
                continue
            hits = [
                locate(
                    schema, d, _as_change(loc.hops[0].table, r), d.values[q], max_hops=1
                )
                for d, r in rows
                if d.values.get(q) is not None
                and not isinstance(d.values.get(q), (list, dict))
            ]
            if hits and all(
                h
                and h[0].hops
                and _hop_cost(h[0].hops[0]) <= 1
                and h[0].hops[0].table != plan.anchor
                for h in hits
            ):
                co.append(q)
                explained |= {h[0].hops[0].column for h in hits}
        roles = column_roles(
            schema, loc.hops[0].table, rows, explained, plan.actor_values
        )
        if len(rows) < 2:
            # One demonstration gives this value. Every row of the table the
            # kind's demonstrations added for their record is evidence for a
            # column that holds the same in all of them (a journal's type).
            linked = [
                (d, ch.after)
                for d, a in zip(plan.demos, plan.anchors)
                for ch in d.diff.of(loc.hops[0].table, "added")
                if ch.after is not None
                and ch.after.get(loc.hops[0].column)
                == (a.after or {}).get(loc.hops[0].ref_column)
            ]
            if len(linked) >= 2:
                wide = column_roles(
                    schema, loc.hops[0].table, linked, explained, plan.actor_values
                )
                for col, role in roles.items():
                    if role.detail.get(
                        "why"
                    ) == "seen in one demonstration only" and wide.get(
                        col, role
                    ).role in ("constant", "actor", "copied"):
                        roles[col] = wide[col]
        group = ChildGroup(
            loc.hops[0].table,
            loc.hops[0],
            {p: rel},
            roles,
            1,
            co_owners=co,
            param=p,
        )
        if dv is None:
            plan.children[gname] = group
        else:
            plan.dispatch_children.setdefault(dv, {})[gname] = group
    for p in lists:
        per_key: dict[str, list[list[Location]]] = {}
        table = link = None
        rows = []
        #: Columns that hold the record's key in every list row so far.
        link_cols: Optional[set[str]] = None
        for d, a in zip(plan.demos, plan.anchors):
            items = d.values.get(p)
            if not isinstance(items, list):
                continue
            key = (
                (a.after or {}).get(schema.tables[plan.anchor].primary_key[0])
                if schema.tables[plan.anchor].primary_key
                else None
            )
            children = [
                ch
                for ch in d.diff.changes
                if ch.kind == "added"
                and ch.after is not None
                and ch.table != plan.anchor
                and key is not None
                and key in ch.after.values()
            ]
            used: set[int] = set()
            for item in items:
                if not isinstance(item, dict):
                    plan.refused.append(f"{p}: list items must be mappings")
                    return
                for i, ch in enumerate(children):
                    if i in used or ch.after is None:
                        continue
                    after = ch.after
                    found_at = {
                        k: locate(schema, d, _as_change(ch.table, after), v, max_hops=1)
                        for k, v in item.items()
                    }
                    if all(found_at.values()):
                        used.add(i)
                        here = {
                            c
                            for c, v in after.items()
                            if v == key
                            and is_link_column(schema, ch.table, c, plan.anchor)
                        }
                        link_cols = here if link_cols is None else link_cols & here
                        table = ch.table
                        link = Hop(
                            "child",
                            ch.table,
                            "",
                            schema.tables[plan.anchor].primary_key[0],
                        )
                        for k, ls in found_at.items():
                            per_key.setdefault(k, []).append(ls)
                        rows.append((d, after))
                        break
                else:
                    plan.unresolved[f"{p} item {item}"] = []
        if table is None or link is None:
            plan.unresolved.setdefault(p, [])
            continue
        # The column that links an item to its record: the one that holds
        # the record's key in every item row. Two that always agree (an
        # item's own id equal to the record's) can't be told apart, unless
        # exactly one is a declared foreign key to the record.
        cols = sorted(link_cols or [])
        declared = [
            c
            for c in cols
            if any(
                fk.column == c and fk.table == plan.anchor
                for fk in schema.tables[table].foreign_keys
            )
        ]
        if len(cols) != 1 and len(declared) == 1:
            cols = declared
        if len(cols) != 1:
            plan.refused.append(
                f"{p}: the demonstrations can't tell which column of {table} "
                "links an item to its record ("
                + (", ".join(cols) or "none holds the record's key in every item")
                + "); record a demonstration where they differ"
            )
            return
        link = Hop("child", table, cols[0], link.ref_column)
        item_locs: dict[str, Location] = {}
        for k, per in per_key.items():
            item_loc, tied = best(_intersect(per))
            if item_loc is None:
                plan.unresolved[f"{p}.{k}"] = tied
                return
            item_locs[k] = item_loc
        explained = {link.column} | {c for x in item_locs.values() for c in x.columns}
        explained |= {x.hops[0].column for x in item_locs.values() if x.hops}
        plan.lists[p] = ChildGroup(
            table,
            link,
            item_locs,
            column_roles(schema, table, rows, explained, plan.actor_values),
            list_param=p,
        )


def _all_groups(plan: KindPlan, dv: Optional[str] = None) -> list[ChildGroup]:
    """Every child group; with ``dv``, the plain ones and that dispatch
    value's own."""

    out = list(plan.children.values())
    for value, groups in plan.dispatch_children.items():
        if dv is None or value == dv:
            out.extend(groups.values())
    return out


def _demo_groups(plan: KindPlan, d: Demo) -> list[ChildGroup]:
    dv = str(d.values.get(plan.dispatch)) if plan.dispatch else None
    return _all_groups(plan, dv) if dv is not None else list(plan.children.values())


def _link_keys(schema: Schema, plan: KindPlan, d: Demo) -> set[str]:
    """Keys a row added beside a write may link to: the rows that write
    added, and its main record (an update's journal names the issue)."""

    keys = set(getattr(d, "_added_keys", set()))
    if d in plan.demos:
        a = plan.anchors[plan.demos.index(d)]
        key = (a.after or a.before or {}).get(plan_pk(schema, plan.anchor))
        if key is not None:
            keys.add(key)
    return keys


def _sides(
    schema: Schema,
    plan: KindPlan,
    actor_values: Mapping[str, str],
    all_demos: Sequence[Demo] = (),
) -> None:
    """Classify every other row the demonstrations touched."""

    child_tables = {g.table: g.param for g in _all_groups(plan)}
    list_tables = {g.table for g in plan.lists.values()}
    pure: dict[str, list[list[tuple[Demo, dict[str, Optional[str]]]]]] = {}
    counts: dict[str, list[int]] = {}
    touched: list[tuple[Demo, RowChange, RowChange]] = []
    for d, a in zip(plan.demos, plan.anchors):
        added_keys: set[str] = set()
        for ch in d.diff.changes:
            if ch.kind == "added" and ch.after is not None:
                at = schema.tables.get(ch.table)
                if at is not None and len(at.primary_key) == 1:
                    v = ch.after.get(at.primary_key[0])
                    if v is not None:
                        added_keys.add(v)
        per_table: dict[str, list[dict[str, Optional[str]]]] = {}
        for ch in d.diff.changes:
            if ch is a or ch.table == plan.anchor and ch.key == a.key:
                continue
            if ch.kind == "removed":
                if ch.table in child_tables or ch.table in list_tables:
                    _mark_replaced(schema, plan, d, a, ch)
                    continue
                plan.removed[ch.table] = plan.removed.get(ch.table, 0) + 1
                continue
            if ch.kind == "changed":
                linked = (
                    ch.table in child_tables
                    and a.after is not None
                    and any(
                        v == (a.after or {}).get(plan_pk(schema, plan.anchor))
                        for v in (ch.after or {}).values()
                    )
                )
                if linked:
                    _mark_replaced(schema, plan, d, a, ch)
                    g = next(x for x in _all_groups(plan) if x.table == ch.table)
                    g.changed_columns = sorted(
                        set(g.changed_columns) | set(ch.changed_columns)
                    )
                elif ch.table != plan.anchor or ch.key != a.key:
                    touched.append((d, a, ch))
                continue
            # added
            if ch.table in list_tables:
                continue
            if ch.table in child_tables and _is_param_row(schema, plan, d, ch):
                per_table.setdefault(ch.table, [])
                counts.setdefault(ch.table, [0] * len(plan.demos))
                continue
            if ch.table in child_tables and _is_empty_param_row(schema, plan, d, a, ch):
                continue
            if _is_ref_row(schema, plan, d, ch):
                continue
            per_table.setdefault(ch.table, []).append(ch.after or {})
        i = plan.demos.index(d)
        for t, added in per_table.items():
            counts.setdefault(t, [0] * len(plan.demos))
            counts[t][i] = max(len(added), 1 if t in child_tables else 0)
            pure.setdefault(t, []).append([(d, r) for r in added])
        d.__dict__["_added_keys"] = added_keys
    for t, cs in sorted(counts.items()):
        rows = [r for group in pure.get(t, []) for r in group]
        if len(set(cs)) == 1 and cs[0] > 0 and rows:
            links: list[set[str]] = [_link_keys(schema, plan, d) for d, _ in rows]
            roles = column_roles(schema, t, rows, set(), actor_values, links)
            if len(rows) < 2:
                # Seen beside one write only (the other writes gave the
                # value this row holds): every row the kind's writes added
                # here is evidence for a column that holds the same in all.
                wide = [
                    (d, ch.after)
                    for d in plan.demos
                    for ch in d.diff.of(t, "added")
                    if ch.after is not None
                ]
                if len(wide) >= 2:
                    wide_links = [_link_keys(schema, plan, d) for d, _ in wide]
                    more = column_roles(
                        schema, t, wide, set(), actor_values, wide_links
                    )
                    for col, role in roles.items():
                        if role.detail.get(
                            "why"
                        ) == "seen in one demonstration only" and more.get(
                            col, role
                        ).role in ("constant", "actor", "copied", "link"):
                            roles[col] = more[col]
            plan.sides.append(SideSpec(t, cs[0], roles, unless=child_tables.get(t)))
        elif rows:
            # The number of added rows differs between demonstrations: a log
            # only when a person says so (bookkeeping question). Until then
            # every added row stops the write.
            plan.log_candidates.add(t)
    _touched_rows(schema, plan, touched, actor_values)
    if plan.mode == "update":
        for a in plan.anchors:
            plan.anchor_changed |= set(a.changed_columns)
        explained = {
            c for loc in plan.locations.values() if not loc.hops for c in loc.columns
        }
        explained |= {
            loc.hops[0].column
            for loc in plan.locations.values()
            if loc.hops and loc.hops[0].kind == "fk"
        }
        for locs in plan.dispatch_locations.values():
            for loc in locs.values():
                explained |= set(loc.columns) if not loc.hops else {loc.hops[0].column}
        extra = plan.anchor_changed - explained
        for col in sorted(extra):
            plan.anchor_extra[col] = _change_kind(
                schema, plan.anchor, col, plan.demos, plan.anchors, actor_values
            )


_WHOLE_NUMBER = re.compile(r"^-?[0-9]+$")

#: Kinds of change a person is offered as the app's own bookkeeping by
#: default. Anything else is listed but not excused unless they add it.
BOOKKEEPING_KINDS = ("timestamp", "counter", "actor")


def _change_kind(
    schema: Schema,
    table: str,
    col: str,
    demos: Sequence[Demo],
    changes: Sequence[RowChange],
    actor_values: Mapping[str, str],
) -> str:
    """``timestamp`` (set to a time inside the demonstration), ``counter``
    (a whole number that went up), ``actor`` (set to the acting user) or
    ``other``, the same in every demonstration that changed it."""

    from openadapt_flow.generate.demo_analysis import _within

    column = schema.tables[table].column(col)
    kinds = set()
    for d, ch in zip(demos, changes):
        before = (ch.before or {}).get(col)
        after = (ch.after or {}).get(col)
        if before == after:
            continue
        if (
            column is not None
            and column.kind in ("date", "datetime")
            and _within(after, d.started_at, d.finished_at)
        ):
            kinds.add("timestamp")
        elif (
            column is not None
            and column.kind in ("number", "text")
            and before is not None
            and after is not None
            and _WHOLE_NUMBER.match(before)
            and _WHOLE_NUMBER.match(after)
            and int(after) > int(before)
        ):
            kinds.add("counter")
        elif after is not None and after in set(actor_values.values()):
            kinds.add("actor")
        else:
            kinds.add("other")
    return kinds.pop() if len(kinds) == 1 else "other"


def _touched_rows(
    schema: Schema,
    plan: KindPlan,
    touched: Sequence[tuple[Demo, RowChange, RowChange]],
    actor_values: Mapping[str, str],
) -> None:
    """Identify each existing row a demonstration changed with no task
    value, so an excuse applies to that row only: the same row (by key) in
    every demonstration (a counter row), or the row a located value names
    through a key of the main record (the contact's account)."""

    by_table: dict[str, list[tuple[Demo, RowChange, RowChange]]] = {}
    for item in touched:
        by_table.setdefault(item[2].table, []).append(item)
    for table, items in sorted(by_table.items()):
        t = schema.tables.get(table)
        cols = sorted({c for _, _, ch in items for c in ch.changed_columns})
        if t is None or len(t.primary_key) != 1:
            plan.unexcused.setdefault(table, set()).update(cols)
            continue
        pk = t.primary_key[0]
        keys = {ch.key for _, _, ch in items}
        demos_with = {id(d) for d, _, _ in items}
        selector: Optional[dict[str, Any]] = None
        how = ""
        if (
            len(keys) == 1
            and len(demos_with) == len(plan.demos)
            and len(items) == len(plan.demos)
        ):
            key = (items[0][2].after or {}).get(pk)
            if key is not None:
                selector = {pk: {"literal": key}}
                how = f"{table} row {pk}={key}"
        if selector is None:
            # Reached from the main record by a key column whose target row
            # holds located values in single columns.
            for p, loc in plan.locations.items():
                if (
                    not loc.hops
                    or loc.hops[0].kind != "fk"
                    or loc.hops[0].table != table
                ):
                    continue
                hop = loc.hops[0]
                if all(
                    (a.after or {}).get(hop.column) == (ch.after or {}).get(pk)
                    for _, a, ch in items
                ):
                    sel = {
                        x.columns[0]: {"param": q}
                        for q, x in plan.locations.items()
                        if len(x.hops) == 1
                        and x.hops[0] == hop
                        and len(x.columns) == 1
                        and x.rule == "exact"
                    }
                    if sel:
                        selector = sel
                        how = f"the {table} row the task's {', '.join(sorted(v['param'] for v in sel.values()))} name"
                        break
        if selector is None or len(items) != len({id(d) for d, _, _ in items}):
            plan.unexcused.setdefault(table, set()).update(cols)
            continue
        kinds = {
            c: _change_kind(
                schema,
                table,
                c,
                [d for d, _, _ in items],
                [ch for _, _, ch in items],
                actor_values,
            )
            for c in cols
        }
        plan.touched.append(
            {
                "table": table,
                "selector": selector,
                "columns": kinds,
                "row": how,
                # A literal key, not one the task's values name.
                "by_key": all("literal" in v for v in selector.values()),
            }
        )


def _version_column(schema: Schema, plan: KindPlan) -> Optional[dict[str, Any]]:
    """A whole-number column of the main record that went up by exactly one
    in every update demonstration (Redmine's ``lock_version``)."""

    t = schema.tables[plan.anchor]
    if len(t.primary_key) != 1:
        return None
    for col in t.columns:
        # Named as a version (``lock_version``, ``version_number``): a user
        # id that happens to go from 1 to 2 is not one.
        if (
            col.kind != "number"
            or col.name in t.primary_key
            or "version" not in col.name.lower()
        ):
            continue
        ok = bool(plan.anchors)
        for a in plan.anchors:
            b0 = (a.before or {}).get(col.name)
            b1 = (a.after or {}).get(col.name)
            if not (
                b0 is not None
                and b1 is not None
                and _WHOLE_NUMBER.match(b0)
                and _WHOLE_NUMBER.match(b1)
                and int(b1) == int(b0) + 1
            ):
                ok = False
                break
        if ok:
            return {"kind": "step", "table": plan.anchor, "column": col.name}
    return None


def _audit_source(
    schema: Schema,
    plan: KindPlan,
    actor_values: Mapping[str, str],
    create_demos: Sequence[Demo] = (),
    update_demos: Sequence[Demo] = (),
) -> Optional[dict[str, Any]]:
    """A table that gained, in every update demonstration, a row naming the
    main record (a column holding its key) and the acting user (a column
    holding the actor's id): the app's own audit trail of saves (EspoCRM's
    ``action_history_record``, Dolibarr's agenda events)."""

    t = schema.tables[plan.anchor]
    if len(t.primary_key) != 1 or not actor_values:
        return None
    actors = set(actor_values.values())
    found: Optional[dict[str, Any]] = None
    for name in sorted(schema.tables):
        table = schema.tables[name]
        if name == plan.anchor or len(table.primary_key) != 1:
            continue
        pairs: Optional[set[tuple[str, str, str]]] = None
        rows: list[dict[str, Optional[str]]] = []
        for d, a in zip(plan.demos, plan.anchors):
            key = (a.after or {}).get(t.primary_key[0])
            here: set[tuple[str, str, str]] = set()
            for ch in d.diff.of(name, "added"):
                r = ch.after or {}
                for lc, lv in r.items():
                    # The audit row's own id never names the record, even
                    # when it happens to equal the record's id.
                    if lv != key or lc in table.primary_key:
                        continue
                    for ac, av in r.items():
                        if ac != lc and av in actors:
                            p = next(k for k, v in actor_values.items() if v == av)
                            here.add((lc, ac, p))
                            rows.append(r)
            pairs = here if pairs is None else pairs & here
        if not pairs or len({p[0] for p in pairs}) != 1:
            continue
        # Several columns hold the acting user (author and assignee): the
        # one named as the author, else as a user, else the first by name.
        lc, ac, param = sorted(
            pairs,
            key=lambda p: (
                "author" not in p[1].lower(),
                "user" not in p[1].lower(),
                p[1],
            ),
        )[0]
        # Only a column naming the record's type (``target_type: Lead``)
        # narrows the read. Anything else the demonstrations had in common
        # (the action, the client address) could hide another user's save,
        # so it isn't used: an entry by anyone else stops the write.
        where: dict[str, Any] = {}
        names = {plan.anchor.lower(), plan.anchor.lower().split("_")[-1]}
        for col in table.columns:
            if col.name in (lc, ac) or col.kind != "text":
                continue
            values = {r.get(col.name) for r in rows}
            if len(values) == 1:
                v = next(iter(values))
                if v is not None and v.lower() in names and v.isidentifier():
                    where[col.name] = v
        # Every entry counts as a save except a code shown to be a read: a
        # demonstration logged it on a record of this table it didn't add or
        # change, and no demonstration logged it on one it did. An unseen
        # code (a bulk edit, an import) or a NULL one counts as a save.
        reads: dict[str, set[str]] = {}
        writes: dict[str, set[str]] = {}
        for d in [*create_demos, *update_demos]:
            # Every record the demonstration added or changed, in any table:
            # an audit row naming one of them is a save (a create logged on a
            # contact, not a read of a lead).
            moved = {
                str((ch.after or {}).get(k))
                for ch in d.diff.changes
                if ch.kind in ("added", "changed") and ch.table in schema.tables
                for k in schema.tables[ch.table].primary_key
            }
            for ch in d.diff.of(name, "added"):
                r = ch.after or {}
                rid = r.get(lc)
                if rid is None:
                    continue
                side = writes if str(rid) in moved else reads
                for col in table.columns:
                    v = r.get(col.name)
                    if v is not None:
                        side.setdefault(col.name, set()).add(v)
        for col in table.columns:
            if (
                col.name in (lc, ac)
                or col.name in table.primary_key
                or col.kind != "text"
                or col.name.lower().endswith("id")
                or col.name in where
            ):
                continue
            deny = reads.get(col.name, set()) - writes.get(col.name, set())
            if deny and all(re.fullmatch(r"[A-Za-z_]{1,64}", x) for x in deny):
                where[col.name] = {"not_in": sorted(deny)}
        found = {
            "kind": "audit",
            "table": name,
            "record_column": lc,
            "author_column": ac,
            "actor_param": param,
            "where": where,
        }
        break
    return found


def _versions_frame(
    b: "SqlBuilder", plan: KindPlan, settings: "Settings"
) -> Optional[dict[str, Any]]:
    v = plan.versions
    if v is None:
        return None
    pk = b.schema.tables[plan.anchor].primary_key[0]
    if v["kind"] == "step":
        sql = (
            f"SELECT {b.text('t', plan.anchor, pk)} AS {b.q('id')}, "
            f"{b.text('t', plan.anchor, v['column'])} AS {b.q('ver')} "
            f"FROM {b.tref(plan.anchor)} t"
        )
        return {
            "read": {"kind": "sql", **settings.read, "sql_query": sql},
            "record_field": "id",
            "target_field": "id",
            "target_surface": "record",
            "version_field": "ver",
        }
    table = v["table"]
    vpk = b.schema.tables[table].primary_key[0]

    def cond(c: str, val: Any) -> str:
        if isinstance(val, str):
            return f"{b.text('t', table, c)} = {b.literal(val)}"
        # A deny-list of read codes: a NULL code still counts as a save.
        ref = b.text("t", table, c)
        listed = ", ".join(b.literal(x) for x in val["not_in"])
        return f"({ref} IS NULL OR {ref} NOT IN ({listed}))"

    conds = " AND ".join(cond(c, val) for c, val in sorted(v["where"].items()))
    sql = (
        f"SELECT {b.text('t', table, v['record_column'])} AS {b.q('id')}, "
        f"{b.text('t', table, vpk)} AS {b.q('ver')}, "
        f"{b.text('t', table, v['author_column'])} AS {b.q('author')} "
        f"FROM {b.tref(table)} t" + (f" WHERE {conds}" if conds else "")
    )
    return {
        "read": {"kind": "sql", **settings.read, "sql_query": sql},
        "record_field": "id",
        "target_field": "id",
        "target_surface": "record",
        "version_field": "ver",
        "author_field": "author",
        "self": [{"param": v["actor_param"]}],
    }


def _mark_replaced(
    schema: Schema, plan: KindPlan, d: Demo, a: RowChange, ch: RowChange
) -> None:
    """An update removed or changed a row linked to the record in a table
    that holds a value: count the record's rows there after the write."""

    if plan.mode != "update" or a.after is None:
        return
    for g in _demo_groups(plan, d):
        if g.table != ch.table:
            continue
        key = a.after.get(g.link.ref_column)
        rows = d.after.get(g.table)
        if rows is None or rows.count_only is not None:
            continue
        consts = [(c, r.value) for c, r in g.roles.items() if r.role == "constant"]
        n = sum(
            1
            for r in rows.rows.values()
            if r.get(g.link.column) == key and all(r.get(c) == v for c, v in consts)
        )
        g.replaced, g.after_count = True, n


def plan_pk(schema: Schema, table: str) -> str:
    pk = schema.tables[table].primary_key
    return pk[0] if len(pk) == 1 else ""


def _is_param_row(schema: Schema, plan: KindPlan, d: Demo, ch: RowChange) -> bool:
    for g in _demo_groups(plan, d):
        p = g.param
        if g.table != ch.table or d.values.get(p) is None or ch.after is None:
            continue
        rel = g.params[p]
        if locate(
            schema,
            d,
            _as_change(ch.table, ch.after),
            d.values[p],
            max_hops=len(rel.hops),
        ):
            return True
    return False


def _is_empty_param_row(
    schema: Schema, plan: KindPlan, d: Demo, a: RowChange, ch: RowChange
) -> bool:
    """A row the app added for a parameter the task left out: linked to the
    main record, holding every constant of that parameter's rows, and an
    empty value where the parameter's value goes."""

    if ch.after is None or a.after is None:
        return False
    for g in _demo_groups(plan, d):
        p = g.param
        if g.table != ch.table or d.values.get(p) is not None:
            continue
        if ch.after.get(g.link.column) != a.after.get(g.link.ref_column):
            continue
        rel = g.params[p]
        cols = rel.columns if not rel.hops else (rel.hops[0].column,)
        if any(ch.after.get(c) not in (None, "") for c in cols):
            continue
        consts = [(c, r.value) for c, r in g.roles.items() if r.role == "constant"]
        if consts and all(ch.after.get(c) == v for c, v in consts):
            g.empty_when_null = True
            return True
    return False


def _is_ref_row(schema: Schema, plan: KindPlan, d: Demo, ch: RowChange) -> bool:
    """A row the write added that a parameter's location points to (a new
    email address row a link row names): declared by the value it holds."""

    if ch.after is None:
        return False
    for g in _demo_groups(plan, d):
        p = g.param
        rel = g.params[p]
        if not rel.hops or rel.hops[0].table != ch.table or d.values.get(p) is None:
            continue
        col = rel.columns[0] if len(rel.columns) == 1 else None
        if (
            col
            and ch.after.get(col) is not None
            and _rule_ok(ch.after.get(col), d.values[p], rel.rule)
        ):
            dv = str(d.values.get(plan.dispatch)) if plan.dispatch else None
            plan.added_refs.setdefault(
                f"{p}@{dv}" if dv else p,
                {
                    "table": ch.table,
                    "column": col,
                    "rule": rel.rule,
                    "param": p,
                    "dv": dv,
                },
            )
            return True
    return False


def _rule_ok(stored: Any, value: Any, rule: str) -> bool:
    from openadapt_flow.generate.demo_analysis import rule_for

    return rule_for(stored, "text", value) == rule


# -- SQL --------------------------------------------------------------------------


class SqlBuilder:
    """SQL text for one dialect. Every value from a task is bound as a
    parameter; table and column names come from the schema and are quoted."""

    def __init__(self, schema: Schema) -> None:
        self.schema = schema
        self.d = schema.dialect
        self.params: dict[str, dict[str, str]] = {}
        #: Tables read through a view (the SQL guard refuses their name).
        self.views: dict[str, str] = {}
        #: (table, key column) -> the displays the task's values are read
        #: through that key with: checked together for uniqueness.
        self.companions: dict[tuple[str, str], list[tuple[tuple[str, ...], str]]] = {}
        #: The main record's table: a key back to it names the record by its
        #: own key, not by a display value, so no uniqueness check applies.
        self.anchor = ""
        self._n = itertools.count()

    def q(self, name: str) -> str:
        return store.quote(self.d, name)

    def tref(self, table: str) -> str:
        """A table as a read names it: its view when it has one."""

        return self.qt(self.views.get(table, table))

    def qt(self, name: str) -> str:
        """A table name, ``schema.table`` quoted part by part."""

        return ".".join(self.q(part) for part in name.split("."))

    def ph(self, param: str) -> str:
        for k, v in self.params.items():
            if v == {"param": param}:
                return f"%({k})s"
        name = f"v{next(self._n)}"
        self.params[name] = {"param": param}
        return f"%({name})s"

    def text(self, alias: str, table: str, column: str) -> str:
        col = self.schema.tables[table].column(column)
        ref = f"{alias}.{self.q(column)}"
        kind = col.kind if col else "other"
        if self.d == "mysql":
            if kind in ("binary", "other"):
                return f"HEX({ref})"
            return f"CAST({ref} AS CHAR)"
        if kind == "binary":
            return f"encode({ref}, 'hex')"
        return f"CAST({ref} AS TEXT)"

    def display(self, alias: str, table: str, columns: Sequence[str], rule: str) -> str:
        if rule == "minutes":
            a, b = (f"{alias}.{self.q(c)}" for c in columns)
            if self.d == "mysql":
                return f"(TIMESTAMPDIFF(SECOND, {a}, {b}) / 60)"
            return f"(EXTRACT(EPOCH FROM ({b} - {a})) / 60)"
        if rule in ("number", "date", "datetime"):
            return f"{alias}.{self.q(columns[0])}"
        if len(columns) == 1:
            return self.text(alias, table, columns[0])
        parts = [self.text(alias, table, c) for c in columns]
        if self.d == "mysql":
            return "CONCAT(" + ", ' ', ".join(parts) + ")"
        return " || ' ' || ".join(parts)

    def path(self, alias: str, table: str, loc: Location, depth: int = 0) -> str:
        if not loc.hops:
            return self.display(alias, table, loc.columns, loc.rule)
        hop, rest = loc.hops[0], Location(loc.hops[1:], loc.columns, loc.rule)
        if hop.kind != "fk":
            raise GenerateError("a child hop can't be read as one value")
        sub = f"a{depth + 1}"
        inner = self.path(sub, hop.table, rest, depth + 1)
        live = self.live(sub, hop.table)
        # A child row's link to its own record names the target by identity
        # and needs no check; any other key into the main table (a parent
        # issue named by its subject) does.
        child_link = hop.table == self.anchor and table != self.anchor
        if not rest.hops and not child_link:
            # The value must name exactly one live row of its table: a
            # second "Jon Park", or "Mary Ann|Lee" beside "Mary|Ann Lee",
            # makes it NULL, so nothing matches and the write stops.
            u = f"u{depth + 1}"
            # Every value the task gives through this key together (a
            # customer's name and town) must name one row: a name shared by
            # two towns is fine when the town tells them apart.
            displays = self.companions.get((hop.table, hop.column)) or [
                (rest.columns, rest.rule)
            ]
            conds = [
                self.same(
                    self.display(u, hop.table, cols, rule),
                    self.display(sub, hop.table, cols, rule),
                    False,
                )
                for cols, rule in displays
            ]
            unique = (
                f"(SELECT COUNT(*) FROM {self.tref(hop.table)} {u} WHERE "
                f"{' AND '.join(conds)}{self.live_and(u, hop.table)}) = 1"
            )
            inner = f"CASE WHEN {unique} THEN {inner} END"
        return (
            f"(SELECT {inner} FROM {self.tref(hop.table)} {sub} WHERE "
            f"{self.same(f'{sub}.{self.q(hop.ref_column)}', f'{alias}.{self.q(hop.column)}', hop.declared)}"
            f"{' AND ' + live if live else ''})"
        )

    def live(self, alias: str, table: str) -> str:
        """The condition that a row isn't soft-deleted (a ``deleted`` or
        ``is_deleted`` flag, or a ``deleted_at`` time), or "" when the table
        has none: a trashed look-alike never resolves a key."""

        t = self.schema.tables.get(table)
        if t is None:
            return ""
        for name in ("deleted", "is_deleted"):
            col = t.column(name)
            if col is not None and col.kind in ("number", "bool"):
                ref = f"{alias}.{self.q(name)}"
                if self.d == "mysql" or col.kind == "number":
                    return f"COALESCE({ref}, 0) = 0"
                return f"NOT COALESCE({ref}, false)"
        if t.column("deleted_at") is not None:
            return f"{alias}.{self.q('deleted_at')} IS NULL"
        return ""

    def live_and(self, alias: str, table: str) -> str:
        cond = self.live(alias, table)
        return f" AND {cond}" if cond else ""

    def same(self, a: str, b: str, declared: bool = True) -> str:
        if declared:
            return f"{a} = {b}"
        if self.d == "mysql":
            return f"CAST({a} AS BINARY) = CAST({b} AS BINARY)"
        return f"CAST({a} AS TEXT) = CAST({b} AS TEXT)"

    def flag(self, expr: str, rule: str, ph: str) -> str:
        if self.d == "mysql":
            if rule in ("number", "minutes"):
                cond = (
                    f"CAST({ph} AS CHAR) REGEXP '^-?[0-9]+([.][0-9]+)?$' AND "
                    f"{expr} = CAST({ph} AS DECIMAL(65,30))"
                )
            elif rule == "date":
                cond = f"{expr} = CAST({ph} AS DATE)"
            else:
                cond = f"{expr} = CAST({ph} AS DATETIME)"
        else:
            if rule in ("number", "minutes"):
                cond = f"CAST({expr} AS numeric) = CAST({ph} AS numeric)"
            elif rule == "date":
                cond = f"{expr} = CAST({ph} AS date)"
            else:
                cond = f"{expr} = CAST({ph} AS timestamp)"
        return f"CASE WHEN {cond} THEN '1' ELSE '0' END"

    def literal(self, text: str) -> str:
        if "\\" in text or "\x00" in text:
            raise GenerateError("a constant holds a backslash or NUL; not pinned")
        return "'" + text.replace("'", "''") + "'"

    def pin(self, alias: str, table: str, column: str, value: Optional[str]) -> str:
        """A flag: this column holds the demonstrated constant (or no value)."""

        col = self.schema.tables[table].column(column)
        ref = f"{alias}.{self.q(column)}"
        if value is None:
            return f"CASE WHEN {ref} IS NULL THEN '1' ELSE '0' END"
        kind = col.kind if col else "text"
        if kind in ("number", "bool"):
            cast = "DECIMAL(65,30)" if self.d == "mysql" else "numeric"
            lhs = ref if self.d == "mysql" else f"CAST({ref} AS numeric)"
            if kind == "bool" and self.d != "mysql":
                lhs = f"CAST(CAST({ref} AS integer) AS numeric)"
                value = "1" if value in ("1", "true", "True") else "0"
            return f"CASE WHEN {lhs} = CAST({self.literal(value)} AS {cast}) THEN '1' ELSE '0' END"
        if kind == "date":
            return f"CASE WHEN {ref} = CAST({self.literal(value)} AS DATE) THEN '1' ELSE '0' END"
        return f"CASE WHEN {self.text(alias, table, column)} = {self.literal(value)} THEN '1' ELSE '0' END"

    def empty(self, alias: str, table: str, column: str) -> str:
        col = self.schema.tables[table].column(column)
        ref = f"{alias}.{self.q(column)}"
        if col is not None and col.kind == "text":
            return f"CASE WHEN {ref} IS NULL OR {ref} = '' THEN '1' ELSE '0' END"
        return f"CASE WHEN {ref} IS NULL THEN '1' ELSE '0' END"

    def copied(
        self, alias: str, detail: Mapping[str, Any], table: str, column: str
    ) -> str:
        src = (
            f"(SELECT s.{self.q(detail['column'])} FROM {self.tref(detail['table'])} s "
            f"WHERE s.{self.q(detail['ref_column'])} = {alias}.{self.q(detail['via'])})"
        )
        ref = f"{alias}.{self.q(column)}"
        return f"CASE WHEN {ref} = {src} THEN '1' ELSE '0' END"

    def key(self, alias: str, table: str) -> Optional[str]:
        pk = self.schema.tables[table].primary_key
        if not pk:
            return None
        if len(pk) == 1:
            return self.text(alias, table, pk[0])
        parts = [self.text(alias, table, c) for c in pk]
        if self.d == "mysql":
            return "CONCAT_WS('|', " + ", ".join(parts) + ")"
        return " || '|' || ".join(parts)


def _alias(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return out[:60]


@dataclass
class Surface:
    name: str
    table: str
    columns: list[tuple[str, str]]  # (alias, expr)
    alias: str = "t"

    def sql(self, b: SqlBuilder) -> str:
        cols = ", ".join(f"{expr} AS {b.q(a)}" for a, expr in self.columns)
        return f"SELECT {cols} FROM {b.qt(self.table)} {self.alias}"


def _base_columns(b: SqlBuilder, table: str, alias: str = "t") -> list[tuple[str, str]]:
    t = b.schema.tables[table]
    cols: list[tuple[str, str]] = []
    key = b.key(alias, table)
    if key is not None:
        cols.append(("id", key))
    for c in t.columns:
        if c.name == "id" and key is not None:
            continue
        cols.append((c.name, b.text(alias, table, c.name)))
    return cols


# -- one contract ------------------------------------------------------------------


@dataclass
class Variant:
    kind: str
    fields: list[str]
    lists: dict[str, int] = field(default_factory=dict)
    dispatch_value: Optional[str] = None
    files: dict[str, str] = field(
        default_factory=dict
    )  # create | found | update -> path
    sample: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        parts = sorted(self.fields) + [
            f"{k}x{n}" for k, n in sorted(self.lists.items())
        ]
        if self.dispatch_value is not None:
            parts.insert(0, f"{self.dispatch_value}")
        text = "+".join(parts)
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        short = re.sub(r"[^A-Za-z0-9_+]", "_", text)[:80]
        return f"{short}.{digest}"


@dataclass
class Settings:
    read: dict[str, Any]
    actor_params: dict[str, str]
    recheck_after_s: Optional[float]
    environment_id: str
    max_window_s: int = 900
    #: Tables a view replaces in surface reads (the SQL guard refuses the
    #: table's name): table -> view name.
    views: dict[str, str] = field(default_factory=dict)
    #: Table -> columns of a new row the person named as not checked (they
    #: vary, are free text left empty, or are the write's time stamp).
    unchecked: dict[str, set[str]] = field(default_factory=dict)


#: Roles of a new row's column that no rule checks: named by the person
#: (``bookkeeping.unchecked``), else pinned empty (free) or the kind refused.
OPEN_ROLES = ("varies", "free", "timestamp")


def _value_expr(param: str, rule: str) -> dict[str, Any]:
    out: dict[str, Any] = {"param": param}
    if rule in ("email", "phone"):
        out["kind"] = rule
    return out


def build_contract(
    schema: Schema,
    plan: KindPlan,
    variant: Variant,
    flavor: str,
    settings: Settings,
    excused: Mapping[str, set[str]],
    logs: set[str],
) -> dict[str, Any]:
    """One ``openadapt.verify/v1`` document for one field set. ``excused``:
    table -> columns a person confirmed as the app's own bookkeeping; each
    is excused only on the rows it was seen changing (the target, or a row
    identified by key or by the task's values), never by name elsewhere."""

    b = SqlBuilder(schema)
    b.views = dict(settings.views)
    b.anchor = plan.anchor
    wanted = set(variant.fields) | set(variant.lists)
    list_keys = {k for g in plan.lists.values() for k in g.params}
    for c_name, c_loc in _all_locations(plan):
        if c_name not in wanted and c_name not in list_keys:
            continue
        if not c_loc.hops or c_loc.hops[-1].kind != "fk":
            continue
        c_key = (c_loc.hops[-1].table, c_loc.hops[-1].column)
        c_entry = (tuple(c_loc.columns), c_loc.rule)
        if c_entry not in b.companions.setdefault(c_key, []):
            b.companions[c_key].append(c_entry)
    fields = set(variant.fields)
    anchor = plan.anchor
    surfaces: dict[str, Surface] = {}
    effects: list[dict[str, Any]] = []
    target_fields: set[str] = set()
    #: Surface -> the fields its declared rows may change.
    by_surface: dict[str, set[str]] = {}
    named_tables: set[str] = {anchor}

    # The main record, with every column and the values its parameters reach.
    main = Surface(
        "record", settings.views.get(anchor, anchor), _base_columns(b, anchor)
    )
    match: dict[str, Any] = {}
    locations = dict(plan.locations)
    if variant.dispatch_value is not None:
        locations.update(plan.dispatch_locations.get(variant.dispatch_value, {}))
    identity_params = []
    for p in sorted(fields):
        loc = locations.get(p)
        if loc is None or (loc.hops and loc.hops[0].kind == "child"):
            continue
        identity_params.append(p)
        key = _place(b, main, anchor, p, loc)
        if loc.rule in ("number", "date", "datetime", "minutes"):
            match[key] = {"literal": "1"}
        else:
            match[key] = _value_expr(p, loc.rule)
        if plan.mode == "update" and p in plan.changes:
            target_fields.add(key)
            target_fields.update(loc.columns if not loc.hops else [loc.hops[0].column])
    if plan.mode == "update" and flavor == "update":
        # The target's own bookkeeping, excused on the target row only.
        target_fields.update(
            c for c in plan.anchor_extra if c in excused.get(anchor, set())
        )
    if plan.mode == "create":
        # ``found`` pins the same constants as ``create`` (a soft-delete
        # flag, a status, a type): a trashed record with the task's values
        # is not "already there". Who created it may differ.
        roles = _anchor_roles(schema, plan, variant, settings.actor_params)
        for col, role in sorted(roles.items()):
            if flavor == "found" and role.role in ("actor", "copied"):
                continue
            _pin(
                b,
                main,
                anchor,
                col,
                role,
                match,
                settings.actor_params,
                settings.unchecked,
            )
    if plan.mode == "create" and flavor == "create":
        support = _support(plan, variant)
        for p in plan.optional:
            if p in fields or p in variant.lists:
                continue
            loc = plan.locations.get(p)
            if loc is None:
                continue
            cols = (
                list(loc.columns)
                if not loc.hops
                else ([loc.hops[0].column] if loc.hops[0].kind == "fk" else [])
            )
            for c in cols:
                # A column another value of this field set fills (a call's
                # parent is its account, or its lead) is not pinned empty.
                if any(
                    (a.after or {}).get(c) not in (None, "")
                    for d, a in support
                    if d.values.get(p) is None
                ):
                    continue
                alias = _add_column(
                    main, f"empty__{_alias(c)}", b.empty("t", anchor, c)
                )
                match[alias] = {"literal": "1"}
    effect: dict[str, Any] = {
        "kind": "record_written",
        "probe": "surface=record|the main record holds the task's values",
        "match": match,
        "expected_count": 1,
    }
    if plan.mode == "create" and flavor == "create":
        effect["count_new_only"] = True
    effects.append(effect)
    surfaces["record"] = main

    # Rows beside the main record that hold a value (a note, a custom field,
    # an email address link), and list items (invoice lines).
    found = flavor == "found"
    groups = [
        (p, g)
        for g in _all_groups(plan, variant.dispatch_value)
        for p in [g.param]
        if p in fields
        or (not found and g.empty_when_null)
        or (not found and plan.mode == "create" and set(g.co_owners) & fields)
    ]
    groups += [(p, g) for p, g in plan.lists.items() if p in variant.lists]
    # A child row names its record by the record's identity: for an update
    # only the values that pick the target, never the ones it changes (the
    # target's older rows would otherwise move with the change).
    main_params = (
        [ip for ip in identity_params if ip in plan.identity]
        if plan.mode == "update"
        else identity_params
    )
    for p, g in groups:
        name = f"rows:{g.table}"
        sname = _alias(name)
        if sname not in surfaces:
            scols = _base_columns(b, g.table)
            link = g.link
            # The main record's values, seen from the child row.
            for ip in main_params:
                loc = locations[ip]
                up = Location(
                    (Hop("fk", anchor, link.column, link.ref_column, True),) + loc.hops,
                    loc.columns,
                    loc.rule,
                )
                expr = b.path("t", g.table, up)
                if loc.rule in ("number", "date", "datetime", "minutes"):
                    expr = b.flag(expr, loc.rule, b.ph(ip))
                scols.append((f"main__{_alias(ip)}", expr))
            surfaces[sname] = Surface(
                sname, settings.views.get(g.table, g.table), scols
            )
            named_tables.add(g.table)
        surf = surfaces[sname]
        items = range(variant.lists[p]) if g.list_param else [None]
        for i in items:
            m: dict[str, Any] = {}
            for ip in main_params:
                loc = locations[ip]
                if loc.rule in ("number", "date", "datetime", "minutes"):
                    m[f"main__{_alias(ip)}"] = {"literal": "1"}
                else:
                    m[f"main__{_alias(ip)}"] = _value_expr(ip, loc.rule)
            for k, loc in g.params.items():
                pname = k if g.list_param is None else f"{p}.{i}.{k}"
                if g.list_param is None and pname not in fields:
                    # Given another owner but not this value: empty.
                    for c in loc.columns if not loc.hops else [loc.hops[0].column]:
                        alias = f"empty__{_alias(c)}"
                        if alias not in {a for a, _ in surf.columns}:
                            surf.columns.append((alias, b.empty("t", g.table, c)))
                        m[alias] = {"literal": "1"}
                    continue
                key = _place(b, surf, g.table, pname, loc)
                if loc.rule in ("number", "date", "datetime", "minutes"):
                    m[key] = {"literal": "1"}
                else:
                    m[key] = _value_expr(pname, loc.rule)
            for col, role in sorted(g.roles.items()):
                if role.role == "link" or (found and role.role in ("actor", "copied")):
                    continue
                _pin(
                    b,
                    surf,
                    g.table,
                    col,
                    role,
                    m,
                    settings.actor_params,
                    settings.unchecked,
                )
            effect = {
                "kind": "record_written",
                "probe": f"surface={sname}|a row beside the main record holds {p}",
                "match": m,
                "expected_count": 1,
            }
            if not found:
                # Found: the row is already there, and nothing changes.
                effect["count_new_only"] = True
            effects.append(effect)
        if plan.mode == "update" and g.replaced and not found:
            # The demonstrations replaced the record's earlier rows here (an
            # old email address link removed): afterwards the record has
            # exactly as many such rows as they left, and its earlier ones
            # may go.
            m = {
                k: v
                for k, v in effects[-1]["match"].items()
                if k.startswith(("main__", "pin__")) or k in g.roles
            }
            effects.append(
                {
                    "kind": "record_written",
                    "probe": f"surface={sname}|the record's rows here after the write",
                    "match": m,
                    "expected_count": g.after_count,
                }
            )
        if plan.mode == "update" and not found:
            by_surface.setdefault(sname, set()).update(g.changed_columns)
    # Existing rows the app changes beside the write (a counter row, the
    # account a contact belongs to): declared by key or by the task's values,
    # with only the confirmed columns allowed to move, on those rows only.
    for spec in plan.touched:
        # A row changed by key (a user's preferences, which even a read may
        # touch) is excused in ``found`` too; a row named by the task's
        # values only when something is written.
        if found and not spec.get("by_key"):
            continue
        # A row the task's values name applies only to a field set that
        # gives them (a call logged on a contact names no lead).
        if any(
            "param" in v and v["param"] not in fields for v in spec["selector"].values()
        ):
            continue
        cols = [c for c in spec["columns"] if c in excused.get(spec["table"], set())]
        if not cols:
            continue
        name = f"raw_{_alias(spec['table'])}"
        if name not in surfaces:
            surfaces[name] = Surface(
                name,
                settings.views.get(spec["table"], spec["table"]),
                _base_columns(b, spec["table"]),
            )
        effects.append(
            {
                "kind": "record_written",
                "probe": f"surface={name}|{spec['row']}, changed by the app",
                "match": dict(spec["selector"]),
                "expected_count": 1,
            }
        )
        # Only on this table's rows: a parent row the app touches can't take
        # the target's fields, nor the target a parent's.
        by_surface.setdefault(name, set()).update(cols)
        if spec["table"] == anchor:
            by_surface.setdefault("record", set()).update(cols)
    # A new row a link row names (a new email address).
    for ref in plan.added_refs.values():
        p = ref["param"]
        if p not in fields or found or ref["dv"] not in (None, variant.dispatch_value):
            continue
        effects.append(
            _raw_effect(
                b,
                schema,
                surfaces,
                settings,
                ref["table"],
                {ref["column"]: _value_expr(p, ref["rule"])},
                1,
                {},
                f"the new {ref['table']} row holds {p}",
            )
        )
    for side in plan.sides:
        if found or (side.unless is not None and side.unless in fields):
            continue
        m = {}
        for col, role in sorted(side.roles.items()):
            if role.role == "constant" and role.value is not None:
                m[col] = {"literal": role.value}
            elif role.role == "actor":
                m[col] = {"param": role.detail["param"]}
        effects.append(
            _raw_effect(
                b,
                schema,
                surfaces,
                settings,
                side.table,
                m,
                side.count,
                side.roles,
                f"{side.count} row(s) the app adds to {side.table}",
            )
        )

    verifier: dict[str, Any] = {"kind": "surfaces", "surfaces": {}}
    for s in surfaces.values():
        sub: dict[str, Any] = {"kind": "sql", **settings.read, "sql_query": s.sql(b)}
        used = {k: v for k, v in b.params.items() if f"%({k})s" in sub["sql_query"]}
        if used:
            sub["sql_query_params"] = used
        verifier["surfaces"][s.name] = sub
    # Main and child effects read named surfaces: no other read is judged
    # against them, and the whole-database frame covers every other table.
    frame: dict[str, Any] = {}
    if not found:
        by_surface.setdefault("record", set()).update(target_fields)
    # Per surface: each declared row may change only its own table's fields.
    frame["target_fields"] = sorted(set().union(*by_surface.values()))
    frame["target_fields_by_surface"] = {
        k: sorted(v) for k, v in sorted(by_surface.items()) if v
    }
    if plan.mode == "update":
        # Another user's save to the target inside the window (a lost
        # update) shows only in the target's versions.
        versions = _versions_frame(b, plan, settings)
        if versions is not None:
            frame["versions"] = versions
    # MySQL, MariaDB and PostgreSQL: the whole-database frame
    allow: list[dict[str, Any]] = []
    for t in sorted(logs):
        if t in named_tables:
            continue
        allow.append({"table": t})
    for t in sorted(named_tables):
        if plan.mode == "create" or flavor == "found":
            # The contract's own read judges the rows the write adds; the
            # whole-database frame still sees any existing row change,
            # now and at the recheck (a late edit to a look-alike).
            allow.append({"table": t})
            continue
        # An update changes a row here, which an append-only table
        # can't allow: its own read judges this table, and the recheck
        # (database only) doesn't see it.
        allow.append(
            {
                "table": t,
                "mode": "skip",
                "reason": "read in full, every column, by this contract's own surface",
            }
        )
    views = {s.table: s.name for s in surfaces.values() if s.name.startswith("raw_")}
    db: dict[str, Any] = {
        "dialect": schema.dialect,
        "read": dict(settings.read),
        "allow_tables": allow,
        "views": views,
        "settle_s": 1,
        "settle_attempts": 5,
    }
    nrf = {
        s.table: sorted(getattr(s, "_new_row_fields", []))
        for s in surfaces.values()
        if s.name.startswith("raw_") and getattr(s, "_new_row_fields", None)
    }
    if nrf:
        db["new_row_fields"] = nrf
    if settings.recheck_after_s:
        db["recheck_after_s"] = settings.recheck_after_s
    frame["database"] = db
    doc: dict[str, Any] = {
        "schema_version": "openadapt.verify/v1",
        "environment_id": settings.environment_id,
        "max_execution_window_s": settings.max_window_s,
        "verifier": verifier,
        "effects": effects,
        "frame": frame,
    }
    return doc


def _all_columns(schema: Schema, tables: set[str]) -> set[str]:
    return {
        c.name for t in tables if t in schema.tables for c in schema.tables[t].columns
    }


def _place(b: SqlBuilder, surf: Surface, table: str, param: str, loc: Location) -> str:
    """Add the column that holds ``param``'s value to ``surf``; its name."""

    if (
        not loc.hops
        and len(loc.columns) == 1
        and loc.rule in ("exact", "email", "phone")
    ):
        return loc.columns[0] if loc.columns[0] != "id" else "id"
    expr = b.path("t", table, loc)
    alias = f"p__{_alias(param)}"
    if loc.rule in ("number", "date", "datetime", "minutes"):
        expr = b.flag(expr, loc.rule, b.ph(param))
    if alias not in {a for a, _ in surf.columns}:
        surf.columns.append((alias, expr))
    return alias


def _pin(
    b: SqlBuilder,
    surf: Surface,
    table: str,
    col: str,
    role: ColumnRole,
    match: dict[str, Any],
    actor_params: Mapping[str, str],
    unchecked: Mapping[str, set[str]] = {},
) -> None:
    if role.role == "varies" and col not in unchecked.get(table, set()):
        why = role.detail.get("why") or "it differs between demonstrations"
        raise Unpinned(f"{table}.{col} is not checked ({why})")
    if role.role == "free" and col not in unchecked.get(table, set()):
        # Empty in every demonstration and not named as unchecked: it stays
        # empty.
        alias = _add_column(surf, f"empty__{_alias(col)}", b.empty("t", table, col))
        match[alias] = {"literal": "1"}
        return
    if role.role == "constant":
        column = b.schema.tables[table].column(col)
        if role.value is not None and column is not None and column.kind == "text":
            match.setdefault(col, {"literal": role.value})
            return
        alias = _add_column(
            surf, f"pin__{_alias(col)}", b.pin("t", table, col, role.value)
        )
        match[alias] = {"literal": "1"}
    elif role.role == "actor":
        match.setdefault(col, {"param": role.detail["param"]})
    elif role.role == "copied":
        alias = _add_column(
            surf, f"pin__{_alias(col)}", b.copied("t", role.detail, table, col)
        )
        match[alias] = {"literal": "1"}


def _add_column(surf: Surface, alias: str, expr: str) -> str:
    """Add a computed column; the same name with another expression (two
    rows of one table pinned to different constants) gets its own name."""

    for a, e in surf.columns:
        if a == alias and e == expr:
            return alias
    if any(a == alias for a, _ in surf.columns):
        alias = f"{alias}__{hashlib.sha256(expr.encode()).hexdigest()[:6]}"
        if any(a == alias for a, _ in surf.columns):
            return alias
    surf.columns.append((alias, expr))
    return alias


def _raw_effect(
    b: SqlBuilder,
    schema: Schema,
    surfaces: dict[str, Surface],
    settings: Settings,
    table: str,
    match: dict[str, Any],
    count: int,
    roles: Mapping[str, ColumnRole],
    note: str,
) -> dict[str, Any]:
    """An effect on a table's own read: rows the app adds beside the write.

    The table is judged by the whole-database frame, which also checks that
    every column these rows set beyond ``match`` is declared in
    ``new_row_fields`` or holds its default."""

    name = f"raw_{_alias(table)}"
    surf = surfaces.get(name)
    if surf is None:
        surf = Surface(name, settings.views.get(table, table), _base_columns(b, table))
        surfaces[name] = surf
    t = schema.tables[table]
    m = dict(match)
    # Text and whole-number constants only: the snapshot's JSON form of
    # a decimal or a date isn't the generator's text form.
    for col, value in list(m.items()):
        c = t.column(col)
        lit = value.get("literal") if isinstance(value, dict) else None
        if (
            c is not None
            and lit is not None
            and not (
                c.kind == "text"
                or (
                    c.kind in ("number", "bool")
                    and c.data_type.startswith(
                        (
                            "int",
                            "tinyint",
                            "smallint",
                            "mediumint",
                            "bigint",
                            "integer",
                        )
                    )
                    and re.fullmatch(r"-?\d+", lit)
                )
            )
        ):
            m.pop(col)
    nrf = set(getattr(surf, "_new_row_fields", set()))
    for c in t.columns:
        if c.name in m:
            continue
        role = roles.get(c.name)
        if role is not None and role.role == "constant" and role.value is None:
            continue
        if (
            role is None
            or role.role != "constant"
            or (role.detail or {}).get("is_default")
        ):
            if role is not None and role.role in OPEN_ROLES:
                # Set freely only when the person named it unchecked. Free
                # text not named holds the empty value every demonstration
                # stored (NULL or ''), matched on the row itself; anything
                # else must hold its default.
                if c.name in settings.unchecked.get(table, set()):
                    nrf.add(c.name)
                elif role.role == "free":
                    empty = (role.detail or {}).get("empty")
                    if empty == "null":
                        m[c.name] = {"null": True}
                        nrf.add(c.name)
                    elif empty == "text":
                        m[c.name] = {"literal": ""}
                        nrf.add(c.name)
                continue
            if role is None or role.role in ("key", "link", "copied", "actor"):
                nrf.add(c.name)
            continue
        nrf.add(c.name)
    surf.__dict__["_new_row_fields"] = nrf
    return {
        "kind": "record_written",
        "probe": f"surface={name}|{note}",
        "match": m,
        "expected_count": count,
        "count_new_only": True,
    }


def _anchor_roles(
    schema: Schema, plan: KindPlan, variant: Variant, actor_params: Mapping[str, str]
) -> dict[str, ColumnRole]:
    """The main row's columns no parameter explains, from the demonstrations
    that give this field set (all of them when none gives exactly it)."""

    support = _support(plan, variant)
    explained: set[str] = set()
    for loc in plan.locations.values():
        if not loc.hops:
            explained |= set(loc.columns)
        elif loc.hops[0].kind == "fk":
            explained.add(loc.hops[0].column)
    rows = [(d, a.after or {}) for d, a in support]
    roles = column_roles(schema, plan.anchor, rows, explained, actor_params)
    if len(support) < len(plan.demos):
        # A column that differs between all demonstrations and holds a
        # generated token (a UUID) is the app's own, whichever ones support
        # this field set.
        everyone = [(d, a.after or {}) for d, a in zip(plan.demos, plan.anchors)]
        for col, role in column_roles(
            schema, plan.anchor, everyone, explained, actor_params
        ).items():
            values = [r.get(col) for _, r in everyone]
            if role.role == "varies" and all(
                v is not None and _TOKEN.match(v) for v in values
            ):
                roles[col] = role
            elif (
                len(support) < 2
                and roles.get(col, role).detail.get("why")
                == "seen in one demonstration only"
                and role.role in ("constant", "actor", "copied")
            ):
                # One demonstration gives this field set, but every
                # demonstration of the kind stored the same there: that
                # agreement is the evidence for the pin.
                roles[col] = role
    return roles


_TOKEN = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{16,})$"
)


def _support(plan: KindPlan, variant: Variant) -> list[tuple[Demo, RowChange]]:
    """The demonstrations that give every field of this set (all of them
    when none does)."""

    fields = set(variant.fields) | set(variant.lists)
    if variant.dispatch_value is not None:
        fields.add(plan.dispatch or "")
    return [
        (d, a)
        for d, a in zip(plan.demos, plan.anchors)
        if fields <= {k for k, v in d.values.items() if v is not None}
    ] or list(zip(plan.demos, plan.anchors))


# -- variants -----------------------------------------------------------------------


def variants(
    schema: Schema, plan: KindPlan, spec: KindSpec, answers: Mapping[str, Any]
) -> list[Variant]:
    out: list[Variant] = []
    lists = list(plan.lists)
    if plan.mode == "update":
        if plan.dispatch is not None:
            values = dict(plan.dispatch_locations)
            # By analogy: another column of the main table, written the way
            # a demonstrated plain column is.
            plain = [
                loc
                for locs in values.values()
                for loc in locs.values()
                if not loc.hops and loc.rule == "exact"
            ]
            if plain:
                vp = next(iter(next(iter(values.values()))))
                for c in schema.tables[plan.anchor].columns:
                    for name in {c.name, _camel(c.name)}:
                        if (
                            name in values
                            or c.name in schema.tables[plan.anchor].primary_key
                        ):
                            continue
                        if c.kind == "text":
                            values[name] = {vp: Location((), (c.name,), "exact")}
                plan.dispatch_locations = values
            for dv in sorted(values):
                fields = sorted(set(plan.identity) | {plan.dispatch, *values[dv]})
                out.append(Variant(plan.kind, fields, dispatch_value=dv))
            return out
        changes = [p for p in plan.changes if p in plan.locations]
        for n in range(1, len(changes) + 1):
            for combo in itertools.combinations(changes, n):
                out.append(Variant(plan.kind, sorted(set(plan.identity) | set(combo))))
        return out[:MAX_VARIANTS]
    located = set(plan.locations)
    base_sets = []
    for d in plan.demos:
        given = {
            k for k, v in d.values.items() if v is not None and not isinstance(v, list)
        } & (located | {g.param for g in plan.children.values()})
        base_sets.append(frozenset(given))
    optional = [p for p in plan.optional if p in located and p not in lists]
    seen: set[frozenset[str]] = set(base_sets)
    # Every demonstrated set first, then sets with fewer optional fields,
    # one field left out at a time across demonstrations, so the cap keeps
    # the sets closest to what was shown.
    sets: list[frozenset[str]] = list(dict.fromkeys(base_sets))
    drops = {base: [p for p in optional if p in base] for base in base_sets}
    for n in range(1, max((len(v) for v in drops.values()), default=0) + 1):
        for base in base_sets:
            for combo in itertools.combinations(drops[base], n):
                s = frozenset(base - set(combo))
                if s not in seen:
                    seen.add(s)
                    sets.append(s)
    # A field the examples leave out sometimes, and that one demonstration
    # gives, may be added to a set from another demonstration.
    for s in list(sets):
        for p in optional:
            if p not in s:
                t = frozenset(s | {p})
                if t not in seen:
                    seen.add(t)
                    sets.append(t)
    sets = sets[:MAX_VARIANTS]
    for s in sets:
        if lists:
            for n in range(1, MAX_LIST + 1):
                out.append(Variant(plan.kind, sorted(s), lists={lists[0]: n}))
        else:
            out.append(Variant(plan.kind, sorted(s)))
    return out


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)


# -- validation ------------------------------------------------------------------------


def flatten(values: Mapping[str, Any]) -> dict[str, Any]:
    """Task values as the contracts bind them: ``lines: [{qty: 4}]`` is
    ``lines.0.qty: 4``; nulls are left out."""

    out: dict[str, Any] = {}
    for k, v in values.items():
        if v is None:
            continue
        if isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    for ik, iv in item.items():
                        if iv is not None:
                            out[f"{k}.{i}.{ik}"] = iv
                else:
                    out[f"{k}.{i}"] = item
        else:
            out[k] = v
    return out


def sample_values(
    plan: KindPlan, variant: Variant, others: Sequence[Demo] = ()
) -> dict[str, Any]:
    """Values to dry-read a contract with: the demonstrations' own (another
    kind's, for a parameter placed from that kind)."""

    out: dict[str, Any] = {}
    for d in [*plan.demos, *others]:
        for k, v in d.values.items():
            if v is not None and k not in out:
                out[k] = v
    if variant.dispatch_value is not None and plan.dispatch:
        out[plan.dispatch] = variant.dispatch_value
    for p, n in variant.lists.items():
        items = out.get(p) if isinstance(out.get(p), list) else []
        if items:
            out[p] = [items[i % len(items)] for i in range(n)]
    for k in variant.fields:
        loc = plan.locations.get(k)
        if k not in out and loc is not None:
            # Placed by name, so no demonstration gave a value: any value of
            # the right type reads the same columns.
            out[k] = {"number": 1, "minutes": 1, "date": "2026-01-01",
                      "datetime": "2026-01-01 00:00"}.get(loc.rule, "x")  # fmt: skip
    keep = set(variant.fields) | set(variant.lists)
    return {k: v for k, v in out.items() if k in keep}


def dry_read(path: Path, params: Mapping[str, Any], mysql: bool) -> Optional[str]:
    """Load the contract and read the live store once, as ``begin_write``
    would. None when it reads; else why not."""

    from openadapt_flow.verifier import Contract, VerifierError
    from openadapt_flow.verify_run import (
        FRAME_DB_DETAIL,
        VerifyRunError,
        capture_baseline,
    )

    try:
        contract = Contract.load(path)
    except VerifierError as exc:
        return f"does not load: {exc}"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            payload = capture_baseline(contract.config, params, Path(tmp) / "b.json")
        except (VerifyRunError, ValueError, KeyError) as exc:
            return f"dry read failed: {exc}"
    state = payload.get("state", {})
    if not state.get("reachable"):
        return "dry read: the store could not be read"
    detail = state.get("detail") or {}
    for name, sub in detail.items():
        if isinstance(sub, dict) and "reachable" in sub and not sub.get("reachable"):
            if name == "openadapt:frame_audit":
                continue
            return f"dry read: surface {name!r} could not be read"
    if mysql:
        snap = detail.get(FRAME_DB_DETAIL)
        if not isinstance(snap, dict):
            from openadapt_flow.runtime.effects.db_snapshot import (
                mac_key,
                take_snapshot,
            )
            from openadapt_flow.verify_run import _audit_sql, _db_connect, _ref_columns

            frame = contract.config.frame
            database: Any = frame.database if frame is not None else None
            try:
                take_snapshot(
                    _db_connect(database.read),
                    database.dialect,
                    key=mac_key(b"0" * 32),
                    modes=database.modes(),
                    max_rows=database.max_rows,
                    extra_queries=_audit_sql(contract.config),
                    ignore=database.all_ignore_columns(),
                    ref_columns=_ref_columns(contract.config),
                    schemas=database.schemas,
                    links=database.recheck_after_s is not None,
                )
                why = "no error on a second read"
            except Exception as exc:  # noqa: BLE001 - reported
                why = f"{type(exc).__name__}: {exc}"
            return f"dry read: the whole-database frame could not be read ({why})"
        if snap.get("code_visible") is False:
            return (
                "dry read: the read account can't see triggers (MySQL needs the "
                "TRIGGER privilege on the database), so the frame can't be checked"
            )
    return None


def check_read_only(connect: Any, dialect: str) -> list[str]:
    """Why the read account is not read-only (empty when it is)."""

    problems: list[str] = []
    conn = connect()
    try:
        cur = conn.cursor()
        if dialect == "mysql":
            cur.execute("SHOW GRANTS")
            for (grant,) in cur.fetchall():
                g = str(grant).upper()
                privs = g.split(" ON ")[0].replace("GRANT ", "")
                for word in (
                    "ALL PRIVILEGES",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "CREATE",
                    "DROP",
                    "ALTER",
                    "INDEX",
                    "EXECUTE",
                    "FILE",
                    "SUPER",
                    "EVENT",
                ):
                    if re.search(rf"\b{word}\b", privs) and not privs.startswith(
                        "USAGE"
                    ):
                        problems.append(f"the read account has {word}: {grant}")
        else:
            # Every role the account is, inherits, or can SET ROLE to (a
            # NOINHERIT membership): what any of them can do, the account can.
            roles = (
                "(SELECT oid, rolname, rolsuper, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE pg_has_role(current_user, oid, 'MEMBER')) r"
            )
            user_schemas = (
                "n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg\\_toast%' "
                "AND n.nspname NOT LIKE 'pg\\_temp%'"
            )

            def who(role: str) -> str:
                return "the read account" + ("" if role == user else f" (as {role})")

            cur.execute("SELECT current_user")
            user = str(cur.fetchone()[0])
            cur.execute(
                "SELECT r.rolname FROM " + roles + " WHERE r.rolsuper OR "
                "r.rolcreaterole OR r.rolcreatedb ORDER BY 1"
            )
            for (role,) in cur.fetchall():
                problems.append(
                    f"{who(role)} is a superuser or can create roles or databases"
                )
            cur.execute(
                "SELECT r.rolname, n.nspname, c.relname, p.priv FROM " + roles + " "
                "CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
                "'TRIGGER', 'REFERENCES']) AS p(priv) "
                "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND "
                + user_schemas
                + " AND (has_table_privilege(r.oid, c.oid, p.priv) OR (p.priv IN "
                "('INSERT', 'UPDATE', 'REFERENCES') AND "
                "has_any_column_privilege(r.oid, c.oid, p.priv))) "
                "ORDER BY 1, 2, 3, 4 LIMIT 20"
            )
            for role, schema_name, t, priv in cur.fetchall():
                problems.append(
                    f"{who(role)} has {priv} on table {schema_name}.{t} "
                    "(or on one of its columns)"
                )
            cur.execute(
                "SELECT r.rolname, n.nspname, c.relname, p.priv FROM " + roles + " "
                "CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN unnest(ARRAY['USAGE', 'UPDATE']) AS p(priv) "
                "WHERE c.relkind = 'S' AND "
                + user_schemas
                + " AND has_sequence_privilege(r.oid, c.oid, p.priv) "
                "ORDER BY 1, 2, 3, 4 LIMIT 20"
            )
            for role, schema_name, t, priv in cur.fetchall():
                problems.append(
                    f"{who(role)} has {priv} on sequence {schema_name}.{t} "
                    "(it can move the counter)"
                )
            cur.execute(
                "SELECT r.rolname, n.nspname FROM " + roles + " "
                "CROSS JOIN pg_namespace n WHERE "
                + user_schemas
                + " AND has_schema_privilege(r.oid, n.oid, 'CREATE') ORDER BY 1, 2"
            )
            for role, schema_name in cur.fetchall():
                problems.append(f"{who(role)} has CREATE on schema {schema_name}")
            cur.execute(
                "SELECT r.rolname FROM " + roles + " WHERE "
                "has_database_privilege(r.oid, current_database(), 'CREATE') ORDER BY 1"
            )
            for (role,) in cur.fetchall():
                problems.append(f"{who(role)} has CREATE on the database")
    finally:
        conn.rollback()
        conn.close()
    return problems


def read_account_notes(connect: Any, dialect: str) -> list[str]:
    """What the read account can do beyond reading, that the contracts
    still need (MySQL: TRIGGER, so the whole-database frame sees triggers;
    it also lets the account create them)."""

    if dialect != "mysql":
        return []
    return (
        [
            "the read account holds TRIGGER (needed to see triggers; it can also "
            "create and drop triggers, so keep its password from the agent)"
        ]
        if _sees_triggers(connect)
        else []
    )


def guard_problem(sql: str) -> Optional[str]:
    from openadapt_flow.runtime.effects.sql import assert_read_only_sql

    try:
        assert_read_only_sql(sql)
    except ValueError as exc:
        return str(exc)
    return None


def _admin_connect(admin_read: Mapping[str, Any]) -> Any:
    import importlib

    module = importlib.import_module(str(admin_read["sql_driver"]))
    args = dict(admin_read.get("sql_connect_args") or {})
    env = admin_read.get("sql_password_env")
    if env:
        if env not in os.environ:
            raise GenerateError(
                f"environment variable {env} (the admin password) is not set"
            )
        args["password"] = os.environ[str(env)]
    return module.connect(**args)


def _account(conn: Any, reader: str) -> str:
    """``'user'@'host'`` from ``user@host`` or ``'user'@'host'``, each part
    escaped by the driver: the account name never reaches SQL as written."""

    m = re.fullmatch(r"'?([^'@]+)'?(?:@'?([^']*)'?)?", reader.strip())
    if m is None:
        raise GenerateError(f"--reader-account {reader!r} is not user@host")
    user, host = m.group(1), m.group(2) if m.group(2) is not None else "%"
    return f"{conn.escape(user)}@{conn.escape(host)}"


def grant_trigger(admin_read: Mapping[str, Any], schema: Schema, reader: str) -> str:
    """MySQL shows a table's triggers only to an account with the TRIGGER
    privilege, and the whole-database frame must see them (a trigger planted
    during a write could otherwise pass). Run only with ``--grant-trigger``."""

    conn = _admin_connect(admin_read)
    try:
        sql = (
            f"GRANT TRIGGER ON {store.quote('mysql', schema.database)}.* TO "
            f"{_account(conn, reader)}"
        )
        conn.cursor().execute(sql)
        conn.commit()
    finally:
        conn.close()
    return sql


def _sees_triggers(connect: Any) -> bool:
    """Does the account hold TRIGGER (or ALL) on the database? A database
    with no triggers shows none either way, so the grant is what's checked."""

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SHOW GRANTS")
        grants = [str(g[0]).upper() for g in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()
    for g in grants:
        privs = g.split(" ON ")[0]
        if "TRIGGER" in privs or "ALL PRIVILEGES" in privs:
            return True
    return False


def view_schema(schema: Schema) -> str:
    """Where the generator's views live: a schema of their own, beside the
    app's, so the app's own upgrades and restores never drop them and the
    whole-database frame doesn't read them as app tables."""

    return f"{schema.database}_oa_read"


def existing_views(
    connect: Any, schema: Schema, tables: Sequence[str]
) -> dict[str, str]:
    """Table -> ``schema.view`` for the views the read account can see."""

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = %s",
            (view_schema(schema),),
        )
        have = {str(r[0]) for r in cur.fetchall()}
    finally:
        conn.rollback()
        conn.close()
    return {
        t: f"{view_schema(schema)}.oa_read_{t}"
        for t in tables
        if f"oa_read_{t}" in have
    }


def create_views(
    admin_read: Mapping[str, Any], schema: Schema, tables: Sequence[str], reader: str
) -> dict[str, str]:
    """Create one read-only view per table the SQL guard refuses by name, in
    the schema ``<database>_oa_read``, and grant the read account SELECT on
    that schema. Only run with ``--create-views``; the connection is the
    admin one given."""

    if schema.dialect != "mysql":
        raise GenerateError("--create-views supports MySQL and MariaDB")
    conn = _admin_connect(admin_read)
    made = {}
    vs = store.quote("mysql", view_schema(schema))
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {vs}")
        for t in tables:
            q = store.quote("mysql", schema.database) + "." + store.quote("mysql", t)
            v = vs + "." + store.quote("mysql", f"oa_read_{t}")
            cur.execute(
                f"CREATE OR REPLACE SQL SECURITY DEFINER VIEW {v} AS SELECT * FROM {q}"
            )
            made[t] = f"{view_schema(schema)}.oa_read_{t}"
        cur.execute(f"GRANT SELECT ON {vs}.* TO {_account(conn, reader)}")
        conn.commit()
    finally:
        conn.close()
    return made


# -- the whole run --------------------------------------------------------------------


def propose(
    *,
    read: dict[str, Any],
    kinds: list[KindSpec],
    demo_dir: Path,
    out: Path,
    actor_params: Mapping[str, str],
    answers: Mapping[str, Any],
    environment_id: str = "",
    admin_read: Optional[Mapping[str, Any]] = None,
    create_views_flag: bool = False,
    reader_account: str = "",
    validate: bool = True,
    grant_trigger_flag: bool = False,
) -> dict[str, Any]:
    """Analyze, ask, emit and validate. Returns the report (also written)."""

    connect = store.connector(read)
    dialect = store.dialect_of(read)
    schema = store.read_schema(connect, dialect)
    problems = check_read_only(connect, dialect)
    if problems:
        raise GenerateError(
            "the read account is not read-only, so no contract is proposed: "
            + "; ".join(problems)
        )
    applied: list[str] = []
    if dialect == "mysql" and not _sees_triggers(connect):
        if grant_trigger_flag:
            if not admin_read or not reader_account:
                raise GenerateError(
                    "--grant-trigger needs --admin-read and --reader-account"
                )
            applied.append(grant_trigger(admin_read, schema, reader_account))
        if not _sees_triggers(connect):
            raise GenerateError(
                "the read account can't see triggers, so the whole-database frame "
                "would never be checked (every write would be UNCERTAIN). MySQL shows "
                "triggers only to an account with the TRIGGER privilege. Rerun with "
                "--grant-trigger --admin-read <admin.yaml> --reader-account <account>, "
                "or have the DBA run: GRANT TRIGGER ON <database>.* TO <account>"
            )
    demos = load_demos(demo_dir)
    env = environment_id or f"generated-{schema.database}"
    env = re.sub(r"[^A-Za-z0-9._:-]", "-", env)
    if len(env) < 8:
        env = (env + "-environment")[:64]
    plans = [analyze(schema, k, demos, actor_params) for k in kinds]
    # Versions of an update's target, chosen once every kind's mode is
    # known: the creates' and the updates' audit rows tell write events apart.
    create_kinds = {p.kind for p in plans if p.mode == "create"}
    update_kinds = {p.kind for p in plans if p.mode == "update"}
    for p in plans:
        if p.mode == "update" and not p.refused:
            # A version column first: every existing row has a version
            # there. An audit trail misses records saved before it, or
            # outside the app, and such a target stops as `missing`.
            p.versions = _version_column(schema, p) or _audit_source(
                schema,
                p,
                actor_params,
                [d for d in demos if d.kind in create_kinds],
                [d for d in demos if d.kind in update_kinds],
            )
    questions: list[Question] = []

    _share_locations(schema, plans, kinds)
    _break_ties_across(plans)
    location_questions: list[Question] = []
    # Q: per kind, a value the generator couldn't place.
    for p in plans:
        if not p.unresolved:
            continue
        chosen = dict(_answer(answers, f"locations.{p.kind}") or {})
        asked = dict(p.unresolved)
        open_ = {}
        for name, tied in list(p.unresolved.items()):
            pick = chosen.get(name)
            if isinstance(pick, int) and 0 <= pick < len(tied):
                _resolve(p, name, tied[pick])
            else:
                open_[name] = tied
        p.unresolved = open_
        if asked:
            parts = []
            for name, tied in asked.items():
                if tied:
                    opts = "; ".join(
                        f"[{i}] {loc.describe(p.anchor)}" for i, loc in enumerate(tied)
                    )
                    parts.append(f"`{name}` is in several places: {opts}")
                else:
                    parts.append(
                        f"`{name}` was not found in any row the demonstrations wrote; "
                        "tasks that give it are stopped"
                    )
            location_questions.append(
                Question(
                    f"locations.{p.kind}",
                    f"Kind `{p.kind}`: "
                    + " ".join(parts)
                    + f" Answer `locations.{p.kind}: {{<parameter>: <number>}}` to pick one.",
                    None,
                )
            )
            if not open_:
                answers = {**answers, f"locations.{p.kind}": chosen}
    # Q: phone numbers. Comparing by digits is a normalization a person
    # confirms per parameter; it is never inferred from a value's shape.
    phones = sorted(
        {
            f"{p.kind}.{name}"
            for p in plans
            for name, loc in _all_locations(p)
            if loc.rule == "phone"
        }
    )
    if phones:
        confirmed = _answer(answers, "phones")
        confirmed = set(confirmed) if isinstance(confirmed, list) else set()
        questions.append(
            Question(
                "phones",
                "These values were stored as other text with the same digits (a "
                "phone number written with spaces stored without): "
                + ", ".join(phones)
                + ". Compare them as phone numbers (the same digits, at least 7, "
                "and a task value that isn't shaped like a phone number is "
                "compared exactly)? Answer `phones: [<kind.parameter>, ...]` with "
                "the ones that are phone numbers; any other is compared exactly.",
                phones,
            )
        )
        for p in plans:
            _confirm_phones(
                p, {x.split(".", 1)[1] for x in confirmed if x.startswith(p.kind + ".")}
            )

    # A row the app changes by key (a user's preferences, a counter) is the
    # app's, whichever kind of write a demonstration happened to make when
    # it changed: the same excuse, on that row only, applies to every kind.
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for p in plans:
        for row_spec in p.touched:
            if row_spec.get("by_key"):
                k = (
                    row_spec["table"],
                    json.dumps(row_spec["selector"], sort_keys=True),
                )
                merged = keyed.setdefault(k, {**row_spec, "columns": {}})
                merged["columns"].update(row_spec["columns"])
    for p in plans:
        p.touched = [x for x in p.touched if not x.get("by_key")] + [
            {**x, "columns": dict(x["columns"])} for x in keyed.values()
        ]

    # Q: bookkeeping, for the whole app at once.
    ans = _answer(answers, "bookkeeping")
    if ans is not None and ans != "accept" and not isinstance(ans, dict):
        raise GenerateError(
            "bookkeeping: answer `accept` or a mapping with accept, logs_add, "
            "columns_add, columns_remove and unchecked"
        )
    if isinstance(ans, dict):
        unknown = set(ans) - {
            "accept",
            "logs_add",
            "columns_add",
            "columns_remove",
            "unchecked",
        }
        if unknown:
            raise GenerateError(f"bookkeeping: unknown answer keys {sorted(unknown)}")
        if "accept" in ans and not isinstance(ans["accept"], bool):
            raise GenerateError("bookkeeping.accept must be true or false")
    # Only `accept` (or `accept: true`) takes the proposed columns; a mapping
    # without it takes only what it names.
    accepted = ans == "accept" or (isinstance(ans, dict) and ans.get("accept") is True)
    ans = ans if isinstance(ans, dict) else {}

    def table_cols(key: str) -> dict[str, set[str]]:
        raw = ans.get(key) or {}
        if not isinstance(raw, dict) or not all(
            isinstance(c, list) and all(isinstance(x, str) for x in c)
            for c in raw.values()
        ):
            raise GenerateError(f"bookkeeping.{key}: a mapping of table -> [columns]")
        return {str(t): set(c) for t, c in raw.items()}

    raw_logs = ans.get("logs_add") or []
    if not isinstance(raw_logs, list) or not all(isinstance(x, str) for x in raw_logs):
        raise GenerateError("bookkeeping.logs_add: a list of tables")
    log_add = set(raw_logs)
    columns_add = table_cols("columns_add")
    columns_remove = table_cols("columns_remove")
    unchecked_named = table_cols("unchecked")
    proposed: dict[str, set[str]] = {}
    offered: dict[str, set[str]] = {}
    where_seen: dict[str, set[str]] = {}
    for p in plans:
        for col, kind in p.anchor_extra.items():
            (proposed if kind in BOOKKEEPING_KINDS else offered).setdefault(
                p.anchor, set()
            ).add(col)
            where_seen.setdefault(f"{p.anchor}", set()).add("the target row")
        for row_spec in p.touched:
            for col, kind in row_spec["columns"].items():
                (proposed if kind in BOOKKEEPING_KINDS else offered).setdefault(
                    row_spec["table"], set()
                ).add(col)
            where_seen.setdefault(row_spec["table"], set()).add(row_spec["row"])
    for t, cols in columns_add.items():
        extra = cols - proposed.get(t, set()) - offered.get(t, set())
        if extra:
            raise GenerateError(
                f"bookkeeping.columns_add names {t}: {', '.join(sorted(extra))}, "
                "which the question didn't list"
            )
    excused: dict[str, set[str]] = {}
    for t in set(proposed) | set(columns_add):
        took = proposed.get(t, set()) if accepted else set()
        excused[t] = (took | columns_add.get(t, set())) - columns_remove.get(t, set())
    # Columns of a new row no rule checks, per kind and table.
    open_by_kind: dict[str, dict[str, dict[str, str]]] = {}
    #: Why a column can't be pinned, where the demonstrations can't tell
    #: (one demonstration, or two explanations that agree).
    why_open: dict[str, str] = {}
    for p in plans:
        own: dict[str, dict[str, str]] = {}
        groups: list[tuple[str, dict[str, ColumnRole]]] = []
        if p.mode == "create" and p.demos:
            for v in [Variant(p.kind, sorted(p.locations))]:
                groups.append((p.anchor, _anchor_roles(schema, p, v, actor_params)))
        groups += [
            (g.table, g.roles) for g in [*p.children.values(), *p.lists.values()]
        ]
        groups += [(sd.table, sd.roles) for sd in p.sides]
        for table, roles_of in groups:
            for col, role in roles_of.items():
                if role.role in OPEN_ROLES:
                    own.setdefault(table, {})[col] = role.role
                    why = str(role.detail.get("why") or "")
                    if "one demonstration" in why or "can't tell" in why:
                        why_open[f"{table}.{col}"] = why
        open_by_kind[p.kind] = own
    unchecked: dict[str, set[str]] = {}
    for own_cols in open_by_kind.values():
        for t, roles_of in own_cols.items():
            unchecked.setdefault(t, set()).update(roles_of)
    proposed_open: dict[str, list[str]] = {}
    for own_cols in open_by_kind.values():
        for t, roles_of in own_cols.items():
            for c, role_name in roles_of.items():
                if role_name != "free" and f"{t}.{c}" not in why_open:
                    proposed_open.setdefault(t, [])
                    if c not in proposed_open[t]:
                        proposed_open[t].append(c)
    proposed_open = {t: sorted(c) for t, c in sorted(proposed_open.items())}
    for t, cols in unchecked_named.items():
        extra = cols - unchecked.get(t, set())
        if extra:
            raise GenerateError(
                f"bookkeeping.unchecked names {t}: {', '.join(sorted(extra))}, "
                "which the question didn't list"
            )
    for p in plans:
        # A column that varies or is the write's time stamp can't be pinned:
        # unnamed, the kind has no contract. Free text left empty is pinned
        # empty instead.
        missing = sorted(
            f"{t}.{c}"
            for t, roles_of in open_by_kind[p.kind].items()
            for c, role in roles_of.items()
            if role != "free" and c not in unchecked_named.get(t, set())
        )
        if missing and not p.refused:
            p.refused.append(
                "new-row columns no rule checks and the answer doesn't name under "
                "bookkeeping.unchecked: " + ", ".join(missing)
            )
    # Tables a person may name as logs, per kind: those the kind's
    # demonstrations added rows to beside the write.
    log_offer = {
        p.kind: set(p.log_candidates) | {s.table for s in p.sides} for p in plans
    }
    candidates = set().union(*log_offer.values()) if plans else set()
    extra_logs = log_add - candidates
    if extra_logs:
        raise GenerateError(
            "bookkeeping.logs_add names tables the question didn't list: "
            + ", ".join(sorted(extra_logs))
        )
    sides = sorted({f"{s.table} ({s.count} per write)" for p in plans for s in p.sides})
    for p in plans:  # a table the person calls a log is no longer counted
        p.logs = log_add & log_offer[p.kind]
        p.sides = [x for x in p.sides if x.table not in p.logs]
    unexcused = {t: c for p in plans for t, c in p.unexcused.items()}
    if proposed or offered or unchecked or candidates or sides or unexcused:
        lines = []
        if proposed:
            lines.append(
                "Proposed as the app's own bookkeeping, excused only on the rows "
                "the demonstrations changed ("
                + "; ".join(
                    f"{t}: {', '.join(sorted(where_seen.get(t, set())))}"
                    for t in sorted(proposed)
                )
                + "): "
                + "; ".join(
                    f"{t}: {', '.join(sorted(c))}" for t, c in sorted(proposed.items())
                )
                + "."
            )
        if offered:
            lines.append(
                "Also changed with no task value, and not excused unless you add "
                "them under columns_add (a write that changes them stops): "
                + "; ".join(
                    f"{t}: {', '.join(sorted(c))}" for t, c in sorted(offered.items())
                )
                + "."
            )
        if unexcused:
            lines.append(
                "Changed on rows the generator can't identify, so a write that "
                "changes them stops: "
                + "; ".join(
                    f"{t}: {', '.join(sorted(c))}" for t, c in sorted(unexcused.items())
                )
                + "."
            )
        if sides:
            lines.append(
                "Rows the app adds beside each write, expected exactly that many "
                "times: " + ", ".join(sides) + "."
            )
        if candidates:
            lines.append(
                "Tables the app added rows to beside the write. None is a log "
                "unless you list it under logs_add (for the kinds whose "
                "demonstrations added rows there, rows may then be added any "
                "number of times, existing rows must not change): "
                + ", ".join(sorted(candidates))
                + "."
            )
        if unchecked:
            lines.append(
                "Columns of a new row no rule checks (they differ between "
                "demonstrations, are free text every demonstration left empty, or "
                "are the write's time stamp). Each is checked only as you name it "
                "under unchecked: free text you don't name must stay empty, and a "
                "kind with another column you don't name has no contract: "
                + "; ".join(
                    f"{t}: {', '.join(sorted(c))}" for t, c in sorted(unchecked.items())
                )
                + "."
            )
        if why_open:
            lines.append(
                "The demonstrations can't tell what these hold, so they are not "
                "pinned: "
                + "; ".join(f"{c} ({w})" for c, w in sorted(why_open.items()))
                + ". Record a demonstration where they differ, or name them "
                "under unchecked."
            )
        questions.append(
            Question(
                "bookkeeping",
                "Is this the app's own bookkeeping? "
                + " ".join(lines)
                + " Answer `bookkeeping: accept`, or a mapping with any of "
                "accept (true takes the proposed columns), logs_add, columns_add, "
                "columns_remove and unchecked. An answer can name only what this "
                "question lists. Unanswered, nothing is excused, no table is a "
                "log and no column is left unchecked.",
                # The proposal names the columns that vary or are time stamps,
                # so accepting it is explicit. Free text every demonstration
                # left empty stays pinned empty unless the person adds it.
                {"accept": True, "unchecked": proposed_open}
                if proposed_open
                else "accept",
            )
        )
    # Q: the recheck window (frame.database.recheck_after_s).
    recheck: Optional[float] = None
    a = _answer(answers, "recheck_after_s")
    recheck = float(a) if isinstance(a, (int, float)) and a > 0 else None
    questions.append(
        Question(
            "recheck_after_s",
            "How long after a verified write can the app still write on its own "
            "(a background job, a delayed retry)? The database is read again that "
            f"many seconds later and a late change refutes the write. Proposed: "
            f"{DEFAULT_RECHECK_S}. Answer `recheck_after_s: <seconds>`.",
            DEFAULT_RECHECK_S,
        )
    )
    for p in plans:  # child groups of values placed after analysis
        _children(schema, p, [], only_missing=True)
    questions.extend(location_questions)
    questions_open = [q for q in questions if _answer(answers, q.key) is None]
    settings = Settings(
        read,
        dict(actor_params),
        recheck or DEFAULT_RECHECK_S,
        env,
        unchecked=unchecked_named,
    )

    # Tables whose name the SQL guard refuses: a view, or a refusal.
    blocked = sorted(
        t
        for t in schema.tables
        if guard_problem(f"SELECT * FROM {store.quote(dialect, t)} t") is not None
    )
    needed = sorted(
        {p.anchor for p in plans if p.anchor in blocked}
        | {
            g.table
            for p in plans
            for g in list(p.children.values()) + list(p.lists.values())
            if g.table in blocked
        }
    )
    if needed:
        existing = existing_views(connect, schema, needed)
        missing = [t for t in needed if t not in existing]
        if missing and create_views_flag:
            if not admin_read or not reader_account:
                raise GenerateError(
                    "--create-views needs --admin-read and --reader-account"
                )
            made = create_views(admin_read, schema, missing, reader_account)
            applied.extend(f"view {v} over {t}" for t, v in made.items())
            existing = existing_views(connect, schema, needed)
        settings.views = existing
        for p in plans:
            tables = {p.anchor} | {
                g.table for g in list(p.children.values()) + list(p.lists.values())
            }
            for t in sorted(tables & set(blocked)):
                if t not in existing:
                    p.refused.append(
                        f"the SQL guard refuses the table name {t!r} in a read; rerun "
                        "with --create-views --admin-read <admin.yaml> --reader-account "
                        "<account> to read it through a view, or wait for the guard fix"
                    )

    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "dialect": dialect,
        "database": schema.database,
        "actor_params": sorted(actor_params),
        "questions_open": [q.key for q in questions_open],
        "kinds": {},
    }
    report: dict[str, Any] = {
        "applied": applied,
        "read_account": read_account_notes(connect, dialect),
        "dialect": dialect,
        "tables": len(schema.tables),
        "questions": [
            {
                "number": i + 1,
                "key": q.key,
                "text": q.text,
                "proposal": q.proposal,
                "answered": _answer(answers, q.key) is not None,
            }
            for i, q in enumerate(questions)
        ],
        "kinds": {},
    }
    for p, spec in zip(plans, kinds):
        entry: dict[str, Any] = {
            "mode": p.mode,
            "main_table": p.anchor,
            "parameters": p.params,
            "identity": sorted(p.identity),
            "lists": sorted(p.lists),
            "dispatch": p.dispatch,
            "variants": [],
        }
        rep: dict[str, Any] = {
            "mode": p.mode,
            "main_table": p.anchor,
            "locations": {
                k: v.describe(p.anchor) for k, v in sorted(p.locations.items())
            },
            "placed_by_name": sorted(p.by_name),
            "versions": p.versions,
            "placed_from_another_kind": sorted(p.shared),
            "demonstrations": len(p.demos),
            "children": {k: g.table for k, g in p.children.items()},
            "dispatch_children": {
                dv: {k: g.table for k, g in gs.items()}
                for dv, gs in p.dispatch_children.items()
            },
            "lists": {k: g.table for k, g in p.lists.items()},
            "side_rows": [
                {"table": s.table, "count": s.count, "unless": s.unless}
                for s in p.sides
            ],
            "logs": sorted(p.logs),
            "log_candidates": sorted(p.log_candidates),
            "touched_rows": p.touched,
            "unexcused": {t: sorted(c) for t, c in p.unexcused.items()},
            "refused": list(p.refused),
            "unresolved": {
                k: [loc.describe(p.anchor) for loc in v]
                for k, v in sorted(p.unresolved.items())
            },
            "emitted": 0,
            "rejected": [],
        }
        if p.removed:
            rep["refused"].append(
                "the demonstrations removed rows the generator can't explain: "
                + ", ".join(f"{t} ({n})" for t, n in sorted(p.removed.items()))
            )
        if p.refused or not p.mode:
            manifest["kinds"][p.kind] = entry
            report["kinds"][p.kind] = rep
            continue
        flavors = ["create", "found"] if p.mode == "create" else ["update", "found"]
        for v in variants(schema, p, spec, answers):
            if any(
                f not in p.locations
                and f != p.dispatch
                and f not in p.children
                and f not in (p.dispatch_locations.get(v.dispatch_value or "", {}))
                for f in v.fields
            ):
                continue
            ok = True
            for flavor in flavors:
                try:
                    doc = build_contract(
                        schema, p, v, flavor, settings, excused, set(p.logs)
                    )
                except Unpinned as exc:
                    rep["rejected"].append(
                        {"variant": v.slug, "flavor": flavor, "why": str(exc)}
                    )
                    ok = False
                    break
                rel = Path("contracts") / p.kind / f"{v.slug}.{flavor}.yaml"
                path = out / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                header = (
                    f"# Generated by openadapt_flow.generate.verify_contract: kind {p.kind}, "
                    f"{flavor}, fields {', '.join(v.fields + [f'{k}x{n}' for k, n in v.lists.items()])}.\n"
                    "# Proposed, not admitted: a person answers QUESTIONS.md first.\n"
                )
                path.write_text(
                    header
                    + yaml.safe_dump(
                        doc, sort_keys=False, allow_unicode=True, width=100
                    ),
                    encoding="utf-8",
                )
                why = None
                for s in doc["verifier"]["surfaces"].values():
                    why = why or guard_problem(s["sql_query"])
                if why is None and validate:
                    why = dry_read(path, flatten(sample_values(p, v, demos)), True)
                if why is not None:
                    path.unlink()
                    rep["rejected"].append(
                        {"variant": v.slug, "flavor": flavor, "why": why}
                    )
                    ok = False
                    break
                v.files[flavor] = str(rel)
            if ok:
                rep["emitted"] += 1
                entry["variants"].append(
                    {
                        "fields": v.fields,
                        "lists": v.lists,
                        "dispatch_value": v.dispatch_value,
                        **{k: f for k, f in v.files.items()},
                    }
                )
            else:
                for f in v.files.values():
                    (out / f).unlink(missing_ok=True)
        manifest["kinds"][p.kind] = entry
        report["kinds"][p.kind] = rep
    (out / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out / "QUESTIONS.md").write_text(
        _questions_md(questions, answers, report), encoding="utf-8"
    )
    report["questions_open"] = [q.key for q in questions_open]
    return report


def _questions_md(
    questions: Sequence[Question], answers: Mapping[str, Any], report: Mapping[str, Any]
) -> str:
    lines = ["# Questions for a person", ""]
    if not questions:
        lines.append(
            "None: every decision followed from the schema and the demonstrations."
        )
    for i, q in enumerate(questions, 1):
        done = _answer(answers, q.key)
        lines.append(f"{i}. {q.text}")
        lines.append(
            f"   - Answer key: `{q.key}`. Proposed: `{json.dumps(q.proposal, ensure_ascii=False)}`."
        )
        lines.append(
            f"   - {'Answered: `' + json.dumps(done, ensure_ascii=False) + '`' if done is not None else 'Not answered yet.'}"
        )
        lines.append("")
    lines.append("## Kinds")
    lines.append("")
    for kind, rep in report["kinds"].items():
        lines.append(
            f"- `{kind}` ({rep['mode'] or 'no contract'}, main table `{rep['main_table']}`): {rep['emitted']} contract(s)."
        )
        if rep.get("demonstrations") == 1 and rep["mode"] == "create":
            lines.append(
                "  - One demonstration only: every column of a new record that the task "
                "doesn't give must hold the value it held then. Record a second create "
                "with other values, so a column computed from the values (a price with "
                "tax) is told apart from a constant."
            )
        for r in rep["refused"]:
            lines.append(f"  - Refused: {r}")
        for r in rep["rejected"][:5]:
            lines.append(f"  - Not emitted ({r['variant']}, {r['flavor']}): {r['why']}")
    return "\n".join(lines) + "\n"


# -- at run time ---------------------------------------------------------------------------


def choose(
    manifest_path: Path,
    kind: str,
    values: Mapping[str, Any],
    extra_params: Optional[Mapping[str, Any]] = None,
    *,
    require_answers: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """The contract for one task, and the params to call ``begin_write`` with.

    The field set is the task's non-null values (a list counts by length).
    For a create kind, the store is read first: when a record with the
    task's values already exists the ``found`` contract applies (nothing may
    change), else the ``create`` one. Raises :class:`GenerateError` when no
    contract fits: the write is then stopped, never sent unchecked.
    """

    manifest_path = Path(manifest_path)
    manifest = load_yaml(manifest_path) or {}
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise GenerateError(f"{manifest_path} is not a contract manifest")
    if require_answers and manifest.get("questions_open"):
        raise GenerateError(
            "the manifest still has open questions: "
            + ", ".join(manifest["questions_open"])
            + " (answer them in answers.yaml and run propose again)"
        )
    entry = (manifest.get("kinds") or {}).get(kind)
    if not entry or not entry.get("variants"):
        raise GenerateError(f"no contract for kind {kind!r}")
    given = {k for k, v in values.items() if v is not None and not isinstance(v, list)}
    lists = {k: len(v) for k, v in values.items() if isinstance(v, list)}
    dispatch = entry.get("dispatch")
    for var in entry["variants"]:
        if set(var["fields"]) != given or dict(var.get("lists") or {}) != lists:
            continue
        if dispatch and str(values.get(dispatch)) != str(var.get("dispatch_value")):
            continue
        params = {**flatten(values), **dict(extra_params or {})}
        base = manifest_path.parent
        # The record already holds the task's values (found, or an update
        # already in place): nothing may change. Else the create or update.
        found = _load_for(base / var["found"], params)
        if _exists(found, params):
            return found, params
        return _load_for(base / var[entry["mode"]], params), params
    raise GenerateError(
        f"no {kind} contract for the fields {sorted(given)}"
        + (f" and lists {lists}" if lists else "")
        + ": record a demonstration that gives them, or the task is stopped"
    )


def _load_for(path: Path, params: Mapping[str, Any]) -> Any:
    """The contract at ``path`` for these values. A value the contract
    compares as a phone number that isn't written like one (fewer than 7
    digits, or other characters) is compared exactly instead."""

    from openadapt_flow.generate.demo_analysis import phone_shaped
    from openadapt_flow.verifier import Contract

    doc = load_yaml(path) or {}
    changed = False
    for effect in doc.get("effects") or []:
        for value in (effect.get("match") or {}).values():
            if (
                isinstance(value, dict)
                and value.get("kind") == "phone"
                and not phone_shaped(params.get(str(value.get("param"))))
            ):
                value.pop("kind")
                changed = True
    if not changed:
        return Contract.load(path)
    contract = Contract.from_dict(doc, name=Path(path).stem)
    contract.config._base_dir = Path(path).resolve().parent
    return contract


def _exists(contract: Any, params: Mapping[str, Any]) -> bool:
    """Does every effect of the found contract hold now (the record, and each
    row beside it that holds a value)?"""

    from openadapt_flow.runtime.effects.effect import record_matches
    from openadapt_flow.runtime.effects.surfaces import surface_of
    from openadapt_flow.verify_run import _build_verifier

    verifier = _build_verifier(contract.config, params)
    state = verifier.capture_pre_state()
    # Every effect of the found contract (the main record, and each row
    # beside it that holds a value) must already hold.
    for effect in contract.config.effects:
        bound = effect.resolve(params)
        sub = (state.detail or {}).get(surface_of(effect) or "record", {})
        if not sub.get("reachable"):
            raise GenerateError("the store could not be read to choose the contract")
        selector = dict(bound.match)
        hits = sum(
            1 for r in sub.get("records", []) if record_matches(dict(r), selector)
        )
        if hits != bound.expected_count:
            return False
    return True


# -- CLI ------------------------------------------------------------------------------------


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _parse_actor(items: Sequence[str]) -> dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise GenerateError(f"--actor-param {item!r}: use NAME=VALUE")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m openadapt_flow.generate.verify_contract",
        description=(
            "Propose openadapt.verify/v1 contracts for an app's kinds of write, from "
            "its SQL schema (MySQL, MariaDB or PostgreSQL) and one demonstration "
            "write per example. Steps: `schema` (check the read account), then for "
            "each example `snapshot` before the write and `record` after it, then "
            "`propose`. Answer QUESTIONS.md in answers.yaml and run `propose` again. "
            "Guide: packages/flow/docs/GENERATE_CONTRACTS.md. For a FHIR ensure-record "
            "contract use openadapt_flow.generate.ensure_contract instead."
        ),
    )
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser(
        "schema", help="read the schema and check the account is read-only"
    )
    s.add_argument(
        "--read",
        required=True,
        type=Path,
        help="read.yaml: sql_driver, sql_connect_args, sql_password_env",
    )
    s.add_argument("--out", type=Path, help="write schema.json here")
    s = sub.add_parser(
        "snapshot", help="snapshot every table before a demonstration write"
    )
    s.add_argument("--read", required=True, type=Path)
    s.add_argument(
        "--out",
        required=True,
        type=Path,
        help="e.g. work/before.json (holds row values: keep it private)",
    )
    s = sub.add_parser(
        "record", help="snapshot again after the write and save the demonstration"
    )
    s.add_argument("--read", required=True, type=Path)
    s.add_argument("--before", required=True, type=Path)
    s.add_argument("--kind", required=True)
    s.add_argument(
        "--values", required=True, help="the task's values as JSON, or @file.json"
    )
    s.add_argument(
        "--demos", required=True, type=Path, help="directory of demonstrations"
    )
    s.add_argument(
        "--name", default="", help="file name stem (default: kind and a number)"
    )
    s = sub.add_parser("propose", help="analyze the demonstrations and write contracts")
    s.add_argument("--read", required=True, type=Path)
    s.add_argument(
        "--kinds",
        required=True,
        type=Path,
        help="kinds.yaml: kinds, parameters, examples",
    )
    s.add_argument("--demos", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    s.add_argument(
        "--answers", type=Path, help="answers.yaml with the person's answers"
    )
    s.add_argument(
        "--actor-param",
        action="append",
        default=[],
        help="NAME=VALUE the caller passes for the acting user (e.g. bot_user_id=5); a column holding it is the actor",
    )
    s.add_argument("--environment-id", default="")
    s.add_argument(
        "--create-views",
        action="store_true",
        help="create a view for a table the SQL guard refuses by name (needs --admin-read)",
    )
    s.add_argument(
        "--admin-read",
        type=Path,
        help="admin.yaml: a connection allowed to CREATE VIEW and GRANT",
    )
    s.add_argument(
        "--reader-account",
        default="",
        help="the read account to GRANT SELECT on created views (MySQL: 'user'@'%%')",
    )
    s.add_argument(
        "--grant-trigger",
        action="store_true",
        help="MySQL: GRANT TRIGGER to the read account through --admin-read, so the frame can see triggers",
    )
    s.add_argument(
        "--no-validate", action="store_true", help="skip the dry read (tests only)"
    )
    args = ap.parse_args(argv)
    try:
        read = load_read(args.read)
        connect = store.connector(read)
        dialect = store.dialect_of(read)
        if args.command == "schema":
            schema = store.read_schema(connect, dialect)
            problems = check_read_only(connect, dialect)
            if args.out:
                args.out.write_text(
                    json.dumps(schema.to_json(), indent=1), encoding="utf-8"
                )
            print(
                f"{dialect} {schema.database}: {len(schema.tables)} tables, {len(schema.views)} views"
            )
            for p in problems:
                print(f"NOT READ-ONLY: {p}")
            return 1 if problems else 0
        if args.command == "snapshot":
            schema = store.read_schema(connect, dialect)
            rows = store.snapshot(connect, schema)
            store.save_snapshot(args.out, rows)
            (args.out.with_suffix(".meta.json")).write_text(
                json.dumps({"taken_at": _now()}), encoding="utf-8"
            )
            print(f"snapshot of {len(rows)} tables -> {args.out}")
            return 0
        if args.command == "record":
            schema = store.read_schema(connect, dialect)
            values = args.values
            if values.startswith("@"):
                values = Path(values[1:]).read_text(encoding="utf-8")
            values = json.loads(values)
            before = store.load_snapshot(args.before)
            meta = args.before.with_suffix(".meta.json")
            started = json.loads(meta.read_text())["taken_at"] if meta.exists() else ""
            after = store.snapshot(connect, schema)
            d = store.diff(before, after)
            args.demos.mkdir(parents=True, exist_ok=True)
            n = len(list(args.demos.glob(f"{args.kind}-*.demo.json"))) + 1
            stem = args.name or f"{args.kind}-{n}"
            snap = f"{stem}.after.json"
            store.save_snapshot(args.demos / snap, after)
            doc = {
                "kind": args.kind,
                "values": values,
                "started_at": started,
                "finished_at": _now(),
                "after_snapshot": snap,
                "diff": d.to_json(),
            }
            path = args.demos / f"{stem}.demo.json"
            path.write_text(
                json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8"
            )
            os.chmod(path, 0o600)
            print(
                f"{path}: {len(d.changes)} row change(s) in {', '.join(d.tables()) or 'no table'}"
            )
            return 0
        answers = load_yaml(args.answers) if args.answers else {}
        admin = load_read(args.admin_read) if args.admin_read else None
        report = propose(
            read=read,
            kinds=load_kinds(args.kinds),
            demo_dir=args.demos,
            out=args.out,
            actor_params=_parse_actor(args.actor_param),
            answers=answers or {},
            environment_id=args.environment_id,
            admin_read=admin,
            create_views_flag=args.create_views,
            reader_account=args.reader_account,
            validate=not args.no_validate,
            grant_trigger_flag=args.grant_trigger,
        )
        print(
            f"questions: {len(report['questions'])} ({len(report['questions_open'])} open) -> {args.out / 'QUESTIONS.md'}"
        )
        for kind, rep in report["kinds"].items():
            status = (
                "; ".join(rep["refused"])
                if rep["refused"]
                else f"{rep['emitted']} contract(s)"
            )
            print(f"  {kind}: {status}")
        return 0
    except (GenerateError, store.StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
